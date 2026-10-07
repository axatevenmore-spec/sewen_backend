"""
Customer approval links for sales documents -- the sales counterpart of the PMS
design-proof link (apps/pms ``ProofShare``, ``/pms/approve/:token``).

Staff generate a link for an estimate, quotation, proforma invoice or sales
invoice; the customer opens ``/sales/approve/:token`` without an account, reads
the document, leaves comments and approves or rejects it once.

What a decision does to the document is per type (``DOC_TYPES``), and is no
more than what staff can already do by hand:

    estimate          Approved -> Accepted, Rejected -> Rejected
    quotation         Approved -> Accepted, Rejected -> Rejected (as /accept/, /reject/)
    proforma_invoice  Approved -> Accepted; a rejection is recorded on the link only
    sales_invoice     recorded on the link only -- an invoice's status is its payment state

Only the link's own decision fields always change; staff are notified either way.
"""
import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.core.audit import notify, record_audit
from apps.core.exceptions import Codes, Conflict, NotFound, ValidationFailed
from apps.core.money import round2

from .models import (
    Estimate,
    ProformaInvoice,
    Quotation,
    QuotationActivity,
    SalesApprovalComment,
    SalesApprovalLink,
    SalesInvoice,
)

DEFAULT_EXPIRY_DAYS = 14
DECISIONS = ("Approved", "Rejected")


@dataclass(frozen=True)
class DocType:
    key: str
    model: type
    title: str
    number_field: str
    audit_entity: str
    #: Statuses in which the customer may still approve or reject.
    decidable: tuple
    #: Document status to set on each decision; a missing key leaves it alone.
    on_decision: dict
    #: Status a Draft moves to when a link is issued (None = unchanged).
    on_share_from_draft: str = None


DOC_TYPES = {
    "estimate": DocType(
        key="estimate",
        model=Estimate,
        title="Estimate",
        number_field="estimate_number",
        audit_entity="Estimate",
        decidable=("Draft", "Sent"),
        on_decision={"Approved": "Accepted", "Rejected": "Rejected"},
    ),
    "quotation": DocType(
        key="quotation",
        model=Quotation,
        title="Quotation",
        number_field="quotation_number",
        audit_entity="Quotation",
        decidable=("Draft", "Sent", "Viewed"),
        on_decision={"Approved": "Accepted", "Rejected": "Rejected"},
        # Same as the existing quotation share: issuing a link sends it.
        on_share_from_draft="Sent",
    ),
    "proforma_invoice": DocType(
        key="proforma_invoice",
        model=ProformaInvoice,
        title="Proforma Invoice",
        number_field="proforma_number",
        audit_entity="ProformaInvoice",
        decidable=("Draft", "Sent"),
        on_decision={"Approved": "Accepted"},
    ),
    "sales_invoice": DocType(
        key="sales_invoice",
        model=SalesInvoice,
        title="Tax Invoice",
        number_field="invoice_number",
        audit_entity="SalesInvoice",
        decidable=("Draft", "Unpaid", "Partially Paid", "Paid"),
        on_decision={},
    ),
}


def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def doc_number(spec, document):
    return getattr(document, spec.number_field, None)


def load_document(link):
    spec = DOC_TYPES[link.doc_type]
    document = spec.model.objects.filter(
        pk=link.document_id, client_id=link.client_id, deleted_at__isnull=True
    ).first()
    if document is None:
        raise NotFound("This document is no longer available.")
    return spec, document


# ---------------------------------------------------------------------------
# Staff side
# ---------------------------------------------------------------------------
@transaction.atomic
def issue_link(*, spec, document, user, recipient_name=None, recipient_email=None,
               message=None, expiry_days=DEFAULT_EXPIRY_DAYS):
    """Create a link; any earlier undecided live link for the document is revoked."""
    if getattr(document, "status", None) == "Cancelled":
        raise Conflict(
            f"A cancelled {spec.title.lower()} cannot be shared for approval.",
            code=Codes.ALREADY_CANCELLED,
        )

    now = timezone.now()
    SalesApprovalLink.objects.filter(
        client_id=document.client_id,
        doc_type=spec.key,
        document_id=document.id,
        status="Active",
        decision__isnull=True,
        deleted_at__isnull=True,
    ).update(
        status="Revoked", revoked_at=now, revoked_reason="Superseded by a newer link", updated_at=now
    )

    token = secrets.token_urlsafe(32)
    link = SalesApprovalLink.objects.create(
        client_id=document.client_id,
        doc_type=spec.key,
        document_id=document.id,
        document_number=doc_number(spec, document),
        token_hash=hash_token(token),
        recipient_name=(recipient_name or "").strip() or document.party_name,
        recipient_email=recipient_email or None,
        message=(message or "").strip() or None,
        expires_at=now + timedelta(days=expiry_days),
        created_by=user,
    )

    if spec.on_share_from_draft and document.status == "Draft":
        document.status = spec.on_share_from_draft
        document.save(update_fields=["status", "updated_at"])
    if spec.key == "quotation":
        QuotationActivity.objects.create(
            quotation=document, event="sent", actor_label=getattr(user, "email", None),
            comment=f"Approval link issued to {link.recipient_name or 'customer'}",
        )

    record_audit(
        client=document.client_id,
        actor=user,
        action="share_approval_link",
        entity_type=spec.audit_entity,
        entity_id=document.id,
        entity_label=doc_number(spec, document),
        description=f"Approval link issued to {link.recipient_name or 'customer'}",
    )
    return link, token


@transaction.atomic
def revoke_link(link, *, user, reason=None):
    if link.status != "Active":
        return link
    link.status = "Revoked"
    link.revoked_at = timezone.now()
    link.revoked_reason = (reason or "").strip() or "Revoked by staff"
    link.save(update_fields=["status", "revoked_at", "revoked_reason", "updated_at"])
    spec = DOC_TYPES[link.doc_type]
    record_audit(
        client=link.client_id,
        actor=user,
        action="revoke_approval_link",
        entity_type=spec.audit_entity,
        entity_id=link.document_id,
        entity_label=link.document_number,
        description=link.revoked_reason,
    )
    return link


def comments_for(spec_key, document_id, client_id):
    return SalesApprovalComment.objects.filter(
        client_id=client_id, doc_type=spec_key, document_id=document_id, deleted_at__isnull=True
    ).order_by("created_at")


def comment_rows(rows):
    return [
        {
            "id": str(row.id),
            "author": row.author_name,
            "authorType": row.author_type,
            "text": row.text,
            "linkId": str(row.link_id) if row.link_id else None,
            "createdAt": row.created_at,
        }
        for row in rows
    ]


def link_row(link):
    """A link as staff see it. The token is never returned after creation."""
    return {
        "id": str(link.id),
        "docType": link.doc_type,
        "documentId": str(link.document_id),
        "documentNumber": link.document_number,
        "recipientName": link.recipient_name,
        "recipientEmail": link.recipient_email,
        "message": link.message,
        "status": "Expired" if link.status == "Active" and link.expires_at < timezone.now() else link.status,
        "decision": link.decision,
        "decidedAt": link.decided_at,
        "decidedBy": link.decided_by,
        "decisionComments": link.decision_comments,
        "rejectionReason": link.rejection_reason,
        "openedAt": link.opened_at,
        "expiresAt": link.expires_at,
        "revokedAt": link.revoked_at,
        "revokedReason": link.revoked_reason,
        "createdBy": getattr(link.created_by, "name", None),
        "createdAt": link.created_at,
    }


# ---------------------------------------------------------------------------
# Customer side
# ---------------------------------------------------------------------------
def resolve_public_link(token):
    """The live link for a public token, or the 404/409 the portal explains."""
    from apps.core.tenancy import set_current_client_id

    link = (
        SalesApprovalLink.objects.filter(token_hash=hash_token(token or ""), deleted_at__isnull=True)
        .select_related("created_by")
        .first()
    )
    if link is None:
        raise NotFound("This approval link is not valid.")
    if link.status == "Revoked" or link.revoked_at is not None:
        raise Conflict("This approval link has been revoked.", code=Codes.TOKEN_REVOKED)
    if link.expires_at < timezone.now():
        if link.status != "Expired":
            link.status = "Expired"
            link.save(update_fields=["status", "updated_at"])
        raise Conflict("This approval link has expired.", code=Codes.TOKEN_EXPIRED)

    set_current_client_id(link.client_id)
    return link


def can_decide(link, spec, document):
    return link.decision is None and document.status in spec.decidable


def public_document(spec, document):
    """What the customer sees: the printed document, nothing internal.

    Built field by field rather than from the staff serializer so internal
    columns (cash split, original items, posting metadata) never leave.
    """
    from apps.core.printing import amount_in_words

    currency = getattr(document.client, "currency", None) or "INR"
    lines = document.line_items.filter(deleted_at__isnull=True).order_by("line_no")
    return {
        "type": spec.key,
        "title": spec.title,
        "number": doc_number(spec, document),
        "status": document.status,
        "date": document.doc_date,
        "validUntil": getattr(document, "valid_until", None),
        "dueDate": getattr(document, "due_date", None),
        "subject": getattr(document, "subject", None),
        "referenceNumber": getattr(document, "reference_number", None),
        "paymentTerms": getattr(document, "payment_terms", None),
        "deliveryTerms": getattr(document, "delivery_terms", None),
        "partyName": document.party_name,
        "partyGstin": document.party_gstin,
        "billingAddress": document.billing_address or {},
        "shippingAddress": document.shipping_address or {},
        "placeOfSupply": document.place_of_supply,
        "currency": currency,
        "lineItems": [
            {
                "lineNo": line.line_no,
                "itemName": line.item_name,
                "description": line.description,
                "hsnCode": line.hsn_code,
                "uom": line.uom,
                "qty": line.qty,
                "rate": line.rate,
                "discountPct": line.discount_pct,
                "discountAmount": round2(line.discount_amount),
                "taxPct": line.tax_pct,
                "taxAmount": round2(line.tax_amount),
                "amount": round2(line.amount),
                "lineTotal": round2(line.line_total),
            }
            for line in lines
        ],
        "totals": {
            "subtotal": round2(document.subtotal),
            "totalDiscount": round2(document.total_discount),
            "taxableValue": round2(document.taxable_value),
            "cgst": round2(document.cgst),
            "sgst": round2(document.sgst),
            "igst": round2(document.igst),
            "cess": round2(document.cess),
            "totalTax": round2(document.total_tax),
            "freightCharges": round2(document.freight_charges),
            "otherCharges": round2(document.other_charges),
            "roundOff": round2(document.round_off),
            "grandTotal": round2(document.total),
            "amountInWords": amount_in_words(document.total, currency),
        },
        "notes": document.notes,
        "terms": document.terms,
    }


def public_payload(link, request):
    from apps.core.printing import company_payload

    spec, document = load_document(link)
    return {
        "company": company_payload(link.client_id, request),
        "document": public_document(spec, document),
        "recipientName": link.recipient_name,
        "message": link.message,
        "createdBy": getattr(link.created_by, "name", None),
        "decision": link.decision,
        "decidedAt": link.decided_at,
        "decidedBy": link.decided_by,
        "decisionComments": link.decision_comments,
        "rejectionReason": link.rejection_reason,
        "canDecide": can_decide(link, spec, document),
        "expiresAt": link.expires_at,
        "comments": comment_rows(comments_for(spec.key, document.id, link.client_id)),
    }


def mark_opened(link, spec, document, request):
    from apps.core.audit import request_ip

    if link.opened_at is None:
        link.opened_at = timezone.now()
        link.save(update_fields=["opened_at", "updated_at"])
    if spec.key == "quotation":
        QuotationActivity.objects.create(
            quotation=document, event="viewed", actor_label=link.recipient_name or "customer",
            ip=request_ip(request), user_agent=(request.META.get("HTTP_USER_AGENT") or "")[:500],
        )
        if document.status == "Sent":
            document.status = "Viewed"
            document.save(update_fields=["status", "updated_at"])


def add_comment(*, link=None, spec_key, document, author_type, author_name, text):
    text = (text or "").strip()
    if not text:
        raise ValidationFailed("Enter a comment.", field_errors={"text": ["Required."]})
    if len(text) > 4000:
        raise ValidationFailed("That comment is too long.", field_errors={"text": ["At most 4000 characters."]})
    return SalesApprovalComment.objects.create(
        client_id=document.client_id,
        doc_type=spec_key,
        document_id=document.id,
        link=link,
        author_type=author_type,
        author_name=(author_name or "").strip()[:200] or None,
        text=text,
    )


@transaction.atomic
def record_decision(link, *, decision, decided_by=None, comments=None, rejection_reason=None, ip=None):
    """The customer's one decision on a link, written through to the document."""
    link = SalesApprovalLink.objects.select_for_update().get(pk=link.pk)
    spec, document = load_document(link)

    if link.decision is not None:
        raise Conflict("A decision has already been recorded for this link.", code=Codes.ALREADY_DECIDED)
    if decision not in DECISIONS:
        raise ValidationFailed(
            "Choose whether to approve or reject.",
            field_errors={"decision": ["Expected 'Approved' or 'Rejected'."]},
        )
    decided_by = (decided_by or "").strip() or link.recipient_name or "Customer"
    comments = (comments or "").strip() or None
    rejection_reason = (rejection_reason or "").strip() or None
    if decision == "Rejected" and not rejection_reason:
        raise ValidationFailed(
            "Tell us why you are rejecting it.",
            field_errors={"rejectionReason": ["Required when rejecting."]},
        )
    if document.status not in spec.decidable:
        raise Conflict(
            f"This {spec.title.lower()} is {document.status.lower()} and can no longer be approved or rejected.",
            code=Codes.ALREADY_DECIDED,
        )

    link.decision = decision
    link.decided_at = timezone.now()
    link.decided_by = decided_by[:200]
    link.decision_comments = comments
    link.rejection_reason = rejection_reason if decision == "Rejected" else None
    link.save()

    before = document.status
    after = spec.on_decision.get(decision)
    if after and after != before:
        document.status = after
        document.save(update_fields=["status", "updated_at"])

    if spec.key == "quotation":
        QuotationActivity.objects.create(
            quotation=document,
            event="accepted" if decision == "Approved" else "rejected",
            actor_label=decided_by,
            comment=rejection_reason or comments,
            ip=ip,
        )

    number = doc_number(spec, document)
    record_audit(
        client=document.client_id,
        actor=None,
        action="customer_approved" if decision == "Approved" else "customer_rejected",
        entity_type=spec.audit_entity,
        entity_id=document.id,
        entity_label=number,
        description=f"{decision} by {decided_by} through the approval link",
        from_value=before,
        to_value=document.status,
        comments=rejection_reason or comments,
        ip=ip,
    )
    notify(
        client=document.client_id,
        recipients=[link.created_by_id, document.created_by_id],
        type="sales.approval_decided",
        category="erp",
        title=f"{spec.title} {number} was {decision.lower()} by {decided_by}",
        body=rejection_reason or comments or f"{document.party_name or 'The customer'} responded to the approval link.",
        entity_type=spec.audit_entity,
        entity_id=document.id,
    )
    return link
