"""
Management command to backfill leads from providers or reprocess failed webhook events.
"""
from datetime import datetime, timedelta, timezone as dt_timezone
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.accounts.models import Client
from apps.crm.integrations.base import InboundLeadEvent
from apps.crm.integrations.registry import get_provider
from apps.crm.integrations.services import create_crm_lead, process_lead_event, record_inbound_event
from apps.crm.models import Lead, LeadSourceIntegration, LeadWebhookEvent


class Command(BaseCommand):
    help = "Backfills leads from external providers or reprocesses failed webhook events."

    def add_arguments(self, parser):
        parser.add_argument("--reprocess-failed", action="store_true", help="Reprocess all failed webhook events")
        parser.add_argument("--backfill", action="store_true", help="Run backfill for active integrations")
        parser.add_argument("--tenant", default="", help="Limit to specific tenant slug or UUID")
        parser.add_argument("--provider", default="", help="Filter by provider (e.g. 'meta')")
        parser.add_argument("--days", type=int, default=30, help="Days of history to backfill (default: 30)")

    def handle(self, *args, **options):
        tenant_ident = options.get("tenant")
        client = None
        if tenant_ident:
            client = Client.objects.filter(slug=tenant_ident).first() or Client.objects.filter(id=tenant_ident).first()
            if not client:
                self.stderr.write(f"Tenant '{tenant_ident}' not found.")
                return

        provider_filter = options.get("provider", "").strip().lower()

        # 1. Reprocess Failed Events
        if options.get("reprocess_failed"):
            events_qs = LeadWebhookEvent.objects.filter(status="failed")
            if provider_filter:
                events_qs = events_qs.filter(provider=provider_filter)
            if client:
                events_qs = events_qs.filter(client=client)

            total = events_qs.count()
            self.stdout.write(f"Found {total} failed webhook event(s) to reprocess.")
            success_count = 0
            for event in events_qs:
                ok = process_lead_event(str(event.id))
                if ok:
                    success_count += 1
            self.stdout.write(self.style.SUCCESS(f"Reprocessed {total} events: {success_count} succeeded."))

        # 2. Backfill from provider API
        if options.get("backfill"):
            integ_qs = LeadSourceIntegration.objects.filter(is_active=True, deleted_at__isnull=True)
            if provider_filter:
                integ_qs = integ_qs.filter(provider=provider_filter)
            if client:
                integ_qs = integ_qs.filter(client=client)

            since_dt = timezone.now() - timedelta(days=options.get("days", 30))
            self.stdout.write(f"Running backfill across {integ_qs.count()} integration(s) since {since_dt}...")

            for integration in integ_qs:
                provider = get_provider(integration.provider)
                if not provider:
                    self.stdout.write(self.style.WARNING(f"Provider {integration.provider} has no registered adapter."))
                    continue

                self.stdout.write(f"Backfilling {integration.name} (Provider: {integration.provider}, ID: {integration.id})...")
                try:
                    leads = provider.backfill(integration, since=since_dt)
                    self.stdout.write(f"Retrieved {len(leads)} lead(s) from provider.")
                    ingested = 0
                    duplicates = 0
                    for normalized in leads:
                        # Check idempotency
                        existing = Lead.objects.filter(
                            client_id=integration.client_id,
                            source_provider=normalized.provider,
                            external_lead_id=normalized.external_lead_id,
                            deleted_at__isnull=True,
                        ).first()
                        if existing:
                            duplicates += 1
                            continue

                        # Record inbound event for audit trail
                        evt = InboundLeadEvent(
                            external_lead_id=normalized.external_lead_id,
                            external_account_id=integration.external_account_id,
                            raw_payload=normalized.raw,
                        )
                        event_row = record_inbound_event(normalized.provider, evt, integration=integration)
                        lead = create_crm_lead(integration, normalized)
                        event_row.lead = lead
                        event_row.status = "processed"
                        event_row.processed_at = timezone.now()
                        event_row.save(update_fields=["lead", "status", "processed_at", "updated_at"])
                        ingested += 1

                    integration.last_synced_at = timezone.now()
                    integration.save(update_fields=["last_synced_at", "updated_at"])
                    self.stdout.write(self.style.SUCCESS(f"  -> Ingested: {ingested}, Skipped Duplicates: {duplicates}"))
                except Exception as exc:
                    self.stdout.write(self.style.ERROR(f"Error backfilling integration {integration.id}: {exc}"))
