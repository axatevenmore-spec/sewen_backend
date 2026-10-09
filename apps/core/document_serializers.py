"""
Shared serializer machinery for sales and purchase documents (db.md §3).

The twelve document types differ in their status machine and their upstream
FKs, not in how a line is written. This module holds the part that is genuinely
common: the frozen line snapshot, the BOM flags, the serial selection, and the
recompute-on-write rule of api.md §5.7.
"""
from rest_framework import serializers

from .serializers import BaseModelSerializer, TenantPrimaryKeyRelatedField

#: The frozen and computed line fields, in the order the UI renders them.
LINE_FIELDS = [
    "id", "line_no", "itemId", "sku", "itemName", "description", "hsn_code", "uom",
    "qty", "rate", "amount", "discount", "discount_amount", "tax", "tax_amount",
    "line_total", "serials",
    "isBomGenerated", "bomSourceItemId", "parentLineId", "isBomPart",
    "isUserModified", "parent_sku",
]

#: Header fields every document exposes.
HEADER_FIELDS = [
    "id", "partyId", "partyName", "isOneTimeParty", "partyType", "partyPhone", "partyEmail",
    "party_gstin", "billing_address",
    "shipping_address", "place_of_supply", "date", "notes", "terms",
    "subtotal", "total_discount", "taxable_value", "cgst", "sgst", "igst", "cess",
    "total_tax", "freight_charges", "other_charges", "round_off", "total",
    "amount_paid", "balance_due", "discount_override",
    "posted_at", "cancelled_at", "cancellation_reason",
    "lineItems", "created_at", "updated_at",
]

READ_ONLY_HEADER_FIELDS = [
    "subtotal", "total_discount", "taxable_value", "cgst", "sgst", "igst", "cess",
    "total_tax", "total", "amount_paid", "balance_due", "posted_at",
    "cancelled_at", "cancellation_reason", "created_at", "updated_at",
]


class DocumentLineSerializer(BaseModelSerializer):
    """One line of any sales or purchase document.

    Money fields are read-only outputs of the server's recompute: the client
    may send ``qty``, ``rate``, ``discount`` and ``tax``, and gets back
    ``amount``, ``taxAmount`` and ``lineTotal`` as the server computed them
    (api.md §5.7).
    """

    itemId = TenantPrimaryKeyRelatedField(
        source="item", model="masters.Item", required=False, allow_null=True
    )
    itemName = serializers.CharField(source="item_name", required=False, allow_null=True)
    # JSON names `discount` and `tax` map to the `_pct` columns (db.md §3.2).
    discount = serializers.DecimalField(
        source="discount_pct", max_digits=7, decimal_places=4,
        coerce_to_string=False, required=False,
    )
    tax = serializers.DecimalField(
        source="tax_pct", max_digits=7, decimal_places=4,
        coerce_to_string=False, required=False,
    )
    isBomGenerated = serializers.BooleanField(source="is_bom_generated", required=False)
    bomSourceItemId = TenantPrimaryKeyRelatedField(
        source="bom_source_item", model="masters.Item", required=False, allow_null=True
    )
    # Self-reference to a sibling line of the *same* document. Carried as an
    # opaque id rather than a relation: the concrete line model differs per
    # document type, and api-integration.md §10.2 already treats every id as an
    # opaque string. The parent line is resolved in the write path, where the
    # whole line set is present.
    parentLineId = serializers.UUIDField(
        source="parent_line_id", required=False, allow_null=True
    )
    isBomPart = serializers.SerializerMethodField()
    isUserModified = serializers.BooleanField(source="is_user_modified", required=False)
    serials = serializers.ListField(
        child=serializers.CharField(), required=False, allow_empty=True
    )

    class Meta:
        fields = LINE_FIELDS
        read_only_fields = ["amount", "discount_amount", "tax_amount", "line_total"]

    def get_isBomPart(self, line):
        """db.md §3.2 -- derived as ``parent_line_id is not null``."""
        return line.parent_line_id is not None


class DocumentSerializer(BaseModelSerializer):
    """Header + nested lines, with the line write path handled once.

    Subclasses declare ``line_model``, ``line_serializer`` and
    ``line_fk_name``; everything else -- freezing the party snapshot, writing
    the lines, recomputing the totals and persisting serial selections -- is
    the same for every document.
    """

    partyId = TenantPrimaryKeyRelatedField(
        source="party", model="masters.Party", required=False, allow_null=True
    )
    partyName = serializers.CharField(source="party_name", required=False, allow_blank=True, allow_null=True)
    isOneTimeParty = serializers.BooleanField(source="is_one_time_party", required=False, default=False)
    partyType = serializers.CharField(source="party_type", required=False, allow_blank=True, allow_null=True)
    partyPhone = serializers.CharField(source="party_phone", required=False, allow_blank=True, allow_null=True)
    partyEmail = serializers.EmailField(source="party_email", required=False, allow_blank=True, allow_null=True)
    date = serializers.DateField(source="doc_date")
    balance_due = serializers.SerializerMethodField()
    lineItems = serializers.ListField(
        child=serializers.DictField(), required=False, write_only=True
    )

    #: Overridden by each concrete document.
    line_model = None
    line_serializer = None
    line_fk_name = None
    line_table_name = None
    #: Quotation-first sales: a line may be an inventory item, or a service /
    #: fabrication / custom / free-text line with no item at all. Such a line
    #: is identified only by its text, so documents that opt in require it.
    #: Never satisfied by inventing an item -- ``itemId`` stays optional.
    require_line_description = False

    def get_balance_due(self, document):
        return document.balance_due

    def to_internal_value(self, data):
        data = dict(data)
        if "billingAddress" in data and "billing_address" not in data:
            data["billing_address"] = data["billingAddress"]
        if "shippingAddress" in data and "shipping_address" not in data:
            data["shipping_address"] = data["shippingAddress"]
        if "placeOfSupply" in data and "place_of_supply" not in data:
            data["place_of_supply"] = data["placeOfSupply"]
        if "partyGstin" in data and "party_gstin" not in data:
            data["party_gstin"] = data["partyGstin"]
        return super().to_internal_value(data)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        is_one_time = attrs.get("is_one_time_party", getattr(self.instance, "is_one_time_party", False))
        party = attrs.get("party", getattr(self.instance, "party", None))
        party_name = (attrs.get("party_name") or getattr(self.instance, "party_name", "") or "").strip()

        if is_one_time:
            if not party_name and not (party and party.name):
                from .exceptions import ValidationFailed
                raise ValidationFailed(
                    "Party name is required for one-time parties.",
                    field_errors={"partyName": ["Required."]},
                )
        elif not party and not party_name and self.instance is None:
            from .exceptions import ValidationFailed
            raise ValidationFailed(
                "Either a registered customer or party name is required.",
                field_errors={"partyId": ["Required."]},
            )
        return attrs

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data.pop("lineItems", None)
        lines = getattr(instance, "_prefetched_lines", None)
        if lines is None:
            lines = instance.line_items.filter(deleted_at__isnull=True).order_by("line_no")
        serialized = self.line_serializer(lines, many=True, context=self.context).data
        self._attach_serials(instance, lines, serialized)
        data["lineItems"] = serialized

        # Ensure compatibility aliases for frontend
        data["partyName"] = instance.party_name or (instance.party.name if instance.party else "")
        data["partyGstin"] = instance.party_gstin or (instance.party.gstin if instance.party else "")
        data["isOneTimeParty"] = bool(instance.is_one_time_party)
        data["partyType"] = instance.party_type or (instance.party.type if instance.party else "")
        data["partyPhone"] = instance.party_phone or (instance.party.phone if instance.party else "")
        data["partyEmail"] = instance.party_email or (instance.party.email if instance.party else "")
        data["billingAddress"] = instance.billing_address or {}
        data["shippingAddress"] = instance.shipping_address or {}
        data["placeOfSupply"] = instance.place_of_supply or ""
        return data

    def _attach_serials(self, instance, lines, serialized):
        if not self.line_table_name:
            return
        from apps.inventory.services import serials_for_lines

        line_ids = [line.id for line in lines]
        if not line_ids:
            return
        grouped = serials_for_lines(instance.client_id, self.line_table_name, line_ids)
        for line, payload in zip(lines, serialized):
            payload["serials"] = grouped.get(line.id, [])

    #: Header amounts that only balance once the lines exist. The db.md §3.1
    #: total check is a plain CHECK constraint, which Postgres evaluates per
    #: statement and cannot defer -- so a header must never be written with
    #: charges before its totals have been computed. These are held back and
    #: applied during the recompute.
    DEFERRED_AMOUNT_FIELDS = (
        "freight_charges",
        "other_charges",
        "round_off",
        "discount_override",
    )

    # -- write path ---------------------------------------------------------
    def create(self, validated_data):
        line_payloads = validated_data.pop("lineItems", [])
        deferred = {
            field: validated_data.pop(field)
            for field in self.DEFERRED_AMOUNT_FIELDS
            if field in validated_data
        }

        document = super().create(validated_data)
        document.freeze_party_snapshot()
        document.save()

        self._write_lines(document, line_payloads, replace=False)

        for field, value in deferred.items():
            setattr(document, field, value)
        self._recalculate(document)
        return document

    def update(self, instance, validated_data):
        line_payloads = validated_data.pop("lineItems", None)
        document = super().update(instance, validated_data)
        if line_payloads is not None:
            self._write_lines(document, line_payloads, replace=True)
        self._recalculate(document)
        return document

    def _write_lines(self, document, payloads, replace):
        from apps.inventory.services import link_line_serials, resolve_serials

        if replace:
            # Drafts cascade; posted documents are never edited (db.md §3.2).
            document.line_items.all().delete()

        for index, payload in enumerate(payloads or [], start=1):
            line_serializer = self.line_serializer(
                data=payload, context=self.context
            )
            line_serializer.is_valid(raise_exception=True)
            data = dict(line_serializer.validated_data)
            serial_numbers = data.pop("serials", [])
            data.setdefault("line_no", index)
            if self.require_line_description:
                self._check_line_text(data, index)

            line = self.line_model(
                client_id=document.client_id,
                **{self.line_fk_name: document},
                **data,
            )
            line.freeze_item_snapshot()
            line.save()

            if serial_numbers and self.line_table_name:
                resolved = resolve_serials(
                    document.client_id, line.item_id, serial_numbers, expected_status=None
                )
                link_line_serials(
                    document.client_id, self.line_table_name, line.id, resolved
                )

    @staticmethod
    def _check_line_text(data, index):
        """A line without an item is named by its description (or item name).

        The two are mirrored so every screen and print view, which read one or
        the other, shows the same text for a custom line.
        """
        if data.get("item") is not None:
            return
        description = (data.get("description") or "").strip()
        item_name = (data.get("item_name") or "").strip()
        if not description and not item_name:
            from .exceptions import ValidationFailed

            raise ValidationFailed(
                f"Line {index} needs a description or an inventory item.",
                field_errors={f"lineItems[{index - 1}].description": ["Required."]},
            )
        data["description"] = description or item_name
        data["item_name"] = item_name or description

    def _recalculate(self, document):
        from apps.sales.services import assert_client_totals_match, recalculate_document

        recalculate_document(document)
        request = self.context.get("request")
        if request is not None and isinstance(request.data, dict):
            assert_client_totals_match(document, request.data.get("totals"))
