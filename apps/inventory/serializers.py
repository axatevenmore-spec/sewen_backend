"""Serializers for the inventory module (api.md §7)."""
from rest_framework import serializers

from apps.core.serializers import (
    BaseModelSerializer,
    BaseSerializer,
    QuantityField,
    TenantPrimaryKeyRelatedField,
)
from apps.masters.models import Item, Location

from .models import (
    DemoUnit,
    FaultyPart,
    # QualityInspection,  # Hidden: QC out of scope
    # QualityStandard,  # Hidden: QC out of scope
    ReworkOrder,
    ScrapLog,
    ServiceUsage,
    StockAudit,
    StockAuditLine,
    StockMovement,
    StockTransfer,
    StockTransferLine,
    ZoneRequest,
    ZoneRequestLine,
)


class StockMovementSerializer(BaseModelSerializer):
    """The movement record of api.md §7.2, field for field."""

    itemId = serializers.CharField(source="item_id", read_only=True)
    itemSku = serializers.CharField(source="item.sku", read_only=True)
    itemName = serializers.CharField(source="item.name", read_only=True)
    itemType = serializers.SerializerMethodField()
    category = serializers.CharField(source="item.category.name", read_only=True, allow_null=True)
    grade = serializers.SerializerMethodField()
    dimensions = serializers.SerializerMethodField()
    specification = serializers.SerializerMethodField()
    locationId = serializers.CharField(source="location_id", read_only=True)
    locationName = serializers.CharField(source="location.name", read_only=True)
    direction = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    isReversed = serializers.SerializerMethodField()
    referenceType = serializers.CharField(source="reference_type", read_only=True, allow_null=True)
    referenceId = serializers.CharField(source="reference_id", read_only=True, allow_null=True)
    referenceNumber = serializers.CharField(source="reference_number", read_only=True, allow_null=True)
    sourceDocumentType = serializers.CharField(source="source_document_type", read_only=True, allow_null=True)
    sourceDocumentId = serializers.CharField(source="source_document_id", read_only=True, allow_null=True)
    originalMovementId = serializers.CharField(source="original_movement_id", read_only=True, allow_null=True)
    reversalMovementId = serializers.CharField(source="reversal_movement_id", read_only=True, allow_null=True)
    creatorName = serializers.SerializerMethodField()
    date = serializers.DateField(source="movement_date", read_only=True)
    serials = serializers.SerializerMethodField()

    class Meta:
        model = StockMovement
        fields = [
            "id", "itemId", "itemSku", "itemName", "itemType", "category", "grade",
            "dimensions", "specification", "locationId", "locationName", "direction",
            "type", "quantity", "weighed_qty", "uom", "unit_cost",
            "reference_type", "referenceType", "referenceId", "reference_number", "referenceNumber",
            "source_document_type", "sourceDocumentType", "sourceDocumentId",
            "originalMovementId", "reversalMovementId", "isReversed", "status",
            "creatorName", "batch_number", "serials", "date", "notes", "created_at",
        ]

    def get_itemType(self, movement):
        if not movement.item:
            return None
        if getattr(movement.item, "item_type", None):
            return movement.item.item_type.name
        if movement.item.category and getattr(movement.item.category, "item_type", None):
            return movement.item.category.item_type.name
        return None

    def get_grade(self, movement):
        if not movement.item:
            return None
        if getattr(movement.item, "grade_id", None) and movement.item.grade:
            return movement.item.grade.code or movement.item.grade.name
        return getattr(movement.item, "metal_grade", None)

    def get_dimensions(self, movement):
        from apps.inventory.views import format_item_spec
        return format_item_spec(movement.item) if movement.item else ""

    def get_specification(self, movement):
        return self.get_dimensions(movement)

    def get_direction(self, movement):
        return "IN" if movement.quantity > 0 else "OUT"

    def get_isReversed(self, movement):
        return movement.reversal_movement_id is not None

    def get_status(self, movement):
        return "Reversed" if movement.reversal_movement_id else "Posted"

    def get_creatorName(self, movement):
        if movement.created_by:
            return getattr(movement.created_by, "name", None) or getattr(movement.created_by, "email", "Staff")
        return "System"

    def get_serials(self, movement):
        from apps.masters.models import ItemSerial

        cached = getattr(movement, "_serials", None)
        if cached is not None:
            return cached
        return list(
            ItemSerial.objects.filter(
                sold_movement_id=movement.id
            ).values_list("serial_no", flat=True)
        ) or list(
            ItemSerial.objects.filter(
                received_movement_id=movement.id
            ).values_list("serial_no", flat=True)
        )


class StockAdjustmentSerializer(BaseSerializer):
    """``POST /inventory/adjustments/`` -- replaces ``adjustItemStock``."""

    itemId = TenantPrimaryKeyRelatedField(queryset=Item.objects.all())
    locationId = TenantPrimaryKeyRelatedField(
        queryset=Location.objects.all(), required=False, allow_null=True
    )
    quantity = QuantityField()
    #: True means "set stock to this number"; False means "add this delta".
    isAbsolute = serializers.BooleanField(required=False, default=False)
    movementType = serializers.CharField(required=False, default="ADJUSTMENT")
    reason = serializers.CharField()
    unitCost = serializers.DecimalField(
        max_digits=18, decimal_places=4, coerce_to_string=False, required=False
    )


class StockPositionSerializer(BaseSerializer):
    itemId = serializers.CharField()
    sku = serializers.CharField()
    name = serializers.CharField()
    itemType = serializers.CharField(allow_null=True, required=False)
    category = serializers.CharField(allow_null=True)
    grade = serializers.CharField(allow_null=True, required=False)
    dimensions = serializers.CharField(allow_null=True, required=False)
    locationId = serializers.CharField(allow_null=True, required=False)
    locationName = serializers.CharField(allow_null=True, required=False)
    uom = serializers.CharField()
    openingBalance = QuantityField(required=False)
    totalInward = QuantityField(required=False)
    totalOutward = QuantityField(required=False)
    onHand = QuantityField()
    reserved = QuantityField()
    available = QuantityField()
    damaged = QuantityField()
    reorderLevel = QuantityField()
    status = serializers.CharField()
    unitCost = serializers.DecimalField(
        max_digits=18, decimal_places=4, coerce_to_string=False
    )
    value = serializers.DecimalField(max_digits=18, decimal_places=2, coerce_to_string=False)


class StockTransferLineSerializer(BaseModelSerializer):
    itemId = TenantPrimaryKeyRelatedField(source="item", queryset=Item.objects.all())
    sku = serializers.CharField(source="item.sku", read_only=True)
    name = serializers.CharField(source="item.name", read_only=True)

    class Meta:
        model = StockTransferLine
        fields = ["id", "itemId", "sku", "name", "qty", "received_qty"]


class StockTransferSerializer(BaseModelSerializer):
    """db.md §7.4 standardises on ``items[]`` and keeps the denormalised names
    as read-only echoes, which is what the mock data's flat form used."""

    items = StockTransferLineSerializer(many=True, required=False)
    sourceLocationId = TenantPrimaryKeyRelatedField(
        source="from_location", queryset=Location.objects.all()
    )
    destLocationId = TenantPrimaryKeyRelatedField(
        source="to_location", queryset=Location.objects.all()
    )
    sourceLocation = serializers.CharField(source="from_location.name", read_only=True)
    destLocation = serializers.CharField(source="to_location.name", read_only=True)
    itemsCount = serializers.SerializerMethodField()
    date = serializers.DateField(source="transfer_date")

    class Meta:
        model = StockTransfer
        fields = [
            "id", "transfer_number", "sourceLocationId", "destLocationId",
            "sourceLocation", "destLocation", "date", "itemsCount", "status",
            "shipped_by", "notes", "items", "created_at", "updated_at",
        ]
        read_only_fields = ["transfer_number", "created_at", "updated_at"]

    def get_itemsCount(self, transfer):
        return transfer.items.filter(deleted_at__isnull=True).count()

    def validate(self, attrs):
        source = attrs.get("from_location") or getattr(self.instance, "from_location", None)
        destination = attrs.get("to_location") or getattr(self.instance, "to_location", None)
        if source and destination and source.id == destination.id:
            raise serializers.ValidationError(
                {"destLocationId": ["Must differ from the source location."]}
            )
        return attrs


class FaultyPartSerializer(BaseModelSerializer):
    itemId = TenantPrimaryKeyRelatedField(source="item", queryset=Item.objects.all())
    sku = serializers.CharField(source="item.sku", read_only=True)
    itemName = serializers.CharField(source="item.name", read_only=True)
    vendor = serializers.CharField(source="vendor.name", read_only=True)
    date = serializers.DateField(source="reported_date")
    qty = QuantityField(source="quantity")
    timeline = serializers.SerializerMethodField()

    class Meta:
        model = FaultyPart
        fields = [
            "id", "rma_number", "date", "itemId", "sku", "itemName", "qty",
            "vendor", "status", "notes", "fault_description", "timeline",
            "created_at", "updated_at",
        ]
        read_only_fields = ["rma_number", "created_at", "updated_at"]

    def get_timeline(self, part):
        """api.md §7.3 -- an ordered progress tracker derived from the status.

        The frontend renders ``{ label, timestamp, status }`` directly, so the
        server produces it rather than storing a parallel list.
        """
        steps = [
            "Reported", "Pending Action", "Sent for Replacement",
            "Replaced", "Credited", "Closed",
        ]
        try:
            current = steps.index(part.status)
        except ValueError:
            current = 0
        return [
            {
                "label": label,
                "timestamp": part.created_at if index == 0 else None,
                "status": (
                    "completed" if index < current
                    else "current" if index == current
                    else "future"
                ),
            }
            for index, label in enumerate(steps)
        ]


# Hidden: Service Usage out of scope (Sweven spec) -- restore by uncommenting this block.
# class ServiceUsageSerializer(BaseModelSerializer):
#     itemId = TenantPrimaryKeyRelatedField(source="item", queryset=Item.objects.all())
#     sku = serializers.CharField(source="item.sku", read_only=True)
#     qty = QuantityField(source="quantity")
#     date = serializers.DateField(source="used_on")

#     class Meta:
#         model = ServiceUsage
#         fields = [
#             "id", "ticket_number", "technician", "itemId", "sku", "qty", "date",
#             "notes", "job_reference", "chargeable", "created_at",
#         ]
#         read_only_fields = ["ticket_number", "created_at"]


class ZoneRequestLineSerializer(BaseModelSerializer):
    itemId = TenantPrimaryKeyRelatedField(source="item", queryset=Item.objects.all())
    sku = serializers.CharField(source="item.sku", read_only=True)
    product = serializers.CharField(source="item.name", read_only=True)
    qty = QuantityField(source="requested_qty")
    warehouseStock = serializers.SerializerMethodField()

    class Meta:
        model = ZoneRequestLine
        fields = ["id", "itemId", "sku", "product", "qty", "issued_qty", "warehouseStock"]

    def get_warehouseStock(self, line):
        from . import services as stock

        client_id = self.context.get("client_id") or line.client_id
        return stock.calculate_item_stock(client_id, line.item_id)["available"]


class ZoneRequestSerializer(BaseModelSerializer):
    lines = ZoneRequestLineSerializer(many=True, required=False)
    zone = serializers.CharField(source="zone_location.name", read_only=True)
    zoneLocationId = TenantPrimaryKeyRelatedField(
        source="zone_location", queryset=Location.objects.all()
    )
    requestedBy = serializers.CharField(source="requested_by_name", required=False, allow_null=True)
    date = serializers.DateField(source="request_date", required=False, allow_null=True)

    class Meta:
        model = ZoneRequest
        fields = [
            "id", "request_number", "requestedBy", "zone", "zoneLocationId",
            "target_sector", "date", "requested_at", "status", "notes",
            "manager_signoff_needed", "reject_reason", "lines",
            "created_at", "updated_at",
        ]
        read_only_fields = ["request_number", "requested_at", "created_at", "updated_at"]


class StockAuditLineSerializer(BaseModelSerializer):
    itemId = TenantPrimaryKeyRelatedField(source="item", queryset=Item.objects.all())
    sku = serializers.CharField(source="item.sku", read_only=True)
    name = serializers.CharField(source="item.name", read_only=True)
    # GeneratedField (counted_qty - system_qty) -- DRF falls back to ModelField
    # for it, which drf-spectacular cannot map (DecimalField() with no args).
    # Declared explicitly so schema generation sees a real DecimalField.
    variance = QuantityField(read_only=True)

    class Meta:
        model = StockAuditLine
        fields = [
            "id", "itemId", "sku", "name", "system_qty", "counted_qty",
            "variance", "reason", "adjustment_movement",
        ]
        read_only_fields = ["variance", "adjustment_movement"]


class StockAuditSerializer(BaseModelSerializer):
    lines = StockAuditLineSerializer(many=True, required=False)

    class Meta:
        model = StockAudit
        fields = [
            "id", "audit_number", "location", "period_month", "status",
            "conducted_by", "posted_at", "notes", "lines", "created_at", "updated_at",
        ]
        read_only_fields = ["audit_number", "posted_at", "created_at", "updated_at"]


# Hidden: QC out of scope
# class QualityStandardSerializer(BaseModelSerializer):
#     category = serializers.CharField(source="category.name", read_only=True)
#     categoryId = TenantPrimaryKeyRelatedField(
#         source="category", model="masters.ItemCategory", required=False, allow_null=True
#     )
#     checks = serializers.JSONField(source="checklist", required=False)
#     active = serializers.BooleanField(source="is_active", required=False)
#
#     class Meta:
#         model = QualityStandard
#         fields = [
#             "id", "name", "category", "categoryId", "checks", "tolerance_pct",
#             "active", "created_at", "updated_at",
#         ]




class ValuationRowSerializer(BaseSerializer):
    itemId = serializers.CharField()
    sku = serializers.CharField()
    name = serializers.CharField()
    category = serializers.CharField(allow_null=True)
    location = serializers.CharField(allow_null=True)
    quantity = QuantityField()
    unitCost = serializers.DecimalField(
        max_digits=18, decimal_places=4, coerce_to_string=False
    )
    value = serializers.DecimalField(max_digits=18, decimal_places=2, coerce_to_string=False)
    ageingBucket = serializers.CharField(allow_null=True)


class DemoUnitSerializer(BaseModelSerializer):
    itemId = TenantPrimaryKeyRelatedField(source="item", model="masters.Item")
    itemName = serializers.CharField(source="item.name", read_only=True)
    itemSku = serializers.CharField(source="item.sku", read_only=True)
    serialNumber = serializers.CharField(source="serial.serial_number", read_only=True, allow_null=True)
    serialId = TenantPrimaryKeyRelatedField(source="serial", model="masters.ItemSerial", required=False, allow_null=True)
    prospectPartyId = TenantPrimaryKeyRelatedField(source="prospect_party", model="masters.Party", required=False, allow_null=True)

    class Meta:
        model = DemoUnit
        fields = [
            "id", "demo_number", "itemId", "itemName", "itemSku", "serialId", "serialNumber",
            "prospect_name", "prospectPartyId", "contact_phone", "contact_email",
            "dispatch_date", "expected_return_date", "actual_return_date",
            "status", "inspection_notes", "condition_on_return", "sale_invoice_ref",
            "notes", "created_at", "updated_at",
        ]
        read_only_fields = ["demo_number", "created_at", "updated_at"]


class ReworkOrderSerializer(BaseModelSerializer):
    itemId = TenantPrimaryKeyRelatedField(source="item", model="masters.Item")
    itemName = serializers.CharField(source="item.name", read_only=True)
    itemSku = serializers.CharField(source="item.sku", read_only=True)
    goodsReceiptId = TenantPrimaryKeyRelatedField(source="goods_receipt", model="purchase.GoodsReceipt", required=False, allow_null=True)
    grnNumber = serializers.CharField(source="goods_receipt.grn_number", read_only=True, allow_null=True)

    class Meta:
        model = ReworkOrder
        fields = [
            "id", "rework_number", "itemId", "itemName", "itemSku", "goodsReceiptId", "grnNumber",
            "quantity", "defect_reason", "root_cause", "rework_labor_hours", "rework_cost",
            "scrap_qty", "scrap_rate_pct", "status", "assigned_technician", "notes",
            "created_at", "updated_at",
        ]
        read_only_fields = ["rework_number", "created_at", "updated_at"]


class ScrapLogSerializer(BaseModelSerializer):
    itemId = TenantPrimaryKeyRelatedField(source="item", model="masters.Item")
    itemName = serializers.CharField(source="item.name", read_only=True)
    reworkOrderId = TenantPrimaryKeyRelatedField(source="rework_order", model="inventory.ReworkOrder", required=False, allow_null=True)
    reworkNumber = serializers.CharField(source="rework_order.rework_number", read_only=True, allow_null=True)

    class Meta:
        model = ScrapLog
        fields = [
            "id", "scrap_number", "itemId", "itemName", "quantity", "scrap_reason",
            "estimated_loss", "reworkOrderId", "reworkNumber", "logged_at",
        ]
        read_only_fields = ["scrap_number", "logged_at"]


# Hidden: QC out of scope
# class QualityInspectionSerializer(BaseModelSerializer):
#     goodsReceiptId = TenantPrimaryKeyRelatedField(
#         source="goods_receipt", model="purchase.GoodsReceipt", required=False, allow_null=True
#     )
#     grnNumber = serializers.CharField(source="goods_receipt.grn_number", read_only=True, allow_null=True)
#     itemId = TenantPrimaryKeyRelatedField(source="item", model="masters.Item")
#     itemName = serializers.CharField(source="item.name", read_only=True)
#     itemSku = serializers.CharField(source="item.sku", read_only=True)
#     inspectorId = TenantPrimaryKeyRelatedField(
#         source="inspector", model="accounts.User", required=False, allow_null=True
#     )
#     inspectorName = serializers.CharField(source="inspector.get_full_name", read_only=True)
#     reworkOrderId = TenantPrimaryKeyRelatedField(
#         source="rework_order", model="inventory.ReworkOrder", required=False, allow_null=True
#     )
#     reworkNumber = serializers.CharField(source="rework_order.rework_number", read_only=True, allow_null=True)
#
#     class Meta:
#         model = QualityInspection
#         fields = [
#             "id", "inspection_number", "goodsReceiptId", "grnNumber",
#             "itemId", "itemName", "itemSku", "batch_lot_number",
#             "sample_size", "received_qty", "accepted_qty", "rejected_qty",
#             "status", "checklist_results", "inspectorId", "inspectorName",
#             "inspector_notes", "inspected_at", "reworkOrderId", "reworkNumber",
#             "created_at", "updated_at",
#         ]
#         read_only_fields = ["inspection_number", "created_at", "updated_at"]


