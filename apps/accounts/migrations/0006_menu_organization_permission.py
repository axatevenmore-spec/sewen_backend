"""Adds ``menu_organization`` for the Organization sidebar section.

Org Chart, Departments, Designations and Locations moved out of HRMS into a
section of their own. Data only, and additive. The id goes to every
Administrator and HR Manager role, and to every role that could already see
the HRMS menu -- the pages were under it until now, so nobody loses them.
"""
from django.db import migrations

PERMISSION = {
    "id": "menu_organization",
    "module": "Menu Access",
    "group": "Navigation Visibility",
    "label": "Organization menu",
    # permission_catalogue.iter_permissions() order; sync_permissions() keeps it.
    "sort_order": 580,
}
GRANT_TO_ROLE_CODES = ("AD", "HR")


def forwards(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    Role = apps.get_model("accounts", "Role")
    RolePermission = apps.get_model("accounts", "RolePermission")

    Permission.objects.get_or_create(
        id=PERMISSION["id"],
        defaults={k: v for k, v in PERMISSION.items() if k != "id"},
    )
    by_code = Role.objects.filter(code__in=GRANT_TO_ROLE_CODES, deleted_at__isnull=True)
    hrms_role_ids = RolePermission.objects.filter(permission_id="menu_hrms").values_list(
        "role_id", flat=True
    )
    by_menu = Role.objects.filter(id__in=hrms_role_ids, deleted_at__isnull=True)
    roles = {role.pk: role for role in [*by_code, *by_menu]}.values()
    RolePermission.objects.bulk_create(
        [RolePermission(role=role, permission_id=PERMISSION["id"]) for role in roles],
        ignore_conflicts=True,
    )


def backwards(apps, schema_editor):
    # Leave the row: roles may have been given it by hand since.
    pass


class Migration(migrations.Migration):
    dependencies = [("accounts", "0005_user_party")]

    operations = [migrations.RunPython(forwards, backwards)]
