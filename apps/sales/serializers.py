"""Sales serializers (api.md §5)."""
from decimal import Decimal

from rest_framework import serializers

from apps.core.money import round2

from apps.core.document_serializers import (
    HEADER_FIELDS,
    READ_ONLY_HEADER_FIELDS,
    DocumentLineSerializer,
    DocumentSerializer,
)
from apps.core.serializers import (
    BaseModelSerializer,
    BaseSerializer,
    MoneyField,
    QuantityField,
    TenantPrimaryKeyRelatedField,
)

from . import services
from .models import (
    DeliveryChallan,
    DeliveryChallanLine,
    Estimate,
    EstimateLine,
    CashPaymentReceipt,
    PaymentAllocation,
    PaymentIn,
    ProformaInvoice,
    ProformaInvoiceLine,
    Quotation,
    QuotationActivity,
    QuotationLine,
    SalesInvoice,
    SalesInvoiceLine,
    SalesInvoiceRevision,
    SalesOrder,
    SalesOrderLine,
    SalesReturn,
    SalesReturnLine,
    WarrantyCard,
    WarrantyCardItem,
)


def line_serializer_for(line_model, table_name, extra_fields=(), extra_read_only=()):
    """Build the line serializer for one document type.

    The parameter is not named ``model``: a class body does not close over the
    enclosing function's locals, so ``model = model`` inside ``class Meta``
    would resolve to the name being defined, not the argument.
    """

    class _LineSerializer(DocumentLineSerializer):
        line_table_name = table_name

        class Meta(DocumentLineSerializer.Meta):
            model = line_model
            fields = DocumentLineSerializer.Meta.fields + list(extra_fields)
            read_only_fields = DocumentLineSerializer.Meta.read_only_fields + list(
                extra_read_only
            )

    _LineSerializer.__name__ = f"{line_model.__name__}Serializer"
    return _LineSerializer


EstimateLineSerializer = line_serializer_for(EstimateLine, "estimate_lines")
QuotationLineSerializer = line_serializer_for(QuotationLine, "quotation_lines")
SalesOrderLineSerializer = line_serializer_for(
    SalesOrderLine,
    "sales_order_lines",
    extra_fields=["dispatched_qty", "invoiced_qty"],
    extra_read_only=["dispatched_qty", "invoiced_qty"],
)
ProformaInvoiceLineSerializer = line_serializer_for(
    ProformaInvoiceLine, "proforma_invoice_lines"
)
DeliveryChallanLineSerializer = line_serializer_for(
    DeliveryChallanLine, "delivery_challan_lines"
)
SalesInvoiceLineSerializer = line_serializer_for(
    SalesInvoiceLine, "sales_invoice_lines",
    extra_fields=["returned_qty"], extra_read_only=["returned_qty"],
)


# ---------------------------------------------------------------------------
# Estimates (api.md §5.2)
# ---------------------------------------------------------------------------
class EstimateSerializer(DocumentSerializer):
    line_model = EstimateLine
    line_serializer = EstimateLineSerializer
    line_fk_name = "estimate"
    line_table_name = "estimate_lines"

    class Meta:
        model = Estimate
        fields = HEADER_FIELDS + ["estimate_number", "status", "valid_until", "crm_lead"]
        read_only_fields = READ_ONLY_HEADER_FIELDS + ["estimate_number"]


# ---------------------------------------------------------------------------
# Quotations (api.md §5.3)
# ---------------------------------------------------------------------------
class QuotationSerializer(DocumentSerializer):
    line_model = QuotationLine
    line_serializer = QuotationLineSerializer
    line_fk_name = "quotation"
    line_table_name = "quotation_lines"

    class Meta:
        model = Quotation
        fields = HEADER_FIELDS + [
            "quotation_number", "status", "valid_until", "subject",
            "estimate", "crm_deal", "crm_lead",
        ]
        read_only_fields = READ_ONLY_HEADER_FIELDS + ["quotation_number"]


class QuotationActivitySerializer(BaseModelSerializer):
    class Meta:
        model = QuotationActivity
        fields = ["id", "event", "actor_label", "comment", "ip", "created_at"]


class ShareRequestSerializer(BaseSerializer):
    expiryDays = serializers.IntegerField(required=False, default=14, min_value=1, max_value=365)
    recipients = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    channel = serializers.ChoiceField(
        choices=["email", "whatsapp", "link"], required=False, default="link"
    )


# ---------------------------------------------------------------------------
# Sales orders (api.md §5.4)
# ---------------------------------------------------------------------------
class SalesOrderSerializer(DocumentSerializer):
    line_model = SalesOrderLine
    line_serializer = SalesOrderLineSerializer
    line_fk_name = "sales_order"
    line_table_name = "sales_order_lines"

    totalSalesValue = MoneyField(source="total_sales_value", required=False, allow_null=True)
    formalInvoiceAmount = MoneyField(source="formal_invoice_amount", required=False, allow_null=True)
    cashAmount = MoneyField(source="cash_amount", required=False, allow_null=True)
    invoice = serializers.SerializerMethodField()
    cashReceipt = serializers.SerializerMethodField()

    class Meta:
        model = SalesOrder
        fields = HEADER_FIELDS + [
            "order_number", "stage", "payment_status", "delivery_date",
            "quotation", "pms_project", "reference_number",
            "total_sales_value", "formal_invoice_amount", "cash_amount",
            "totalSalesValue", "formalInvoiceAmount", "cashAmount",
            "invoice", "cashReceipt",
        ]
        read_only_fields = READ_ONLY_HEADER_FIELDS + ["order_number", "payment_status"]

    def get_invoice(self, order):
        inv = order.invoices.filter(deleted_at__isnull=True).exclude(status="Cancelled").first()
        if not inv:
            return None
        return {
            "id": inv.id,
            "invoiceNumber": inv.invoice_number,
            "total": float(inv.total),
            "status": inv.status,
            "date": inv.doc_date.isoformat() if inv.doc_date else None,
        }

    def get_cashReceipt(self, order):
        pmt = order.cash_receipts.filter(deleted_at__isnull=True).exclude(status="Cancelled").first()
        if not pmt:
            return None
        return {
            "id": pmt.id,
            "receiptNumber": pmt.payment_number,
            "amount": float(pmt.amount),
            "date": pmt.payment_date.isoformat() if pmt.payment_date else None,
            "mode": pmt.mode,
            "reference": pmt.reference_number,
            "status": pmt.status,
        }


class ConvertLinesSerializer(BaseSerializer):
    """Partial conversion (api.md §5.4): ``{ lines: [{ lineId, qty, serials[] }] }``."""

    lines = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    date = serializers.DateField(required=False)
    locationId = serializers.CharField(required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True)


# ---------------------------------------------------------------------------
# Proforma invoices (api.md §5.5)
# ---------------------------------------------------------------------------
class ProformaInvoiceSerializer(DocumentSerializer):
    line_model = ProformaInvoiceLine
    line_serializer = ProformaInvoiceLineSerializer
    line_fk_name = "proforma_invoice"
    line_table_name = "proforma_invoice_lines"

    totalSalesValue = MoneyField(source="total_sales_value", required=False, allow_null=True)
    formalInvoiceAmount = MoneyField(source="formal_invoice_amount", required=False, allow_null=True)
    cashAmount = MoneyField(source="cash_amount", required=False, allow_null=True)
    invoice = serializers.SerializerMethodField()
    cashReceipt = serializers.SerializerMethodField()

    class Meta:
        model = ProformaInvoice
        fields = HEADER_FIELDS + [
            "proforma_number", "status", "valid_until", "sales_order",
            "total_sales_value", "formal_invoice_amount", "cash_amount",
            "totalSalesValue", "formalInvoiceAmount", "cashAmount",
            "invoice", "cashReceipt",
        ]
        read_only_fields = READ_ONLY_HEADER_FIELDS + ["proforma_number"]

    def get_invoice(self, proforma):
        inv = proforma.invoices.filter(deleted_at__isnull=True).exclude(status="Cancelled").first()
        if not inv:
            return None
        return {
            "id": inv.id,
            "invoiceNumber": inv.invoice_number,
            "total": float(inv.total),
            "status": inv.status,
            "date": inv.doc_date.isoformat() if inv.doc_date else None,
        }

    def get_cashReceipt(self, proforma):
        pmt = proforma.cash_receipts.filter(deleted_at__isnull=True).exclude(status="Cancelled").first()
        if not pmt:
            return None
        return {
            "id": pmt.id,
            "receiptNumber": pmt.payment_number,
            "amount": float(pmt.amount),
            "date": pmt.payment_date.isoformat() if pmt.payment_date else None,
            "mode": pmt.mode,
            "reference": pmt.reference_number,
            "status": pmt.status,
        }


# ---------------------------------------------------------------------------
# Delivery challans (api.md §5.6)
# ---------------------------------------------------------------------------
class DeliveryChallanSerializer(DocumentSerializer):
    line_model = DeliveryChallanLine
    line_serializer = DeliveryChallanLineSerializer
    line_fk_name = "delivery_challan"
    line_table_name = "delivery_challan_lines"

    class Meta:
        model = DeliveryChallan
        fields = HEADER_FIELDS + [
            "challan_number", "status", "sales_order", "quotation",
            "dispatch_date", "vehicle_number", "transporter", "lr_number",
            "delivery_location", "location", "delivered_at",
        ]
        read_only_fields = READ_ONLY_HEADER_FIELDS + ["challan_number", "delivered_at"]


# ---------------------------------------------------------------------------
# Sales invoices (api.md §5.7)
# ---------------------------------------------------------------------------
class SalesInvoiceRevisionSerializer(BaseModelSerializer):
    revisionNumber = serializers.IntegerField(source="revision_number")
    oldInvoiceAmount = MoneyField(source="old_invoice_amount")
    newInvoiceAmount = MoneyField(source="new_invoice_amount")
    oldCashAmount = MoneyField(source="old_cash_amount")
    newCashAmount = MoneyField(source="new_cash_amount")
    difference = MoneyField()
    reason = serializers.CharField(allow_blank=True, required=False)
    changedBy = serializers.CharField(source="changed_by.email", read_only=True)
    changedAt = serializers.DateTimeField(source="changed_at", read_only=True)
    cashReceiptNumber = serializers.CharField(source="cash_receipt.receipt_number", read_only=True)

    class Meta:
        model = SalesInvoiceRevision
        fields = [
            "id", "revisionNumber", "oldInvoiceAmount", "newInvoiceAmount",
            "oldCashAmount", "newCashAmount", "difference", "reason",
            "changedBy", "changedAt", "cashReceiptNumber",
        ]


class SalesInvoiceSerializer(DocumentSerializer):
    line_model = SalesInvoiceLine
    line_serializer = SalesInvoiceLineSerializer
    line_fk_name = "sales_invoice"
    line_table_name = "sales_invoice_lines"

    #: ``Overdue`` is layered on at read time, never stored (db.md §12).
    displayStatus = serializers.SerializerMethodField()

    salesOrderId = TenantPrimaryKeyRelatedField(
        source="sales_order", model="sales.SalesOrder", required=False, allow_null=True
    )
    deliveryChallanId = TenantPrimaryKeyRelatedField(
        source="delivery_challan", model="sales.DeliveryChallan", required=False, allow_null=True
    )
    proformaInvoiceId = TenantPrimaryKeyRelatedField(
        source="proforma_invoice", model="sales.ProformaInvoice", required=False, allow_null=True
    )

    totalSalesValue = MoneyField(source="total_sales_value", required=False, allow_null=True)
    formalInvoiceAmount = MoneyField(source="total", read_only=True)
    cashAmount = MoneyField(source="cash_amount", required=False)
    totalAllocated = serializers.SerializerMethodField()
    remainingAmount = serializers.SerializerMethodField()
    revisions = SalesInvoiceRevisionSerializer(many=True, read_only=True)
    cashReceipt = serializers.SerializerMethodField()

    class Meta:
        model = SalesInvoice
        fields = HEADER_FIELDS + [
            "invoice_number", "status", "displayStatus", "due_date",
            "sales_order", "delivery_challan", "proforma_invoice", "location",
            "salesOrderId", "deliveryChallanId", "proformaInvoiceId",
            "totalSalesValue", "formalInvoiceAmount", "cashAmount",
            "totalAllocated", "remainingAmount", "revisions", "cashReceipt",
            "irn", "eway_bill_number",
        ]
        read_only_fields = READ_ONLY_HEADER_FIELDS + ["invoice_number", "status"]

    def get_displayStatus(self, invoice):
        return services.display_status(invoice)

    def get_totalAllocated(self, invoice):
        cash = invoice.cash_amount or Decimal("0.00")
        return round2(invoice.total + cash)

    def get_remainingAmount(self, invoice):
        total_sales = invoice.total_sales_value if invoice.total_sales_value is not None else round2(invoice.total + (invoice.cash_amount or Decimal("0.00")))
        allocated = round2(invoice.total + (invoice.cash_amount or Decimal("0.00")))
        rem = round2(total_sales - allocated)
        return rem if rem > Decimal("0.00") else Decimal("0.00")

    def get_cashReceipt(self, invoice):
        receipt = invoice.cash_receipts.filter(deleted_at__isnull=True).exclude(status__in=["CANCELLED", "VOIDED"]).first()
        return CashPaymentReceiptSerializer(receipt, context=self.context).data if receipt else None


class InvoiceOutstandingSerializer(BaseSerializer):
    total = MoneyField()
    totalSalesValue = MoneyField(required=False)
    formalInvoiceAmount = MoneyField(required=False)
    cashAmount = MoneyField(required=False)
    totalAllocated = MoneyField(required=False)
    remaining = MoneyField(required=False)
    taxableAmount = MoneyField(required=False)
    gst = MoneyField(required=False)
    paid = MoneyField()
    paidAgainstInvoice = MoneyField(required=False)
    withoutBillCash = MoneyField(required=False)
    totalReceived = MoneyField(required=False)
    outstanding = MoneyField()
    dueDate = serializers.DateField(allow_null=True)
    daysOverdue = serializers.IntegerField()
    ageingBucket = serializers.CharField()


# ---------------------------------------------------------------------------
# Payments in (api.md §5.8)
# ---------------------------------------------------------------------------
class PaymentAllocationSerializer(BaseModelSerializer):
    documentId = serializers.CharField(source="document_id", read_only=True)

    class Meta:
        model = PaymentAllocation
        fields = ["id", "document_type", "documentId", "amount", "allocated_at"]


class CashPaymentReceiptSerializer(BaseModelSerializer):
    customerId = TenantPrimaryKeyRelatedField(source="party", model="masters.Party")
    customerName = serializers.CharField(source="party.name", read_only=True)
    paymentId = serializers.UUIDField(source="payment_id", read_only=True)
    invoiceId = TenantPrimaryKeyRelatedField(
        source="invoice", model="sales.SalesInvoice", required=False, allow_null=True
    )
    invoiceNumber = serializers.CharField(source="invoice.invoice_number", read_only=True)
    receiptNumber = serializers.CharField(source="receipt_number", read_only=True)
    date = serializers.DateField(source="payment_date")
    paymentMode = serializers.CharField(source="mode")
    referenceNumber = serializers.CharField(source="reference_number", required=False, allow_blank=True, allow_null=True)
    createdBy = serializers.CharField(source="created_by.email", read_only=True)
    cancelledBy = serializers.CharField(source="cancelled_by.email", read_only=True)
    cancelledAt = serializers.DateTimeField(source="cancelled_at", read_only=True)
    cancellationReason = serializers.CharField(source="cancellation_reason", read_only=True)

    class Meta:
        model = CashPaymentReceipt
        fields = [
            "id", "receiptNumber", "paymentId", "customerId", "customerName",
            "invoiceId", "invoiceNumber", "amount", "date", "paymentMode",
            "referenceNumber", "description", "notes", "status",
            "createdBy", "cancelledBy", "cancelledAt", "cancellationReason",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "receiptNumber", "paymentId", "status", "createdBy", "cancelledBy",
            "cancelledAt", "cancellationReason", "created_at", "updated_at",
        ]


class PaymentInSerializer(BaseModelSerializer):
    customerId = TenantPrimaryKeyRelatedField(source="party", model="masters.Party")
    customerName = serializers.CharField(source="party.name", read_only=True)
    paymentType = serializers.CharField(source="payment_type", default="WITH_BILL")
    bankAccountId = TenantPrimaryKeyRelatedField(
        source="bank_account", model="accounting.BankAccount", required=False, allow_null=True
    )
    date = serializers.DateField(source="payment_date")
    allocations = serializers.SerializerMethodField()
    unallocatedAmount = serializers.SerializerMethodField()
    cashReceipt = CashPaymentReceiptSerializer(source="cash_receipt", read_only=True)
    invoiceId = TenantPrimaryKeyRelatedField(
        source="invoice", model="sales.SalesInvoice", required=False, allow_null=True
    )
    invoiceNumber = serializers.CharField(source="invoice.invoice_number", read_only=True)
    salesOrderId = TenantPrimaryKeyRelatedField(
        source="sales_order", model="sales.SalesOrder", required=False, allow_null=True
    )
    salesOrderNumber = serializers.CharField(source="sales_order.order_number", read_only=True)
    proformaInvoiceId = TenantPrimaryKeyRelatedField(
        source="proforma_invoice", model="sales.ProformaInvoice", required=False, allow_null=True
    )
    proformaInvoiceNumber = serializers.CharField(source="proforma_invoice.proforma_number", read_only=True)
    allocationsInput = serializers.ListField(
        child=serializers.DictField(), required=False, write_only=True
    )

    class Meta:
        model = PaymentIn
        fields = [
            "id", "payment_number", "paymentType", "customerId", "customerName", "date", "amount",
            "mode", "bankAccountId", "reference_number", "description", "notes",
            "allocated_amount", "unallocatedAmount", "allocations", "cashReceipt", "status",
            "invoiceId", "invoiceNumber", "salesOrderId", "salesOrderNumber", "proformaInvoiceId", "proformaInvoiceNumber", "allocationsInput", "created_at", "updated_at",
        ]
        read_only_fields = [
            "payment_number", "allocated_amount", "status", "created_at", "updated_at",
        ]


    def get_allocations(self, payment):
        rows = PaymentAllocation.objects.filter(
            client_id=payment.client_id,
            payment_side="in",
            payment_id=payment.id,
            deleted_at__isnull=True,
        )
        return PaymentAllocationSerializer(rows, many=True).data

    def get_unallocatedAmount(self, payment):
        return payment.unallocated_amount


class AllocateSerializer(BaseSerializer):
    allocations = serializers.ListField(child=serializers.DictField(), allow_empty=False)


# ---------------------------------------------------------------------------
# Sales returns (api.md §5.9)
# ---------------------------------------------------------------------------
class SalesReturnLineSerializer(DocumentLineSerializer):
    salesInvoiceLineId = serializers.PrimaryKeyRelatedField(
        source="sales_invoice_line", queryset=SalesInvoiceLine.objects.all()
    )
    returnedQty = QuantityField(source="returned_qty")

    class Meta(DocumentLineSerializer.Meta):
        model = SalesReturnLine
        fields = DocumentLineSerializer.Meta.fields + ["salesInvoiceLineId", "returnedQty"]


class SalesReturnSerializer(DocumentSerializer):
    line_model = SalesReturnLine
    line_serializer = SalesReturnLineSerializer
    line_fk_name = "sales_return"
    line_table_name = "sales_return_lines"

    salesInvoiceId = TenantPrimaryKeyRelatedField(
        source="sales_invoice", queryset=SalesInvoice.objects.all()
    )

    class Meta:
        model = SalesReturn
        fields = HEADER_FIELDS + [
            "return_number", "credit_note_number", "status", "salesInvoiceId",
            "reason", "location",
        ]
        read_only_fields = READ_ONLY_HEADER_FIELDS + [
            "return_number", "credit_note_number",
        ]


# Hidden: Warranty Cards out of scope (Sweven spec) -- restore by uncommenting this block.
# # ---------------------------------------------------------------------------
# # Warranty cards (api.md §5.10)
# # ---------------------------------------------------------------------------
# class WarrantyCardItemSerializer(BaseModelSerializer):
#     itemId = TenantPrimaryKeyRelatedField(source="item", model="masters.Item")
#     serials = serializers.ListField(
#         child=serializers.CharField(), required=False, default=list
#     )

#     class Meta:
#         model = WarrantyCardItem
#         fields = ["id", "itemId", "sku", "item_name", "qty", "serials"]



# Hidden: Warranty Cards out of scope (Sweven spec) -- restore by uncommenting this block.
# class WarrantyCardSerializer(BaseModelSerializer):
#     """Two independent status fields -- do not collapse them (api.md §5.10)."""

#     customerId = TenantPrimaryKeyRelatedField(source="party", model="masters.Party")
#     customerName = serializers.CharField(source="party.name", read_only=True)
#     challanNumber = serializers.CharField(
#         source="delivery_challan.challan_number", read_only=True
#     )
#     invoiceNumber = serializers.CharField(
#         source="sales_invoice.invoice_number", read_only=True
#     )
#     #: DERIVED on every read (api.md §5.10) -- never stored.
#     coverageStatus = serializers.SerializerMethodField()
#     items = WarrantyCardItemSerializer(many=True, required=False)

#     class Meta:
#         model = WarrantyCard
#         fields = [
#             "id", "card_number", "customerId", "customerName", "contact_person",
#             "billing_address", "shipping_address", "gstin",
#             "delivery_challan", "challanNumber", "sales_invoice", "invoiceNumber",
#             "sales_order", "delivery_date", "delivery_location",
#             "warranty_period", "warranty_unit", "warranty_start_event",
#             "start_date", "expiry_date", "expiring_soon_days",
#             "document_status", "coverageStatus",
#             "suspended_reason", "cancelled_reason", "void_reason",
#             "terms", "notes", "items", "created_at", "updated_at",
#         ]
#         read_only_fields = ["card_number", "created_at", "updated_at"]


#     def get_coverageStatus(self, card):
#         return services.coverage_status(card)

#     def create(self, validated_data):
#         items = validated_data.pop("items", [])
#         card = super().create(validated_data)
#         self._write_items(card, items)
#         return card

#     def update(self, instance, validated_data):
#         items = validated_data.pop("items", None)
#         card = super().update(instance, validated_data)
#         if items is not None:
#             card.items.all().delete()
#             self._write_items(card, items)
#         return card

#     def _write_items(self, card, rows):
#         from apps.inventory.services import resolve_serials
#         from .models import WarrantyCardSerial

#         for row in rows:
#             serial_numbers = row.pop("serials", [])
#             item = row.get("item")
#             line = WarrantyCardItem.objects.create(
#                 client_id=card.client_id,
#                 warranty_card=card,
#                 sku=item.sku if item else row.get("sku"),
#                 item_name=item.name if item else row.get("item_name"),
#                 **row,
#             )
#             if serial_numbers and item is not None:
#                 resolved = resolve_serials(
#                     card.client_id, item.id, serial_numbers, expected_status=None
#                 )
#                 WarrantyCardSerial.objects.bulk_create(
#                     [
#                         WarrantyCardSerial(
#                             client_id=card.client_id, warranty_card=card, serial=serial
#                         )
#                         for serial in resolved
#                     ],
#                     ignore_conflicts=True,
#                 )


class ReasonSerializer(BaseSerializer):
    reason = serializers.CharField(required=False, allow_blank=True)
