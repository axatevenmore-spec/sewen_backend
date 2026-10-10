"""
Which CRM records a user may see.

Managers see the whole team's records. Everyone else -- a Sales Executive, or
an employee with no CRM permission at all -- sees what they are assigned to:

    lead       they own it, are on its users list, created it, or have a task on it
    deal       they own it, created it, have a task on it, or can see its lead
    project    they own it, created it, or can see its deal
    contract   they created it, or can see its deal

Tasks and task allocations were already scoped to their assignee
(``TaskViewSet`` / ``TaskAllocationViewSet``); the same managers see all.
"""
from django.db.models import Q

from apps.core.permissions import has_permission

from .models import Contract, CrmProject, Deal, Lead, LeadUser, Task

#: Holders see every lead, deal, project and contract (a Sales Manager).
TEAM_SCOPE = ("assign_task", "manage_pipeline", "manage_deals")
#: Administration section -- admins oversee everything, as in PMS.
ADMIN_PERMISSION = "menu_admin"
#: Who runs CRM projects besides the sales team (the Project Manager role).
PROJECT_MANAGERS = ("create_project", "edit_project")


def sees_all(user):
    return bool(
        getattr(user, "is_superuser", False)
        or has_permission(user, ADMIN_PERMISSION)
        or has_permission(user, TEAM_SCOPE)
    )


def sees_all_projects(user):
    return sees_all(user) or has_permission(user, PROJECT_MANAGERS)


def _live(model, **filters):
    return model.objects.filter(deleted_at__isnull=True, **filters)


def lead_ids(user):
    tenant = {"client_id": user.client_id}
    ids = set(
        _live(Lead, **tenant)
        .filter(Q(owner=user) | Q(created_by=user))
        .values_list("id", flat=True)
    )
    ids |= set(_live(LeadUser, user=user, **tenant).values_list("lead_id", flat=True))
    ids |= set(
        _live(Task, assignee=user, lead__isnull=False, **tenant).values_list("lead_id", flat=True)
    )
    return ids


def deal_ids(user, leads=None):
    leads = lead_ids(user) if leads is None else leads
    tenant = {"client_id": user.client_id}
    ids = set(
        _live(Deal, **tenant)
        .filter(Q(owner=user) | Q(created_by=user) | Q(lead_id__in=leads))
        .values_list("id", flat=True)
    )
    ids |= set(
        _live(Task, assignee=user, deal__isnull=False, **tenant).values_list("deal_id", flat=True)
    )
    return ids


def project_ids(user):
    deals = deal_ids(user)
    return set(
        _live(CrmProject, client_id=user.client_id)
        .filter(Q(owner=user) | Q(created_by=user) | Q(deal_id__in=deals))
        .values_list("id", flat=True)
    ) | set(
        _live(Deal, pk__in=deals, crm_project__isnull=False).values_list("crm_project_id", flat=True)
    )


def contract_ids(user):
    return set(
        _live(Contract, client_id=user.client_id)
        .filter(Q(created_by=user) | Q(deal_id__in=deal_ids(user)))
        .values_list("id", flat=True)
    )
