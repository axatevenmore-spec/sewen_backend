"""Every workspace gets the Deals pipeline's five stages in CRM System Setup.

The Deals board always shows Draft / Sent / Open / Won / Lost (``Deal.STAGES``),
but a workspace created before the deal-stage catalogue was seeded had none,
so System Setup's Deal Stages listed nothing. Missing ones are added; a stage
the workspace already has (by name) is left as it is.
"""
from django.db import migrations

PIPELINE_STAGES = [
    # name, sequence, icon, bg, fg, is_won, is_lost
    ("Draft", 1, "file", "#eff6ff", "#2563eb", False, False),
    ("Sent", 2, "send", "#f5f3ff", "#7c3aed", False, False),
    ("Open", 3, "clock", "#fff7ed", "#ea580c", False, False),
    ("Won", 4, "file", "#f0fdf4", "#16a34a", True, False),
    ("Lost", 5, "lost", "#fef2f2", "#dc2626", False, True),
]


def add_pipeline_stages(apps, schema_editor):
    Client = apps.get_model("accounts", "Client")
    DealStage = apps.get_model("crm", "DealStage")
    for client in Client.objects.all():
        live = DealStage.objects.filter(client=client, deleted_at__isnull=True)
        for name, sequence, icon, bg, fg, is_won, is_lost in PIPELINE_STAGES:
            if live.filter(name__iexact=name).exists():
                continue
            DealStage.objects.create(
                client=client, name=name, sequence=sequence, pipeline="Sales",
                icon=icon, bg=bg, fg=fg, is_won=is_won, is_lost=is_lost, is_active=True,
            )


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0011_lead_reopen_stage"),
        ("accounts", "0006_menu_organization_permission"),
    ]

    operations = [
        migrations.RunPython(add_pipeline_stages, migrations.RunPython.noop),
    ]
