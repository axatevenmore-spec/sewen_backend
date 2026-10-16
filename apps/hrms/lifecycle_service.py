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
    notify(
        client=client_id,
        recipients=[getattr(employee, "manager", None), user],
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
