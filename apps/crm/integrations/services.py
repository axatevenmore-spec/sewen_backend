"""
Lead ingestion processing pipeline and service orchestrator.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone as dt_timezone
import logging
import re
from typing import Any, Dict, List, Optional

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import Client, User
from apps.core.numbering import allocate_number
from apps.core.tenancy import tenant_context
from apps.core.validators import validate_email_format, validate_phone_number
from apps.crm.models import Lead, LeadSourceIntegration, LeadWebhookEvent, Source, Stage
from apps.crm.services import run_stage_automation

from .base import InboundLeadEvent, NormalizedLead
from .registry import get_provider

logger = logging.getLogger(__name__)

# Background worker pool for async processing
_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="lead_webhook_worker")


def clean_phone_safe(raw_phone: Optional[str]) -> Optional[str]:
    """Cleans phone number using CRM conventions, falling back gracefully without crashing."""
    if not raw_phone:
        return None
    try:
        return validate_phone_number(raw_phone, required=False)
    except Exception:
        # Fallback sanitize: keep digits, spaces, hyphens, and leading +
        cleaned = re.sub(r"[^\d+\-\s()]", "", str(raw_phone)).strip()
        return cleaned or None


def clean_email_safe(raw_email: Optional[str]) -> Optional[str]:
    """Cleans and lowercases email address, falling back gracefully."""
    if not raw_email:
        return None
    try:
        return validate_email_format(raw_email, required=False)
    except Exception:
        return str(raw_email).strip().lower() or None


def resolve_integration(provider_key: str, external_account_id: Optional[str]) -> Optional[LeadSourceIntegration]:
    """
    Finds an active integration by provider and external account id (e.g. Meta Page ID).
    """
    if not external_account_id:
        return None
    return LeadSourceIntegration.objects.filter(
        provider=provider_key,
        external_account_id=str(external_account_id),
        is_active=True,
        deleted_at__isnull=True,
    ).select_related("client").first()


def record_inbound_event(
    provider_key: str,
    event: InboundLeadEvent,
    integration: Optional[LeadSourceIntegration] = None,
) -> LeadWebhookEvent:
    """
    Stores raw webhook event with idempotency. Returns existing row if duplicate.
    """
    client_id = integration.client_id if integration else None

    # Check if duplicate already exists
    existing = LeadWebhookEvent.objects.filter(
        provider=provider_key,
        external_lead_id=event.external_lead_id,
    ).first()
    if existing:
        logger.info(
            "Webhook event already exists for provider=%s external_lead_id=%s (status=%s)",
            provider_key,
            event.external_lead_id,
            existing.status,
        )
        return existing

    try:
        with transaction.atomic():
            return LeadWebhookEvent.objects.create(
                client_id=client_id,
                integration=integration,
                provider=provider_key,
                external_lead_id=event.external_lead_id,
                payload=event.raw_payload or {},
                status="pending",
            )
    except IntegrityError:
        # Concurrent insert won the race
        return LeadWebhookEvent.objects.get(
            provider=provider_key,
            external_lead_id=event.external_lead_id,
        )


def apply_field_mapping(
    normalized: NormalizedLead,
    mapping_config: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Applies custom field mappings from integration settings to override or map
    non-standard form questions into CRM lead fields.
    """
    mapped_overrides: Dict[str, Any] = {}
    if not mapping_config or not isinstance(mapping_config, dict):
        return mapped_overrides

    # mapping_config: e.g. { "form_question_key": "crm_lead_field_name" }
    for src_key, target_field in mapping_config.items():
        if src_key in normalized.custom_fields:
            val = normalized.custom_fields.get(src_key)
            if target_field in ("name", "fullName"):
                mapped_overrides["name"] = str(val)
            elif target_field in ("email",):
                mapped_overrides["email"] = clean_email_safe(val)
            elif target_field in ("phone", "phoneNumber"):
                mapped_overrides["phone"] = clean_phone_safe(val)
            elif target_field in ("company", "companyName"):
                mapped_overrides["company"] = str(val)
            elif target_field in ("city",):
                mapped_overrides["city"] = str(val)
            elif target_field in ("job_title", "jobTitle"):
                mapped_overrides["job_title"] = str(val)
            elif target_field in ("industry",):
                mapped_overrides["industry"] = str(val)

    return mapped_overrides


def create_crm_lead(
    integration: LeadSourceIntegration,
    normalized: NormalizedLead,
) -> Lead:
    """
    Creates a Lead in the CRM using standard creation logic and side effects.
    """
    client = integration.client
    client_id = integration.client_id
    settings_dict = integration.settings or {}

    with tenant_context(client_id):
        # 1. Resolve Stage
        stage = None
        custom_stage_id = settings_dict.get("default_stage_id")
        if custom_stage_id:
            stage = Stage.objects.filter(
                client_id=client_id, pk=custom_stage_id, is_active=True, deleted_at__isnull=True
            ).first()

        if not stage:
            stage = Stage.objects.filter(
                client_id=client_id, is_active=True, deleted_at__isnull=True
            ).order_by("sequence").first()

        if not stage:
            raise ValueError(f"No active Lead Stage configured for tenant {client_id}")

        # 2. Resolve Owner
        owner = None
        custom_owner_id = settings_dict.get("default_owner_id")
        if custom_owner_id:
            owner = User.objects.filter(
                client_id=client_id, pk=custom_owner_id, deleted_at__isnull=True, status="Active"
            ).first()

        # 3. Resolve Source
        source_name = settings_dict.get("source_name") or ("Meta" if normalized.provider == "meta" else normalized.provider.title())
        source, _ = Source.objects.get_or_create(
            client_id=client_id,
            name=source_name,
            defaults={"is_active": True},
        )

        # 4. Resolve Field Overrides
        overrides = apply_field_mapping(normalized, settings_dict.get("field_mapping") or {})

        lead_name = (
            overrides.get("name")
            or normalized.name
            or "Unknown Lead"
        ).strip()

        lead_email = overrides.get("email") or clean_email_safe(normalized.email)
        lead_phone = overrides.get("phone") or clean_phone_safe(normalized.phone)
        lead_company = overrides.get("company") or normalized.company
        lead_city = overrides.get("city") or normalized.city
        lead_job_title = overrides.get("job_title") or normalized.job_title
        lead_industry = overrides.get("industry")

        custom_values = dict(normalized.custom_fields)
        if normalized.attribution:
            custom_values["_attribution"] = normalized.attribution

        with transaction.atomic():
            lead_number = allocate_number(client, "LEAD")
            lead = Lead.objects.create(
                client_id=client_id,
                lead_number=lead_number,
                name=lead_name,
                company=lead_company,
                phone=lead_phone,
                email=lead_email,
                city=lead_city,
                job_title=lead_job_title,
                industry=lead_industry,
                stage=stage,
                source=source,
                owner=owner,
                custom_values=custom_values,
                source_provider=normalized.provider,
                external_lead_id=normalized.external_lead_id,
                source_platform=normalized.attribution.get("platform"),
                source_meta=normalized.attribution,
            )

            # Fire stage automation tasks and assignee notifications
            run_stage_automation(lead, stage, user=None)

        logger.info(
            "Created CRM Lead %s (%s) for provider=%s external_lead_id=%s",
            lead.lead_number,
            lead.id,
            normalized.provider,
            normalized.external_lead_id,
        )
        return lead


def process_lead_event(event_id: str) -> bool:
    """
    Executes the ingestion pipeline for a single LeadWebhookEvent:
    1. Load pending event -> mark 'processing'.
    2. Resolve integration.
    3. fetchLeadDetails -> normalize.
    4. Apply field mapping.
    5. Check duplicate (provider, external_lead_id).
    6. Create lead via CRM service.
    7. Mark 'processed', link lead_id.
    8. Error handling with retry backoff.
    """
    try:
        event = LeadWebhookEvent.objects.select_related("integration", "integration__client").get(id=event_id)
    except LeadWebhookEvent.DoesNotExist:
        logger.error("LeadWebhookEvent %s not found for processing", event_id)
        return False

    if event.status in ("processed", "duplicate"):
        logger.info("LeadWebhookEvent %s already finished with status=%s", event_id, event.status)
        return True

    event.status = "processing"
    event.attempts += 1
    event.save(update_fields=["status", "attempts", "updated_at"])

    provider = get_provider(event.provider)
    if not provider:
        event.status = "failed"
        event.error_message = f"Unsupported provider: {event.provider}"
        event.save(update_fields=["status", "error_message", "updated_at"])
        logger.error("Provider %s is not registered", event.provider)
        return False

    integration = event.integration
    if not integration:
        # Try resolving by external_account_id from payload
        external_account_id = (
            (event.payload or {}).get("page_id")
            or (event.payload or {}).get("account_id")
        )
        integration = resolve_integration(event.provider, external_account_id)
        if integration:
            event.integration = integration
            event.client = integration.client
            event.save(update_fields=["integration", "client", "updated_at"])

    if not integration or not integration.is_active:
        event.status = "failed"
        event.error_message = (
            f"No active integration found for provider={event.provider} "
            f"payload={event.payload.get('page_id')}"
        )
        event.save(update_fields=["status", "error_message", "updated_at"])
        logger.warning(
            "Could not resolve active integration for event %s (provider=%s)",
            event.id,
            event.provider,
        )
        return False

    try:
        # Idempotency check 1: Has a Lead already been created for this provider + external_lead_id?
        existing_lead = Lead.objects.filter(
            client_id=integration.client_id,
            source_provider=event.provider,
            external_lead_id=event.external_lead_id,
            deleted_at__isnull=True,
        ).first()

        if existing_lead:
            event.status = "duplicate"
            event.lead = existing_lead
            event.processed_at = timezone.now()
            event.error_message = f"Duplicate lead already exists with id {existing_lead.lead_number}"
            event.save(update_fields=["status", "lead", "processed_at", "error_message", "updated_at"])
            logger.info(
                "Event %s marked duplicate: Lead %s already exists for %s:%s",
                event.id,
                existing_lead.lead_number,
                event.provider,
                event.external_lead_id,
            )
            return True

        # Fetch details
        inbound_event = InboundLeadEvent(
            external_lead_id=event.external_lead_id,
            external_account_id=integration.external_account_id,
            raw_payload=event.payload,
        )
        details = provider.fetch_lead_details(inbound_event, integration)

        # Normalize
        normalized = provider.normalize(details, inbound_event)

        # Create CRM Lead
        lead = create_crm_lead(integration, normalized)

        # Mark processed
        event.lead = lead
        event.status = "processed"
        event.processed_at = timezone.now()
        event.error_message = ""
        event.save(update_fields=["lead", "status", "processed_at", "error_message", "updated_at"])
        return True

    except Exception as exc:
        logger.exception("Error processing lead event %s: %s", event.id, exc)
        event.error_message = str(exc)[:1000]
        # Max ~5 attempts
        if event.attempts >= 5:
            event.status = "failed"
        else:
            event.status = "pending"  # Eligible for retry
        event.save(update_fields=["status", "error_message", "updated_at"])
        return False


def dispatch_lead_events(event_ids: List[str], run_sync: bool = False) -> None:
    """
    Dispatches events for processing. If run_sync is True (tests/CLI), runs immediately.
    """
    for event_id in event_ids:
        if run_sync or not getattr(settings, "LEAD_WEBHOOK_ASYNC", True):
            process_lead_event(event_id)
        else:
            _EXECUTOR.submit(process_lead_event, event_id)
