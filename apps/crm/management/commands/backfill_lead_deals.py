"""
Give every lead that has no deal its deal in the Deals pipeline.

    python manage.py backfill_lead_deals             # every tenant
    python manage.py backfill_lead_deals --tenant sweven
    python manage.py backfill_lead_deals --dry-run   # only report

A new lead gets a Draft deal when it is created (``signals.ensure_lead_deal``),
but leads made before that existed have none, so the pipeline shows nothing for
them. The deal opens where the lead stands: Lost / Won for a closed lead, Draft
otherwise -- the same rule the stage sync applies from then on.
"""
from django.core.management.base import BaseCommand

from apps.accounts.models import Client
from apps.core.tenancy import tenant_context
from apps.crm import services
from apps.crm.models import Deal, Lead


class Command(BaseCommand):
    help = "Open a Deals-pipeline deal for every lead that has none."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", help="Only this tenant's slug.")
        parser.add_argument("--dry-run", action="store_true", help="Report without creating deals.")

    def handle(self, *args, **options):
        clients = Client.objects.all()
        if options["tenant"]:
            clients = clients.filter(slug=options["tenant"])

        total = 0
        for client in clients.order_by("slug"):
            with tenant_context(client.id, push_to_db=False):
                with_deal = Deal.objects.filter(
                    client=client, deleted_at__isnull=True, lead__isnull=False
                ).values_list("lead_id", flat=True)
                leads = (
                    Lead.objects.filter(client=client, deleted_at__isnull=True)
                    .exclude(id__in=list(with_deal))
                    .select_related("stage", "party", "owner")
                    .order_by("created_at")
                )
                counts = {}
                for lead in leads:
                    stage = "Draft"
                    if lead.stage_id and lead.stage.is_lost:
                        stage = "Lost"
                    elif lead.stage_id and lead.stage.is_won:
                        stage = "Won"
                    if not options["dry_run"]:
                        services.open_lead_deal(lead, stage, user=None, reason=" (existing lead)")
                    counts[stage] = counts.get(stage, 0) + 1
                made = sum(counts.values())
                total += made
                if made:
                    detail = ", ".join(f"{n} {stage}" for stage, n in sorted(counts.items()))
                    self.stdout.write(f"  {client.slug}: {made} deal(s) -- {detail}")

        verb = "would be created" if options["dry_run"] else "created"
        self.stdout.write(self.style.SUCCESS(f"{total} deal(s) {verb}."))
