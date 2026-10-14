"""
Client portal reorder.

A customer logged in to the portal may reorder items they have bought before.
Two endpoints, both under ``/portal/client/``:

    GET  orderable-items/  -- the items this customer may reorder
    POST place-order/      -- a Draft sales order for some of them

Everything that matters is decided here, never by the client:

  - **Who the customer is**: ``request.user.party``, and only that. The
    email-matching widening in ``User.get_customer_party_ids`` (used by the
    read-only project tracker) is deliberately not used for a write.
  - **What they may order**: an active item that appears on a line of one of
    *their* valid documents -- a sales order past Draft and not Cancelled, or a
    finalized, uncancelled sales invoice. Item-less custom lines and BOM-
    generated part lines are not reorderable.
  - **At what price**: the rate, discount and tax of the latest valid line
    for that item and customer. The item's current ``tax_pct`` wins over the
    historical one when it is set, because the rate in force is the one owed.
    Totals come from ``services.recalculate_document`` like every other order.

The order is created in the existing initial stage (Draft) and follows the
normal workflow from there: staff review it, and confirming it runs the
credit-limit check (``SalesOrderViewSet.perform_update``).
"""
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core import idempotency
from apps.core.audit import record_audit, request_ip, snapshot
from apps.core.exceptions import PermissionDenied, ValidationFailed
from apps.core.money import DEFAULT_TAX_PCT, round4
from apps.core.naming import camelize
from apps.core.numbering import allocate_number
from apps.core.pagination import envelope
from apps.core.permissions import HasModulePermission
from apps.core.throttling import PortalOrderThrottle, SafeUserRateThrottle
from apps.inventory import services as stock
from apps.masters.models import Item, Party

from . import services
from .models import (
    ORDER_SOURCE_CLIENT_PORTAL,
    SalesInvoiceLine,
    SalesOrder,
    SalesOrderLine,
)
from .serializers import PortalPlaceOrderSerializer

#: A document in one of these never counts as a purchase.
NON_PURCHASE_ORDER_STAGES = ("Draft", "Cancelled")
NON_PURCHASE_INVOICE_STATUSES = ("Draft", "Cancelled")

PORTAL_PERMISSION = "place_portal_order"


# ---------------------------------------------------------------------------
# Who
# ---------------------------------------------------------------------------
def portal_party(request, *, for_ordering=False):
    """The customer party the caller acts for, or 403."""
    user = request.user
    party = None
    if getattr(user, "party_id", None):
        party = Party.objects.filter(
            pk=user.party_id, client_id=request.client_id, deleted_at__isnull=True
        ).first()
    if party is None or not party.is_customer:
        raise PermissionDenied(
            "This login is not linked to a customer account.", code="NO_CUSTOMER_PARTY"
        )
    if party.status == "Inactive" or (for_ordering and party.status != "Active"):
        raise PermissionDenied(
            f"Your customer account is {party.status}. Please contact us to place an order.",
            code="PARTY_NOT_ACTIVE",
        )
    return party


# ---------------------------------------------------------------------------
# What, and at what price
# ---------------------------------------------------------------------------
def _latest_lines(line_model, header, excluded_states, state_field, client_id, party_id, item_ids):
    """The newest valid line per item on one document type, as dicts."""
    queryset = line_model.objects.filter(
        client_id=client_id,
        deleted_at__isnull=True,
        item__isnull=False,
        parent_line__isnull=True,
        is_bom_generated=False,
        qty__gt=0,
        **{
            f"{header}__party_id": party_id,
            f"{header}__deleted_at__isnull": True,
            f"{header}__cancelled_at__isnull": True,
        },
    ).exclude(**{f"{header}__{state_field}__in": excluded_states})
    if item_ids is not None:
        queryset = queryset.filter(item_id__in=item_ids)
    return (
        queryset.order_by(
            "item_id", f"-{header}__doc_date", f"-{header}__created_at", "-created_at"
        )
        .distinct("item_id")
        .values(
            "item_id", "rate", "discount_pct", "tax_pct", "created_at",
            doc_date=F(f"{header}__doc_date"),
            header_created_at=F(f"{header}__created_at"),
        )
    )


def purchase_history(client_id, party_id, item_ids=None):
    """``{item_id: latest valid line}`` across sales orders and invoices.

    An invoice raised from an order repeats that order's line; whichever is
    newer wins, which is the invoice when the two differ (the billed rate).
    """
    latest = {}
    sources = (
        (SalesOrderLine, "sales_order", NON_PURCHASE_ORDER_STAGES, "stage"),
        (SalesInvoiceLine, "sales_invoice", NON_PURCHASE_INVOICE_STATUSES, "status"),
    )
    for line_model, header, excluded, state_field in sources:
        for row in _latest_lines(
            line_model, header, excluded, state_field, client_id, party_id, item_ids
        ):
            key = (row["doc_date"], row["header_created_at"], row["created_at"])
            current = latest.get(row["item_id"])
            if current is None or key >= current["_key"]:
                latest[row["item_id"]] = {**row, "_key": key}
    return latest


def _tax_rate(item, history_row):
    if item.tax_pct is not None:
        return item.tax_pct
    if history_row and history_row.get("tax_pct") is not None:
        return history_row["tax_pct"]
    return DEFAULT_TAX_PCT


def orderable_items(client_id, party):
    history = purchase_history(client_id, party.id)
    if not history:
        return []
    items = list(
        Item.objects.filter(
            client_id=client_id,
            pk__in=list(history),
            deleted_at__isnull=True,
            lifecycle_status="Active",
        ).order_by("name", "sku")
    )
    stock.annotate_items_with_stock(client_id, [item for item in items if item.holds_stock])

    rows = []
    for item in items:
        last = history[item.id]
        rows.append(
            {
                "id": str(item.id),
                "name": item.name,
                "itemCode": item.sku,
                "uom": item.uom,
                "taxRate": _tax_rate(item, last),
                "lastOrderedRate": last["rate"],
                "lastDiscountPct": last["discount_pct"],
                "lastOrderedDate": last["doc_date"],
                # None for a service item, or when stock has not been derived.
                "availableStock": getattr(item, "available_qty", None)
                if item.holds_stock
                else None,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Placing the order
# ---------------------------------------------------------------------------
def _unit_matches(item, uom):
    wanted = (uom or "").strip().lower()
    if not wanted:
        return True
    return wanted in {(item.uom or "").strip().lower(), (item.sales_unit or "").strip().lower()}


def _line_description(item, note):
    note = (note or "").strip()
    if not note:
        return None  # freeze_item_snapshot copies the item's own text
    base = (item.description or "").strip()
    return f"{base}\nCustomer note: {note}" if base else f"Customer note: {note}"


def place_order(request, party, data):
    """Validate every line against the customer's history, then create the
    order in one transaction. Any ineligible line rejects the whole request."""
    client_id = request.client_id
    user = request.user
    lines = data["lines"]
    item_ids = [line["item_id"] for line in lines]

    history = purchase_history(client_id, party.id, item_ids)
    items = {
        item.id: item
        for item in Item.objects.filter(
            client_id=client_id, pk__in=item_ids, deleted_at__isnull=True
        ).select_related("category__item_type", "item_type", "grade")
    }

    errors = {}
    for index, line in enumerate(lines):
        item = items.get(line["item_id"])
        # One message whether the id is unknown, another tenant's, never
        # bought or archived -- the portal must not reveal which.
        if item is None or item.id not in history or item.lifecycle_status != "Active":
            errors[f"lines[{index}].itemId"] = ["This item is not available to reorder."]
        elif not _unit_matches(item, line.get("uom")):
            errors[f"lines[{index}].uom"] = [f"This item is sold in {item.uom}."]
    if errors:
        raise ValidationFailed(
            "Some items in this order cannot be ordered.",
            code="ITEM_NOT_ORDERABLE",
            field_errors=errors,
        )

    today = timezone.localdate()
    delivery_date = data.get("delivery_date")
    if delivery_date is not None and delivery_date < today:
        raise ValidationFailed(
            "The delivery date cannot be in the past.",
            field_errors={"deliveryDate": ["Pick today or a later date."]},
        )

    with transaction.atomic():
        order = SalesOrder(
            client_id=client_id,
            party=party,
            doc_date=today,
            stage="Draft",
            delivery_date=delivery_date,
            order_source=ORDER_SOURCE_CLIENT_PORTAL,
            is_portal_order=True,
            portal_client_notes=(data.get("notes") or "").strip() or None,
            created_by=user,
            updated_by=user,
        )
        order.freeze_party_snapshot()
        order.order_number = allocate_number(user.client, "SO", today)
        order.save()

        rows = []
        for line_no, line in enumerate(lines, start=1):
            item = items[line["item_id"]]
            last = history[item.id]
            row = SalesOrderLine(
                client_id=client_id,
                sales_order=order,
                line_no=line_no,
                item=item,
                uom=item.uom,
                description=_line_description(item, line.get("notes")),
                qty=round4(line["quantity"]),
                rate=last["rate"],
                discount_pct=last["discount_pct"] or 0,
                tax_pct=_tax_rate(item, last),
                created_by=user,
                updated_by=user,
            )
            row.freeze_item_snapshot()
            row.save()
            rows.append(row)

        services.recalculate_document(order, lines=rows)

        record_audit(
            client=client_id,
            actor=user,
            action="create",
            entity_type="SalesOrder",
            entity_id=order.id,
            entity_label=order.order_number,
            description=f"Placed from the client portal by {user.email}",
            after=snapshot(order),
            ip=request_ip(request),
        )
    return order, rows


def order_confirmation(order, rows):
    return {
        "id": str(order.id),
        "orderNumber": order.order_number,
        "stage": order.stage,
        "orderSource": order.order_source,
        "isPortalOrder": order.is_portal_order,
        "date": order.doc_date,
        "deliveryDate": order.delivery_date,
        "placeOfSupply": order.place_of_supply or "",
        "totals": {
            "subtotal": order.subtotal,
            "totalDiscount": order.total_discount,
            "taxableValue": order.taxable_value,
            "cgst": order.cgst,
            "sgst": order.sgst,
            "igst": order.igst,
            "totalTax": order.total_tax,
            "roundOff": order.round_off,
            "total": order.total,
        },
        "lines": [
            {
                "lineNo": row.line_no,
                "itemId": str(row.item_id),
                "itemCode": row.sku,
                "name": row.item_name,
                "uom": row.uom,
                "quantity": row.qty,
                "rate": row.rate,
                "discountPct": row.discount_pct,
                "taxRate": row.tax_pct,
                "taxAmount": row.tax_amount,
                "lineTotal": row.line_total,
            }
            for row in rows
        ],
    }


def portal_field_errors(errors):
    """Serializer errors as ``{"lines[1].quantity": [...]}``.

    The shared handler stringifies a list of per-line error dicts; the portal
    form binds each message to its row, so the paths are built here, in the
    same shape ``place_order`` uses for its own line errors.
    """
    flat = {}
    for key, value in errors.items():
        name = camelize(key)
        if isinstance(value, list) and value and all(isinstance(row, dict) for row in value):
            for index, row in enumerate(value):
                for field, messages in row.items():
                    flat[f"{name}[{index}].{camelize(field)}"] = [str(m) for m in messages]
        elif isinstance(value, dict):
            for field, messages in value.items():
                path = name if field == "non_field_errors" else f"{name}.{camelize(field)}"
                flat[path] = [str(m) for m in messages]
        else:
            flat[name] = [str(m) for m in value]
    return flat


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------
class PortalOrderableItemsView(APIView):
    """``GET /portal/client/orderable-items/``."""

    permission_classes = [HasModulePermission]
    required_permissions = [PORTAL_PERMISSION]

    def get(self, request):
        party = portal_party(request)
        return Response(envelope(orderable_items(request.client_id, party)))


class PortalPlaceOrderView(APIView):
    """``POST /portal/client/place-order/`` -- honours ``Idempotency-Key``."""

    permission_classes = [HasModulePermission]
    required_permissions = [PORTAL_PERMISSION]
    throttle_classes = [SafeUserRateThrottle, PortalOrderThrottle]

    def post(self, request):
        party = portal_party(request, for_ordering=True)
        serializer = PortalPlaceOrderSerializer(data=request.data)
        if not serializer.is_valid():
            raise ValidationFailed(field_errors=portal_field_errors(serializer.errors))

        # Keyed per user as well as per path, so one customer can never be
        # handed another's stored response.
        try:
            record = idempotency.begin(
                request, request.client_id, f"{request.path}#{request.user.pk}", request.data
            )
        except idempotency.Replay as replay:
            response = Response(replay.body, status=replay.status)
            response["Idempotency-Replayed"] = "true"
            return response

        try:
            order, rows = place_order(request, party, serializer.validated_data)
            response = Response(order_confirmation(order, rows), status=status.HTTP_201_CREATED)
        except Exception:
            idempotency.fail(record)
            raise
        return idempotency.finish(record, response, order)
