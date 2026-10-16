"""Adds ``place_portal_order`` for the client portal's reorder endpoints.

Data only, and additive. The id goes to every Administrator role (which holds
the whole catalogue) and to every Customer role -- the endpoints still serve
only a user linked to a customer party, so the grant alone opens nothing to
staff.
"""
from django.db import migrations

PERMISSION = {
    "id": "place_portal_order",
    "module": "Sales",
    "group": "Client Portal",
    "label": "Reorder purchased items from the client portal",
    # permission_catalogue.iter_permissions() order; sync_permissions() keeps it.
    "sort_order": 1070,
}
GRANT_TO_ROLE_CODES = ("AD", "CU")


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
    dependencies = [("accounts", "0006_menu_organization_permission")]

    operations = [migrations.RunPython(forwards, backwards)]
