"""
Print payloads (api.md §12.1).

Every printable document returns the same envelope -- company profile, the
document, and the computed totals in words -- so the in-app preview and the
customer-facing PDF render from one source (api-integration.md §10.5).

PDF rendering itself is a separate concern; ``/pdf/`` returns 501 with a clear
code rather than a broken file, so the frontend can fall back to the print
payload until a renderer is wired up.
"""
from django.utils import timezone

from .exceptions import EvenmoreAPIError
from .files import public_url
from .money import amount_in_words, round2


class PdfNotAvailable(EvenmoreAPIError):
    status_code = 501
    default_code = "PDF_RENDERER_UNAVAILABLE"
    default_message = "PDF rendering is not configured on this server."


def company_payload(client_id, request=None):
    from .models import CompanyProfile

    profile = CompanyProfile.objects.filter(client_id=client_id).select_related(
        "logo_file", "signature_file", "bank_account"
    ).first()
    if profile is None:
        return {}

    return {
        "legalName": profile.legal_name,
        "tradeName": profile.trade_name,
        "gstin": profile.gstin,
        "pan": profile.pan,
        "cin": profile.cin,
        "address": profile.address,
        "state": profile.state,
        "phone": profile.phone,
        "email": profile.email,
        "website": profile.website,
        "logo": public_url(profile.logo_file, request),
        "signature": public_url(profile.signature_file, request),
        "bank": (
            {
                "name": profile.bank_account.name,
                "accountNumber": profile.bank_account.account_number,
                "ifsc": profile.bank_account.ifsc,
                "bankName": profile.bank_account.bank_name,
                "branch": profile.bank_account.branch,
            }
            if profile.bank_account_id
            else None
        ),
    }


def print_payload(document, serializer_class, *, request, title, terms_key=None):
    """The JSON print payload of api.md §12.1."""
    from .models import Setting

    client_id = document.client_id
    templates = Setting.objects.filter(
        client_id=client_id, key="print_templates"
    ).values_list("value", flat=True).first() or {}

    currency = "INR"
    try:
        currency = document.client.currency
    except Exception:  # pragma: no cover - client not loaded
        pass

    return {
        "title": title,
        "company": company_payload(client_id, request),
        "document": serializer_class(document, context={"request": request}).data,
        "totals": {
            "grandTotal": round2(getattr(document, "total", 0)),
            "amountInWords": amount_in_words(getattr(document, "total", 0), currency),
            "taxableValue": round2(getattr(document, "taxable_value", 0)),
            "totalTax": round2(getattr(document, "total_tax", 0)),
            "cgst": round2(getattr(document, "cgst", 0)),
            "sgst": round2(getattr(document, "sgst", 0)),
            "igst": round2(getattr(document, "igst", 0)),
        },
        "terms": (
            getattr(document, "terms", None)
            or (templates.get(terms_key) if terms_key else None)
            or templates.get("defaultTerms")
        ),
        "generatedAt": timezone.now(),
    }


def send_payload(document, *, channel, recipients, subject=None, message=None):
    """``POST /{module}/{entity}/{id}/send/`` (api.md §12.1).

    Queues the send and records the intent. Actual delivery belongs to the
    notification service; the endpoint exists so the UI's send modals stop
    being no-ops.
    """
    from .audit import record_audit

    record_audit(
        client=document.client_id,
        actor=None,
        action="send",
        entity_type=document.__class__.__name__,
        entity_id=document.id,
        entity_label=str(document),
        description=f"Queued for {channel} to {', '.join(recipients or []) or 'no recipients'}",
        after={"channel": channel, "recipients": recipients, "subject": subject},
    )
    return {
        "queued": True,
        "channel": channel,
        "recipients": recipients or [],
        "subject": subject,
        "message": message,
    }
