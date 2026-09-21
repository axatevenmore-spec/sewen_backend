"""Purchase serializers (api.md §6)."""
from rest_framework import serializers

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
from apps.sales.serializers import line_serializer_for

from .models import (
    Expense,
    GoodsReceipt,
    GoodsReceiptLine,
    PaymentOut,
    PurchaseBill,
    PurchaseBillLine,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseReturn,
    PurchaseReturnLine,
)

PurchaseOrderLineSerializer = line_serializer_for(
    PurchaseOrderLine,
    "purchase_order_lines",
    extra_fields=["received_qty", "billed_qty"],
    extra_read_only=["received_qty", "billed_qty"],
)


class PurchaseBillLineSerializer(DocumentLineSerializer):
    """Carries the weight-receiving and landed-cost fields (api.md §6.3, §6.4)."""

    receivedQty = QuantityField(source="received_qty", required=False)
    receivedWeight = QuantityField(source="received_weight", required=False, allow_null=True)
    theoreticalWeight = QuantityField(
        source="theoretical_weight", required=False, allow_null=True, read_only=True
    )
    variationPct = serializers.DecimalField(
        source="variation_pct", max_digits=9, decimal_places=4,
        coerce_to_string=False, read_only=True,
    )
    tolerancePct = serializers.DecimalField(
        source="tolerance_pct", max_digits=7, decimal_places=4,
        coerce_to_string=False, required=False, allow_null=True,
    )
    isWeightItem = serializers.BooleanField(source="is_weight_item", read_only=True)
    batchNumber = serializers.CharField(
        source="batch_number", required=False, allow_null=True, allow_blank=True
    )
    landedUnitCost = serializers.DecimalField(
        source="landed_unit_cost", max_digits=18, decimal_places=4,
        coerce_to_string=False, read_only=True,
    )

    class Meta(DocumentLineSerializer.Meta):
        model = PurchaseBillLine
        fields = DocumentLineSerializer.Meta.fields + [
            "receivedQty", "receivedWeight", "theoreticalWeight", "variationPct",
            "tolerancePct", "isWeightItem", "batchNumber", "returned_qty",
            "base_unit_cost", "apportioned_cost", "landedUnitCost",
        ]
        read_only_fields = DocumentLineSerializer.Meta.read_only_fields + [
            "returned_qty", "base_unit_cost", "apportioned_cost",
        ]


# ---------------------------------------------------------------------------
# Purchase orders (api.md §6.2)
# ---------------------------------------------------------------------------
class PurchaseOrderSerializer(DocumentSerializer):
    line_model = PurchaseOrderLine
    line_serializer = PurchaseOrderLineSerializer
    line_fk_name = "purchase_order"
    line_table_name = "purchase_order_lines"

    vendorId = TenantPrimaryKeyRelatedField(source="party", model="masters.Party")
    vendorName = serializers.CharField(source="party_name", read_only=True)
    #: The UI shows the derived billed status, not the stored one (api.md §6.2).
    billedStatus = serializers.SerializerMethodField()

    class Meta:
        model = PurchaseOrder
        fields = HEADER_FIELDS + [
            "po_number", "status", "expected_date", "location",
            "reference_number", "auto_generated", "vendorId", "vendorName",
            "billedStatus",
        ]
        read_only_fields = READ_ONLY_HEADER_FIELDS + ["po_number", "auto_generated"]


    def get_billedStatus(self, order):
        from .services import po_billed_status

        # Only computed on the detail view -- running it per row would make the
        # list N+1 (db.md §15).
        if self.context.get("include_billed_status"):
            return po_billed_status(order)
        return None


class PurchaseBillSerializer(DocumentSerializer):
    line_model = PurchaseBillLine
    line_serializer = PurchaseBillLineSerializer
    line_fk_name = "purchase_bill"
    line_table_name = "purchase_bill_lines"

    vendorId = TenantPrimaryKeyRelatedField(source="party", model="masters.Party")
    vendorName = serializers.CharField(source="party_name", read_only=True)
    vendorBillNumber = serializers.CharField(
        source="vendor_bill_number", required=False, allow_null=True, allow_blank=True
    )
    goodsReceived = serializers.BooleanField(source="goods_received", read_only=True)
    qcStatus = serializers.CharField(source="qc_status", read_only=True)

    class Meta:
        model = PurchaseBill
        fields = HEADER_FIELDS + [
            "bill_number", "vendorBillNumber", "status", "due_date",
            "purchase_order", "location", "goodsReceived", "received_date",
            "qcStatus", "qc_note", "vendorId", "vendorName",
        ]
        read_only_fields = READ_ONLY_HEADER_FIELDS + [
            "bill_number", "received_date", "qc_note",
        ]



class ReceiveGoodsSerializer(BaseSerializer):
    """``POST /purchase/bills/{id}/receive-goods/`` (api.md §6.4).

    A line may be addressed by ``lineId``, ``lineIndex`` or ``sku`` -- all three
    appear in the frontend's call sites.
    """

    lines = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    qcStatus = serializers.ChoiceField(
        choices=["Approved", "Pending Approval", "Rejected", "Rework"],
        required=False,
        allow_null=True,
    )
    locationId = serializers.CharField(required=False, allow_null=True)


class QcSerializer(BaseSerializer):
    status = serializers.ChoiceField(
        choices=["Approved", "Pending Approval", "Rejected", "Rework"]
    )
    note = serializers.CharField(required=False, allow_blank=True, allow_null=True)


class GoodsReceiptLineSerializer(BaseModelSerializer):
    itemId = serializers.CharField(source="item_id", read_only=True)
    sku = serializers.CharField(source="item.sku", read_only=True)
    itemName = serializers.CharField(source="item.name", read_only=True)

    class Meta:
        model = GoodsReceiptLine
        fields = [
            "id", "itemId", "sku", "itemName", "ordered_qty", "received_qty",
            "weighed_qty", "rejected_qty", "batch_number", "unit_cost",
            "variation_pct",
        ]


class GoodsReceiptSerializer(BaseModelSerializer):
    lines = GoodsReceiptLineSerializer(many=True, read_only=True)
    vendorName = serializers.CharField(source="party.name", read_only=True)
    billNumber = serializers.CharField(
        source="purchase_bill.bill_number", read_only=True
    )

    class Meta:
        model = GoodsReceipt
        fields = [
            "id", "grn_number", "purchase_order", "purchase_bill", "billNumber",
            "party", "vendorName", "receipt_date", "location", "qc_status",
            "qc_note", "qc_at", "lines", "created_at",
        ]


# ---------------------------------------------------------------------------
# Payments out, returns, expenses
# ---------------------------------------------------------------------------
class PaymentOutSerializer(BaseModelSerializer):
    vendorId = TenantPrimaryKeyRelatedField(source="party", model="masters.Party")
    vendorName = serializers.CharField(source="party.name", read_only=True)
    bankAccountId = TenantPrimaryKeyRelatedField(
        source="bank_account", model="accounting.BankAccount", required=False, allow_null=True
    )
    date = serializers.DateField(source="payment_date")
    unallocatedAmount = serializers.SerializerMethodField()

    class Meta:
        model = PaymentOut
        fields = [
            "id", "payment_number", "vendorId", "vendorName", "date", "amount",
            "mode", "bankAccountId", "reference_number", "notes",
            "allocated_amount", "unallocatedAmount", "status",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "payment_number", "allocated_amount", "status", "created_at", "updated_at",
        ]


    def get_unallocatedAmount(self, payment):
        return payment.unallocated_amount


class PurchaseReturnLineSerializer(DocumentLineSerializer):
    purchaseBillLineId = serializers.PrimaryKeyRelatedField(
        source="purchase_bill_line", queryset=PurchaseBillLine.objects.all()
    )
    returnedQty = QuantityField(source="returned_qty")

    class Meta(DocumentLineSerializer.Meta):
        model = PurchaseReturnLine
        fields = DocumentLineSerializer.Meta.fields + ["purchaseBillLineId", "returnedQty"]


class PurchaseReturnSerializer(DocumentSerializer):
    line_model = PurchaseReturnLine
    line_serializer = PurchaseReturnLineSerializer
    line_fk_name = "purchase_return"
    line_table_name = "purchase_return_lines"

    purchaseBillId = TenantPrimaryKeyRelatedField(
        source="purchase_bill", queryset=PurchaseBill.objects.all()
    )

    class Meta:
        model = PurchaseReturn
        fields = HEADER_FIELDS + [
            "return_number", "debit_note_number", "status", "purchaseBillId",
            "reason", "location",
        ]
        read_only_fields = READ_ONLY_HEADER_FIELDS + [
            "return_number", "debit_note_number",
        ]


class ExpenseSerializer(BaseModelSerializer):
    categoryId = TenantPrimaryKeyRelatedField(
        source="category", model="accounting.ExpenseCategory", required=False, allow_null=True
    )
    categoryName = serializers.CharField(source="category.name", read_only=True)
    vendorId = TenantPrimaryKeyRelatedField(
        source="party", model="masters.Party", required=False, allow_null=True
    )
    vendorName = serializers.CharField(source="party.name", read_only=True)
    bankAccountId = TenantPrimaryKeyRelatedField(
        source="bank_account", model="accounting.BankAccount", required=False, allow_null=True
    )
    receiptFileId = TenantPrimaryKeyRelatedField(
        source="receipt_file", model="core.File", required=False, allow_null=True
    )
    date = serializers.DateField(source="expense_date")

    class Meta:
        model = Expense
        fields = [
            "id", "expense_number", "categoryId", "categoryName", "vendorId",
            "vendorName", "date", "amount", "tax_amount", "total", "payment_mode",
            "bankAccountId", "receiptFileId", "account", "reference_number",
            "notes", "status", "created_at", "updated_at",
        ]
        read_only_fields = ["expense_number", "total", "created_at", "updated_at"]



class BillOutstandingSerializer(BaseSerializer):
    total = MoneyField()
    paid = MoneyField()
    outstanding = MoneyField()
    dueDate = serializers.DateField(allow_null=True)
    daysOverdue = serializers.IntegerField()
    ageingBucket = serializers.CharField()
