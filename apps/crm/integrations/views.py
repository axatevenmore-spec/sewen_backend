"""
Generic webhook view for provider-agnostic lead ingestion.
"""
import logging
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from .registry import get_provider
from .services import (
    dispatch_lead_events,
    record_inbound_event,
    resolve_integration,
)

logger = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name="dispatch")
class LeadWebhookView(View):
    """
    Unified inbound webhook endpoint for all lead source providers.
    Mounted at /webhooks/leads/<provider>/ and /api/v1/webhooks/leads/<provider>/.
    """

    def get(self, request: HttpRequest, provider: str, *args, **kwargs) -> HttpResponse:
        """Handles provider URL verification / handshake challenge."""
        provider_adapter = get_provider(provider)
        if not provider_adapter:
            logger.warning("GET webhook requested for unknown provider '%s'", provider)
            return JsonResponse({"message": f"Provider '{provider}' not found."}, status=404)

        response = provider_adapter.handle_verification(request)
        if response is not None:
            return response

        return JsonResponse(
            {"message": f"Verification not supported or required for provider '{provider}'."},
            status=400,
        )

    def post(self, request: HttpRequest, provider: str, *args, **kwargs) -> HttpResponse:
        """
        Handles inbound lead submission event:
        1. Resolves adapter (or 404).
        2. Verifies request authenticity / HMAC signature (or 403).
        3. Parses events and records them in raw log.
        4. Responds 200 OK immediately.
        5. Dispatches async processing.
        """
        provider_adapter = get_provider(provider)
        if not provider_adapter:
            logger.warning("POST webhook received for unknown provider '%s'", provider)
            return JsonResponse({"message": f"Provider '{provider}' not found."}, status=404)

        # Authenticity / Signature check
        is_authentic = provider_adapter.verify_authenticity(request)
        if not is_authentic:
            logger.warning("Authenticity verification failed for provider '%s'", provider)
            return JsonResponse(
                {"message": "Unauthorized / Invalid signature or payload."},
                status=403,
            )

        # Parse events
        events = provider_adapter.parse_events(request)
        if not events:
            # Acknowledgement for empty / non-leadgen change event
            return JsonResponse({"received": True, "count": 0}, status=200)

        event_ids = []
        for event in events:
            integration = resolve_integration(provider, event.external_account_id)
            event_row = record_inbound_event(provider, event, integration=integration)
            event_ids.append(str(event_row.id))

        # Dispatch async (or sync if configured for tests)
        dispatch_lead_events(event_ids)

        # Fast 200 response
        return JsonResponse(
            {"received": True, "events": len(event_ids)},
            status=200,
        )
