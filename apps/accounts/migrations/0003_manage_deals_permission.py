"""Adds the ``manage_deals`` permission the Deals and Contracts views require.

Data only, and additive: nothing is deleted or renamed. The id is granted to
every Administrator role (which by definition holds the whole catalogue) and
to every Sales Manager role (which owns the CRM pipeline) -- before this,
deal and contract writes were refused for everyone but superusers.
"""
from django.db import migrations

PERMISSION = {
    "id": "manage_deals",
    "module": "CRM",
    "group": "Deals & Contracts",
    "label": "Create, edit and delete deals and contracts",
    # permission_catalogue.iter_permissions() gives the 91st entry 900.
    "sort_order": 900,
}
GRANT_TO_ROLE_CODES = ("AD", "SM")


def forwards(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    Role = apps.get_model("accounts", "Role")
    RolePermission = apps.get_model("accounts", "RolePermission")

    Permission.objects.get_or_create(
        id=PERMISSION["id"],
        defaults={k: v for k, v in PERMISSION.items() if k != "id"},
    )
    roles = Role.objects.filter(code__in=GRANT_TO_ROLE_CODES, deleted_at__isnull=True)
    RolePermission.objects.bulk_create(
        [RolePermission(role=role, permission_id=PERMISSION["id"]) for role in roles],
        ignore_conflicts=True,
    )


def backwards(apps, schema_editor):
    # Leave the row: roles may have been given it by hand since.
    pass


class Migration(migrations.Migration):
    dependencies = [("accounts", "0002_initial")]

    operations = [migrations.RunPython(forwards, backwards)]
