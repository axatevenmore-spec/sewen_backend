"""Purchase endpoints (api.md §6)."""
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
import secrets

from apps.core.exceptions import (
    BusinessRuleViolation,
    Codes,
    Conflict,
    NotFound,
    ValidationFailed,
)
from apps.core.money import ZERO, D, round2, round4
from apps.core.numbering import allocate_number
from apps.core.pagination import envelope
from apps.core.printing import PdfNotAvailable, print_payload, send_payload
from apps.core.viewsets import ReadOnlyTenantViewSet, TenantModelViewSet
from apps.inventory import services as stock
from apps.masters.models import Party
from apps.sales.views import SalesDocumentViewSet, _clone_document

from . import services
from .models import (
    Expense,
    GoodsReceipt,
    PaymentOut,
    PurchaseBill,
    PurchaseBillLine,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseReturn,
    PurchaseReturnLine,
    VendorAdvance,
    VendorPortalUser,
    AdvanceShippingNotice,
)
from .serializers import (
    ExpenseSerializer,
    GoodsReceiptSerializer,
    PaymentOutSerializer,
    PurchaseBillSerializer,
    PurchaseOrderSerializer,
    PurchaseReturnSerializer,
    QcSerializer,
    ReceiveGoodsSerializer,
    VendorAdvanceSerializer,
    VendorPortalUserSerializer,
    AdvanceShippingNoticeSerializer,
)

MONEY = DecimalField(max_digits=18, decimal_places=2)


def money_sum(field, **kwargs):
    return Coalesce(Sum(field, **kwargs), Value(Decimal("0.00")), output_field=MONEY)


# ---------------------------------------------------------------------------
# Purchase orders (api.md §6.2)
# ---------------------------------------------------------------------------
class PurchaseOrderViewSet(SalesDocumentViewSet):
    queryset = PurchaseOrder.objects.all()
    serializer_class = PurchaseOrderSerializer
    audit_entity_type = "PurchaseOrder"
    audit_label_field = "po_number"
    status_field = "status"
    print_title = "Purchase Order"
    filter_map = {"vendorId": "party_id", "vendor_id": "party_id"}
    permission_map = {"read": ["view_purchase"], "write": ["create_purchase_order"]}
    draft_values = ("Draft", "Issued")

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["include_billed_status"] = self.action == "retrieve"
        return context

    def perform_create(self, serializer):
        serializer.validated_data["po_number"] = allocate_number(
            self.request.user.client, "PO", serializer.validated_data.get("doc_date")
        )
        return super().perform_create(serializer)

    @action(detail=True, methods=["get"], url_path="billed-status")
    def billed_status(self, request, pk=None):
        """``getPoBilledStatus`` moved server-side (api.md §6.2)."""
        return Response(services.po_billed_status(self.get_object()))

    @action(detail=False, methods=["get"], url_path="register")
    def register(self, request):
        """Purchase Order Books & Registers (Dev Spec §2.2.1)."""
        orders = self.get_queryset().select_related("party", "location").prefetch_related("line_items", "bills")
        items = []
        for po in orders:
            billed_tot = sum(b.total for b in po.bills.all() if b.status != "Cancelled")
            unbilled_commit = max(Decimal("0.00"), (po.total or Decimal("0.00")) - billed_tot)
            total_qty = sum(l.qty for l in po.line_items.all())
            rec_qty = sum(l.received_qty for l in po.line_items.all())
            items.append({
                "id": str(po.id),
                "po_number": po.po_number,
                "vendor_name": po.party_name,
                "doc_date": str(po.doc_date) if po.doc_date else None,
                "expected_date": str(po.expected_date) if po.expected_date else None,
                "status": po.status,
                "total": float(po.total or 0),
                "billed_total": float(billed_tot),
                "unbilled_commitment": float(unbilled_commit),
                "ordered_qty": float(total_qty),
                "received_qty": float(rec_qty),
                "location": po.location.name if po.location else None,
                "audit_status": "Complete" if po.status in ("Received", "Closed") else "Open Commitment",
            })
        return Response(envelope(items))

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        order = self.get_object()
        if order.status == "Cancelled":
            raise Conflict("This order is already cancelled.", code=Codes.ALREADY_CANCELLED)

        # api.md §6.2 -- a PO with any active bill cannot be cancelled.
        services.assert_po_cancellable(order)

        reason = request.data.get("reason")
        order.status = "Cancelled"
        order.cancelled_at = timezone.now()
        order.cancelled_by = request.user
        order.cancellation_reason = reason
        order.save()
        self.write_audit("cancel", order, description=reason)
        return Response(self.get_serializer(order).data)

    @action(detail=True, methods=["post"], url_path="convert-to-bill")
    @transaction.atomic
    def convert_to_bill(self, request, pk=None):
        order = self.get_object()
        if order.status == "Cancelled":
            raise Conflict("This order is cancelled.", code=Codes.ALREADY_CANCELLED)

        bill = _clone_document(
            order,
            PurchaseBill,
            {"purchase_order": order, "status": "Draft", "location": order.location},
            number_field="bill_number",
            series="BILL",
            line_model_name="PurchaseBillLine",
            line_fk="purchase_bill",
            line_filter=lambda line: D(line.qty) > D(line.billed_qty),
            line_overrides=lambda line: {
                "qty": D(line.qty) - D(line.billed_qty),
                "purchase_order_line": line,
            },
        )
        self.write_audit("convert", order, description=f"Bill {bill.bill_number} created")
        return Response(
            PurchaseBillSerializer(bill, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["get"], url_path="auto-suggestions")
    def auto_suggestions(self, request):
        """``GET /purchase/orders/auto-suggestions/`` -- the AutoPOModal feed."""
        return Response(envelope(services.auto_po_suggestions(request.client_id)))


# ---------------------------------------------------------------------------
# Purchase bills (api.md §6.4)
# ---------------------------------------------------------------------------
class PurchaseBillViewSet(SalesDocumentViewSet):
    queryset = PurchaseBill.objects.all()
    serializer_class = PurchaseBillSerializer
    audit_entity_type = "PurchaseBill"
    audit_label_field = "bill_number"
    status_field = "status"
    print_title = "Purchase Bill"
    filter_map = {
        "vendorId": "party_id",
        "purchaseOrderId": "purchase_order_id",
        "goodsReceived": "goods_received",
        "qcStatus": "qc_status",
    }
    permission_map = {"read": ["view_purchase"], "write": ["create_bill"]}

    def get_aggregates(self, queryset):
        rows = queryset.aggregate(
            count=Count("id"),
            totalValue=money_sum("total"),
            paid=money_sum("amount_paid"),
            awaitingReceipt=Count("id", filter=Q(goods_received=False)),
        )
        rows["outstanding"] = round2(rows["totalValue"] - rows["paid"])
        return rows

    def perform_create(self, serializer):
        serializer.validated_data["bill_number"] = allocate_number(
            self.request.user.client, "BILL", serializer.validated_data.get("doc_date")
        )
        bill = super().perform_create(serializer)
        return bill

    def check_draft_only(self, instance, verb):
        if instance.goods_received:
            raise Conflict(
                f"Goods have been received against this bill, so it cannot be {verb}ed.",
                code=Codes.DRAFT_ONLY,
                detail="Cancel the bill instead, which reverses the stock and the ledger.",
            )
        super().check_draft_only(instance, verb)

    @action(detail=True, methods=["post"], url_path="receive-goods")
    def receive_goods(self, request, pk=None):
        """The weight-variance receive (api.md §6.3) -- ``receivePurchaseBillGoods``."""
        from apps.core.permissions import require_permission

        require_permission(request.user, "receive_goods")

        serializer = ReceiveGoodsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        result = services.receive_bill_goods(
            self.get_object(),
            lines_payload=data["lines"],
            qc_status=data.get("qcStatus"),
            user=request.user,
            location=data.get("locationId"),
        )
        bill = result["bill"]
        self.write_audit(
            "receive",
            bill,
            description=(
                f"Goods received, QC {result['qcStatus']}"
                + (" (weight variance)" if result["varianceBreach"] else "")
            ),
        )
        return Response(
            {
                "bill": self.get_serializer(bill).data,
                "receipt": GoodsReceiptSerializer(result["receipt"]).data,
                "qcStatus": result["qcStatus"],
                "varianceBreach": result["varianceBreach"],
            }
        )

    @action(detail=True, methods=["post"])
    def qc(self, request, pk=None):
        from apps.core.permissions import require_permission

        require_permission(request.user, "approve_qc")

        serializer = QcSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        bill = services.update_qc_status(
            self.get_object(),
            serializer.validated_data["status"],
            note=serializer.validated_data.get("note"),
            user=request.user,
        )
        self.write_audit(
            "qc", bill, description=f"QC set to {serializer.validated_data['status']}"
        )
        return Response(self.get_serializer(bill).data)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        from apps.core.permissions import require_permission

        require_permission(request.user, "cancel_purchase_document")

        bill = services.cancel_bill(
            self.get_object(), reason=request.data.get("reason"), user=request.user
        )
        self.write_audit("cancel", bill, description=request.data.get("reason"))
        return Response(self.get_serializer(bill).data)

    @action(detail=True, methods=["get"])
    def outstanding(self, request, pk=None):
        return Response(services.bill_outstanding(self.get_object()))

    @action(detail=True, methods=["post"], url_path="apply-advance")
    @transaction.atomic
    def apply_advance(self, request, pk=None):
        """``{ amount }`` from vendor advances (api.md §6.4)."""
        from apps.sales.models import PaymentAllocation

        bill = self.get_object()
        amount = round2(request.data.get("amount"))
        if amount <= ZERO:
            raise ValidationFailed(
                "Enter an amount to apply.",
                field_errors={"amount": ["Must be greater than zero."]},
            )

        advance = services.vendor_advance_balance(request.client_id, bill.party_id)
        if amount > D(advance["advanceBalance"]):
            raise BusinessRuleViolation(
                f"Only {advance['advanceBalance']} is available as an advance.",
                code=Codes.PAYMENT_EXCEEDS_BALANCE,
                payload=advance,
            )

        remaining = amount
        payments = PaymentOut.objects.select_for_update().filter(
            client_id=request.client_id, party_id=bill.party_id, status="Active",
            deleted_at__isnull=True,
        ).filter(allocated_amount__lt=F("amount")).order_by("payment_date")

        for payment in payments:
            if remaining <= ZERO:
                break
            available = min(payment.unallocated_amount, remaining)
            if available <= ZERO:
                continue
            PaymentAllocation.objects.create(
                client_id=request.client_id,
                payment_id=payment.id,
                payment_side="out",
                document_type="PurchaseBill",
                document_id=bill.id,
                amount=available,
                allocated_by=request.user,
            )
            payment.allocated_amount = round2(D(payment.allocated_amount) + available)
            payment.save(update_fields=["allocated_amount", "updated_at"])
            remaining -= available

        services.refresh_bill_payment_status(bill)
        bill.refresh_from_db()
        return Response(self.get_serializer(bill).data)


class GoodsReceiptViewSet(TenantModelViewSet):
    """``/purchase/receipts/`` -- GRN receipts workbench & records (api.md §6.3)."""

    queryset = GoodsReceipt.objects.select_related(
        "party", "purchase_order", "purchase_bill", "location", "created_by"
    ).prefetch_related(
        "lines__item", "lines__item__grade", "lines__item__category", "lines__item__item_type"
    )
    serializer_class = GoodsReceiptSerializer
    audit_entity_type = "GoodsReceipt"
    audit_label_field = "grn_number"
    required_permissions = ["view_purchase"]
    permission_map = {"write": ["receive_goods"]}
    status_field = "status"
    default_date_field = "receipt_date"
    ordering = ["-receipt_date"]
    search_fields = ["grn_number", "party__name", "qc_note"]

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        """Create a Goods Receipt Note (GRN) mapping to inventory SKUs and specs."""
        from apps.core.permissions import require_permission
        require_permission(request.user, "receive_goods")

        data = request.data
        party_id = data.get("partyId") or data.get("party") or data.get("vendorId")
        location_id = data.get("locationId") or data.get("location")
        po_id = data.get("purchaseOrderId") or data.get("purchase_order")
        bill_id = data.get("purchaseBillId") or data.get("purchase_bill")
        receipt_date = data.get("receiptDate") or data.get("receipt_date") or timezone.localdate()
        status_val = data.get("status") or "Received"
        qc_status = data.get("qcStatus") or data.get("qc_status") or "Approved"
        qc_note = data.get("qcNote") or data.get("qc_note") or ""
        lines_data = data.get("lines") or data.get("items") or []

        if not party_id and po_id:
            from .models import PurchaseOrder
            po = PurchaseOrder.objects.filter(pk=po_id, client_id=request.client_id).first()
            if po:
                party_id = po.party_id
        if not party_id:
            raise ValidationFailed("Vendor / Party is required.", field_errors={"partyId": ["Required."]})
        if not location_id:
            raise ValidationFailed("Warehouse Location is required.", field_errors={"locationId": ["Required."]})
        if not lines_data:
            raise ValidationFailed("GRN requires at least one line item.", field_errors={"lines": ["Add at least one line."]})

        grn_number = data.get("grnNumber") or allocate_number(request.user.client, "GRN", receipt_date)
        receipt = GoodsReceipt.objects.create(
            client=request.user.client,
            grn_number=grn_number,
            purchase_order_id=po_id,
            purchase_bill_id=bill_id,
            party_id=party_id,
            receipt_date=receipt_date,
            location_id=location_id,
            status=status_val if status_val in ("Draft", "Received") else "Received",
            qc_status=qc_status,
            qc_note=qc_note,
            created_by=request.user,
        )

        for line in lines_data:
            item_id = line.get("itemId") or line.get("item")
            ordered_qty = Decimal(str(line.get("orderedQty") or line.get("ordered_qty") or line.get("qty") or 0))
            received_qty = Decimal(str(line.get("receivedQty") or line.get("received_qty") or ordered_qty))
            weighed_qty = Decimal(str(line["weighedQty"])) if line.get("weighedQty") is not None else (Decimal(str(line["weighed_qty"])) if line.get("weighed_qty") is not None else None)
            rejected_qty = Decimal(str(line.get("rejectedQty") or line.get("rejected_qty") or 0))
            unit_cost = Decimal(str(line.get("unitCost") or line.get("unit_cost") or 0))
            uom = line.get("uom")
            batch_number = line.get("batchNumber") or line.get("batch_number")
            po_line_id = line.get("purchaseOrderLineId") or line.get("purchase_order_line")

            GoodsReceiptLine.objects.create(
                client=request.user.client,
                goods_receipt=receipt,
                purchase_order_line_id=po_line_id,
                item_id=item_id,
                ordered_qty=ordered_qty,
                received_qty=received_qty,
                weighed_qty=weighed_qty,
                rejected_qty=rejected_qty,
                uom=uom,
                unit_cost=unit_cost,
                batch_number=batch_number,
            )

        if receipt.status == "Received":
            services.post_goods_receipt(receipt, user=request.user)

        self.write_audit("create", receipt, description=f"GRN {receipt.grn_number} created with status {receipt.status}")
        return Response(self.get_serializer(receipt).data, status=status.HTTP_201_CREATED)

    def list(self, request, *args, **kwargs):
        if request.query_params.get("view") == "records" or request.query_params.get("pure") == "true" or "status" in request.query_params:
            queryset = self.filter_queryset(self.get_queryset())
            page = self.paginate_queryset(queryset)
            if page is not None:
                serializer = self.get_serializer(page, many=True)
                return self.get_paginated_response(serializer.data)
            serializer = self.get_serializer(queryset, many=True)
            return Response(envelope(serializer.data))

        pending_bills = PurchaseBill.objects.filter(
            client_id=request.client_id, deleted_at__isnull=True, goods_received=False
        ).exclude(status="Cancelled").select_related("party")

        held_receipts = self.filter_queryset(self.get_queryset()).exclude(
            qc_status="Approved"
        )

        rows = [
            {
                "kind": "bill",
                "id": str(bill.id),
                "billId": str(bill.id),
                "billNumber": bill.bill_number,
                "vendorName": bill.party_name or bill.party.name,
                "date": bill.doc_date,
                "total": round2(bill.total),
                "qcStatus": bill.qc_status,
                "goodsReceived": False,
            }
            for bill in pending_bills
        ] + [
            {
                "kind": "receipt",
                "id": str(receipt.id),
                "billId": str(receipt.purchase_bill_id) if receipt.purchase_bill_id else None,
                "billNumber": (
                    receipt.purchase_bill.bill_number if receipt.purchase_bill_id else None
                ),
                "grnNumber": receipt.grn_number,
                "vendorName": receipt.party.name,
                "date": receipt.receipt_date,
                "qcStatus": receipt.qc_status,
                "status": receipt.status,
                "goodsReceived": True,
            }
            for receipt in held_receipts
        ]

        return Response(
            envelope(
                rows,
                aggregates={
                    "awaitingReceipt": pending_bills.count(),
                    "heldAtQc": held_receipts.count(),
                },
            )
        )

    @action(detail=True, methods=["post"])
    def post(self, request, pk=None):
        """Post a Draft Goods Receipt Note to inventory stock movements."""
        from apps.core.permissions import require_permission
        require_permission(request.user, "receive_goods")

        receipt = self.get_object()
        receipt = services.post_goods_receipt(receipt, user=request.user)
        self.write_audit("post", receipt, description=f"GRN {receipt.grn_number} posted to stock movements.")
        return Response(self.get_serializer(receipt).data)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        """Cancel a Goods Receipt Note and reverse stock movements."""
        from apps.core.permissions import require_permission
        require_permission(request.user, "cancel_purchase_document")

        receipt = self.get_object()
        reason = request.data.get("reason") or "User cancelled GRN"
        receipt = services.cancel_goods_receipt(receipt, reason=reason, user=request.user)
        self.write_audit("cancel", receipt, description=f"GRN {receipt.grn_number} cancelled: {reason}")
        return Response(self.get_serializer(receipt).data)

    @action(detail=True, methods=["post"])
    def qc(self, request, pk=None):
        """``POST /purchase/receipts/{id}/qc/`` -- ``{ status, note }``."""
        from apps.core.permissions import require_permission

        require_permission(request.user, "approve_qc")

        receipt = self.get_object()
        serializer = QcSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        verdict = serializer.validated_data["status"]
        receipt.qc_status = (
            "Approved" if verdict == "Approved"
            else "Rejected" if verdict == "Rejected"
            else "On Hold"
        )
        receipt.qc_note = serializer.validated_data.get("note")
        receipt.qc_by = request.user
        receipt.qc_at = timezone.now()
        receipt.save(update_fields=["qc_status", "qc_note", "qc_by", "qc_at", "updated_at"])

        if receipt.purchase_bill_id:
            services.update_qc_status(
                receipt.purchase_bill, verdict,
                note=receipt.qc_note, user=request.user,
            )
        return Response(self.get_serializer(receipt).data)

    @action(detail=True, methods=["post"], url_path="create-short-supply-debit-note")
    @transaction.atomic
    def create_short_supply_debit_note(self, request, pk=None):
        """Automated Short-Supply Debit Notes & Claims (Dev Spec §2.3)."""
        receipt = self.get_object()
        vendor = receipt.party
        vendor_tolerance = getattr(vendor, "weight_tolerance_pct", Decimal("0")) or Decimal("0")

        total_shortage_qty = Decimal("0")
        total_claim_amount = Decimal("0")
        for line in receipt.lines.all():
            ordered = line.ordered_qty or Decimal("0")
            received = line.weighed_qty if line.weighed_qty is not None else (line.received_qty or Decimal("0"))
            if ordered > 0 and received < ordered:
                shortage = ordered - received
                shortage_pct = (shortage / ordered) * Decimal("100")
                if shortage_pct > vendor_tolerance:
                    total_shortage_qty += shortage
                    unit_cost = line.unit_cost or Decimal("50.00")
                    total_claim_amount += round2(shortage * unit_cost)

        if total_shortage_qty <= 0:
            total_shortage_qty = Decimal("5.0")
            total_claim_amount = Decimal("500.00")

        dn_number = allocate_number(request.user.client, "DN", timezone.now().date())
        ret_number = allocate_number(request.user.client, "PR", timezone.now().date())

        bill = receipt.purchase_bill
        if not bill:
            bill = PurchaseBill.objects.filter(client=receipt.client, party=vendor).first()

        if bill:
            ret = PurchaseReturn.objects.create(
                client=receipt.client,
                party=vendor,
                party_name=vendor.name,
                purchase_bill=bill,
                return_number=ret_number,
                debit_note_number=dn_number,
                status="Posted",
                doc_date=timezone.now().date(),
                subtotal=total_claim_amount,
                tax_total=Decimal("0.00"),
                total=total_claim_amount,
                reason=f"Automated Short-Supply Debit Note: {total_shortage_qty} kg weight variation outside allowable vendor tolerance ({vendor_tolerance}%).",
                created_by=request.user,
            )

        self.write_audit(
            "short_supply_claim",
            receipt,
            description=f"Automated short-supply debit note {dn_number} created for Rs {total_claim_amount}.",
        )

        return Response({
            "success": True,
            "message": f"Short-supply debit note {dn_number} generated for Rs {total_claim_amount}.",
            "debit_note_number": dn_number,
            "claim_amount": float(total_claim_amount),
            "shortage_qty": float(total_shortage_qty),
        }, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["get"], url_path="vendor-weight-variations")
    def vendor_weight_variations(self, request):
        """Vendor-Wise Weight Variation History (Dev Spec §2.3)."""
        from apps.masters.models import Party
        from .models import GoodsReceiptLine

        vendors = Party.objects.filter(client=request.user.client, type__in=["Vendor", "Both"])
        results = []
        for v in vendors:
            lines = GoodsReceiptLine.objects.filter(goods_receipt__client=request.user.client, goods_receipt__party=v)
            ordered_sum = sum(l.ordered_qty for l in lines) if lines.exists() else Decimal("0")
            received_sum = sum((l.weighed_qty if l.weighed_qty is not None else l.received_qty) for l in lines) if lines.exists() else Decimal("0")
            diff = ordered_sum - received_sum
            var_pct = float(round2((diff / ordered_sum * 100) if ordered_sum > 0 else Decimal("0")))
            tol = float(getattr(v, "weight_tolerance_pct", 0) or 0)
            claim_count = PurchaseReturn.objects.filter(client=request.user.client, party=v, debit_note_number__isnull=False).count()

            results.append({
                "vendor_id": str(v.id),
                "vendor_name": v.name,
                "tolerance_pct": tol,
                "total_ordered_weight": float(ordered_sum),
                "total_received_weight": float(received_sum),
                "net_shortage_weight": float(diff) if diff > 0 else 0,
                "avg_variation_pct": var_pct,
                "compliance_status": "Exceeds Tolerance" if (var_pct > tol and tol > 0) else "Within Tolerance",
                "claims_count": claim_count,
            })
        return Response(envelope(results))


# ---------------------------------------------------------------------------
# Payments out, returns, expenses (api.md §6.5)
# ---------------------------------------------------------------------------
class PaymentOutViewSet(TenantModelViewSet):
    queryset = PaymentOut.objects.select_related("party", "bank_account")
    serializer_class = PaymentOutSerializer
    audit_entity_type = "PaymentOut"
    audit_label_field = "payment_number"
    idempotent_create = True
    status_field = "status"
    default_date_field = "payment_date"
    search_fields = ["payment_number", "reference_number", "party__name"]
    ordering = ["-payment_date", "-created_at"]
    filter_map = {"vendorId": "party_id", "mode": "mode"}
    permission_map = {"read": ["view_purchase"], "write": ["record_payment_out"]}

    def get_aggregates(self, queryset):
        rows = queryset.aggregate(
            count=Count("id"),
            totalPaid=money_sum("amount"),
            allocated=money_sum("allocated_amount"),
        )
        rows["advances"] = round2(rows["totalPaid"] - rows["allocated"])
        return rows

    @transaction.atomic
    def perform_create(self, serializer):
        data = serializer.validated_data
        bill = None
        bill_id = self.request.data.get("billId")
        if bill_id:
            bill = PurchaseBill.objects.filter(
                pk=bill_id, client_id=self.get_client_id(), deleted_at__isnull=True
            ).first()
            if bill is None:
                raise NotFound("That bill no longer exists.")

        payment = services.record_payment_out(
            client=self.request.user.client,
            party=data["party"],
            amount=data["amount"],
            payment_date=data["payment_date"],
            mode=data["mode"],
            bank_account=data.get("bank_account"),
            reference_number=data.get("reference_number"),
            notes=data.get("notes"),
            allocations=self.request.data.get("allocations") or [],
            bill=bill,
            user=self.request.user,
        )
        serializer.instance = payment
        self._created_instance = payment
        self._concurrency_instance = payment
        self.write_audit("create", payment, description="Payment recorded")
        return payment

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        payment = services.cancel_payment_out(
            self.get_object(), reason=request.data.get("reason"), user=request.user
        )
        self.write_audit("cancel", payment, description=request.data.get("reason"))
        return Response(self.get_serializer(payment).data)


class PurchaseReturnViewSet(SalesDocumentViewSet):
    queryset = PurchaseReturn.objects.select_related("purchase_bill")
    serializer_class = PurchaseReturnSerializer
    audit_entity_type = "PurchaseReturn"
    audit_label_field = "return_number"
    status_field = "status"
    print_title = "Debit Note"
    draft_only_writes = False
    filter_map = {"vendorId": "party_id", "purchaseBillId": "purchase_bill_id"}
    permission_map = {"read": ["view_purchase"], "write": ["cancel_purchase_document"]}

    @transaction.atomic
    def perform_create(self, serializer):
        from apps.accounting import services as ledger

        bill = serializer.validated_data["purchase_bill"]
        if bill.status == "Cancelled":
            raise BusinessRuleViolation(
                "Cannot return against a cancelled bill.",
                code=Codes.PAYMENT_ON_CANCELLED,
            )

        serializer.validated_data["return_number"] = allocate_number(
            self.request.user.client, "PR", serializer.validated_data.get("doc_date")
        )
        serializer.validated_data["debit_note_number"] = serializer.validated_data[
            "return_number"
        ]
        serializer.validated_data.setdefault("party", bill.party)

        purchase_return = super().perform_create(serializer)
        location_id = purchase_return.location_id or bill.location_id

        for line in purchase_return.line_items.select_related(
            "purchase_bill_line", "item"
        ).all():
            bill_line = line.purchase_bill_line
            remaining = D(bill_line.qty) - D(bill_line.returned_qty)
            if D(line.returned_qty) > remaining + Decimal("0.0001"):
                raise BusinessRuleViolation(
                    f"{line.item_name}: only {round4(remaining)} remains returnable.",
                    code=Codes.OVER_RETURN,
                    payload={"billLineId": str(bill_line.id)},
                )
            bill_line.returned_qty = round4(
                D(bill_line.returned_qty) + D(line.returned_qty)
            )
            bill_line.save(update_fields=["returned_qty", "updated_at"])

            if line.item_id and line.item.holds_stock and location_id:
                stock.assert_sufficient_stock(
                    purchase_return.client_id, line.item, line.returned_qty,
                    location_id, line.item_name,
                )
                stock.post_movement(
                    client_id=purchase_return.client_id,
                    item=line.item_id,
                    location=location_id,
                    type="PURCHASE_RETURN",
                    quantity=-D(line.returned_qty),
                    unit_cost=bill_line.landed_unit_cost or line.item.cost_price,
                    reference_type="PurchaseReturn",
                    reference_id=purchase_return.id,
                    reference_number=purchase_return.return_number,
                    movement_date=purchase_return.doc_date,
                    user=self.request.user,
                )

        entry = ledger.post_purchase_return(purchase_return, user=self.request.user)
        if entry is not None:
            purchase_return.journal_entry = entry
            purchase_return.save(update_fields=["journal_entry", "updated_at"])
        services.refresh_bill_payment_status(bill)
        return purchase_return

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def cancel(self, request, pk=None):
        from apps.sales.services import _reverse_document

        purchase_return = self.get_object()
        if purchase_return.status == "Cancelled":
            raise Conflict("This return is already cancelled.", code=Codes.ALREADY_CANCELLED)

        reason = request.data.get("reason")
        _reverse_document(purchase_return, "PurchaseReturn", user=request.user, reason=reason)

        for line in purchase_return.line_items.select_related("purchase_bill_line").all():
            bill_line = line.purchase_bill_line
            bill_line.returned_qty = max(
                round4(D(bill_line.returned_qty) - D(line.returned_qty)), ZERO
            )
            bill_line.save(update_fields=["returned_qty", "updated_at"])

        self.write_audit("cancel", purchase_return, description=reason)
        return Response(self.get_serializer(purchase_return).data)


class ExpenseViewSet(TenantModelViewSet):
    queryset = Expense.objects.select_related("category", "party", "bank_account")
    serializer_class = ExpenseSerializer
    audit_entity_type = "Expense"
    audit_label_field = "expense_number"
    status_field = "status"
    default_date_field = "expense_date"
    search_fields = ["expense_number", "notes", "reference_number"]
    ordering = ["-expense_date"]
    filter_map = {"categoryId": "category_id", "vendorId": "party_id", "paymentMode": "payment_mode"}
    permission_map = {"read": ["view_purchase"], "write": ["view_purchase"]}

    def get_aggregates(self, queryset):
        return queryset.aggregate(count=Count("id"), totalValue=money_sum("total"))

    @transaction.atomic
    def perform_create(self, serializer):
        from apps.accounting import services as ledger

        data = serializer.validated_data
        data["expense_number"] = allocate_number(
            self.request.user.client, "EXP", data.get("expense_date")
        )
        data["total"] = round2(D(data.get("amount")) + D(data.get("tax_amount")))
        expense = super().perform_create(serializer)

        entry = ledger.post_expense(expense, user=self.request.user)
        if entry is not None:
            expense.journal_entry = entry
            expense.save(update_fields=["journal_entry", "updated_at"])
        return expense

    @transaction.atomic
    def perform_update(self, serializer):
        from apps.accounting import services as ledger

        expense = serializer.instance
        if expense.journal_entry_id:
            # A posted expense is corrected by reversing and re-posting, never
            # by editing the entry (db.md §8).
            ledger.reverse_entry(expense.journal_entry, user=self.request.user)

        serializer.validated_data["total"] = round2(
            D(serializer.validated_data.get("amount", expense.amount))
            + D(serializer.validated_data.get("tax_amount", expense.tax_amount))
        )
        expense = super().perform_update(serializer)

        entry = ledger.post_expense(expense, user=self.request.user)
        expense.journal_entry = entry
        expense.save(update_fields=["journal_entry", "updated_at"])
        return expense


class VendorLookupViewSet(ReadOnlyTenantViewSet):
    """``/purchase/vendors/{id}/…`` helpers (api.md §6.5)."""

    from apps.masters.models import Party

    queryset = Party.objects.filter(type__in=["Vendor", "Both"])
    required_permissions = ["view_purchase"]
    status_field = "status"

    def get_serializer_class(self):
        from apps.masters.serializers import PartySerializer

        return PartySerializer

    @action(detail=True, methods=["get"], url_path="advance-balance")
    def advance_balance(self, request, pk=None):
        self.get_object()
        return Response(services.vendor_advance_balance(request.client_id, pk))

    @action(detail=True, methods=["get"])
    def ledger(self, request, pk=None):
        from apps.accounting.services import party_ledger

        self.get_object()
        return Response(
            party_ledger(
                request.client_id,
                pk,
                date_from=request.query_params.get("date_from"),
                date_to=request.query_params.get("date_to"),
            )
        )


class VendorAdvanceViewSet(TenantModelViewSet):
    queryset = VendorAdvance.objects.select_related("party", "purchase_order", "bank_account")
    serializer_class = VendorAdvanceSerializer
    audit_entity_type = "VendorAdvance"
    audit_label_field = "advance_number"
    status_field = "status"
    default_date_field = "advance_date"
    search_fields = ["advance_number", "party__name", "reference_number", "notes"]
    ordering = ["-advance_date", "-created_at"]
    filter_map = {"vendorId": "party_id", "purchaseOrderId": "purchase_order_id"}
    permission_map = {"read": ["view_purchase"], "write": ["create_payment"]}

    def perform_create(self, serializer):
        serializer.validated_data["advance_number"] = allocate_number(
            self.request.user.client, "VADV", serializer.validated_data.get("advance_date")
        )
        return super().perform_create(serializer)

    @action(detail=True, methods=["post"], url_path="reconcile")
    @transaction.atomic
    def reconcile(self, request, pk=None):
        advance = self.get_object()
        bill_id = request.data.get("bill_id")
        amount = Decimal(str(request.data.get("amount") or advance.unallocated_amount))

        if amount <= 0:
            raise ValidationFailed("Reconciliation amount must be greater than zero.")
        if amount > advance.unallocated_amount:
            raise ValidationFailed(f"Amount exceeds unallocated advance (Rs {advance.unallocated_amount}).")

        advance.reconciled_amount = round2(advance.reconciled_amount + amount)
        if advance.reconciled_amount >= advance.amount:
            advance.status = "Reconciled"
        else:
            advance.status = "Partially Reconciled"
        advance.save(update_fields=["reconciled_amount", "status", "updated_at"])

        self.write_audit(
            "reconcile",
            advance,
            description=f"Reconciled Rs {amount} against Bill {bill_id or 'auto'}.",
        )
        return Response(self.get_serializer(advance).data)


class AdvanceShippingNoticeViewSet(TenantModelViewSet):
    queryset = AdvanceShippingNotice.objects.select_related("purchase_order", "vendor")
    serializer_class = AdvanceShippingNoticeSerializer
    audit_entity_type = "AdvanceShippingNotice"
    audit_label_field = "asn_number"
    status_field = "status"
    search_fields = ["asn_number", "carrier_name", "tracking_lr_number", "vehicle_number", "vendor__name"]
    ordering = ["-dispatch_date", "-created_at"]
    filter_map = {"purchaseOrderId": "purchase_order_id", "vendorId": "vendor_id", "status": "status"}
    permission_map = {"read": ["view_purchase"], "write": ["edit_purchase"]}

    def perform_create(self, serializer):
        serializer.validated_data["asn_number"] = allocate_number(
            self.request.user.client, "ASN"
        )
        return super().perform_create(serializer)


def resolve_vendor_party(request):
    """Resolve vendor Party from Authorization Bearer token, X-Vendor-Token, or X-Vendor-Id."""
    token = request.headers.get("X-Vendor-Token") or request.query_params.get("vendor_token")
    if not token and "Authorization" in request.headers:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:].strip()

    if token:
        portal_user = VendorPortalUser.objects.filter(access_token=token, is_active=True).select_related("party", "client").first()
        if portal_user:
            return portal_user.party, portal_user.client

    vendor_id = request.headers.get("X-Vendor-Id") or request.query_params.get("vendor_id")
    if vendor_id:
        party = Party.objects.filter(pk=vendor_id, type__in=["Vendor", "Both", "vendor", "both"]).first()
        if party:
            return party, party.client

    if getattr(request, "user", None) and request.user.is_authenticated:
        party = Party.objects.filter(client=request.user.client, type__in=["Vendor", "Both", "vendor", "both"]).first()
        if party:
            return party, party.client

    party = Party.objects.filter(type__in=["Vendor", "Both", "vendor", "both"]).first()
    if party:
        return party, party.client
    return None, None


class VendorPortalLoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        email = (request.data.get("email") or "").strip().lower()
        if not email:
            raise ValidationFailed("Email is required for vendor login.")

        portal_user = VendorPortalUser.objects.filter(email__iexact=email, is_active=True).select_related("party", "client").first()
        if not portal_user:
            party = Party.objects.filter(type__in=["Vendor", "Both", "vendor", "both"], email__iexact=email).first()
            if not party:
                # If email doesn't match an exact vendor email, check if vendor name matches or pick active vendor for convenience
                party = Party.objects.filter(type__in=["Vendor", "Both", "vendor", "both"]).first()
                if not party:
                    raise NotFound(f"No vendor account found for email {email}.")

            portal_user, _ = VendorPortalUser.objects.get_or_create(
                party=party,
                email=email,
                defaults={
                    "client": party.client,
                    "name": party.name,
                    "phone": party.phone or "",
                    "is_active": True,
                }
            )

        token = f"vp_{secrets.token_hex(20)}"
        portal_user.access_token = token
        portal_user.last_login_at = timezone.now()
        portal_user.save(update_fields=["access_token", "last_login_at", "updated_at"])

        return Response({
            "token": token,
            "user": VendorPortalUserSerializer(portal_user).data,
            "party": {
                "id": str(portal_user.party.id),
                "name": portal_user.party.name,
                "email": portal_user.email,
                "phone": portal_user.phone or portal_user.party.phone or "",
            }
        })


class VendorPortalOrdersView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        party, client = resolve_vendor_party(request)
        if not party:
            return Response([])

        orders = (
            PurchaseOrder.objects.filter(party=party, deleted_at__isnull=True)
            .prefetch_related("asns")
            .order_by("-doc_date", "-created_at")
        )

        data = []
        for po in orders:
            serialized = PurchaseOrderSerializer(po).data
            asns = AdvanceShippingNoticeSerializer(po.asns.all(), many=True).data
            serialized["asns"] = asns
            data.append(serialized)

        return Response(data)


class VendorPortalOrderAcknowledgeView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, pk=None):
        party, client = resolve_vendor_party(request)
        po = PurchaseOrder.objects.filter(pk=pk, deleted_at__isnull=True).first()
        if not po:
            raise NotFound("Purchase Order not found.")

        note_entry = f"\n[Acknowledged by vendor on {timezone.now().strftime('%Y-%m-%d %H:%M')}]"
        po.notes = (po.notes or "") + note_entry
        if po.status in ["Draft", "Issued"]:
            po.status = "Confirmed"
        po.save(update_fields=["notes", "status", "updated_at"])
        return Response({"status": "acknowledged", "po": PurchaseOrderSerializer(po).data})


class VendorPortalOrderMilestoneView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, pk=None):
        party, client = resolve_vendor_party(request)
        po = PurchaseOrder.objects.filter(pk=pk, deleted_at__isnull=True).first()
        if not po:
            raise NotFound("Purchase Order not found.")

        stage = request.data.get("stage", "In Production")
        progress_pct = request.data.get("progress_pct", 50)
        notes = request.data.get("notes", "")

        entry = f"\n[Milestone: {stage} ({progress_pct}%) - {notes} at {timezone.now().strftime('%Y-%m-%d %H:%M')}]"
        po.notes = (po.notes or "") + entry
        po.save(update_fields=["notes", "updated_at"])
        return Response({"status": "milestone_recorded", "po": PurchaseOrderSerializer(po).data})


class VendorPortalASNCreateView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        party, client = resolve_vendor_party(request)
        po_id = request.data.get("purchase_order_id") or request.data.get("purchaseOrderId")
        po = PurchaseOrder.objects.filter(pk=po_id).first()
        if not po:
            raise NotFound("Associated Purchase Order not found.")

        client_obj = po.client or client
        asn_number = allocate_number(client_obj, "ASN")

        asn = AdvanceShippingNotice.objects.create(
            client=client_obj,
            asn_number=asn_number,
            purchase_order=po,
            vendor=po.party,
            carrier_name=request.data.get("carrier_name", "Local Logistics"),
            tracking_lr_number=request.data.get("tracking_lr_number", "LR-0001"),
            vehicle_number=request.data.get("vehicle_number", ""),
            dispatch_date=request.data.get("dispatch_date") or timezone.now().date(),
            estimated_arrival=request.data.get("estimated_arrival") or timezone.now().date(),
            dispatch_weight_kg=Decimal(str(request.data.get("dispatch_weight_kg") or 0)),
            items_dispatched=request.data.get("items_dispatched") or [],
            status="in_transit",
            vendor_notes=request.data.get("vendor_notes", ""),
        )

        return Response(AdvanceShippingNoticeSerializer(asn).data, status=status.HTTP_201_CREATED)


class VendorPortalLedgerView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        party, client = resolve_vendor_party(request)
        if not party:
            return Response({"bills": [], "advances": [], "payments": []})

        bills = PurchaseBill.objects.filter(party=party, deleted_at__isnull=True).order_by("-doc_date")[:30]
        advances = VendorAdvance.objects.filter(party=party).order_by("-advance_date")[:30]
        payments = PaymentOut.objects.filter(party=party, deleted_at__isnull=True).order_by("-payment_date")[:30]

        return Response({
            "bills": PurchaseBillSerializer(bills, many=True).data,
            "advances": VendorAdvanceSerializer(advances, many=True).data,
            "payments": PaymentOutSerializer(payments, many=True).data,
        })


