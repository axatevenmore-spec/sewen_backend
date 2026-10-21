"""Synchronize all default roles and permissions according to Sweven Functional Spec.

Ensures all default roles (AD, GM, PM, EM, TC, SE, SM, HR, CU, AC, PU, ST) exist
with their respective permissions across all client tenants in the database.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    from apps.accounts.permission_catalogue import sync_permissions, seed_roles
    Client = apps.get_model("accounts", "Client")
    sync_permissions()
    for client in Client.objects.all():
        seed_roles(client, sync_existing=True)


def backwards(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [("accounts", "0007_place_portal_order_permission")]

    operations = [migrations.RunPython(forwards, backwards)]
