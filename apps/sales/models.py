"""
Sales (db.md §5, api.md §5).

The pipeline, walkable both ways through nullable upstream FKs:

    Estimate -> Quotation -> Sales Order -> [Proforma] -> Challan -> Invoice -> Payment In
                                                |                       |
                                          Warranty Card           Sales Return

Line-level links matter as much as header links: ``sales_invoice_lines
.sales_order_line`` and ``delivery_challan_lines.sales_order_line`` are what
make partial conversion and double-depletion avoidance tractable (db.md §5.1).
"""
from django.db import models

from apps.core.documents import (
    DocumentHeader,
    DocumentLine,
    number_unique_constraint,
    total_check_constraint,
)
from apps.core.models import LegacyIdMixin, TenantModel

# --- api.md Appendix A -------------------------------------------------------
ESTIMATE_STATUSES = ["Draft", "Sent", "Accepted", "Rejected", "Converted", "Expired"]
QUOTATION_STATUSES = [
    "Draft", "Sent", "Viewed", "Accepted", "Rejected", "Expired",
    "Confirmed", "Converted", "Invoiced", "Cancelled",
]
ORDER_STAGES = [
    "Draft", "Confirmed", "Packing", "Dispatched", "Delivered", "Invoiced", "Cancelled",
]
#: Stages whose open lines no longer reserve stock (api.md §5.4, db.md §5.2).
NON_RESERVING_STAGES = ["Delivered", "Invoiced", "Cancelled"]
PAYMENT_STATUSES = ["Unpaid", "Partially Paid", "Paid"]
PROFORMA_STATUSES = ["Draft", "Sent", "Accepted", "Converted", "Expired", "Cancelled"]
CHALLAN_STATUSES = [
    "Draft", "Dispatched", "In Transit", "Out for Delivery", "Delivered", "Cancelled",
]
#: A challan in any of these has moved its stock (``dispatch_challan`` ran).
#: In carrier order -- the challan tracker only ever moves forward through them.
CHALLAN_SHIPPED_STATUSES = ["Dispatched", "In Transit", "Out for Delivery", "Delivered"]
INVOICE_STATUSES = ["Draft", "Unpaid", "Partially Paid", "Paid", "Cancelled"]
# The strings the UI's <select> actually emits (api.md §0: where code and
# spec disagree, the frontend code is the fact). PaymentInPage, PaymentOutPage,
# PurchaseBillsPage and SalesInvoicesView between them offer all of these.
PAYMENT_MODES = [
    "Cash", "Bank", "UPI", "Cheque", "Card",
    "Bank Transfer", "Bank Wire", "ACH", "Corporate Card",
]


def choices(values):
    return [(value, value) for value in values]


#: What a sales line is, for a metal / fabrication business (Sweven spec §2.1:
#: "professional quotes for machines and spare parts", plus MS table
#: fabrication). Free of any inventory item: a Fabrication or Service line has
#: no item at all.
# Replaced (sheet-metal sales): Sweven's work is steel / iron sheet, plate and
# section, so those lead; the earlier kinds stay valid for existing rows.
# LINE_KINDS = ["Machine", "Spare Part", "Fabrication", "Service", "Custom"]
LINE_KINDS = [
    "Sheet Metal", "Section / Pipe", "Fabrication",
    "Machine", "Spare Part", "Service", "Custom",
]


class MetalLineFields(models.Model):
    """Optional metal-industry detail on a sales document line.

    Carried through every conversion (estimate -> quotation -> order ->
    challan / invoice) so what was quoted is what is billed. All optional:
    an inventory line without them behaves exactly as before.
    """

    line_kind = models.TextField(choices=choices(LINE_KINDS), null=True, blank=True)
    #: e.g. "MS IS 2062", "SS 304".
    material_grade = models.TextField(null=True, blank=True)
    #: Size / section / thickness / finish, e.g. "1800x900x750 mm, 40x40 pipe, powder coated".
    specification = models.TextField(null=True, blank=True)
    #: Weight of one unit in kg -- total weight is qty x unit_weight.
    unit_weight = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    #: The sheet-metal calculator behind the line, as the form captured it:
    #: material, grade, form, finish, thickness / width / length (mm), kg per
    #: metre, pieces, weight per piece and billing basis ("kg" or "pcs").
    #: A record of how qty was worked out -- qty, rate and the totals stay the
    #: columns the server computes from.
    sheet_spec = models.JSONField(null=True, blank=True)

    class Meta:
        abstract = True


#: The columns above, for the conversions that copy a line field by field.
# Replaced: + sheet_spec.
# METAL_LINE_FIELDS = ["line_kind", "material_grade", "specification", "unit_weight"]
METAL_LINE_FIELDS = ["line_kind", "material_grade", "specification", "unit_weight", "sheet_spec"]


# ---------------------------------------------------------------------------
# Estimates (api.md §5.2)
# ---------------------------------------------------------------------------
class Estimate(DocumentHeader):
    estimate_number = models.TextField(null=True, blank=True)
    status = models.TextField(choices=choices(ESTIMATE_STATUSES), default="Draft")
    valid_until = models.DateField(null=True, blank=True)
    converted_quotation = models.ForeignKey(
        "Quotation", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    crm_lead = models.ForeignKey(
        "crm.Lead", null=True, blank=True, on_delete=models.SET_NULL, related_name="estimates"
    )

    class Meta:
        db_table = "estimates"
        ordering = ["-doc_date", "-created_at"]
        constraints = [
            number_unique_constraint("estimates", "estimate_number"),
            total_check_constraint("estimates"),
        ]

    def __str__(self):
        return self.estimate_number or f"Estimate {self.id}"


# Replaced (metal-industry sales): + MetalLineFields.
# class EstimateLine(DocumentLine):
class EstimateLine(DocumentLine, MetalLineFields):
    estimate = models.ForeignKey(Estimate, on_delete=models.CASCADE, related_name="line_items")

    class Meta(DocumentLine.Meta):
        db_table = "estimate_lines"
        constraints = [
            models.UniqueConstraint(fields=["estimate", "line_no"], name="uq_estimate_line_no")
        ]


# ---------------------------------------------------------------------------
# Quotations (api.md §5.3)
# ---------------------------------------------------------------------------
class Quotation(DocumentHeader):
    quotation_number = models.TextField(null=True, blank=True)
    status = models.TextField(choices=choices(QUOTATION_STATUSES), default="Draft")
    valid_until = models.DateField(null=True, blank=True)
    estimate = models.ForeignKey(
        Estimate, null=True, blank=True, on_delete=models.SET_NULL, related_name="quotations"
    )
    crm_deal = models.ForeignKey(
        "crm.Deal", null=True, blank=True, on_delete=models.SET_NULL, related_name="quotations"
    )
    crm_lead = models.ForeignKey(
        "crm.Lead", null=True, blank=True, on_delete=models.SET_NULL, related_name="quotations"
    )
    subject = models.TextField(null=True, blank=True)
    # Quotation-first sales: the commercial header the printed quotation
    # carries. Free text, because a quotation is often raised before the
    # salesperson or signatory exists as a user in this workspace.
    reference_number = models.TextField(null=True, blank=True)
    salesperson = models.TextField(null=True, blank=True)
    payment_terms = models.TextField(null=True, blank=True)
    delivery_terms = models.TextField(null=True, blank=True)
    authorized_person = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "quotations"
        ordering = ["-doc_date", "-created_at"]
        constraints = [
            number_unique_constraint("quotations", "quotation_number"),
            total_check_constraint("quotations"),
        ]

    def __str__(self):
        return self.quotation_number or f"Quotation {self.id}"


# Replaced (metal-industry sales): + MetalLineFields.
# class QuotationLine(DocumentLine):
class QuotationLine(DocumentLine, MetalLineFields):
    quotation = models.ForeignKey(Quotation, on_delete=models.CASCADE, related_name="line_items")

    class Meta(DocumentLine.Meta):
        db_table = "quotation_lines"
        constraints = [
            models.UniqueConstraint(fields=["quotation", "line_no"], name="uq_quotation_line_no")
        ]


class QuotationShare(TenantModel):
    """db.md §5.3 -- replaces ``services/localQuotationSharing.js``.

    Tokens are opaque random bytes, hashed at rest, single-quotation, expiring
    and revocable. The base64 encoder they replace was a security placeholder.
    """

    quotation = models.ForeignKey(Quotation, on_delete=models.CASCADE, related_name="shares")
    token_hash = models.TextField(unique=True)
    recipients = models.JSONField(default=list, blank=True)
    channel = models.TextField(null=True, blank=True)  # email | whatsapp | link
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        db_table = "quotation_shares"
        ordering = ["-created_at"]


class QuotationActivity(models.Model):
    """Every public view writes a row -- ``GET .../activity/`` reads them."""

    EVENTS = [
        ("sent", "sent"),
        ("viewed", "viewed"),
        ("opened", "opened"),
        ("accepted", "accepted"),
        ("rejected", "rejected"),
        ("commented", "commented"),
        ("revoked", "revoked"),
    ]

    id = models.BigAutoField(primary_key=True)
    quotation = models.ForeignKey(
        Quotation, on_delete=models.CASCADE, related_name="activity"
    )
    share = models.ForeignKey(
        QuotationShare, null=True, blank=True, on_delete=models.SET_NULL, related_name="activity"
    )
    event = models.TextField(choices=EVENTS)
    actor_label = models.TextField(null=True, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(null=True, blank=True)
    comment = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "quotation_activity"
        ordering = ["-created_at"]


class SalesApprovalLink(TenantModel):
    """A customer approval link for an estimate, quotation, proforma or invoice.

    The sales counterpart of ``pms.ProofShare`` (``/sales/approve/:token``
    outside the app shell): opaque token hashed at rest, expiring, revocable,
    one decision per link. ``decided_by`` is text -- the customer has no
    account. The document is addressed by ``doc_type`` + ``document_id`` so one
    table serves the four document types (apps/sales/approval_links.py).
    """

    DOC_TYPES = [
        ("estimate", "Estimate"),
        ("quotation", "Quotation"),
        ("proforma_invoice", "Proforma invoice"),
        ("sales_invoice", "Sales invoice"),
    ]
    STATUSES = [("Active", "Active"), ("Revoked", "Revoked"), ("Expired", "Expired")]
    DECISIONS = [("Approved", "Approved"), ("Rejected", "Rejected")]

    doc_type = models.TextField(choices=DOC_TYPES)
    document_id = models.UUIDField()
    #: The number as it was when shared, for lists and notifications.
    document_number = models.TextField(null=True, blank=True)
    token_hash = models.TextField(unique=True)
    recipient_name = models.TextField(null=True, blank=True)
    recipient_email = models.EmailField(null=True, blank=True)
    #: The covering note shown to the customer above the document.
    message = models.TextField(null=True, blank=True)
    status = models.TextField(choices=STATUSES, default="Active")
    decision = models.TextField(choices=DECISIONS, null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.TextField(null=True, blank=True)
    decision_comments = models.TextField(null=True, blank=True)
    rejection_reason = models.TextField(null=True, blank=True)
    opened_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_reason = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "sales_approval_links"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["doc_type", "document_id"], name="ix_sales_approval_links_doc"),
        ]


class SalesApprovalComment(TenantModel):
    """The review thread on one shared document, written by staff and customer."""

    AUTHOR_TYPES = [("Staff", "Staff"), ("Client", "Client")]

    doc_type = models.TextField(choices=SalesApprovalLink.DOC_TYPES)
    document_id = models.UUIDField()
    link = models.ForeignKey(
        SalesApprovalLink, null=True, blank=True, on_delete=models.SET_NULL, related_name="comments"
    )
    author_type = models.TextField(choices=AUTHOR_TYPES, default="Staff")
    author_name = models.TextField(null=True, blank=True)
    text = models.TextField()

    class Meta:
        db_table = "sales_approval_comments"
        ordering = ["created_at"]
        indexes = [
            models.Index(
                fields=["doc_type", "document_id", "created_at"],
                name="ix_sales_approval_comments",
            ),
        ]


# ---------------------------------------------------------------------------
# Sales orders (api.md §5.4)
# ---------------------------------------------------------------------------
class SalesOrder(DocumentHeader):
    order_number = models.TextField(null=True, blank=True)
    stage = models.TextField(choices=choices(ORDER_STAGES), default="Draft")
    #: DERIVED from payment_allocations vs invoice totals (db.md §12).
    payment_status = models.TextField(choices=choices(PAYMENT_STATUSES), default="Unpaid")
    delivery_date = models.DateField(null=True, blank=True)
    quotation = models.ForeignKey(
        Quotation, null=True, blank=True, on_delete=models.SET_NULL, related_name="orders"
    )
    pms_project = models.ForeignKey(
        "pms.Project", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    reference_number = models.TextField(null=True, blank=True)
    total_sales_value = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    formal_invoice_amount = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    cash_amount = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)

    class Meta:
        db_table = "sales_orders"
        ordering = ["-doc_date", "-created_at"]
        constraints = [
            number_unique_constraint("sales_orders", "order_number"),
            total_check_constraint("sales_orders"),
        ]
        indexes = [models.Index(fields=["client", "stage"], name="ix_sales_orders_stage")]

    def __str__(self):
        return self.order_number or f"Sales order {self.id}"

    @property
    def reserves_stock(self):
        return self.stage not in NON_RESERVING_STAGES and self.deleted_at is None


# Replaced (metal-industry sales): + MetalLineFields.
# class SalesOrderLine(DocumentLine):
class SalesOrderLine(DocumentLine, MetalLineFields):
    sales_order = models.ForeignKey(
        SalesOrder, on_delete=models.CASCADE, related_name="line_items"
    )
    #: DERIVED from challan / invoice lines in the conversion transaction.
    dispatched_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    invoiced_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)

    class Meta(DocumentLine.Meta):
        db_table = "sales_order_lines"
        constraints = [
            models.UniqueConstraint(
                fields=["sales_order", "line_no"], name="uq_sales_order_line_no"
            ),
            # The database half of api.md §5.4's "over-dispatch or over-invoice
            # returns 422". The handler returns the friendly error; the
            # constraint guarantees no concurrent pair of requests slips past.
            models.CheckConstraint(
                condition=models.Q(dispatched_qty__lte=models.F("qty")),
                name="ck_sol_dispatch",
            ),
            models.CheckConstraint(
                condition=models.Q(invoiced_qty__lte=models.F("qty")), name="ck_sol_invoice"
            ),
        ]
        indexes = [
            models.Index(fields=["item", "sales_order"], name="ix_sales_order_lines_item")
        ]

    @property
    def pending_qty(self):
        return (self.qty or 0) - (self.dispatched_qty or 0)


# ---------------------------------------------------------------------------
# Proforma invoices (api.md §5.5)
# ---------------------------------------------------------------------------
class ProformaInvoice(DocumentHeader):
    """Moves no stock and posts no ledger entry -- advance collection only."""

    proforma_number = models.TextField(null=True, blank=True)
    status = models.TextField(choices=choices(PROFORMA_STATUSES), default="Draft")
    valid_until = models.DateField(null=True, blank=True)
    sales_order = models.ForeignKey(
        SalesOrder, null=True, blank=True, on_delete=models.SET_NULL, related_name="proformas"
    )
    total_sales_value = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    formal_invoice_amount = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    cash_amount = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)

    class Meta:
        db_table = "proforma_invoices"
        ordering = ["-doc_date", "-created_at"]
        constraints = [
            number_unique_constraint("proforma_invoices", "proforma_number"),
            total_check_constraint("proforma_invoices"),
        ]


# Replaced (metal-industry sales): + MetalLineFields.
# class ProformaInvoiceLine(DocumentLine):
class ProformaInvoiceLine(DocumentLine, MetalLineFields):
    proforma_invoice = models.ForeignKey(
        ProformaInvoice, on_delete=models.CASCADE, related_name="line_items"
    )
    sales_order_line = models.ForeignKey(
        SalesOrderLine, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta(DocumentLine.Meta):
        db_table = "proforma_invoice_lines"
        constraints = [
            models.UniqueConstraint(
                fields=["proforma_invoice", "line_no"], name="uq_proforma_line_no"
            )
        ]


# ---------------------------------------------------------------------------
# Delivery challans (api.md §5.6)
# ---------------------------------------------------------------------------
class DeliveryChallan(DocumentHeader):
    """Dispatching posts ``SALE`` movements and consumes the selected serials."""

    challan_number = models.TextField(null=True, blank=True)
    status = models.TextField(choices=choices(CHALLAN_STATUSES), default="Draft")
    sales_order = models.ForeignKey(
        SalesOrder, null=True, blank=True, on_delete=models.SET_NULL, related_name="challans"
    )
    quotation = models.ForeignKey(
        Quotation, null=True, blank=True, on_delete=models.SET_NULL, related_name="challans"
    )
    dispatch_date = models.DateField(null=True, blank=True)
    vehicle_number = models.TextField(null=True, blank=True)
    transporter = models.TextField(null=True, blank=True)
    lr_number = models.TextField(null=True, blank=True)
    delivery_location = models.TextField(null=True, blank=True)
    location = models.ForeignKey(
        "masters.Location", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    delivered_at = models.DateTimeField(null=True, blank=True)
    # -- weighbridge (steel is dispatched and billed by weight) ---------------
    weighbridge_slip = models.TextField(null=True, blank=True)
    #: kg. Net is DERIVED as gross - tare when both are given.
    gross_weight = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    tare_weight = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    net_weight = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)

    class Meta:
        db_table = "delivery_challans"
        ordering = ["-doc_date", "-created_at"]
        constraints = [
            number_unique_constraint("delivery_challans", "challan_number"),
            total_check_constraint("delivery_challans"),
        ]

    def __str__(self):
        return self.challan_number or f"Challan {self.id}"


# Replaced (metal-industry sales): + MetalLineFields.
# class DeliveryChallanLine(DocumentLine):
class DeliveryChallanLine(DocumentLine, MetalLineFields):
    delivery_challan = models.ForeignKey(
        DeliveryChallan, on_delete=models.CASCADE, related_name="line_items"
    )
    sales_order_line = models.ForeignKey(
        SalesOrderLine, null=True, blank=True, on_delete=models.SET_NULL, related_name="challan_lines"
    )

    class Meta(DocumentLine.Meta):
        db_table = "delivery_challan_lines"
        constraints = [
            models.UniqueConstraint(
                fields=["delivery_challan", "line_no"], name="uq_challan_line_no"
            )
        ]


# ---------------------------------------------------------------------------
# Sales invoices (api.md §5.7)
# ---------------------------------------------------------------------------
class SalesInvoice(DocumentHeader):
    """``Overdue`` is derived from ``due_date`` and outstanding at read time,
    never stored (api.md §5.7, db.md §12)."""

    invoice_number = models.TextField(null=True, blank=True)
    status = models.TextField(choices=choices(INVOICE_STATUSES), default="Draft")
    due_date = models.DateField(null=True, blank=True)
    sales_order = models.ForeignKey(
        SalesOrder, null=True, blank=True, on_delete=models.SET_NULL, related_name="invoices"
    )
    delivery_challan = models.ForeignKey(
        DeliveryChallan, null=True, blank=True, on_delete=models.SET_NULL, related_name="invoices"
    )
    proforma_invoice = models.ForeignKey(
        ProformaInvoice, null=True, blank=True, on_delete=models.SET_NULL, related_name="invoices"
    )
    location = models.ForeignKey(
        "masters.Location", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    journal_entry = models.ForeignKey(
        "accounting.JournalEntry", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    irn = models.TextField(null=True, blank=True)
    eway_bill_number = models.TextField(null=True, blank=True)
    total_sales_value = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    cash_amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    original_items = models.JSONField(null=True, blank=True, default=list)

    class Meta:
        db_table = "sales_invoices"
        ordering = ["-doc_date", "-created_at"]
        constraints = [
            number_unique_constraint("sales_invoices", "invoice_number"),
            total_check_constraint("sales_invoices"),
        ]
        indexes = [
            models.Index(fields=["client", "status"], name="ix_sales_invoices_status"),
            models.Index(fields=["client", "-doc_date", "id"], name="ix_sales_invoices_date"),
        ]

    def __str__(self):
        return self.invoice_number or f"Invoice {self.id}"


# Replaced (metal-industry sales): + MetalLineFields.
# class SalesInvoiceLine(DocumentLine):
class SalesInvoiceLine(DocumentLine, MetalLineFields):
    sales_invoice = models.ForeignKey(
        SalesInvoice, on_delete=models.CASCADE, related_name="line_items"
    )
    sales_order_line = models.ForeignKey(
        SalesOrderLine, null=True, blank=True, on_delete=models.SET_NULL, related_name="invoice_lines"
    )
    delivery_challan_line = models.ForeignKey(
        DeliveryChallanLine,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="invoice_lines",
    )
    #: DERIVED -- how much of this line has come back on a credit note.
    returned_qty = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    original_rate = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    original_amount = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    original_line_total = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)

    class Meta(DocumentLine.Meta):
        db_table = "sales_invoice_lines"
        constraints = [
            models.UniqueConstraint(
                fields=["sales_invoice", "line_no"], name="uq_invoice_line_no"
            ),
            models.CheckConstraint(
                condition=models.Q(returned_qty__lte=models.F("qty")), name="ck_sil_returned"
            ),
        ]


class SalesInvoiceRevision(TenantModel):
    """Preserves history of Sales Invoice / Cash Receipt allocation revisions (api.md §5.7)."""

    invoice = models.ForeignKey(
        SalesInvoice, on_delete=models.CASCADE, related_name="revisions"
    )
    cash_receipt = models.ForeignKey(
        "CashPaymentReceipt", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    revision_number = models.PositiveIntegerField(default=1)
    old_invoice_amount = models.DecimalField(max_digits=18, decimal_places=2)
    new_invoice_amount = models.DecimalField(max_digits=18, decimal_places=2)
    old_cash_amount = models.DecimalField(max_digits=18, decimal_places=2)
    new_cash_amount = models.DecimalField(max_digits=18, decimal_places=2)
    difference = models.DecimalField(max_digits=18, decimal_places=2)
    reason = models.TextField(null=True, blank=True)
    changed_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "sales_invoice_revisions"
        ordering = ["revision_number", "changed_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["invoice", "revision_number"],
                name="uq_sales_invoice_revision_number",
            )
        ]

    def __str__(self):
        return f"{self.invoice} Rev {self.revision_number}"


# ---------------------------------------------------------------------------
# Payments in and allocation (db.md §5.4)
# ---------------------------------------------------------------------------
class PaymentIn(TenantModel, LegacyIdMixin):
    payment_number = models.TextField()
    party = models.ForeignKey("masters.Party", on_delete=models.PROTECT, related_name="payments_in")
    payment_date = models.DateField()
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    mode = models.TextField(choices=choices(PAYMENT_MODES), default="Bank")
    bank_account = models.ForeignKey(
        "accounting.BankAccount", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    reference_number = models.TextField(null=True, blank=True)
    notes = models.TextField(null=True, blank=True)
    description = models.TextField(null=True, blank=True)
    payment_type = models.TextField(
        choices=[("WITH_BILL", "With Bill"), ("WITHOUT_BILL", "Without Bill / Cash")],
        default="WITH_BILL",
    )
    invoice = models.ForeignKey(
        "SalesInvoice", null=True, blank=True, on_delete=models.SET_NULL, related_name="payments"
    )
    sales_order = models.ForeignKey(
        "SalesOrder", null=True, blank=True, on_delete=models.SET_NULL, related_name="cash_receipts"
    )
    proforma_invoice = models.ForeignKey(
        "ProformaInvoice", null=True, blank=True, on_delete=models.SET_NULL, related_name="cash_receipts"
    )
    #: DERIVED from payment_allocations. The unallocated remainder *is* the
    #: customer advance -- there is no separate balance column (db.md §5.4).
    allocated_amount = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    status = models.TextField(
        choices=[("Active", "Active"), ("Cancelled", "Cancelled")], default="Active"
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.TextField(null=True, blank=True)
    journal_entry = models.ForeignKey(
        "accounting.JournalEntry", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        db_table = "payments_in"
        ordering = ["-payment_date", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "payment_number"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_payments_in_number",
            ),
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="ck_payments_in_amount"),
            models.CheckConstraint(
                condition=models.Q(allocated_amount__lte=models.F("amount")),
                name="ck_payments_in_alloc",
            ),
        ]

    def __str__(self):
        return self.payment_number

    @property
    def unallocated_amount(self):
        return (self.amount or 0) - (self.allocated_amount or 0)


class PaymentAllocation(TenantModel):
    """One allocation table for both sides (db.md §5.4).

    Keeps outstanding calculations single-sourced: ``sales_invoices
    .amount_paid``, customer advances and vendor advances are all queries over
    this table rather than parallel balance columns.
    """

    SIDES = [("in", "in"), ("out", "out")]
    DOCUMENT_TYPES = [
        ("SalesInvoice", "SalesInvoice"),
        ("PurchaseBill", "PurchaseBill"),
        ("SalesReturn", "SalesReturn"),
        ("PurchaseReturn", "PurchaseReturn"),
    ]

    payment_id = models.UUIDField()
    payment_side = models.TextField(choices=SIDES)
    document_type = models.TextField(choices=DOCUMENT_TYPES)
    document_id = models.UUIDField()
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    allocated_at = models.DateTimeField(auto_now_add=True)
    allocated_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        db_table = "payment_allocations"
        constraints = [
            models.UniqueConstraint(
                fields=["payment_side", "payment_id", "document_type", "document_id"],
                name="uq_payment_allocations",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="ck_payment_alloc_amount"
            ),
        ]
        indexes = [
            models.Index(fields=["document_type", "document_id"], name="ix_payment_alloc_doc"),
            models.Index(fields=["payment_side", "payment_id"], name="ix_payment_alloc_payment"),
        ]


class CashPaymentReceipt(TenantModel, LegacyIdMixin):
    """Cash / Unbilled Payment Receipt (without GST tax invoice)."""

    STATUS_CHOICES = [
        ("RECEIVED", "Received"),
        ("CANCELLED", "Cancelled"),
        ("VOIDED", "Voided"),
    ]

    receipt_number = models.TextField()
    payment = models.OneToOneField(
        PaymentIn, on_delete=models.CASCADE, related_name="cash_receipt", null=True, blank=True
    )
    party = models.ForeignKey("masters.Party", on_delete=models.PROTECT, related_name="cash_receipts")
    invoice = models.ForeignKey(
        "SalesInvoice", null=True, blank=True, on_delete=models.SET_NULL, related_name="cash_receipts"
    )
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    payment_date = models.DateField()
    mode = models.TextField(choices=choices(PAYMENT_MODES), default="Cash")
    reference_number = models.TextField(null=True, blank=True)
    description = models.TextField(null=True, blank=True)
    notes = models.TextField(null=True, blank=True)
    status = models.TextField(choices=STATUS_CHOICES, default="RECEIVED")
    created_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    cancelled_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "cash_payment_receipts"
        ordering = ["-payment_date", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "receipt_number"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_cash_receipt_number",
            ),
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="ck_cash_receipt_amount"),
        ]

    def __str__(self):
        return self.receipt_number



# ---------------------------------------------------------------------------
# Sales returns / credit notes (api.md §5.9)
# ---------------------------------------------------------------------------
class SalesReturn(DocumentHeader):
    return_number = models.TextField(null=True, blank=True)
    credit_note_number = models.TextField(null=True, blank=True)
    status = models.TextField(
        choices=choices(["Draft", "Posted", "Cancelled"]), default="Posted"
    )
    sales_invoice = models.ForeignKey(
        SalesInvoice, on_delete=models.PROTECT, related_name="returns"
    )
    reason = models.TextField(null=True, blank=True)
    location = models.ForeignKey(
        "masters.Location", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    journal_entry = models.ForeignKey(
        "accounting.JournalEntry", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        db_table = "sales_returns"
        ordering = ["-doc_date", "-created_at"]
        constraints = [
            number_unique_constraint("sales_returns", "return_number"),
            total_check_constraint("sales_returns"),
        ]


class SalesReturnLine(DocumentLine):
    sales_return = models.ForeignKey(
        SalesReturn, on_delete=models.CASCADE, related_name="line_items"
    )
    sales_invoice_line = models.ForeignKey(
        SalesInvoiceLine, on_delete=models.PROTECT, related_name="return_lines"
    )
    returned_qty = models.DecimalField(max_digits=18, decimal_places=4)

    class Meta(DocumentLine.Meta):
        db_table = "sales_return_lines"
        constraints = [
            models.UniqueConstraint(
                fields=["sales_return", "line_no"], name="uq_sales_return_line_no"
            ),
            models.CheckConstraint(
                condition=models.Q(returned_qty__gt=0), name="ck_srl_returned_qty"
            ),
        ]


# ---------------------------------------------------------------------------
# Warranty cards (api.md §5.10)
# ---------------------------------------------------------------------------
class WarrantyCard(TenantModel, LegacyIdMixin):
    """Two independent status fields -- do not collapse them (api.md §5.10).

    ``document_status`` is set by user action. ``coverageStatus`` is **derived**
    on every read from the dates plus the document status, so it is not a
    column here: storing it would need a nightly job that is always a day wrong
    in some timezone (db.md §5.5).
    """

    DOCUMENT_STATUSES = ["Draft", "Generated", "Suspended", "Cancelled", "Void"]
    WARRANTY_UNITS = ["Years", "Months"]
    START_EVENTS = ["Delivery", "Invoice", "Installation"]

    card_number = models.TextField()
    party = models.ForeignKey("masters.Party", on_delete=models.PROTECT, related_name="warranties")
    contact_person = models.TextField(null=True, blank=True)
    billing_address = models.JSONField(default=dict, blank=True)
    shipping_address = models.JSONField(default=dict, blank=True)
    gstin = models.TextField(null=True, blank=True)

    delivery_challan = models.ForeignKey(
        DeliveryChallan, null=True, blank=True, on_delete=models.SET_NULL, related_name="warranties"
    )
    sales_invoice = models.ForeignKey(
        SalesInvoice, null=True, blank=True, on_delete=models.SET_NULL, related_name="warranties"
    )
    sales_order = models.ForeignKey(
        SalesOrder, null=True, blank=True, on_delete=models.SET_NULL, related_name="warranties"
    )

    delivery_date = models.DateField(null=True, blank=True)
    delivery_location = models.TextField(null=True, blank=True)
    warranty_period = models.IntegerField(default=1)
    warranty_unit = models.TextField(choices=choices(WARRANTY_UNITS), default="Years")
    warranty_start_event = models.TextField(choices=choices(START_EVENTS), default="Delivery")
    start_date = models.DateField(null=True, blank=True)
    expiry_date = models.DateField(null=True, blank=True)
    #: The "expiring soon" window used by the coverage derivation.
    expiring_soon_days = models.IntegerField(default=30)

    document_status = models.TextField(choices=choices(DOCUMENT_STATUSES), default="Draft")
    suspended_reason = models.TextField(null=True, blank=True)
    cancelled_reason = models.TextField(null=True, blank=True)
    void_reason = models.TextField(null=True, blank=True)
    terms = models.TextField(null=True, blank=True)
    notes = models.TextField(null=True, blank=True)

    class Meta:
        db_table = "warranty_cards"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["client", "card_number"],
                condition=models.Q(deleted_at__isnull=True),
                name="uq_warranty_number",
            )
        ]

    def __str__(self):
        return self.card_number


class WarrantyCardItem(TenantModel):
    warranty_card = models.ForeignKey(
        WarrantyCard, on_delete=models.CASCADE, related_name="items"
    )
    item = models.ForeignKey("masters.Item", on_delete=models.PROTECT, related_name="+")
    sku = models.TextField(null=True, blank=True)
    item_name = models.TextField(null=True, blank=True)
    qty = models.DecimalField(max_digits=18, decimal_places=4, default=1)

    class Meta:
        db_table = "warranty_card_items"


class WarrantyCardSerial(TenantModel):
    warranty_card = models.ForeignKey(
        WarrantyCard, on_delete=models.CASCADE, related_name="serial_links"
    )
    serial = models.ForeignKey(
        "masters.ItemSerial", on_delete=models.PROTECT, related_name="warranty_links"
    )

    class Meta:
        db_table = "warranty_card_serials"
        constraints = [
            models.UniqueConstraint(
                fields=["warranty_card", "serial"], name="pk_warranty_card_serials"
            )
        ]
