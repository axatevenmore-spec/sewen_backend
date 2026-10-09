"""Serializers for parties, items, categories, units, locations and BOM (api.md §4)."""
import re

from rest_framework import serializers

from apps.core.serializers import (
    BaseModelSerializer,
    BaseSerializer,
    MoneyField,
    QuantityField,
    TenantPrimaryKeyRelatedField,
)

from .models import (
    CategoryCustomField,
    CategoryPart,
    Item,
    ItemCategory,
    ItemPart,
    ItemSerial,
    Location,
    Party,
    PartyContact,
    Unit,
)


def slug_code(name, fallback):
    """A short, stable code derived from a name -- ``Mild Steel`` -> ``MILD-STEEL``.

    Category and location codes are labels the user reads, not numbers from the
    §1.7 series, so a missing one is filled in here rather than allocated.
    """
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", (name or "").strip()).strip("-").upper()
    return cleaned[:24] or fallback


# ---------------------------------------------------------------------------
# Parties (api.md §4.1)
# ---------------------------------------------------------------------------
class PartyContactSerializer(BaseModelSerializer):
    class Meta:
        model = PartyContact
        fields = ["id", "name", "role", "phone", "email", "is_primary"]


class PartySerializer(BaseModelSerializer):
    """The exact shape api.md §4.1 documents.

    ``balance`` is read-only: it is derived from the ledger and never written
    directly (db.md §4.1).
    """

    contacts = PartyContactSerializer(many=True, read_only=True)
    ledgerAccount = serializers.CharField(source="ledger_account.name", read_only=True)
    ledgerAccountId = TenantPrimaryKeyRelatedField(
        source="ledger_account", model="accounting.Account", required=False, allow_null=True
    )

    class Meta:
        model = Party
        fields = [
            "id", "code", "type", "name", "phone", "email",
            "gst_treatment", "gstin", "gst_notes", "place_of_supply",
            "tds_applicable", "tds_section", "tds_rate",
            "tcs_applicable", "tcs_rate",
            "ledgerAccount", "ledgerAccountId", "credit_limit", "payment_terms",
            "bank_account_number", "ifsc_code", "bank_name", "account_holder_name",
            "opening_balance", "balance",
            "billing_address", "shipping_address", "contacts", "status",
            "weight_tolerance_pct",
            "created_at", "updated_at",
        ]
        read_only_fields = ["balance", "created_at", "updated_at"]
        # api.md 1.7: the server allocates the CUST-/VEND- code in
        # ``PartyViewSet.perform_create``; the client must never invent one, so
        # it is optional on the wire rather than required.
        extra_kwargs = {"code": {"required": False, "allow_blank": True}}

    def validate_code(self, value):
        client_id = self.context.get("client_id")
        existing = Party.objects.filter(
            client_id=client_id, code=value, deleted_at__isnull=True
        )
        if self.instance is not None:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise serializers.ValidationError("A party with this code already exists.")
        return value


class PartySummarySerializer(BaseSerializer):
    """``GET /parties/{id}/summary/`` -- the Customer 360 drawer.

    Replaces ``Customer360Drawer``'s reduce over four context arrays, which
    under-reports the moment those lists are paginated
    (api-integration.md §9.1.3).
    """

    partyId = serializers.CharField()
    name = serializers.CharField()
    balance = MoneyField()
    creditLimit = MoneyField(allow_null=True)
    outstanding = MoneyField()
    lifetimeValue = MoneyField()
    openOrders = serializers.IntegerField()
    openInvoices = serializers.IntegerField()
    lastOrderDate = serializers.DateField(allow_null=True)
    lastInvoiceDate = serializers.DateField(allow_null=True)
    unallocatedAdvance = MoneyField()


# ---------------------------------------------------------------------------
# Categories, units, locations
# ---------------------------------------------------------------------------
class CategoryCustomFieldSerializer(BaseModelSerializer):
    class Meta:
        model = CategoryCustomField
        fields = ["id", "name", "type", "options", "required", "sort_order"]


class ItemCategorySerializer(BaseModelSerializer):
    customFields = CategoryCustomFieldSerializer(
        source="custom_fields", many=True, required=False
    )
    itemCount = serializers.SerializerMethodField()

    class Meta:
        model = ItemCategory
        fields = [
            "id", "name", "code", "kind", "description", "has_sub_parts",
            "lead_time_days", "default_hsn_code", "customFields", "itemCount",
            "created_at", "updated_at",
        ]
        extra_kwargs = {"code": {"required": False, "allow_blank": True}}

    def validate(self, attrs):
        """Derive the short code from the name when the client omits it."""
        attrs = super().validate(attrs)
        if not attrs.get("code") and not self.instance:
            attrs["code"] = slug_code(attrs.get("name"), "CAT")
        return attrs

    def get_itemCount(self, category):
        cached = getattr(category, "item_count", None)
        if cached is not None:
            return cached
        return category.items.filter(deleted_at__isnull=True).count()

    def create(self, validated_data):
        custom_fields = validated_data.pop("custom_fields", [])
        category = super().create(validated_data)
        self._sync_custom_fields(category, custom_fields)
        return category

    def update(self, instance, validated_data):
        custom_fields = validated_data.pop("custom_fields", None)
        category = super().update(instance, validated_data)
        if custom_fields is not None:
            self._sync_custom_fields(category, custom_fields)
        return category

    def _sync_custom_fields(self, category, rows):
        CategoryCustomField.objects.filter(category=category).delete()
        CategoryCustomField.objects.bulk_create(
            [
                CategoryCustomField(
                    client_id=category.client_id, category=category, **row
                )
                for row in rows
            ]
        )


class UnitSerializer(BaseModelSerializer):
    class Meta:
        model = Unit
        fields = ["id", "code", "label", "created_at"]


class LocationSerializer(BaseModelSerializer):
    class Meta:
        model = Location
        fields = [
            "id", "code", "name", "type", "parent", "address", "is_active",
            "created_at", "updated_at",
        ]
        extra_kwargs = {"code": {"required": False, "allow_blank": True}}

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if not attrs.get("code") and not self.instance:
            attrs["code"] = slug_code(attrs.get("name"), "LOC")
        return attrs


# ---------------------------------------------------------------------------
# Items (api.md §4.2)
# ---------------------------------------------------------------------------
class ItemSerializer(BaseModelSerializer):
    """The ``InventoryItem`` typedef plus the fields ``addInventoryItem`` adds.

    ``availableQty``, ``reservedQty`` and ``status`` are **not columns** -- they
    are joined on from the movement ledger at read time (db.md §4.2), so the
    JSON contract is unchanged while the source of truth moved.
    """

    category = serializers.CharField(source="category.name", read_only=True)
    categoryId = TenantPrimaryKeyRelatedField(
        source="category", queryset=ItemCategory.objects.all(), required=False, allow_null=True
    )
    vendor = serializers.CharField(source="vendor.name", read_only=True)
    vendorId = TenantPrimaryKeyRelatedField(
        source="vendor", queryset=Party.objects.all(), required=False, allow_null=True
    )
    location = serializers.CharField(source="default_location.name", read_only=True)
    locationId = TenantPrimaryKeyRelatedField(
        source="default_location", queryset=Location.objects.all(), required=False,
        allow_null=True,
    )
    unitConversionFactor = serializers.DecimalField(
        source="unit_conversion_factor", max_digits=18, decimal_places=6,
        coerce_to_string=False, required=False,
    )

    # -- derived, read-only -------------------------------------------------
    availableQty = serializers.SerializerMethodField()
    reservedQty = serializers.SerializerMethodField()
    onHandQty = serializers.SerializerMethodField()
    damagedQty = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    serialNumbers = serializers.SerializerMethodField()

    class Meta:
        model = Item
        fields = [
            "id", "sku", "name", "description", "category", "categoryId",
            "item_kind", "vendor", "vendorId",
            "uom", "purchase_unit", "sales_unit", "unitConversionFactor",
            "availableQty", "reservedQty", "onHandQty", "damagedQty",
            "reorder_level", "location", "locationId", "status", "lifecycle_status",
            "cost_price", "selling_price", "hsn_code", "tax_pct",
            "tracking_mode", "serialNumbers",
            "is_weight_item", "theoretical_weight", "weight_unit", "tolerance_pct",
            "metal_grade",
            "has_sheet_spec", "sheet_thickness", "sheet_thickness_unit",
            "sheet_height", "sheet_height_unit",
            "sheet_width", "sheet_width_unit", "sheet_length", "sheet_length_unit",
            "sheet_weight_kg", "dimension_unit",
            "has_tube_spec", "tube_profile", "outer_diameter", "outer_width",
            "outer_height", "wall_thickness", "tube_length", "weight_per_meter", "weight_per_piece",
            "custom_field_values",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def to_internal_value(self, data):
        data = dict(data)
        camel_map = {
            "metalGrade": "metal_grade",
            "hasSheetSpec": "has_sheet_spec",
            "sheetThickness": "sheet_thickness",
            "sheetThicknessUnit": "sheet_thickness_unit",
            "sheetHeight": "sheet_height",
            "sheetHeightUnit": "sheet_height_unit",
            "sheetWidth": "sheet_width",
            "sheetWidthUnit": "sheet_width_unit",
            "sheetLength": "sheet_length",
            "sheetLengthUnit": "sheet_length_unit",
            "sheetWeightKg": "sheet_weight_kg",
            "dimensionUnit": "dimension_unit",
            "hasTubeSpec": "has_tube_spec",
            "tubeProfile": "tube_profile",
            "outerDiameter": "outer_diameter",
            "outerWidth": "outer_width",
            "outerHeight": "outer_height",
            "wallThickness": "wall_thickness",
            "tubeLength": "tube_length",
            "weightPerMeter": "weight_per_meter",
            "weightPerPiece": "weight_per_piece",
            "isWeightItem": "is_weight_item",
            "theoreticalWeight": "theoretical_weight",
            "weightUnit": "weight_unit",
            "tolerancePct": "tolerance_pct",
            "costPrice": "cost_price",
            "sellingPrice": "selling_price",
            "hsnCode": "hsn_code",
            "taxPct": "tax_pct",
            "reorderLevel": "reorder_level",
            "trackingMode": "tracking_mode",
            "lifecycleStatus": "lifecycle_status",
            "itemKind": "item_kind",
        }
        for c_key, s_key in camel_map.items():
            if c_key in data and s_key not in data:
                data[s_key] = data[c_key]
        return super().to_internal_value(data)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["metalGrade"] = instance.metal_grade or ""
        data["hasSheetSpec"] = bool(instance.has_sheet_spec)
        data["sheetThickness"] = instance.sheet_thickness
        data["sheetThicknessUnit"] = instance.sheet_thickness_unit or "mm"
        data["sheetHeight"] = instance.sheet_height
        data["sheetWidth"] = instance.sheet_width
        data["sheetLength"] = instance.sheet_length
        data["sheetWeightKg"] = instance.sheet_weight_kg
        data["dimensionUnit"] = instance.dimension_unit or "mm"
        data["hasTubeSpec"] = bool(instance.has_tube_spec)
        data["tubeProfile"] = instance.tube_profile or ""
        data["outerDiameter"] = instance.outer_diameter
        data["outerWidth"] = instance.outer_width
        data["outerHeight"] = instance.outer_height
        data["wallThickness"] = instance.wall_thickness
        data["tubeLength"] = instance.tube_length
        data["weightPerMeter"] = instance.weight_per_meter
        data["weightPerPiece"] = instance.weight_per_piece
        data["theoreticalWeight"] = instance.theoretical_weight
        data["isWeightItem"] = bool(instance.is_weight_item)
        data["weightUnit"] = instance.weight_unit or "kg"
        data["tolerancePct"] = instance.tolerance_pct
        return data

    def get_availableQty(self, item):
        return getattr(item, "available_qty", None)

    def get_reservedQty(self, item):
        return getattr(item, "reserved_qty", None)

    def get_onHandQty(self, item):
        return getattr(item, "on_hand_qty", None)

    def get_damagedQty(self, item):
        return getattr(item, "damaged_qty", None)

    def get_status(self, item):
        return getattr(item, "stock_status", None)

    def get_serialNumbers(self, item):
        """Kept for contract compatibility; the source of truth is
        ``item_serials``, which an array could never carry (db.md §4.3)."""
        if item.tracking_mode != "Serial":
            return []
        cached = getattr(item, "_serial_numbers", None)
        if cached is not None:
            return cached
        return list(
            item.serials.filter(
                status__in=["available", "reserved"], deleted_at__isnull=True
            ).values_list("serial_no", flat=True)
        )

    def validate_sku(self, value):
        client_id = self.context.get("client_id")
        existing = Item.objects.filter(client_id=client_id, sku=value, deleted_at__isnull=True)
        if self.instance is not None:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise serializers.ValidationError("An item with this SKU already exists.")
        return value

    def validate(self, attrs):
        """api.md §4.2 -- weight items need a theoretical weight to be received
        against a weighbridge at all."""
        from .metal_calc import calculate_sheet_weight, calculate_tube_weight

        has_sheet = attrs.get("has_sheet_spec", getattr(self.instance, "has_sheet_spec", False))
        has_tube = attrs.get("has_tube_spec", getattr(self.instance, "has_tube_spec", False))
        metal_grade = attrs.get("metal_grade", getattr(self.instance, "metal_grade", "MS"))

        if has_sheet:
            length = attrs.get("sheet_length", getattr(self.instance, "sheet_length", None))
            width = attrs.get("sheet_width", getattr(self.instance, "sheet_width", None))
            thickness = attrs.get("sheet_thickness", getattr(self.instance, "sheet_thickness", None))
            if length and width and thickness:
                calc = calculate_sheet_weight(length, width, thickness, material_or_grade=metal_grade)
                if calc.get("is_valid"):
                    if not attrs.get("sheet_weight_kg"):
                        attrs["sheet_weight_kg"] = calc["weight_per_piece"]
                    if not attrs.get("weight_per_piece"):
                        attrs["weight_per_piece"] = calc["weight_per_piece"]
                    if attrs.get("is_weight_item") and not attrs.get("theoretical_weight"):
                        attrs["theoretical_weight"] = calc["weight_per_piece"]

        if has_tube:
            profile = attrs.get("tube_profile", getattr(self.instance, "tube_profile", "Round"))
            wall_t = attrs.get("wall_thickness", getattr(self.instance, "wall_thickness", None))
            length = attrs.get("tube_length", getattr(self.instance, "tube_length", None))
            od = attrs.get("outer_diameter", getattr(self.instance, "outer_diameter", None))
            ow = attrs.get("outer_width", getattr(self.instance, "outer_width", None))
            oh = attrs.get("outer_height", getattr(self.instance, "outer_height", None))
            if profile and wall_t:
                calc = calculate_tube_weight(
                    profile=profile,
                    wall_thickness_mm=wall_t,
                    length_mm=length,
                    outer_diameter_mm=od,
                    outer_width_mm=ow,
                    outer_height_mm=oh,
                    material_or_grade=metal_grade,
                )
                if calc.get("is_valid"):
                    if not attrs.get("weight_per_meter"):
                        attrs["weight_per_meter"] = calc.get("weight_per_meter")
                    if calc.get("weight_per_piece") and not attrs.get("weight_per_piece"):
                        attrs["weight_per_piece"] = calc.get("weight_per_piece")
                    if attrs.get("is_weight_item") and not attrs.get("theoretical_weight"):
                        attrs["theoretical_weight"] = calc.get("weight_per_piece") or calc.get("weight_per_meter")

        is_weight_item = attrs.get(
            "is_weight_item",
            self.instance.is_weight_item if self.instance else False,
        )
        theoretical = attrs.get(
            "theoretical_weight",
            self.instance.theoretical_weight if self.instance else None,
        )
        if is_weight_item and not theoretical:
            raise serializers.ValidationError(
                {"theoreticalWeight": ["Required for a weight-tracked item."]}
            )
        return attrs


class ItemStockSerializer(BaseSerializer):
    onHand = QuantityField()
    reserved = QuantityField()
    damaged = QuantityField()
    available = QuantityField()
    status = serializers.CharField()
    byLocation = serializers.ListField(child=serializers.DictField(), required=False)


class ItemSerialSerializer(BaseModelSerializer):
    class Meta:
        model = ItemSerial
        fields = [
            "id", "serial_no", "status", "batch_number", "location",
            "warranty_card", "created_at",
        ]


class ItemPartSerializer(BaseModelSerializer):
    """Machine BOM line."""

    partItemId = TenantPrimaryKeyRelatedField(
        source="part_item", queryset=Item.objects.all()
    )
    sku = serializers.CharField(source="part_item.sku", read_only=True)
    name = serializers.CharField(source="part_item.name", read_only=True)
    uom = serializers.CharField(source="part_item.uom", read_only=True)

    class Meta:
        model = ItemPart
        fields = ["id", "partItemId", "sku", "name", "uom", "required_qty"]


class CategoryPartSerializer(BaseModelSerializer):
    itemId = TenantPrimaryKeyRelatedField(source="item", queryset=Item.objects.all())
    sku = serializers.CharField(source="item.sku", read_only=True)
    name = serializers.CharField(source="item.name", read_only=True)

    class Meta:
        model = CategoryPart
        fields = ["id", "itemId", "sku", "name", "default_qty"]


class ImportRowsSerializer(BaseSerializer):
    """``POST /{module}/{entity}/import/`` (api.md §12.2).

    ``ImportModal`` parses the CSV in the browser and hands the page an array of
    row objects, so import is a JSON endpoint, not a multipart upload.
    """

    rows = serializers.ListField(child=serializers.DictField(), allow_empty=False)
    dryRun = serializers.BooleanField(required=False, default=False)
