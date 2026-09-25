"""
Tenant provisioning and data reset.

``bootstrap_configuration`` gives a tenant the configuration it needs to work
-- number series, company profile row, settings, chart of accounts, units and
the stage catalogues of CRM, PMS and HRMS -- and nothing else. No parties,
items, documents, leads, projects or employees: a tenant starts empty.

``clear_business_data`` is the reverse: it deletes a tenant's business rows
and keeps its access and configuration (users, roles, the permission
catalogue, company profile, settings, chart of accounts, stage catalogues),
so people can still sign in to an empty workspace.
"""
from decimal import Decimal

from django.apps import apps
from django.db import transaction
from django.db.models import ProtectedError, RestrictedError

from .numbering import seed_series_for_client

#: Rows that survive ``clear_business_data``: access, configuration and
#: catalogues. Every other tenant-owned model is business data.
KEPT_MODELS = {
    # Access. ``permissions`` and ``role_permissions`` are never touched.
    "accounts.Client",
    "accounts.Permission",
    "accounts.Role",
    "accounts.RolePermission",
    "accounts.User",
    "accounts.UserPermission",
    "accounts.UserSession",
    "accounts.PasswordResetToken",
    # Workspace configuration.
    "core.NumberSeries",
    "core.Setting",
    "core.CompanyProfile",
    "accounting.Account",
    "accounting.ExpenseCategory",
    "masters.Unit",
    "inventory.QualityStandard",
    # Stage catalogues and templates.
    "crm.Stage",
    "crm.DealStage",
    "crm.Source",
    "crm.Industry",
    "crm.LostReason",
    "crm.MasterTask",
    "crm.StageTask",
    "crm.Form",
    "crm.UserAllocation",
    "pms.Department",
    "pms.StageConfig",
    "pms.Settings",
    "hrms.LeaveType",
    "hrms.WorkingDay",
    "hrms.ApprovalChain",
    "hrms.AssetCategory",
    "hrms.PolicyCategory",
}

DEFAULT_UNITS = (
    ("Nos", "Numbers"), ("Kg", "Kilogram"), ("Mtr", "Metre"),
    ("Sqft", "Square Feet"), ("Set", "Set"), ("Ltr", "Litre"),
)

DEFAULT_CRM_STAGE_TASKS = (
    ("New Lead", "Introductory Call", "Tele Caller Executive", 1),
    ("Details Collected", "Send Company Profile", "BDE", 1),
    ("Quotation Shared", "Quotation Follow-up", "BDE", 2),
    ("Demo Pending", "Schedule Demo", "Area Sales Manager", 2),
    ("Negotiation", "Negotiation Review", "Area Sales Manager", 1),
)

DEFAULT_PMS_DEPARTMENTS = (
    ("Design", "#1f6bff", 18),
    ("Production", "#6d28d9", 24),
    ("Quality", "#0e7490", 12),
    ("Packaging", "#7c3aed", 10),
    ("Installation", "#1d4ed8", 8),
)

#: (name, department, duration in days, needs document, needs approval)
DEFAULT_PMS_STAGES = (
    ("Design & Drawing", "Design", "3", True, True),
    ("Fabrication", "Production", "7", False, False),
    ("Quality Inspection", "Quality", "2", True, False),
    ("Packaging", "Packaging", "1", False, False),
    ("Installation", "Installation", "2", False, True),
)

#: (name, annual entitlement, accrual, paid)
DEFAULT_LEAVE_TYPES = (
    ("Casual Leave", "12", "Yearly", True),
    ("Sick Leave", "8", "Yearly", True),
    ("Earned Leave", "18", "Monthly", True),
    ("Loss of Pay", "0", "None", False),
)


def bootstrap_configuration(client):
    """Create the configuration a tenant needs. Idempotent; never overwrites."""
    from apps.accounting.services import seed_chart_of_accounts
    from apps.core.models import CompanyProfile, Setting
    from apps.core.views import DEFAULT_PREFERENCES, DEFAULT_TAX_SETTINGS
    from apps.crm.models import Stage, StageTask
    from apps.crm.services import seed_crm_configuration
    from apps.hrms.models import LeaveType, WorkingDay
    from apps.masters.models import Unit
    from apps.pms.models import Department, StageConfig
    from apps.pms.services import get_settings

    seed_series_for_client(client)

    # Blank on purpose: the company fills in its own name, GSTIN and address.
    CompanyProfile.objects.get_or_create(
        client=client, defaults={"legal_name": client.name, "trade_name": client.name}
    )
    for key, value in (("preferences", DEFAULT_PREFERENCES), ("tax", DEFAULT_TAX_SETTINGS)):
        Setting.objects.get_or_create(client=client, key=key, defaults={"value": value})

    seed_chart_of_accounts(client)

    for code, label in DEFAULT_UNITS:
        Unit.objects.get_or_create(client=client, code=code, defaults={"label": label})

    seed_crm_configuration(client)
    stages = {stage.name: stage for stage in Stage.objects.filter(client=client)}
    for stage_name, title, role, offset in DEFAULT_CRM_STAGE_TASKS:
        stage = stages.get(stage_name)
        if stage is None:
            continue
        StageTask.objects.get_or_create(
            client=client,
            stage=stage,
            title=title,
            defaults={
                "assignee_role": role,
                "offset_days": offset,
                "priority": "Medium",
                "auto_create": True,
            },
        )

    get_settings(client.id)
    departments = {}
    for name, color, capacity in DEFAULT_PMS_DEPARTMENTS:
        departments[name], _ = Department.objects.get_or_create(
            client=client, name=name, defaults={"color": color, "capacity": capacity}
        )
    for sequence, (name, department, duration, needs_doc, needs_approval) in enumerate(
        DEFAULT_PMS_STAGES, start=1
    ):
        StageConfig.objects.get_or_create(
            client=client,
            name=name,
            defaults={
                "sequence": sequence,
                "department": departments[department],
                "default_duration": Decimal(duration),
                "duration_unit": "Days",
                "required_document": needs_doc,
                "required_approval": needs_approval,
            },
        )

    for weekday in range(7):
        WorkingDay.objects.get_or_create(
            client=client,
            weekday=weekday,
            location=None,
            defaults={"is_working": weekday < 6},  # six-day week
        )
    for name, entitlement, accrual, paid in DEFAULT_LEAVE_TYPES:
        LeaveType.objects.get_or_create(
            client=client,
            name=name,
            defaults={
                "annual_entitlement": Decimal(entitlement),
                "accrual": accrual,
                "is_paid": paid,
                "carry_forward_cap": Decimal("10") if accrual != "None" else None,
            },
        )


def business_models():
    """Every tenant-owned model whose rows are business data."""
    models = []
    for model in apps.get_models():
        label = model._meta.label
        if label in KEPT_MODELS or model._meta.proxy:
            continue
        field_names = {field.name for field in model._meta.fields}
        if "client" in field_names:
            models.append(model)
    return models


def clear_business_data(client):
    """Delete ``client``'s business rows; keep access and configuration.

    Returns ``{model label: rows deleted}``. Stored file bytes are removed
    only after the transaction commits, so a failed reset keeps them.
    """
    from apps.core.files import delete_bytes
    from apps.core.models import File, NumberSeries

    storage_keys = list(
        File._base_manager.filter(client=client).values_list("storage_key", flat=True)
    )
    deleted = {}
    with transaction.atomic():
        # Delete in passes rather than hand-maintaining an order: a model
        # still referenced through PROTECT is retried after its referrers go.
        pending = business_models()
        while pending:
            blocked = []
            for model in pending:
                try:
                    with transaction.atomic():
                        _, per_model = model._base_manager.filter(client=client).delete()
                except (ProtectedError, RestrictedError):
                    blocked.append(model)
                    continue
                for label, count in per_model.items():
                    deleted[label] = deleted.get(label, 0) + count
            if len(blocked) == len(pending):
                names = ", ".join(model._meta.label for model in blocked)
                raise RuntimeError(f"Could not clear {names}: rows are still referenced.")
            pending = blocked

        # Numbering starts again from 1 once nothing carries a number.
        NumberSeries.objects.filter(client=client).update(next_value=1)

        def remove_bytes():
            for key in storage_keys:
                delete_bytes(key)

        transaction.on_commit(remove_bytes)
    return {label: count for label, count in deleted.items() if count}
