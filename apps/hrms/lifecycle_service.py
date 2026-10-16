"""Employee Lifecycle Service for SEWEN ERP HRMS.

Manages the end-to-end employee offboarding lifecycle:
- Termination & Resignation review, approval, and clearance checklists
- Preventing premature offboarding for future-dated exits
- Transactional, idempotent execution of effective offboarding
- Employment status transition (Active/Notice Period -> Resigned/Terminated)
- Application user access deactivation
- Active team membership and headcount reconciliation
- Outstanding asset clearance auditing without premature completion
- Final settlement and payroll clearance preparation
- Post-exit attendance and leave blocking
- Comprehensive audit trails and sensitive data protection
"""

import logging
from datetime import timedelta
from decimal import Decimal
from django.db import transaction
from django.utils import timezone
from apps.core.audit import notify, record_audit
from apps.core.exceptions import Conflict, PermissionDenied, ValidationFailed

logger = logging.getLogger(__name__)

STANDARD_CLEARANCE_CHECKLIST = [
    {
        "title": "IT Equipment & Assets Handover",
        "department": "IT",
        "notes": "Verify return of laptop, company smartphone, ID access badges, and secure keys.",
    },
    {
        "title": "Finance & Dues Settlement",
        "department": "Finance",
        "notes": "Clear pending expense claims, travel advances, corporate card balances, and finalize settlement.",
    },
    {
        "title": "Departmental Handover & Knowledge Transfer",
        "department": "Operations",
        "notes": "Complete project handovers, reassign active repositories/tickets, and obtain manager signoff.",
    },
    {
        "title": "HR Exit Formalities & Relieving Documents",
        "department": "HR",
        "notes": "Conduct exit interview, document PF/gratuity transfer details, and prepare relieving letter.",
    },
    {
        "title": "System & ERP Access Deactivation",
        "department": "Security",
        "notes": "Revoke corporate email, SSO, VPN, and SEWEN ERP login credentials.",
    },
]


def _resolve_user_recipients(client_id, employees=None, users=None):
    """Given a list of Employee instances/IDs and User instances/IDs, resolve to User instances."""
    from apps.accounts.models import User
    resolved = []
    if users:
        for u in users:
            if isinstance(u, User):
                resolved.append(u)
            elif u:
                found = User.objects.filter(client_id=client_id, id=u).first()
                if found:
                    resolved.append(found)

    if employees:
        for emp in employees:
            if not emp:
                continue
            emp_id = getattr(emp, "id", emp)
            found = User.objects.filter(client_id=client_id, employee_id=emp_id).first()
            if found:
                resolved.append(found)
    return resolved


def initialize_clearance_checklist(exit_instance):
    """Seed standard clearance checklist items for a Resignation or Termination."""
    from apps.hrms.models import Resignation, ResignationChecklistItem, Termination

    client_id = exit_instance.client_id
    is_resignation = isinstance(exit_instance, Resignation)

    existing_query = ResignationChecklistItem.objects.filter(
        client_id=client_id,
        resignation=exit_instance if is_resignation else None,
        termination=None if is_resignation else exit_instance,
    )
    if existing_query.exists():
        return list(existing_query)

    items = []
    for entry in STANDARD_CLEARANCE_CHECKLIST:
        item = ResignationChecklistItem.objects.create(
            client_id=client_id,
            resignation=exit_instance if is_resignation else None,
            termination=None if is_resignation else exit_instance,
            title=entry["title"],
            department=entry["department"],
            notes=entry["notes"],
            is_completed=False,
            completed_at=None,
        )
        items.append(item)
    return items


def get_clearance_status(exit_instance):
    """Summarize clearance checklist progress for an exit record."""
    from apps.hrms.models import Resignation, ResignationChecklistItem

    is_resignation = isinstance(exit_instance, Resignation)
    items = ResignationChecklistItem.objects.filter(
        client_id=exit_instance.client_id,
        resignation=exit_instance if is_resignation else None,
        termination=None if is_resignation else exit_instance,
    )
    total = items.count()
    completed = items.filter(is_completed=True).count()
    pending = total - completed
    return {
        "total": total,
        "completed": completed,
        "pending": pending,
        "is_cleared": (total > 0 and pending == 0),
        "items": items,
    }


def resolve_effective_exit_date(exit_instance):
    """Determine the authoritative effective last working date."""
    from apps.hrms.models import Resignation

    last_day = getattr(exit_instance, "last_working_day", None)
    if last_day:
        return last_day

    if isinstance(exit_instance, Resignation):
        submitted = exit_instance.submitted_on or timezone.localdate()
        notice_days = exit_instance.notice_period_days or 30
        return submitted + timedelta(days=notice_days)

    # For Termination, fallback to notice_date or today
    return getattr(exit_instance, "notice_date", None) or timezone.localdate()


@transaction.atomic
def execute_employee_offboarding(exit_instance, user=None, reason=None, force=False):
    """Execute the full employee offboarding process transactionally and idempotently.

    Parameters:
        exit_instance: Resignation or Termination model instance
        user: Actor executing the offboarding
        reason: Optional audit override note
        force: If True, executes even if future-dated or clearance incomplete
    """
    from apps.accounts.models import User
    from apps.hrms.models import (
        Asset, AssetAssignment, Employee, Resignation, Team, Termination
    )

    client_id = exit_instance.client_id
    employee = exit_instance.employee
    is_resignation = isinstance(exit_instance, Resignation)
    canonical_module = "Resignations" if is_resignation else "Terminations"
    target_emp_status = "Resigned" if is_resignation else "Terminated"
    target_exit_status = "Relieved & Completed" if is_resignation else "Completed"

    # Refresh and lock objects to ensure transactional consistency
    employee = Employee.objects.select_for_update().get(pk=employee.pk)
    if is_resignation:
        exit_instance = Resignation.objects.select_for_update().get(pk=exit_instance.pk)
    else:
        exit_instance = Termination.objects.select_for_update().get(pk=exit_instance.pk)

    # 1. IDEMPOTENCY CHECK
    if employee.status in ("Resigned", "Terminated") and exit_instance.status in (
        "Completed", "Relieved & Completed", "Settled", "Exit Clearance Completed"
    ):
        logger.info(
            "Employee %s (%s) is already offboarded with status %s.",
            employee.name, employee.employee_code, employee.status,
        )
        return exit_instance, False, "Already offboarded"

    effective_date = resolve_effective_exit_date(exit_instance)
    today = timezone.localdate()

    # 2. FUTURE-DATED EXIT CHECK
    # Keep employee active / on notice while exit is pending; do not mark effective prematurely.
    if effective_date > today and not force:
        if employee.status == "Active":
            employee.status = "Notice Period"
            employee.save(update_fields=["status", "updated_at"])
            initialize_clearance_checklist(exit_instance)
            record_audit(
                client=client_id,
                actor=user,
                action="update_status",
                entity_type="Employee",
                entity_id=employee.id,
                entity_label=f"{employee.employee_code} - {employee.name}",
                description=f"{canonical_module} approved; employee placed on Notice Period until effective date {effective_date}.",
                before={"status": "Active"},
                after={"status": "Notice Period", "effective_date": str(effective_date)},
            )
        msg = f"Exit is scheduled for future date {effective_date}. Employee remains on Notice Period until effective date."
        return exit_instance, False, msg

    # 3. EXECUTE EFFECTIVE OFFBOARDING MUTATIONS
    prev_emp_status = employee.status
    employee.status = target_emp_status
    employee.save(update_fields=["status", "updated_at"])

    # 3A. Deactivate User Account / Application Login
    linked_users = User.objects.filter(
        client_id=client_id,
        deleted_at__isnull=True,
    ).filter(employee=employee) | User.objects.filter(
        client_id=client_id,
        deleted_at__isnull=True,
        email__iexact=employee.email,
    )
    for u in linked_users.distinct():
        u.is_active = False
        u.status = "Inactive"
        u.save(update_fields=["is_active", "status", "updated_at"])

    # 3B. Team Membership and Headcount Reconciliation
    teams = Team.objects.filter(client_id=client_id, members=employee, deleted_at__isnull=True)
    for t in teams:
        t.members.remove(employee)
        active_count = t.members.filter(deleted_at__isnull=True).exclude(status__in=["Terminated", "Resigned"]).count()
        t.headcount = active_count
        if t.lead_id == employee.id:
            t.lead = None
            t.lead_name = f"{employee.name} (Exited)"
        if t.approver_id == employee.id:
            t.approver = None
            t.approver_name = f"{employee.name} (Exited)"
        t.save(update_fields=["headcount", "lead", "lead_name", "approver", "approver_name", "updated_at"])

    # 3C. Trigger Outstanding Asset Clearance Audit (Without prematurely marking completed)
    assigned_assets = Asset.objects.filter(
        client_id=client_id,
        assigned_employee=employee,
        deleted_at__isnull=True,
    )
    active_assignments = AssetAssignment.objects.filter(
        client_id=client_id,
        employee=employee,
        returned_at__isnull=True,
        deleted_at__isnull=True,
    )
    outstanding_asset_count = assigned_assets.count() + active_assignments.count()

    # 3D. Update Exit Record Status
    exit_instance.status = target_exit_status
    if not exit_instance.decided_at:
        exit_instance.decided_at = timezone.now()
    if hasattr(exit_instance, "approver") and not exit_instance.approver:
        if user and getattr(user, "is_authenticated", False):
            exit_instance.approver = user

    history_entry = {
        "step": getattr(exit_instance, "current_step", 1),
        "action": "Offboarded",
        "role": "HR Administrator",
        "user_id": str(user.id) if user and getattr(user, "is_authenticated", False) else None,
        "user_name": getattr(user, "name", None) or "SYSTEM",
        "timestamp": timezone.now().isoformat(),
        "remark": reason or f"Employee offboarding finalized. Employment status set to {target_emp_status}.",
        "effective_date": str(effective_date),
        "outstanding_assets": outstanding_asset_count,
    }
    if not isinstance(exit_instance.approval_history, list):
        exit_instance.approval_history = []
    exit_instance.approval_history.append(history_entry)

    save_fields = ["status", "decided_at", "approval_history", "updated_at"]
    if hasattr(exit_instance, "approver"):
        save_fields.append("approver")
    exit_instance.save(update_fields=[f for f in save_fields if hasattr(exit_instance, f)])

    # 3E. Comprehensive Audit Log
    record_audit(
        client=client_id,
        actor=user,
        action="execute_offboarding",
        entity_type="Employee",
        entity_id=employee.id,
        entity_label=f"{employee.employee_code} - {employee.name}",
        description=(
            f"{canonical_module} offboarding executed. Employment status changed from {prev_emp_status} "
            f"to {target_emp_status}. Effective date: {effective_date}. "
            f"Outstanding assets flagged: {outstanding_asset_count}."
        ),
        before={"status": prev_emp_status, "user_active": True},
        after={
            "status": target_emp_status,
            "user_active": False,
            "effective_date": str(effective_date),
            "outstanding_assets": outstanding_asset_count,
        },
        comments=reason or exit_instance.reason or "",
    )

    # 3F. Notifications to Management and HR
    recipients = _resolve_user_recipients(
        client_id,
        employees=[getattr(employee, "manager", None)],
        users=[user] if user and getattr(user, "is_authenticated", False) else None
    )
    if recipients:
        notify(
            client=client_id,
            recipients=recipients,
            type="employee_offboarded",
            category="hrms",
            title=f"Employee Offboarded: {employee.name}",
            body=f"{employee.name} ({employee.employee_code}) offboarding completed as {target_emp_status}.",
            entity_type="Employee",
            entity_id=employee.id,
            actor=user,
        )

    return exit_instance, True, f"Employee {employee.name} offboarding successfully executed ({target_emp_status})."


@transaction.atomic
def cancel_exit_process(exit_instance, user=None, reason=None):
    """Cancel an active resignation or termination where permitted before final offboarding."""
    from apps.hrms.models import Employee

    client_id = exit_instance.client_id
    employee = exit_instance.employee

    if exit_instance.status in ("Completed", "Relieved & Completed", "Settled"):
        raise Conflict("Cannot cancel an exit process that has already been finalized.", code="ALREADY_FINALIZED")

    prev_status = exit_instance.status
    exit_instance.status = "Cancelled"
    if hasattr(exit_instance, "remark"):
        exit_instance.remark = reason or "Exit request cancelled / revoked"

    history_entry = {
        "step": getattr(exit_instance, "current_step", 1),
        "action": "Cancelled",
        "role": "HR Administrator",
        "user_id": str(user.id) if user and getattr(user, "is_authenticated", False) else None,
        "user_name": getattr(user, "name", None) or "SYSTEM",
        "timestamp": timezone.now().isoformat(),
        "remark": reason or "Exit process cancelled",
    }
    if not isinstance(exit_instance.approval_history, list):
        exit_instance.approval_history = []
    exit_instance.approval_history.append(history_entry)

    save_fields = ["status", "approval_history", "updated_at"]
    if hasattr(exit_instance, "remark"):
        save_fields.append("remark")
    exit_instance.save(update_fields=[f for f in save_fields if hasattr(exit_instance, f)])

    # Revert employee status to Active if currently in Notice Period
    if employee.status == "Notice Period":
        employee.status = "Active"
        employee.save(update_fields=["status", "updated_at"])

    record_audit(
        client=client_id,
        actor=user,
        action="cancel_exit",
        entity_type="Employee",
        entity_id=employee.id,
        entity_label=f"{employee.employee_code} - {employee.name}",
        description=f"Exit process cancelled. Employee status restored to {employee.status}.",
        before={"exit_status": prev_status},
        after={"exit_status": "Cancelled", "employee_status": employee.status},
        comments=reason or "",
    )

    return exit_instance


def process_due_exits(client_id=None):
    """Scheduled batch worker to process exits reaching their effective last working day."""
    from apps.accounts.models import Client
    from apps.hrms.models import Resignation, Termination

    today = timezone.localdate()
    clients = Client.objects.filter(status="Active")
    if client_id:
        clients = clients.filter(id=client_id)

    processed_count = 0
    error_count = 0

    for c in clients:
        # 1. Due Resignations
        due_resignations = Resignation.objects.filter(
            client_id=c.id,
            status__in=["Approved", "Clearance In Progress", "Exit Interview Scheduled", "Exit Clearance In Progress"],
            last_working_day__lte=today,
            employee__status__in=["Active", "Notice Period", "Probation", "On Leave"],
            deleted_at__isnull=True,
        ).select_related("employee")

        for rsg in due_resignations:
            try:
                _, executed, _ = execute_employee_offboarding(rsg)
                if executed:
                    processed_count += 1
            except Exception as e:
                logger.error("Error processing due resignation %s: %s", rsg.id, e)
                error_count += 1

        # 2. Due Terminations
        due_terminations = Termination.objects.filter(
            client_id=c.id,
            status__in=["Approved", "In Exit Clearance", "Initiated"],
            last_working_day__lte=today,
            employee__status__in=["Active", "Notice Period", "Probation", "On Leave"],
            deleted_at__isnull=True,
        ).select_related("employee")

        for trm in due_terminations:
            try:
                _, executed, _ = execute_employee_offboarding(trm)
                if executed:
                    processed_count += 1
            except Exception as e:
                logger.error("Error processing due termination %s: %s", trm.id, e)
                error_count += 1

    return {"processed": processed_count, "errors": error_count, "message": f"{processed_count} exit(s) processed"}


def parse_duration_to_days(val, default=90):
    """Parse human duration string (e.g. '90 Days (3 Months)', '3 Months', '60 Days') to integer days."""
    if val is None or val == "":
        return default
    if isinstance(val, (int, float)):
        return int(val)
    s = str(val).strip().lower()
    import re
    m_days = re.search(r"(\d+)\s*(?:day|days|d)", s)
    if m_days:
        return int(m_days.group(1))
    m_months = re.search(r"(\d+)\s*(?:month|months|m)", s)
    if m_months:
        return int(m_months.group(1)) * 30
    m_weeks = re.search(r"(\d+)\s*(?:week|weeks|w)", s)
    if m_weeks:
        return int(m_weeks.group(1)) * 7
    m_num = re.search(r"(\d+)", s)
    if m_num:
        return int(m_num.group(1))
    return default


def calculate_probation_end_date(start_date, duration_val, default_days=90):
    """Calculate the probation end date from start_date and duration."""
    if not start_date:
        start_date = timezone.localdate()
    days = parse_duration_to_days(duration_val, default=default_days)
    return start_date + timedelta(days=days)


def calculate_notice_last_working_day(submitted_on, notice_days, client_id=None):
    """Calculate expected last working day considering holidays and calendar."""
    from apps.hrms.models import Holiday
    if not submitted_on:
        submitted_on = timezone.localdate()
    target_date = submitted_on + timedelta(days=int(notice_days or 30))
    if client_id:
        for _ in range(7):
            is_hol = Holiday.objects.filter(
                client_id=client_id, date=target_date, deleted_at__isnull=True
            ).exists()
            if is_hol:
                target_date += timedelta(days=1)
            else:
                break
    return target_date


@transaction.atomic
def confirm_employee_probation(employee, user=None, effective_date=None, remarks=None):
    """Confirm employee probation and transition employment status to Active."""
    from apps.hrms.models import Employee
    employee = Employee.objects.select_for_update().get(pk=employee.pk)

    eff_date = effective_date or timezone.localdate()
    prev_status = employee.status
    prev_conf_status = employee.confirmation_status

    employee.confirmation_status = "Confirmed"
    if employee.status == "Probation":
        employee.status = "Active"
    employee.confirmation_date = eff_date
    employee.confirmation_remarks = remarks or "Probation confirmed."

    history_item = {
        "action": "Confirmed",
        "actor_id": str(user.id) if user and getattr(user, "is_authenticated", False) else None,
        "actor_name": getattr(user, "name", "SYSTEM"),
        "effective_date": str(eff_date),
        "timestamp": timezone.now().isoformat(),
        "remarks": remarks or "Probation period successfully completed and confirmed.",
    }
    if not isinstance(employee.probation_history, list):
        employee.probation_history = []
    employee.probation_history.append(history_item)

    employee.save(update_fields=[
        "status", "confirmation_status", "confirmation_date",
        "confirmation_remarks", "probation_history", "updated_at"
    ])

    record_audit(
        client=employee.client_id,
        actor=user,
        action="confirm_probation",
        entity_type="Employee",
        entity_id=employee.id,
        entity_label=f"{employee.employee_code} - {employee.name}",
        description=f"Probation confirmed for {employee.name}. Status updated to {employee.status}.",
        before={"status": prev_status, "confirmation_status": prev_conf_status},
        after={"status": employee.status, "confirmation_status": "Confirmed", "confirmation_date": str(eff_date)},
        comments=remarks or "",
    )

    recipients = _resolve_user_recipients(
        employee.client_id,
        employees=[getattr(employee, "manager", None), employee],
    )
    if recipients:
        notify(
            client=employee.client_id,
            recipients=recipients,
            type="probation_confirmed",
            category="hrms",
            title=f"Probation Confirmed: {employee.name}",
            body=f"Congratulations! {employee.name} ({employee.employee_code}) has been confirmed as a permanent employee.",
            entity_type="Employee",
            entity_id=employee.id,
            actor=user,
        )

    return employee


@transaction.atomic
def extend_employee_probation(employee, user=None, extension_days=None, new_end_date=None, reason=None):
    """Extend employee probation duration with specified new end date or duration."""
    from apps.hrms.models import Employee
    employee = Employee.objects.select_for_update().get(pk=employee.pk)

    prev_end = employee.probation_end_date or timezone.localdate()
    prev_conf_status = employee.confirmation_status

    if new_end_date:
        target_end = new_end_date
    elif extension_days:
        target_end = prev_end + timedelta(days=int(extension_days))
    else:
        target_end = prev_end + timedelta(days=30)

    days_added = (target_end - prev_end).days

    employee.probation_end_date = target_end
    employee.confirmation_status = "Extended"
    employee.status = "Probation"

    history_item = {
        "action": "Extended",
        "actor_id": str(user.id) if user and getattr(user, "is_authenticated", False) else None,
        "actor_name": getattr(user, "name", "SYSTEM"),
        "previous_end_date": str(prev_end),
        "new_end_date": str(target_end),
        "extension_days": days_added,
        "timestamp": timezone.now().isoformat(),
        "reason": reason or "Probation extended for further performance evaluation.",
    }
    if not isinstance(employee.probation_history, list):
        employee.probation_history = []
    employee.probation_history.append(history_item)

    employee.save(update_fields=[
        "status", "probation_end_date", "confirmation_status",
        "probation_history", "updated_at"
    ])

    record_audit(
        client=employee.client_id,
        actor=user,
        action="extend_probation",
        entity_type="Employee",
        entity_id=employee.id,
        entity_label=f"{employee.employee_code} - {employee.name}",
        description=f"Probation extended by {days_added} days for {employee.name}. New end date: {target_end}.",
        before={"probation_end_date": str(prev_end), "confirmation_status": prev_conf_status},
        after={"probation_end_date": str(target_end), "confirmation_status": "Extended"},
        comments=reason or "",
    )

    recipients = _resolve_user_recipients(
        employee.client_id,
        employees=[getattr(employee, "manager", None), employee],
    )
    if recipients:
        notify(
            client=employee.client_id,
            recipients=recipients,
            type="probation_extended",
            category="hrms",
            title=f"Probation Extended: {employee.name}",
            body=f"Probation period for {employee.name} ({employee.employee_code}) has been extended until {target_end}.",
            entity_type="Employee",
            entity_id=employee.id,
            actor=user,
        )

    return employee


@transaction.atomic
def recalculate_probations_for_setting_change(client_id, new_duration_str, user=None):
    """Recalculate probation end dates for active unconfirmed employees when HR Admin probation duration changes."""
    from apps.hrms.models import Employee
    new_days = parse_duration_to_days(new_duration_str, default=90)

    unconfirmed_emps = Employee.objects.filter(
        client_id=client_id,
        confirmation_status="On Probation",
        deleted_at__isnull=True,
    )
    updated_count = 0
    for emp in unconfirmed_emps:
        start_dt = emp.probation_start_date or emp.joining_date or timezone.localdate()
        new_end_dt = start_dt + timedelta(days=new_days)
        emp.probation_period = new_duration_str
        emp.probation_end_date = new_end_dt
        emp.save(update_fields=["probation_period", "probation_end_date", "updated_at"])
        updated_count += 1

    if updated_count > 0:
        record_audit(
            client=client_id,
            actor=user,
            action="update_settings",
            entity_type="Setting",
            entity_id=client_id,
            entity_label="HRMS Probation Setting",
            description=f"HR Admin probation period updated to {new_duration_str}. Recalculated {updated_count} active probation record(s).",
        )
    return updated_count


@transaction.atomic
def waive_resignation_notice(resignation, user=None, effective_last_day=None, reason=None):
    """Waive remaining notice period for a resigning employee."""
    from apps.hrms.models import Resignation
    resignation = Resignation.objects.select_for_update().get(pk=resignation.pk)
    employee = resignation.employee

    eff_date = effective_last_day or timezone.localdate()
    prev_last_day = resignation.last_working_day

    resignation.notice_waived = True
    resignation.waiver_reason = reason or "Notice period waived by authorized management."
    if not resignation.original_last_working_day:
        resignation.original_last_working_day = prev_last_day or eff_date
    resignation.last_working_day = eff_date

    history_entry = {
        "step": getattr(resignation, "current_step", 1),
        "action": "Notice Waived",
        "role": "HR Administrator",
        "user_id": str(user.id) if user and getattr(user, "is_authenticated", False) else None,
        "user_name": getattr(user, "name", None) or "SYSTEM",
        "timestamp": timezone.now().isoformat(),
        "remark": reason or "Notice period waived.",
        "revised_last_working_day": str(eff_date),
        "previous_last_working_day": str(prev_last_day) if prev_last_day else None,
    }
    if not isinstance(resignation.approval_history, list):
        resignation.approval_history = []
    resignation.approval_history.append(history_entry)

    resignation.save(update_fields=[
        "notice_waived", "waiver_reason", "original_last_working_day",
        "last_working_day", "approval_history", "updated_at"
    ])

    if employee:
        employee.last_working_day = eff_date
        employee.save(update_fields=["last_working_day", "updated_at"])

    record_audit(
        client=resignation.client_id,
        actor=user,
        action="waive_notice",
        entity_type="Resignation",
        entity_id=resignation.id,
        entity_label=f"Resignation - {employee.name if employee else 'Employee'}",
        description=f"Notice period waived for {employee.name if employee else 'Employee'}. Last working day revised to {eff_date}.",
        before={"last_working_day": str(prev_last_day) if prev_last_day else None, "notice_waived": False},
        after={"last_working_day": str(eff_date), "notice_waived": True},
        comments=reason or "",
    )
    return resignation


@transaction.atomic
def shorten_resignation_notice(resignation, user=None, revised_last_working_day=None, reason=None):
    """Shorten notice period to an agreed earlier last working day."""
    from apps.hrms.models import Resignation
    resignation = Resignation.objects.select_for_update().get(pk=resignation.pk)
    employee = resignation.employee

    prev_last_day = resignation.last_working_day
    eff_date = revised_last_working_day or timezone.localdate()

    if not resignation.original_last_working_day:
        resignation.original_last_working_day = prev_last_day or eff_date
    resignation.last_working_day = eff_date

    history_entry = {
        "step": getattr(resignation, "current_step", 1),
        "action": "Notice Shortened",
        "role": "HR Administrator",
        "user_id": str(user.id) if user and getattr(user, "is_authenticated", False) else None,
        "user_name": getattr(user, "name", None) or "SYSTEM",
        "timestamp": timezone.now().isoformat(),
        "remark": reason or "Notice period shortened by mutual agreement.",
        "revised_last_working_day": str(eff_date),
        "previous_last_working_day": str(prev_last_day) if prev_last_day else None,
    }
    if not isinstance(resignation.approval_history, list):
        resignation.approval_history = []
    resignation.approval_history.append(history_entry)

    resignation.save(update_fields=[
        "original_last_working_day", "last_working_day",
        "approval_history", "updated_at"
    ])

    if employee:
        employee.last_working_day = eff_date
        employee.save(update_fields=["last_working_day", "updated_at"])

    record_audit(
        client=resignation.client_id,
        actor=user,
        action="shorten_notice",
        entity_type="Resignation",
        entity_id=resignation.id,
        entity_label=f"Resignation - {employee.name if employee else 'Employee'}",
        description=f"Notice period shortened for {employee.name if employee else 'Employee'}. Last working day revised to {eff_date}.",
        before={"last_working_day": str(prev_last_day) if prev_last_day else None},
        after={"last_working_day": str(eff_date)},
        comments=reason or "",
    )
    return resignation


def evaluate_employee_lifecycle(client_id=None):
    """Evaluate probation status and exit deadlines across employees."""
    from apps.accounts.models import Client, User
    from apps.hrms.models import Employee
    from apps.core.models import Notification

    today = timezone.localdate()
    clients = Client.objects.filter(status="Active")
    if client_id:
        clients = clients.filter(id=client_id)

    due_confirmation_count = 0
    due_exit_results = process_due_exits(client_id)

    for c in clients:
        probation_emps = Employee.objects.filter(
            client_id=c.id,
            status="Probation",
            confirmation_status__in=["On Probation", "Extended"],
            probation_end_date__lte=today,
            deleted_at__isnull=True,
        ).select_related("manager")

        for emp in probation_emps:
            emp.confirmation_status = "Due for Confirmation"
            emp.save(update_fields=["confirmation_status", "updated_at"])
            due_confirmation_count += 1

            existing_notif = Notification.objects.filter(
                client_id=c.id,
                entity_type="Employee",
                entity_id=emp.id,
                type="probation_due",
                created_at__date=today,
            ).exists()

            if not existing_notif:
                hr_admins = User.objects.filter(
                    client_id=c.id,
                    deleted_at__isnull=True,
                    role__permissions__id__in=["view_staff", "edit_staff"],
                ).distinct()
                recipients = list(hr_admins[:5])
                if emp.manager and hasattr(emp.manager, "users"):
                    mgr_user = emp.manager.users.first()
                    if mgr_user and mgr_user not in recipients:
                        recipients.append(mgr_user)

                notify(
                    client=c.id,
                    recipients=recipients,
                    type="probation_due",
                    category="hrms",
                    title=f"Probation Confirmation Due: {emp.name}",
                    body=f"{emp.name} ({emp.employee_code}) completed probation on {emp.probation_end_date} and is due for confirmation.",
                    entity_type="Employee",
                    entity_id=emp.id,
                )

    return {
        "due_confirmation": due_confirmation_count,
        "exits_processed": due_exit_results.get("processed", 0),
        "message": f"{due_confirmation_count} employee(s) due for confirmation, {due_exit_results.get('processed', 0)} exit(s) processed."
    }
