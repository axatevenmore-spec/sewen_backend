"""Approval Workflow Engine for SEWEN ERP HRMS.

Manages multi-tier approval chains, sequence enforcement, approver authorization,
rejection, resubmission, audit logging, and notifications for HR requests
(Leave, Travel Requisitions, Grievances, Resignations, and Terminations).
"""

import logging
from django.db.models import Q
from django.utils import timezone
from apps.core.audit import notify
from apps.core.exceptions import Conflict, PermissionDenied, ValidationFailed

logger = logging.getLogger(__name__)


def write_audit(user, action, instance, description=""):
    """Helper to record audit trail for workflow actions."""
    try:
        from apps.core.audit import record_audit
        client_id = getattr(instance, "client_id", None)
        if not client_id and user:
            client_id = getattr(user, "client_id", None)
        if client_id:
            record_audit(
                client=client_id,
                actor=user,
                action=action,
                entity_type=instance._meta.model_name,
                entity_id=getattr(instance, "id", None),
                description=description,
            )
    except Exception as e:
        logger.warning("Failed to record audit log: %s", e)


MODULE_CANONICAL = {
    "leave": "Leave Management",
    "leave management": "Leave Management",
    "travel": "Travel Requisitions",
    "travel requisitions": "Travel Requisitions",
    "travel request": "Travel Requisitions",
    "travel requests": "Travel Requisitions",
    "grievance": "Grievances",
    "grievances": "Grievances",
    "complaint": "Grievances",
    "complaints": "Grievances",
    "resignation": "Resignations",
    "resignations": "Resignations",
    "termination": "Terminations",
    "terminations": "Terminations",
    "attendance regularization": "Attendance Regularization",
    "asset allocation": "Asset Allocation",
    "payroll revisions": "Payroll & Compensation Revisions",
    "payroll & compensation revisions": "Payroll & Compensation Revisions",
    "expense reimbursement": "Expense Reimbursement",
}


def normalize_module_name(name: str) -> str:
    """Returns canonical module name for workflow matching."""
    if not name:
        return "General"
    cleaned = str(name).strip().lower()
    return MODULE_CANONICAL.get(cleaned, str(name).strip())


def get_active_workflow(client_id, module_name: str):
    """Find the active ApprovalChain for a tenant and module."""
    from apps.hrms.models import ApprovalChain

    canonical = normalize_module_name(module_name)
    chain = ApprovalChain.objects.filter(
        client_id=client_id,
        applies_to__iexact=canonical,
        status="Active",
    ).first()
    if not chain and canonical != module_name:
        chain = ApprovalChain.objects.filter(
            client_id=client_id,
            applies_to__iexact=module_name,
            status="Active",
        ).first()
    if not chain:
        chain = ApprovalChain.objects.filter(
            client_id=client_id,
            name__icontains=canonical,
            status="Active",
        ).first()
    return chain


def get_workflow_steps(chain) -> list:
    """Extract and normalize ordered steps from an ApprovalChain."""
    if not chain or not isinstance(chain.steps, list) or len(chain.steps) == 0:
        return []

    normalized = []
    for idx, s in enumerate(chain.steps):
        if not isinstance(s, dict):
            continue
        tier = s.get("tier") or s.get("level") or (idx + 1)
        role = s.get("role") or s.get("name") or s.get("title") or f"Tier {tier} Approver"
        auto_esc = s.get("autoEscalateDays") or 3
        normalized.append({
            "tier": int(tier),
            "role": str(role).strip(),
            "autoEscalateDays": int(auto_esc),
        })

    normalized.sort(key=lambda x: x["tier"])
    return normalized


def resolve_approver_users(client_id, role_name: str, employee=None):
    """Resolve potential User approvers for a given workflow step role."""
    from apps.accounts.models import User

    role_lower = str(role_name or "").strip().lower()
    approvers = set()

    # 1. Direct Line Manager / Supervisor
    if any(k in role_lower for k in ["line manager", "direct line manager", "reporting manager", "supervisor"]):
        if employee and getattr(employee, "manager", None):
            mgr_users = User.objects.filter(
                client_id=client_id,
                employee=employee.manager,
                is_active=True,
                deleted_at__isnull=True,
            )
            approvers.update(mgr_users)

    # 2. Team Leader / Team Lead
    if any(k in role_lower for k in ["team lead", "team leader", "squad lead"]):
        if employee:
            teams = getattr(employee, "teams", None)
            if teams:
                for t in teams.all():
                    if t.lead:
                        lead_users = User.objects.filter(
                            client_id=client_id,
                            employee=t.lead,
                            is_active=True,
                            deleted_at__isnull=True,
                        )
                        approvers.update(lead_users)

    # 3. Department Head / HOD
    if any(k in role_lower for k in ["department head", "dept head", "hod", "head of department"]):
        if employee and getattr(employee, "department", None):
            dept_users = User.objects.filter(
                client_id=client_id,
                employee__department=employee.department,
                employee__designation__name__icontains="Head",
                is_active=True,
                deleted_at__isnull=True,
            )
            approvers.update(dept_users)

    # 4. HR Roles (HR Operations Lead, HR Director, HR Admin)
    if any(k in role_lower for k in ["hr", "human resources", "hr operations", "hr director"]):
        hr_users = User.objects.filter(
            client_id=client_id,
            is_active=True,
            deleted_at__isnull=True,
        ).filter(
            Q(role__permissions__id__in=["approve_leave", "edit_staff"]) |
            Q(role__code__in=["HR", "HR_ADMIN", "HR_MANAGER"]) |
            Q(role__name__icontains="HR")
        ).distinct()
        approvers.update(hr_users)

    # 5. Finance Roles (Finance Lead, CFO, Finance Approver)
    if any(k in role_lower for k in ["finance", "cfo", "financial officer", "accounts lead"]):
        fin_users = User.objects.filter(
            client_id=client_id,
            is_active=True,
            deleted_at__isnull=True,
        ).filter(
            Q(role__permissions__id__in=["view_payroll", "edit_payroll", "menu_accounting"]) |
            Q(role__code__in=["FINANCE", "CFO", "ACCOUNTS"]) |
            Q(role__name__icontains="Finance")
        ).distinct()
        approvers.update(fin_users)

    # 6. Specific user name or email match
    user_match = User.objects.filter(
        client_id=client_id,
        is_active=True,
        deleted_at__isnull=True,
    ).filter(
        Q(name__iexact=role_name) | Q(email__iexact=role_name)
    )
    approvers.update(user_match)

    # 7. Fallback: Admins / Superusers
    admins = User.objects.filter(
        client_id=client_id,
        is_active=True,
        deleted_at__isnull=True,
    ).filter(
        Q(is_superuser=True) |
        Q(role__code__in=["Admin", "ADMIN", "Owner", "OWNER"]) |
        Q(role__name__in=["Admin", "Owner"])
    ).distinct()
    approvers.update(admins)

    return list(approvers)


def can_user_approve_step(user, instance, step: dict, module_name: str) -> tuple[bool, str]:
    """Check if a User is authorized to approve the current workflow step."""
    if not user or not user.is_authenticated:
        return False, "User is not authenticated"

    # Superuser or Tenant Admin has authority to oversee workflows
    user_role_code = str(getattr(user.role, "code", "") or "").upper() if getattr(user, "role", None) else ""
    user_role_name = str(getattr(user.role, "name", "") or "") if getattr(user, "role", None) else ""
    if getattr(user, "is_superuser", False) or user_role_code in ("ADMIN", "OWNER") or user_role_name in ("Admin", "Owner"):
        return True, "Administrator privilege"

    # Prevent employee from approving their own request unless they are an admin
    req_employee = getattr(instance, "employee", None) or getattr(instance, "raised_by_employee", None)
    if req_employee and getattr(user, "employee_id", None) == req_employee.id:
        return False, "Employees cannot approve their own requests"

    # Module-specific system permissions
    canonical = normalize_module_name(module_name)
    if canonical == "Leave Management":
        from apps.core.permissions import has_permission
        if has_permission(user, "approve_leave"):
            return True, "Has approve_leave permission"

    if canonical in ("Travel Requisitions", "Grievances", "Resignations", "Terminations"):
        from apps.core.permissions import has_permission
        if has_permission(user, "edit_staff"):
            return True, "Has edit_staff permission"

    # Check designated step approver match
    step_role = step.get("role", "")
    eligible_users = resolve_approver_users(instance.client_id, step_role, req_employee)
    if any(u.id == user.id for u in eligible_users):
        return True, f"Matches designated approver role: {step_role}"

    return False, f"You are not authorized to approve at Step {step.get('tier', 1)} ({step_role})"


def initialize_request_workflow(instance, module_name: str, user=None):
    """Attach active workflow, initialize step 1, log submission and notify approver."""
    chain = get_active_workflow(instance.client_id, module_name)
    canonical = normalize_module_name(module_name)

    if chain:
        instance.approval_chain = chain
        instance.current_step = 1
        steps = get_workflow_steps(chain)
        first_role = steps[0]["role"] if steps else "Direct Line Manager"
    else:
        instance.current_step = 1
        first_role = "Direct Line Manager"

    user_name = getattr(user, "name", None) or getattr(user, "email", "Employee") if user else "Employee"
    user_id = str(user.id) if user and getattr(user, "id", None) else None

    history_entry = {
        "step": 1,
        "action": "Submitted",
        "role": "Requester",
        "user_id": user_id,
        "user_name": user_name,
        "timestamp": timezone.now().isoformat(),
        "remark": f"{canonical} request submitted for approval",
        "pending_role": first_role,
    }

    if not isinstance(instance.approval_history, list):
        instance.approval_history = []
    instance.approval_history.append(history_entry)

    # Save fields safely
    update_fields = ["current_step", "approval_history", "updated_at"]
    if hasattr(instance, "approval_chain_id"):
        update_fields.append("approval_chain")
    instance.save(update_fields=[f for f in update_fields if hasattr(instance, f)])

    # Notify Step 1 approvers
    notify_step_approvers(instance, 1, first_role, canonical, actor=user)
    return instance


def notify_step_approvers(instance, step_num: int, role_name: str, module_name: str, actor=None):
    """Send in-app notification to the approvers of the current step."""
    employee = getattr(instance, "employee", None) or getattr(instance, "raised_by_employee", None)
    emp_name = getattr(employee, "name", "an employee") if employee else "an employee"

    approver_users = resolve_approver_users(instance.client_id, role_name, employee)
    if not approver_users:
        return

    notify(
        client=instance.client,
        recipients=approver_users,
        type="approval_required",
        category="HRMS",
        title=f"Approval Required: {module_name} (Step {step_num})",
        body=f"A {module_name} request for {emp_name} requires your approval as {role_name}.",
        entity_type=instance.__class__.__name__,
        entity_id=instance.id,
        actor=actor,
        payload={"step": step_num, "role": role_name, "module": module_name},
    )


def notify_requester(instance, action: str, module_name: str, remark: str = None, actor=None):
    """Notify the requester of approval, rejection, or status change."""
    employee = getattr(instance, "employee", None) or getattr(instance, "raised_by_employee", None)
    if not employee:
        return

    from apps.accounts.models import User
    req_users = User.objects.filter(
        client_id=instance.client_id,
        employee=employee,
        is_active=True,
    )
    if not req_users.exists():
        return

    title = f"{module_name} Request {action.capitalize()}"
    body = f"Your {module_name} request has been {action.lower()}."
    if remark:
        body += f" Remark: {remark}"

    notify(
        client=instance.client,
        recipients=list(req_users),
        type="approval_update",
        category="HRMS",
        title=title,
        body=body,
        entity_type=instance.__class__.__name__,
        entity_id=instance.id,
        actor=actor,
        payload={"action": action, "module": module_name, "remark": remark},
    )


def process_approval_action(
    instance,
    module_name: str,
    user,
    action: str,
    remark: str = None,
    target_status: str = None,
) -> tuple[object, bool]:
    """Execute approval, rejection, or resubmission through the workflow.

    Returns:
        (updated_instance, is_final_decision: bool)
    """
    canonical = normalize_module_name(module_name)
    action_clean = str(action).strip().lower()

    if action_clean not in ("approve", "reject", "resubmit"):
        raise ValidationFailed(f"Invalid approval action: {action}. Must be 'approve', 'reject', or 'resubmit'.")

    # If instance already has terminal status
    terminal_statuses = {"Approved", "Rejected", "Cancelled", "Settled", "Completed", "Resolved", "Dismissed"}
    if instance.status in terminal_statuses and action_clean != "resubmit":
        raise Conflict(f"Cannot {action_clean} request because it is already {instance.status}.")

    user_name = getattr(user, "name", None) or getattr(user, "email", "Approver")
    user_id = str(user.id) if user and getattr(user, "id", None) else None

    # 1. RESUBMISSION
    if action_clean == "resubmit":
        chain = getattr(instance, "approval_chain", None) or get_active_workflow(instance.client_id, canonical)
        steps = get_workflow_steps(chain) if chain else []
        first_role = steps[0]["role"] if steps else "Direct Line Manager"

        instance.current_step = 1
        instance.status = "Pending Review" if canonical == "Leave Management" else "Submitted"
        if canonical == "Grievances":
            instance.status = "Open"

        history_entry = {
            "step": 1,
            "action": "Resubmitted",
            "role": "Requester",
            "user_id": user_id,
            "user_name": user_name,
            "timestamp": timezone.now().isoformat(),
            "remark": remark or "Request resubmitted for approval",
            "pending_role": first_role,
        }
        if not isinstance(instance.approval_history, list):
            instance.approval_history = []
        instance.approval_history.append(history_entry)

        update_fields = ["current_step", "status", "approval_history", "updated_at"]
        instance.save(update_fields=[f for f in update_fields if hasattr(instance, f)])
        notify_step_approvers(instance, 1, first_role, canonical, actor=user)
        write_audit(user, "resubmit", instance, description=f"{canonical} resubmitted")
        return instance, False

    # 2. APPROVAL OR REJECTION
    chain = getattr(instance, "approval_chain", None) or get_active_workflow(instance.client_id, canonical)
    steps = get_workflow_steps(chain) if chain else []
    total_steps = len(steps)
    current_step_num = getattr(instance, "current_step", 1) or 1

    current_step_info = None
    if steps:
        current_step_info = next((s for s in steps if s["tier"] == current_step_num), None)
        if not current_step_info:
            current_step_info = steps[min(current_step_num - 1, len(steps) - 1)]
    else:
        current_step_info = {"tier": 1, "role": "Direct Line Manager"}

    # Authorize user for current step
    authorized, auth_msg = can_user_approve_step(user, instance, current_step_info, canonical)
    if not authorized:
        raise PermissionDenied(auth_msg)

    # 2A. REJECTION
    if action_clean == "reject":
        instance.status = "Rejected" if canonical != "Grievances" else "Dismissed"
        if hasattr(instance, "approver"):
            instance.approver = user
        if hasattr(instance, "decided_at"):
            instance.decided_at = timezone.now()
        if hasattr(instance, "remark"):
            instance.remark = remark

        history_entry = {
            "step": current_step_num,
            "action": "Rejected",
            "role": current_step_info["role"],
            "user_id": user_id,
            "user_name": user_name,
            "timestamp": timezone.now().isoformat(),
            "remark": remark or "Request rejected",
        }
        if not isinstance(instance.approval_history, list):
            instance.approval_history = []
        instance.approval_history.append(history_entry)

        update_fields = ["status", "approval_history", "approver", "decided_at", "remark", "updated_at"]
        instance.save(update_fields=[f for f in update_fields if hasattr(instance, f)])

        notify_requester(instance, "Rejected", canonical, remark=remark, actor=user)
        write_audit(user, "reject", instance, description=f"{canonical} rejected at step {current_step_num}: {remark}")
        return instance, True

    # 2B. APPROVAL
    is_final = not steps or current_step_num >= total_steps

    if is_final:
        # Final approval step
        instance.status = target_status or ("Approved" if canonical != "Grievances" else "Resolved")
        if canonical == "Terminations" and not target_status:
            instance.status = "In Exit Clearance"
        if canonical == "Resignations" and not target_status:
            instance.status = "Exit Interview Scheduled"

        if hasattr(instance, "approver"):
            instance.approver = user
        if hasattr(instance, "decided_at"):
            instance.decided_at = timezone.now()
        if hasattr(instance, "remark"):
            instance.remark = remark

        history_entry = {
            "step": current_step_num,
            "action": "Approved",
            "role": current_step_info["role"],
            "user_id": user_id,
            "user_name": user_name,
            "timestamp": timezone.now().isoformat(),
            "remark": remark or "Final approval granted",
            "final": True,
        }
        if not isinstance(instance.approval_history, list):
            instance.approval_history = []
        instance.approval_history.append(history_entry)

        # Execute module-specific final business logic
        if canonical == "Leave Management":
            from apps.hrms import services
            # Call approve_leave service which balances quotas & writes attendance
            saved_history = list(instance.approval_history)
            instance = services.approve_leave(instance, user=user, remark=remark)
            instance.approval_history = saved_history
            instance.save(update_fields=["approval_history"])
        else:
            update_fields = ["status", "approval_history", "approver", "decided_at", "remark", "updated_at"]
            instance.save(update_fields=[f for f in update_fields if hasattr(instance, f)])

        notify_requester(instance, "Approved", canonical, remark=remark, actor=user)
        write_audit(user, "approve", instance, description=f"{canonical} final approval granted: {remark}")
        return instance, True

    else:
        # Advance to next tier in the workflow
        next_step_num = current_step_num + 1
        next_step_info = next((s for s in steps if s["tier"] == next_step_num), None)
        next_role = next_step_info["role"] if next_step_info else f"Tier {next_step_num} Approver"

        instance.current_step = next_step_num
        if canonical == "Leave Management":
            instance.status = f"Pending Tier {next_step_num}"
        elif canonical == "Grievances":
            instance.status = "Under Review"

        history_entry = {
            "step": current_step_num,
            "action": "Approved",
            "role": current_step_info["role"],
            "user_id": user_id,
            "user_name": user_name,
            "timestamp": timezone.now().isoformat(),
            "remark": remark or f"Approved at Tier {current_step_num}, escalated to {next_role}",
            "next_step": next_step_num,
            "pending_role": next_role,
        }
        if not isinstance(instance.approval_history, list):
            instance.approval_history = []
        instance.approval_history.append(history_entry)

        update_fields = ["current_step", "status", "approval_history", "updated_at"]
        instance.save(update_fields=[f for f in update_fields if hasattr(instance, f)])

        # Notify the next tier's approvers
        notify_step_approvers(instance, next_step_num, next_role, canonical, actor=user)
        write_audit(
            user, "approve_step", instance,
            description=f"{canonical} approved at step {current_step_num}, routed to {next_role}",
        )
        return instance, False
