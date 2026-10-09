from datetime import date
from decimal import Decimal
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.accounting.services import seed_chart_of_accounts
from apps.masters.models import Party
from apps.sales.models import CashPaymentReceipt, PaymentIn, SalesInvoice
from apps.sales import services


class WithBillWithoutBillPaymentTests(TestCase):
    def setUp(self):
        self.client_obj, _ = Client.objects.get_or_create(
            slug="test-tenant", defaults={"name": "Test Tenant"}
        )
        seed_chart_of_accounts(self.client_obj)
        self.user = User.objects.filter(email="sales_user@example.com").first()
        if not self.user:
            self.user = User.objects.create_superuser(
                email="sales_user@example.com",
                password="password123",
                client=self.client_obj,
            )
        self.party = Party.objects.create(
            client=self.client_obj,
            code="CUST-001",
            type="Customer",
            name="Acme Corp",
            email="acme@example.com",
        )
        self.invoice = SalesInvoice.objects.create(
            client=self.client_obj,
            invoice_number="INV-TEST-001",
            party=self.party,
            party_name="Acme Corp",
            doc_date=date.today(),
            due_date=date.today(),
            subtotal=Decimal("1000.00"),
            taxable_value=Decimal("1000.00"),
            total_tax=Decimal("180.00"),
            total=Decimal("1180.00"),
            amount_paid=Decimal("0.00"),
            status="Unpaid",
            posted_at=timezone.now(),
        )

    def test_record_with_bill_payment_reduces_outstanding(self):
        payment = services.record_payment_in(
            client=self.client_obj,
            party=self.party,
            amount=Decimal("500.00"),
            payment_date=date.today(),
            payment_type="WITH_BILL",
            invoice=self.invoice,
            mode="Cash",
            user=self.user,
        )
        self.assertEqual(payment.payment_type, "WITH_BILL")
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.amount_paid, Decimal("500.00"))
        self.assertEqual(self.invoice.status, "Partially Paid")

        out = services.invoice_outstanding(self.invoice)
        self.assertEqual(out["paidAgainstInvoice"], Decimal("500.00"))
        self.assertEqual(out["withoutBillCash"], Decimal("0.00"))
        self.assertEqual(out["outstanding"], Decimal("680.00"))

    def test_record_without_bill_cash_leaves_invoice_untouched(self):
        payment = services.record_payment_in(
            client=self.client_obj,
            party=self.party,
            amount=Decimal("300.00"),
            payment_date=date.today(),
            payment_type="WITHOUT_BILL",
            invoice=self.invoice,
            mode="Cash",
            description="Counter cash sale advance",
            user=self.user,
        )
        self.assertEqual(payment.payment_type, "WITHOUT_BILL")

        # Must generate CashPaymentReceipt linked to the invoice
        receipt = CashPaymentReceipt.objects.filter(payment=payment).first()
        self.assertIsNotNone(receipt)
        self.assertTrue(receipt.receipt_number.startswith("CPR"))
        self.assertEqual(receipt.amount, Decimal("300.00"))
        self.assertEqual(receipt.status, "RECEIVED")
        self.assertEqual(receipt.invoice, self.invoice)

        # Invoice MUST NOT be affected (cash does not satisfy GST tax invoice balance)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.amount_paid, Decimal("0.00"))
        self.assertEqual(self.invoice.status, "Unpaid")

        out = services.invoice_outstanding(self.invoice)
        self.assertEqual(out["paidAgainstInvoice"], Decimal("0.00"))
        self.assertEqual(out["withoutBillCash"], Decimal("300.00"))
        self.assertEqual(out["totalReceived"], Decimal("300.00"))
        # Invoice Outstanding = Invoice Total - Valid With-Bill Payments = 1180.00
        self.assertEqual(out["outstanding"], Decimal("1180.00"))

        # Verify unrelated invoice does NOT get this cash (OR bug fix)
        unrelated_inv = SalesInvoice.objects.create(
            client=self.client_obj,
            invoice_number="INV-UNRELATED",
            party=self.party,
            party_name="Acme Corp",
            doc_date=date.today(),
            due_date=date.today(),
            subtotal=Decimal("500.00"),
            taxable_value=Decimal("500.00"),
            total_tax=Decimal("90.00"),
            total=Decimal("590.00"),
            amount_paid=Decimal("0.00"),
            status="Unpaid",
        )
        unrelated_out = services.invoice_outstanding(unrelated_inv)
        self.assertEqual(unrelated_out["withoutBillCash"], Decimal("0.00"))

    def test_cancel_cash_receipt_reverses_and_updates_balance(self):
        payment = services.record_payment_in(
            client=self.client_obj,
            party=self.party,
            amount=Decimal("400.00"),
            payment_date=date.today(),
            payment_type="WITHOUT_BILL",
            mode="Cash",
            description="Test payment",
            user=self.user,
        )
        receipt = payment.cash_receipt
        self.assertEqual(receipt.status, "RECEIVED")

        cancelled = services.cancel_cash_receipt(
            receipt, reason="Customer refunded", user=self.user
        )
        self.assertEqual(cancelled.status, "CANCELLED")
        self.assertEqual(cancelled.cancellation_reason, "Customer refunded")

        payment.refresh_from_db()
        self.assertEqual(payment.status, "Cancelled")

        out = services.invoice_outstanding(self.invoice)
        self.assertEqual(out["withoutBillCash"], Decimal("0.00"))
        self.assertEqual(out["outstanding"], Decimal("1180.00"))

    def test_cash_receipt_api_endpoints(self):
        api_client = APIClient()
        tokens = build_tokens(self.user)
        api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

        # Create without-bill payment
        payment = services.record_payment_in(
            client=self.client_obj,
            party=self.party,
            amount=Decimal("250.00"),
            payment_date=date.today(),
            payment_type="WITHOUT_BILL",
            mode="Cash",
            user=self.user,
        )
        receipt = payment.cash_receipt

        # List receipts
        resp = api_client.get("/api/v1/sales/cash-receipts/")
        self.assertEqual(resp.status_code, 200)

        # Cancel receipt via API action
        resp = api_client.post(
            f"/api/v1/sales/cash-receipts/{receipt.id}/cancel/",
            {"reason": "Wrong entry"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, "CANCELLED")

    def test_case_a_cross_customer_allocation_rejected(self):
        """Test Case A: Attempting to allocate payment from Customer 1 to Customer 2's invoice must be rejected."""
        party2 = Party.objects.create(
            client=self.client_obj,
            code="CUST-002",
            type="Customer",
            name="Beta Ltd",
            email="beta@example.com",
        )
        invoice2 = SalesInvoice.objects.create(
            client=self.client_obj,
            invoice_number="INV-TEST-002",
            party=party2,
            party_name="Beta Ltd",
            doc_date=date.today(),
            due_date=date.today(),
            subtotal=Decimal("60000.00"),
            taxable_value=Decimal("60000.00"),
            total_tax=Decimal("0.00"),
            total=Decimal("60000.00"),
            amount_paid=Decimal("0.00"),
            status="Unpaid",
            posted_at=timezone.now(),
        )
        api_client = APIClient()
        tokens = build_tokens(self.user)
        api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

        # Attempt to record payment for party 1 against party 2's invoice via API
        resp = api_client.post(
            "/api/v1/sales/payments/",
            {
                "partyId": str(self.party.id),
                "invoiceId": str(invoice2.id),
                "amount": "50000.00",
                "paymentDate": str(date.today()),
                "mode": "Cash",
                "paymentType": "WITH_BILL",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

        # Also direct service call must raise BusinessRuleViolation
        with self.assertRaises(Exception):
            services.record_payment_in(
                client=self.client_obj,
                party=self.party,
                amount=Decimal("50000.00"),
                payment_date=date.today(),
                mode="Cash",
                payment_type="WITH_BILL",
                allocations=[{"invoiceId": str(invoice2.id), "amount": "50000.00"}],
                user=self.user,
            )

    def test_case_d_multiple_installments_lead_to_zero_outstanding(self):
        """Test Case D: Multiple installments strictly in PaymentIn leading to zero outstanding and Paid status."""
        inv = SalesInvoice.objects.create(
            client=self.client_obj,
            invoice_number="INV-INSTALLMENT-001",
            party=self.party,
            party_name="Acme Corp",
            doc_date=date.today(),
            due_date=date.today(),
            subtotal=Decimal("100000.00"),
            taxable_value=Decimal("100000.00"),
            total_tax=Decimal("0.00"),
            total=Decimal("100000.00"),
            amount_paid=Decimal("0.00"),
            status="Unpaid",
            posted_at=timezone.now(),
        )
        # Payment 1: 25,000 -> Outstanding = 75,000, Partially Paid
        pay1 = services.record_payment_in(
            client=self.client_obj,
            party=self.party,
            amount=Decimal("25000.00"),
            payment_date=date.today(),
            mode="Cash",
            payment_type="WITH_BILL",
            invoice=inv,
            user=self.user,
        )
        inv.refresh_from_db()
        self.assertEqual(inv.status, "Partially Paid")
        out1 = services.invoice_outstanding(inv)
        self.assertEqual(out1["outstanding"], Decimal("75000.00"))

        # Payment 2: 25,000 -> Outstanding = 50,000, Partially Paid
        pay2 = services.record_payment_in(
            client=self.client_obj,
            party=self.party,
            amount=Decimal("25000.00"),
            payment_date=date.today(),
            mode="Cash",
            payment_type="WITH_BILL",
            invoice=inv,
            user=self.user,
        )
        inv.refresh_from_db()
        self.assertEqual(inv.status, "Partially Paid")
        out2 = services.invoice_outstanding(inv)
        self.assertEqual(out2["outstanding"], Decimal("50000.00"))

        # Payment 3: 50,000 -> Outstanding = 0.00, Paid
        pay3 = services.record_payment_in(
            client=self.client_obj,
            party=self.party,
            amount=Decimal("50000.00"),
            payment_date=date.today(),
            mode="Cash",
            payment_type="WITH_BILL",
            invoice=inv,
            user=self.user,
        )
        inv.refresh_from_db()
        self.assertEqual(inv.status, "Paid")
        out3 = services.invoice_outstanding(inv)
        self.assertEqual(out3["outstanding"], Decimal("0.00"))

        # Verify 3 distinct PaymentIn records exist and total collected = 100,000
        payments = PaymentIn.objects.filter(invoice=inv)
        self.assertEqual(payments.count(), 3)
        total_collected = sum(p.amount for p in payments)
        self.assertEqual(total_collected, Decimal("100000.00"))

    def test_case_e_excess_payment_saved_as_advance(self):
        """Test Case E: Excess payment: invoice balance paid to zero, unallocated remainder saved as customer advance."""
        inv = SalesInvoice.objects.create(
            client=self.client_obj,
            invoice_number="INV-ADVANCE-001",
            party=self.party,
            party_name="Acme Corp",
            doc_date=date.today(),
            due_date=date.today(),
            subtotal=Decimal("40000.00"),
            taxable_value=Decimal("40000.00"),
            total_tax=Decimal("0.00"),
            total=Decimal("40000.00"),
            amount_paid=Decimal("0.00"),
            status="Unpaid",
            posted_at=timezone.now(),
        )
        # Customer pays 50,000, allocated 40,000 to invoice
        payment = services.record_payment_in(
            client=self.client_obj,
            party=self.party,
            amount=Decimal("50000.00"),
            payment_date=date.today(),
            mode="Cash",
            payment_type="WITH_BILL",
            allocations=[{"invoiceId": str(inv.id), "amount": "40000.00"}],
            user=self.user,
        )
        inv.refresh_from_db()
        self.assertEqual(inv.status, "Paid")
        self.assertEqual(inv.amount_paid, Decimal("40000.00"))
        out = services.invoice_outstanding(inv)
        self.assertEqual(out["outstanding"], Decimal("0.00"))

        payment.refresh_from_db()
        self.assertEqual(payment.allocated_amount, Decimal("40000.00"))
        self.assertEqual(payment.unallocated_amount, Decimal("10000.00"))


class SalesInvoiceCashAllocationTests(TestCase):
    def setUp(self):
        self.client_obj, _ = Client.objects.get_or_create(
            slug="test-tenant-allocation", defaults={"name": "Test Tenant Allocation"}
        )
        seed_chart_of_accounts(self.client_obj)
        self.user = User.objects.create_superuser(
            email="allocation_user@example.com",
            password="password123",
            client=self.client_obj,
        )
        self.party = Party.objects.create(
            client=self.client_obj,
            code="CUST-ALLOC-001",
            type="Customer",
            name="Apex Dynamics",
            email="apex@example.com",
        )
        from apps.masters.models import Location
        self.location = Location.objects.create(
            client=self.client_obj,
            code="WH-001",
            name="Main Warehouse",
            type="Warehouse",
            is_active=True,
        )
        # Create Sales Invoice with initial Total Sales Value = 1,00,000
        # Line 1: qty 10, rate 10,000 -> 1,00,000
        self.invoice = SalesInvoice.objects.create(
            client=self.client_obj,
            invoice_number="INV-ALLOC-001",
            party=self.party,
            party_name="Apex Dynamics",
            doc_date=date.today(),
            due_date=date.today(),
            subtotal=Decimal("100000.00"),
            taxable_value=Decimal("100000.00"),
            total_tax=Decimal("0.00"),
            total=Decimal("100000.00"),
            total_sales_value=Decimal("100000.00"),
            cash_amount=Decimal("0.00"),
            amount_paid=Decimal("0.00"),
            status="Draft",
        )
        from apps.sales.models import SalesInvoiceLine
        self.line1 = SalesInvoiceLine.objects.create(
            client=self.client_obj,
            sales_invoice=self.invoice,
            line_no=1,
            item_name="Industrial Pump",
            qty=Decimal("10.0000"),
            rate=Decimal("10000.0000"),
            amount=Decimal("100000.00"),
            tax_pct=Decimal("0.00"),
            tax_amount=Decimal("0.00"),
            line_total=Decimal("100000.00"),
            original_rate=Decimal("10000.0000"),
            original_amount=Decimal("100000.00"),
            original_line_total=Decimal("100000.00"),
        )

    def test_initial_allocation_70_30(self):
        """Initial: Total = 100,000 -> Invoice = 70,000, Cash = 30,000."""
        inv = services.update_sales_allocation(
            self.invoice,
            formal_invoice_amount=Decimal("70000.00"),
            user=self.user,
            reason="Initial 70/30 split",
        )
        self.assertEqual(inv.total, Decimal("70000.00"))
        self.assertEqual(inv.cash_amount, Decimal("30000.00"))
        self.assertEqual(inv.total_sales_value, Decimal("100000.00"))

        # One CashPaymentReceipt created
        receipts = CashPaymentReceipt.objects.filter(invoice=inv, deleted_at__isnull=True)
        self.assertEqual(receipts.count(), 1)
        receipt = receipts.first()
        self.assertEqual(receipt.amount, Decimal("30000.00"))
        self.assertEqual(receipt.status, "RECEIVED")
        self.assertEqual(receipt.mode, "Cash")

        # Item-level lines recalculation equals formal invoice total
        self.line1.refresh_from_db()
        self.assertEqual(self.line1.line_total, Decimal("70000.00"))
        self.assertEqual(self.line1.original_line_total, Decimal("100000.00"))

        # Revision history
        revisions = inv.revisions.all()
        self.assertEqual(revisions.count(), 1)
        rev = revisions.first()
        self.assertEqual(rev.revision_number, 1)
        self.assertEqual(rev.new_invoice_amount, Decimal("70000.00"))
        self.assertEqual(rev.new_cash_amount, Decimal("30000.00"))
        self.assertEqual(rev.cash_receipt, receipt)

    def test_increase_invoice_80_cash_20(self):
        """70k/30k -> User increases invoice to 80k: Cash automatically 20k."""
        services.update_sales_allocation(
            self.invoice,
            formal_invoice_amount=Decimal("70000.00"),
            user=self.user,
        )
        # Increase invoice to 80,000
        inv = services.update_sales_allocation(
            self.invoice,
            formal_invoice_amount=Decimal("80000.00"),
            user=self.user,
            reason="Increase invoice to 80k",
        )
        self.assertEqual(inv.total, Decimal("80000.00"))
        self.assertEqual(inv.cash_amount, Decimal("20000.00"))

        # No duplicate receipt - existing receipt updated to 20,000
        receipts = CashPaymentReceipt.objects.filter(invoice=inv, deleted_at__isnull=True)
        self.assertEqual(receipts.count(), 1)
        self.assertEqual(receipts.first().amount, Decimal("20000.00"))

        # Item level line total equals 80,000
        self.line1.refresh_from_db()
        self.assertEqual(self.line1.line_total, Decimal("80000.00"))

        # Revision 2 recorded
        revisions = list(inv.revisions.order_by("revision_number"))
        self.assertEqual(len(revisions), 2)
        rev2 = revisions[1]
        self.assertEqual(rev2.revision_number, 2)
        self.assertEqual(rev2.old_invoice_amount, Decimal("70000.00"))
        self.assertEqual(rev2.new_invoice_amount, Decimal("80000.00"))
        self.assertEqual(rev2.old_cash_amount, Decimal("30000.00"))
        self.assertEqual(rev2.new_cash_amount, Decimal("20000.00"))
        self.assertEqual(rev2.difference, Decimal("10000.00"))

    def test_decrease_invoice_60_cash_40(self):
        """80k/20k -> User decreases invoice to 60k: Cash automatically 40k."""
        services.update_sales_allocation(
            self.invoice,
            formal_invoice_amount=Decimal("80000.00"),
            user=self.user,
        )
        inv = services.update_sales_allocation(
            self.invoice,
            formal_invoice_amount=Decimal("60000.00"),
            user=self.user,
            reason="Decrease invoice to 60k",
        )
        self.assertEqual(inv.total, Decimal("60000.00"))
        self.assertEqual(inv.cash_amount, Decimal("40000.00"))

        # Single receipt updated
        receipts = CashPaymentReceipt.objects.filter(invoice=inv, deleted_at__isnull=True)
        self.assertEqual(receipts.count(), 1)
        self.assertEqual(receipts.first().amount, Decimal("40000.00"))

        # Line items equal 60,000
        self.line1.refresh_from_db()
        self.assertEqual(self.line1.line_total, Decimal("60000.00"))

    def test_50_50_split(self):
        """60k/40k -> User changes to 50/50: Invoice = 50k, Cash = 50k."""
        services.update_sales_allocation(
            self.invoice,
            formal_invoice_amount=Decimal("60000.00"),
            user=self.user,
        )
        inv = services.update_sales_allocation(
            self.invoice,
            formal_invoice_amount=Decimal("50000.00"),
            user=self.user,
            reason="50/50 split",
        )
        self.assertEqual(inv.total, Decimal("50000.00"))
        self.assertEqual(inv.cash_amount, Decimal("50000.00"))

        out = services.invoice_outstanding(inv)
        self.assertEqual(out["totalSalesValue"], Decimal("100000.00"))
        self.assertEqual(out["formalInvoiceAmount"], Decimal("50000.00"))
        self.assertEqual(out["cashAmount"], Decimal("50000.00"))
        self.assertEqual(out["totalAllocated"], Decimal("100000.00"))
        self.assertEqual(out["remaining"], Decimal("0.00"))

    def test_validation_prevent_exceeding_total_sales(self):
        """Never allow Invoice + Cash > Total Sales Value or negative amounts."""
        from apps.core.exceptions import ValidationFailed
        with self.assertRaises(ValidationFailed):
            services.update_sales_allocation(
                self.invoice,
                formal_invoice_amount=Decimal("110000.00"),
                user=self.user,
            )
        with self.assertRaises(ValidationFailed):
            services.update_sales_allocation(
                self.invoice,
                formal_invoice_amount=Decimal("-5000.00"),
                user=self.user,
            )

    def test_finalized_invoice_preserves_history_and_posts_credit_note(self):
        """When an invoice is finalized, revisions preserve history and post credit notes."""
        # Initial 80k / 20k
        services.update_sales_allocation(
            self.invoice,
            formal_invoice_amount=Decimal("80000.00"),
            user=self.user,
        )
        # Finalize invoice
        finalized = services.finalize_invoice(self.invoice, user=self.user)
        self.assertIsNotNone(finalized.posted_at)
        self.assertEqual(finalized.status, "Unpaid")

        # User decreases invoice from 80k to 60k
        revised = services.update_sales_allocation(
            finalized,
            formal_invoice_amount=Decimal("60000.00"),
            user=self.user,
            reason="Post-finalization reduction",
        )
        self.assertEqual(revised.total, Decimal("60000.00"))
        self.assertEqual(revised.cash_amount, Decimal("40000.00"))

        # Credit note issued for the 20,000 difference
        from apps.sales.models import SalesReturn
        credit_notes = SalesReturn.objects.filter(sales_invoice=revised)
        self.assertEqual(credit_notes.count(), 1)
        cn = credit_notes.first()
        self.assertEqual(cn.total, Decimal("20000.00"))
        self.assertIsNotNone(cn.journal_entry_id)

    def test_allocate_split_api_endpoint(self):
        """Test POST /api/v1/sales/invoices/{id}/allocate-split/"""
        api_client = APIClient()
        tokens = build_tokens(self.user)
        api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

        resp = api_client.post(
            f"/api/v1/sales/invoices/{self.invoice.id}/allocate-split/",
            {"formalInvoiceAmount": "75000.00", "reason": "API 75/25 split"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(Decimal(str(data["total"])), Decimal("75000.00"))
        self.assertEqual(Decimal(str(data["cashAmount"])), Decimal("25000.00"))
        self.assertEqual(Decimal(str(data["totalSalesValue"])), Decimal("100000.00"))
        self.assertEqual(len(data["revisions"]), 1)
        self.assertEqual(data["revisions"][0]["revisionNumber"], 1)


class SalesPipelineLinkTests(TestCase):
    """The document chain the UI walks: every hop is linked and reaches the API."""

    def setUp(self):
        self.client_obj, _ = Client.objects.get_or_create(
            slug="pipeline-tenant", defaults={"name": "Pipeline Tenant"}
        )
        seed_chart_of_accounts(self.client_obj)
        self.user = User.objects.create_superuser(
            email="pipeline_user@example.com",
            password="password123",
            client=self.client_obj,
        )
        self.party = Party.objects.create(
            client=self.client_obj, code="CUST-PIPE", type="Customer", name="Pipe Co",
        )
        from apps.masters.models import Location
        Location.objects.create(
            client=self.client_obj, code="WH-PIPE", name="Main Warehouse",
            type="Warehouse", is_active=True,
        )
        self.api = APIClient()
        tokens = build_tokens(self.user)
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

    def _line(self, qty=2, rate=500):
        # A free-text line: no item, so no stock is needed to dispatch it.
        return {"description": "Fabricated MS table", "qty": qty, "rate": rate, "tax": 18}

    def _post(self, url, data=None, expected=201):
        resp = self.api.post(url, data or {}, format="json")
        self.assertEqual(resp.status_code, expected, resp.content)
        return resp.json()

    def test_estimate_to_delivered_challan_chain(self):
        estimate = self._post("/api/v1/sales/estimates/", {
            "partyId": str(self.party.id), "date": str(date.today()),
            "lineItems": [self._line()],
        })

        # A status-only PATCH must leave the lines alone.
        resp = self.api.patch(
            f"/api/v1/sales/estimates/{estimate['id']}/", {"status": "Sent"}, format="json"
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["status"], "Sent")
        self.assertEqual(len(resp.json()["lineItems"]), 1)

        quotation = self._post(f"/api/v1/sales/estimates/{estimate['id']}/convert-to-quotation/")
        self.assertEqual(quotation["estimate"], estimate["id"])
        self.assertTrue(quotation["quotationNumber"].startswith("QT"))

        order = self._post(f"/api/v1/sales/quotations/{quotation['id']}/convert-to-order/")
        self.assertEqual(order["quotation"], quotation["id"])
        resp = self.api.patch(
            f"/api/v1/sales/orders/{order['id']}/", {"stage": "Confirmed"}, format="json"
        )
        self.assertEqual(resp.status_code, 200, resp.content)

        challan = self._post(f"/api/v1/sales/orders/{order['id']}/convert-to-challan/", {})
        self.assertEqual(challan["salesOrder"], order["id"])
        self.assertEqual(challan["status"], "Draft")

        # Tracking a draft is refused: nothing has shipped.
        self._post(
            f"/api/v1/sales/challans/{challan['id']}/track/", {"status": "In Transit"}, expected=409
        )

        dispatched = self._post(f"/api/v1/sales/challans/{challan['id']}/dispatch/", expected=200)
        self.assertEqual(dispatched["status"], "Dispatched")

        for step in ("In Transit", "In Transit", "Out for Delivery", "Delivered"):
            tracked = self._post(
                f"/api/v1/sales/challans/{challan['id']}/track/", {"status": step}, expected=200
            )
            self.assertEqual(tracked["status"], step)
        self.assertIsNotNone(tracked["deliveredAt"])

        # Forward-only.
        self._post(
            f"/api/v1/sales/challans/{challan['id']}/track/", {"status": "In Transit"}, expected=409
        )
        self._post(
            f"/api/v1/sales/challans/{challan['id']}/track/", {"status": "Shipped"}, expected=400
        )

        resp = self.api.get(f"/api/v1/sales/orders/{order['id']}/")
        self.assertEqual(resp.json()["stage"], "Delivered")

    def test_direct_documents_accept_their_upstream_links(self):
        order = self._post("/api/v1/sales/orders/", {
            "partyId": str(self.party.id), "date": str(date.today()),
            "lineItems": [self._line()],
        })
        proforma = self._post("/api/v1/sales/proforma-invoices/", {
            "partyId": str(self.party.id), "date": str(date.today()),
            "salesOrder": order["id"], "lineItems": [self._line()],
        })
        self.assertEqual(proforma["salesOrder"], order["id"])

        invoice = self._post(f"/api/v1/sales/proforma-invoices/{proforma['id']}/convert-to-invoice/")
        self.assertEqual(invoice["proformaInvoiceId"], proforma["id"])
        self.assertEqual(invoice["salesOrderId"], order["id"])
        resp = self.api.get(f"/api/v1/sales/proforma-invoices/{proforma['id']}/")
        self.assertEqual(resp.json()["status"], "Converted")

        challan = self._post("/api/v1/sales/challans/", {
            "partyId": str(self.party.id), "date": str(date.today()),
            "salesOrder": order["id"], "transporter": "Blue Dart",
            "vehicleNumber": "GJ05AB1234", "lineItems": [self._line()],
        })
        self.assertEqual(challan["salesOrder"], order["id"])
        self.assertEqual(challan["vehicleNumber"], "GJ05AB1234")


class QuotationFirstSalesTests(TestCase):
    """Quotation-first sales: Inventory is optional.

    Customer -> Quotation -> Customer Approval -> Sales Order -> Delivery ->
    Invoice -> Payment-In, with inventory, service, fabrication, custom and
    free-text lines -- and never a fake inventory item for a custom line.
    """

    API = "/api/v1/sales"

    def setUp(self):
        from apps.masters.models import Item

        self.client_obj, _ = Client.objects.get_or_create(
            slug="quote-first-tenant", defaults={"name": "Quote First Tenant"}
        )
        seed_chart_of_accounts(self.client_obj)
        self.user = User.objects.create_superuser(
            email="quote_first@example.com", password="password123", client=self.client_obj,
        )
        self.party = Party.objects.create(
            client=self.client_obj, code="CUST-QF", type="Customer", name="Fab Buyer Ltd",
            billing_address={"line1": "Plot 7", "city": "Surat", "state": "Gujarat"},
            shipping_address={"line1": "Site B", "city": "Vapi", "state": "Gujarat"},
        )
        self.panel = Item.objects.create(
            client=self.client_obj, sku="SS-PANEL", name="SS Panel", uom="Nos",
            hsn_code="7326", selling_price=Decimal("5000"), cost_price=Decimal("3000"),
        )
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.user)['access']}")

    # -- helpers --------------------------------------------------------------
    def _post(self, url, data=None, expected=201):
        resp = self.api.post(url, data or {}, format="json")
        self.assertEqual(resp.status_code, expected, resp.content)
        return resp.json()

    def _warehouse_with_stock(self, qty=100):
        from apps.inventory import services as stock
        from apps.masters.models import Location

        location = Location.objects.create(
            client=self.client_obj, code="WH-QF", name="Main Warehouse",
            type="Warehouse", is_active=True,
        )
        stock.post_movement(
            client_id=self.client_obj.id, item=self.panel.id, location=location.id,
            type="ADJUSTMENT", quantity=Decimal(qty), unit_cost=Decimal("3000"),
            notes="Opening stock", user=self.user,
        )
        return location

    def _on_hand(self):
        from apps.inventory.models import StockBalance

        return sum(
            (b.on_hand for b in StockBalance.objects.filter(item=self.panel)), Decimal("0")
        )

    def _inventory_line(self, qty=10):
        return {"itemId": str(self.panel.id), "qty": qty, "rate": 5000, "tax": 18}

    def _custom_line(self):
        return {
            "description": "Custom MS Fabrication Work", "qty": 1, "uom": "Job",
            "hsnCode": "998873", "rate": 85000, "tax": 18,
        }

    def _quotation(self, lines, **extra):
        return self._post(f"{self.API}/quotations/", {
            "partyId": str(self.party.id), "date": str(date.today()),
            "validUntil": str(date.today()), "lineItems": lines, **extra,
        })

    # -- 1-5: quotation lines -------------------------------------------------
    def test_inventory_quotation(self):
        quote = self._quotation([self._inventory_line()])
        line = quote["lineItems"][0]
        self.assertEqual(line["itemId"], str(self.panel.id))
        self.assertEqual(line["itemName"], "SS Panel")
        self.assertEqual(line["uom"], "Nos")
        self.assertEqual(Decimal(str(quote["subtotal"])), Decimal("50000.00"))

    def test_non_inventory_custom_quotation_creates_no_item(self):
        from apps.masters.models import Item

        before = Item.objects.filter(client=self.client_obj).count()
        quote = self._quotation([self._custom_line()])
        line = quote["lineItems"][0]
        self.assertIsNone(line["itemId"])
        self.assertEqual(line["description"], "Custom MS Fabrication Work")
        self.assertEqual(line["itemName"], "Custom MS Fabrication Work")
        self.assertEqual(line["uom"], "Job")
        self.assertEqual(line["hsnCode"], "998873")
        self.assertEqual(Decimal(str(quote["total"])), Decimal("100300.00"))
        # Never a fake inventory product for a custom line.
        self.assertEqual(Item.objects.filter(client=self.client_obj).count(), before)

    def test_service_and_free_text_lines(self):
        quote = self._quotation([
            {"description": "Installation & commissioning (service)", "qty": 2, "uom": "Day",
             "rate": 7500, "tax": 18},
            {"itemName": "Transport charges", "qty": 1, "rate": 2000, "tax": 0},
        ])
        first, second = quote["lineItems"]
        self.assertIsNone(first["itemId"])
        self.assertEqual(second["description"], "Transport charges")
        self.assertEqual(Decimal(str(quote["total"])), Decimal("19700.00"))

    def test_line_without_item_or_description_is_refused(self):
        resp = self.api.post(f"{self.API}/quotations/", {
            "partyId": str(self.party.id), "date": str(date.today()),
            "lineItems": [{"qty": 1, "rate": 100}],
        }, format="json")
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_mixed_quotation_with_discount_and_tax(self):
        quote = self._quotation(
            [
                {**self._inventory_line(), "discount": 10},
                {**self._custom_line(), "tax": 12},
            ],
            referenceNumber="ENQ-42", salesperson="R. Shah",
            paymentTerms="50% advance", deliveryTerms="Ex-works, 3 weeks",
            notes="Prices valid for this order only", terms="Subject to Surat jurisdiction",
            authorizedPerson="A. Patel",
        )
        # 10 x 5000 = 50000, -10% = 45000, +18% = 8100 -> 53100
        # 1 x 85000, +12% = 10200 -> 95200
        self.assertEqual(Decimal(str(quote["subtotal"])), Decimal("135000.00"))
        self.assertEqual(Decimal(str(quote["totalDiscount"])), Decimal("5000.00"))
        self.assertEqual(Decimal(str(quote["taxableValue"])), Decimal("130000.00"))
        self.assertEqual(Decimal(str(quote["totalTax"])), Decimal("18300.00"))
        self.assertEqual(Decimal(str(quote["total"])), Decimal("148300.00"))
        # No company state configured -> intra-state default: CGST + SGST.
        self.assertEqual(
            Decimal(str(quote["cgst"])) + Decimal(str(quote["sgst"])), Decimal("18300.00")
        )
        for key, value in {
            "referenceNumber": "ENQ-42", "salesperson": "R. Shah",
            "paymentTerms": "50% advance", "deliveryTerms": "Ex-works, 3 weeks",
            "notes": "Prices valid for this order only",
            "terms": "Subject to Surat jurisdiction", "authorizedPerson": "A. Patel",
        }.items():
            self.assertEqual(quote[key], value, key)
        self.assertEqual(quote["shippingAddress"]["city"], "Vapi")

    # -- 6: PDF payload ---------------------------------------------------------
    def test_print_payload_carries_letterhead_lines_and_terms(self):
        from apps.core.models import CompanyProfile

        CompanyProfile.objects.update_or_create(
            client=self.client_obj,
            defaults={"legal_name": "SEWEN Engineering", "trade_name": "SEWEN", "gstin": "24ABCDE1234F1Z5"},
        )
        quote = self._quotation(
            [self._inventory_line(), self._custom_line()],
            paymentTerms="50% advance", authorizedPerson="A. Patel",
        )
        resp = self.api.get(f"{self.API}/quotations/{quote['id']}/print/")
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["company"]["tradeName"], "SEWEN")
        self.assertEqual(body["title"], "Quotation")
        self.assertEqual(len(body["document"]["lineItems"]), 2)
        self.assertEqual(body["document"]["paymentTerms"], "50% advance")
        self.assertEqual(body["document"]["authorizedPerson"], "A. Patel")
        self.assertEqual(Decimal(str(body["totals"]["grandTotal"])), Decimal(str(quote["total"])))

    # -- approval + 7: quotation -> sales order ---------------------------------
    def test_approval_then_conversion_preserves_lines_and_links(self):
        quote = self._quotation(
            [self._inventory_line(), self._custom_line()], referenceNumber="PO-778",
        )
        accepted = self._post(f"{self.API}/quotations/{quote['id']}/accept/", expected=200)
        self.assertEqual(accepted["status"], "Accepted")
        # Approval is recorded once; a second accept is refused.
        self._post(f"{self.API}/quotations/{quote['id']}/accept/", expected=409)

        order = self._post(f"{self.API}/quotations/{quote['id']}/convert-to-order/")
        self.assertEqual(order["quotation"], quote["id"])
        self.assertEqual(order["quotationNumber"], quote["quotationNumber"])
        self.assertEqual(order["referenceNumber"], "PO-778")
        self.assertEqual(Decimal(str(order["total"])), Decimal(str(quote["total"])))
        inv_line, custom_line = order["lineItems"]
        self.assertEqual(inv_line["itemId"], str(self.panel.id))
        self.assertIsNone(custom_line["itemId"])
        self.assertEqual(custom_line["description"], "Custom MS Fabrication Work")
        self.assertEqual(custom_line["uom"], "Job")

        quote_after = self.api.get(f"{self.API}/quotations/{quote['id']}/").json()
        self.assertEqual(quote_after["status"], "Converted")
        self.assertEqual(quote_after["salesOrders"][0]["id"], order["id"])
        # Converting twice is refused -- no second order from one quotation.
        self._post(f"{self.API}/quotations/{quote['id']}/convert-to-order/", expected=409)

    def test_rejected_or_cancelled_quotation_cannot_convert(self):
        rejected = self._quotation([self._custom_line()])
        self._post(f"{self.API}/quotations/{rejected['id']}/reject/", {"reason": "Too costly"}, expected=200)
        self._post(f"{self.API}/quotations/{rejected['id']}/convert-to-order/", expected=409)

        cancelled = self._quotation([self._custom_line()])
        body = self._post(
            f"{self.API}/quotations/{cancelled['id']}/cancel/", {"reason": "Duplicate"}, expected=200
        )
        self.assertEqual(body["status"], "Cancelled")
        self.assertEqual(body["cancellationReason"], "Duplicate")
        self._post(f"{self.API}/quotations/{cancelled['id']}/convert-to-order/", expected=409)

    # -- 8, 9: sales order -> invoice without inventory -------------------------
    def test_custom_only_flow_needs_no_warehouse(self):
        from apps.inventory.models import StockMovement
        from apps.masters.models import Location

        self.assertFalse(Location.objects.filter(client=self.client_obj).exists())
        quote = self._quotation([self._custom_line()])
        order = self._post(f"{self.API}/quotations/{quote['id']}/convert-to-order/")

        # Completion / delivery is optional, and a challan of custom lines needs no warehouse.
        challan = self._post(f"{self.API}/orders/{order['id']}/convert-to-challan/", {})
        self._post(f"{self.API}/challans/{challan['id']}/dispatch/", expected=200)

        invoice = self._post(f"{self.API}/orders/{order['id']}/convert-to-invoice/", {})
        self.assertEqual(invoice["salesOrderId"], order["id"])
        self.assertEqual(invoice["quotationId"], quote["id"])
        self.assertIsNone(invoice["lineItems"][0]["itemId"])
        final = self._post(f"{self.API}/invoices/{invoice['id']}/finalize/", expected=200)
        self.assertEqual(final["status"], "Unpaid")
        self.assertEqual(Decimal(str(final["total"])), Decimal("100300.00"))
        self.assertFalse(StockMovement.objects.filter(client=self.client_obj).exists())

        order_after = self.api.get(f"{self.API}/orders/{order['id']}/").json()
        self.assertEqual(order_after["stage"], "Invoiced")

    def test_direct_custom_invoice(self):
        invoice = self._post(f"{self.API}/invoices/", {
            "partyId": str(self.party.id), "date": str(date.today()),
            "lineItems": [self._custom_line()],
        })
        final = self._post(f"{self.API}/invoices/{invoice['id']}/finalize/", expected=200)
        self.assertTrue(final["invoiceNumber"])
        self.assertEqual(Decimal(str(final["totalTax"])), Decimal("15300.00"))

    # -- 15: existing inventory sales ------------------------------------------
    def test_mixed_invoice_moves_stock_for_inventory_lines_only(self):
        self._warehouse_with_stock(100)
        quote = self._quotation([self._inventory_line(10), self._custom_line()])
        order = self._post(f"{self.API}/quotations/{quote['id']}/convert-to-order/")
        invoice = self._post(f"{self.API}/orders/{order['id']}/convert-to-invoice/", {})
        self._post(f"{self.API}/invoices/{invoice['id']}/finalize/", expected=200)
        self.assertEqual(self._on_hand(), Decimal("90"))

    def test_inventory_sale_via_challan_is_not_double_depleted(self):
        self._warehouse_with_stock(100)
        quote = self._quotation([self._inventory_line(4)])
        order = self._post(f"{self.API}/quotations/{quote['id']}/convert-to-order/")
        challan = self._post(f"{self.API}/orders/{order['id']}/convert-to-challan/", {})
        self._post(f"{self.API}/challans/{challan['id']}/dispatch/", expected=200)
        self.assertEqual(self._on_hand(), Decimal("96"))
        invoice = self._post(f"{self.API}/orders/{order['id']}/convert-to-invoice/", {})
        self._post(f"{self.API}/invoices/{invoice['id']}/finalize/", expected=200)
        self.assertEqual(self._on_hand(), Decimal("96"))

    def test_inventory_line_still_needs_a_warehouse(self):
        quote = self._quotation([self._inventory_line(1)])
        order = self._post(f"{self.API}/quotations/{quote['id']}/convert-to-order/")
        invoice = self._post(f"{self.API}/orders/{order['id']}/convert-to-invoice/", {})
        resp = self.api.post(f"{self.API}/invoices/{invoice['id']}/finalize/", {}, format="json")
        self.assertEqual(resp.status_code, 422, resp.content)
        self.assertEqual(resp.json()["code"], "NO_LOCATION")

    # -- 10-13: payments and history -------------------------------------------
    def _finalized_custom_invoice(self):
        quote = self._quotation([self._custom_line()])
        order = self._post(f"{self.API}/quotations/{quote['id']}/convert-to-order/")
        invoice = self._post(f"{self.API}/orders/{order['id']}/convert-to-invoice/", {})
        self._post(f"{self.API}/invoices/{invoice['id']}/finalize/", expected=200)
        return quote, order, invoice

    def test_with_bill_and_cash_payments_keep_gst_invoice_value(self):
        from apps.accounting.models import Account, BankAccount

        bank = BankAccount.objects.create(
            client=self.client_obj, name="HDFC Current", type="Bank",
            account=Account.objects.get(client=self.client_obj, code="1300"),
        )
        quote, order, invoice = self._finalized_custom_invoice()
        before = self.api.get(f"{self.API}/invoices/{invoice['id']}/").json()

        with_bill = self._post(f"{self.API}/payments/", {
            "customerId": str(self.party.id), "date": str(date.today()),
            "amount": 50000, "mode": "Bank", "paymentType": "WITH_BILL",
            "bankAccountId": str(bank.id), "invoiceId": invoice["id"],
        })
        self.assertEqual(with_bill["paymentType"], "WITH_BILL")

        cash = self._post(f"{self.API}/payments/", {
            "customerId": str(self.party.id), "date": str(date.today()),
            "amount": 10000, "mode": "Cash", "paymentType": "WITHOUT_BILL",
            "invoiceId": invoice["id"],
        })
        self.assertEqual(cash["paymentType"], "WITHOUT_BILL")
        self.assertIsNotNone(cash["cashReceipt"])

        after = self.api.get(f"{self.API}/invoices/{invoice['id']}/").json()
        # The GST invoice's value and tax never move with a payment.
        for key in ("subtotal", "taxableValue", "totalTax", "cgst", "sgst", "igst", "total"):
            self.assertEqual(after[key], before[key], key)
        # Only the with-bill payment settles the invoice; cash stays separate.
        self.assertEqual(Decimal(str(after["amountPaid"])), Decimal("50000.00"))
        self.assertEqual(after["status"], "Partially Paid")
        outstanding = self.api.get(f"{self.API}/invoices/{invoice['id']}/outstanding/").json()
        self.assertEqual(Decimal(str(outstanding["withoutBillCash"])), Decimal("10000.00"))
        self.assertEqual(Decimal(str(outstanding["outstanding"])), Decimal("50300.00"))

        # Customer history: Quotation -> Sales Order -> Invoice -> Payment.
        self.assertEqual(after["salesOrderNumber"], order["orderNumber"])
        self.assertEqual(after["quotationNumber"], quote["quotationNumber"])
        payments = self.api.get(f"{self.API}/payments/?customerId={self.party.id}").json()
        numbers = {row["invoiceNumber"] for row in payments["results"]}
        self.assertEqual(numbers, {after["invoiceNumber"]})
        for doc in ("quotations", "orders", "invoices"):
            rows = self.api.get(f"{self.API}/{doc}/?customerId={self.party.id}").json()["results"]
            self.assertTrue(rows, doc)
            self.assertTrue(all(row["partyId"] == str(self.party.id) for row in rows), doc)

    # -- 14: permissions -------------------------------------------------------
    def test_quotation_writes_need_create_quotation(self):
        from apps.accounts.models import Role, RolePermission
        from apps.accounts.permission_catalogue import sync_permissions

        sync_permissions()
        role = Role.objects.create(client=self.client_obj, code="VIEWER", name="Viewer")
        RolePermission.objects.create(role=role, permission_id="view_sales")
        viewer = User.objects.create_user(
            email="viewer_qf@example.com", password="pass-12345",
            client=self.client_obj, name="viewer", role=role,
        )
        quote = self._quotation([self._custom_line()])
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(viewer)['access']}")
        self.assertEqual(api.get(f"{self.API}/quotations/{quote['id']}/").status_code, 200)
        for verb in ("accept", "reject", "cancel", "convert-to-order"):
            resp = api.post(f"{self.API}/quotations/{quote['id']}/{verb}/", {}, format="json")
            self.assertEqual(resp.status_code, 403, verb)
        resp = api.post(f"{self.API}/quotations/", {
            "partyId": str(self.party.id), "date": str(date.today()),
            "lineItems": [self._custom_line()],
        }, format="json")
        self.assertEqual(resp.status_code, 403)

    # -- 16: edit / cancel ------------------------------------------------------
    def test_edit_draft_then_cancel_downstream(self):
        quote = self._quotation([self._custom_line()])
        resp = self.api.patch(f"{self.API}/quotations/{quote['id']}/", {
            "paymentTerms": "100% advance",
            "lineItems": [self._custom_line(), {"description": "Painting", "qty": 1, "rate": 5000, "tax": 18}],
        }, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        edited = resp.json()
        self.assertEqual(edited["paymentTerms"], "100% advance")
        self.assertEqual(len(edited["lineItems"]), 2)
        self.assertEqual(Decimal(str(edited["total"])), Decimal("106200.00"))

        order = self._post(f"{self.API}/quotations/{quote['id']}/convert-to-order/")
        invoice = self._post(f"{self.API}/orders/{order['id']}/convert-to-invoice/", {})
        self._post(f"{self.API}/invoices/{invoice['id']}/finalize/", expected=200)
        # An order with a live invoice cannot be cancelled...
        self._post(f"{self.API}/orders/{order['id']}/cancel/", {"reason": "x"}, expected=409)
        # ...until the invoice is cancelled first.
        cancelled = self._post(f"{self.API}/invoices/{invoice['id']}/cancel/", {"reason": "Wrong rate"}, expected=200)
        self.assertEqual(cancelled["status"], "Cancelled")
        order_cancel = self._post(f"{self.API}/orders/{order['id']}/cancel/", {"reason": "Customer withdrew"}, expected=200)
        self.assertEqual(order_cancel["stage"], "Cancelled")

    # -- metal-industry line detail ----------------------------------------------
    def test_metal_line_detail_travels_quotation_to_invoice(self):
        fabrication = {
            **self._custom_line(), "lineKind": "Fabrication", "materialGrade": "MS IS 2062",
            "specification": "1800x900x750 mm, 40x40 pipe, powder coated", "unitWeight": 45.5,
        }
        spare = {**self._inventory_line(2), "lineKind": "Spare Part"}
        quote = self._quotation([fabrication, spare])
        self.assertEqual(quote["lineItems"][0]["materialGrade"], "MS IS 2062")
        self.assertEqual(Decimal(str(quote["lineItems"][0]["unitWeight"])), Decimal("45.5"))

        order = self._post(f"{self.API}/quotations/{quote['id']}/convert-to-order/")
        challan = self._post(f"{self.API}/orders/{order['id']}/convert-to-challan/", {
            "lines": [{"lineId": order["lineItems"][0]["id"], "qty": 1}],
        })
        invoice = self._post(f"{self.API}/orders/{order['id']}/convert-to-invoice/", {})
        from_challan = self._post(f"{self.API}/challans/{challan['id']}/convert-to-invoice/")
        for doc in (order, challan, invoice, from_challan):
            line = doc["lineItems"][0]
            self.assertEqual(line["lineKind"], "Fabrication")
            self.assertEqual(line["materialGrade"], "MS IS 2062")
            self.assertEqual(line["specification"], "1800x900x750 mm, 40x40 pipe, powder coated")
            self.assertEqual(Decimal(str(line["unitWeight"])), Decimal("45.5"))
        self.assertEqual(order["lineItems"][1]["lineKind"], "Spare Part")

    def test_unknown_line_kind_is_refused(self):
        resp = self.api.post(f"{self.API}/quotations/", {
            "partyId": str(self.party.id), "date": str(date.today()),
            "lineItems": [{**self._custom_line(), "lineKind": "Banana"}],
        }, format="json")
        self.assertEqual(resp.status_code, 400, resp.content)

    # -- sheet-metal calculator + weighbridge -----------------------------------
    def _sheet_line(self):
        # SS 304 sheet 1.5 x 1250 x 2500 mm: 1.5*1250*2500*7.93/1e6 = 37.172 kg/pc, 10 pcs, billed by kg.
        return {
            "description": "SS 304 Sheet 2B", "lineKind": "Sheet Metal",
            "materialGrade": "SS 304", "specification": "Sheet · 2B · 1.5 x 1250 x 2500 mm · 10 pcs",
            "qty": 371.72, "uom": "Kg", "rate": 245, "tax": 18, "hsnCode": "7219",
            "sheetSpec": {
                "material": "SS 304", "form": "Sheet", "finish": "2B", "thicknessMm": 1.5,
                "widthMm": 1250, "lengthMm": 2500, "pieces": 10, "weightPerPiece": 37.172, "basis": "kg",
            },
        }

    def test_sheet_spec_travels_and_bills_by_weight(self):
        quote = self._quotation([self._sheet_line()])
        line = quote["lineItems"][0]
        self.assertEqual(line["sheetSpec"]["pieces"], 10)
        self.assertEqual(line["sheetSpec"]["basis"], "kg")
        self.assertEqual(line["lineKind"], "Sheet Metal")
        # Billed per kg: 371.72 kg x 245 = 91071.40, +18% GST.
        self.assertEqual(Decimal(str(quote["subtotal"])), Decimal("91071.40"))
        order = self._post(f"{self.API}/quotations/{quote['id']}/convert-to-order/")
        invoice = self._post(f"{self.API}/orders/{order['id']}/convert-to-invoice/", {})
        self.assertEqual(invoice["lineItems"][0]["sheetSpec"]["thicknessMm"], 1.5)
        self.assertEqual(invoice["lineItems"][0]["uom"], "Kg")

    def test_bad_sheet_spec_is_refused(self):
        for spec in ({"basis": "tonne"}, {"thicknessMm": -1}, "sheet"):
            resp = self.api.post(f"{self.API}/quotations/", {
                "partyId": str(self.party.id), "date": str(date.today()),
                "lineItems": [{**self._sheet_line(), "sheetSpec": spec}],
            }, format="json")
            self.assertEqual(resp.status_code, 400, (spec, resp.content))

    def test_challan_weighbridge_net_weight(self):
        quote = self._quotation([self._sheet_line()])
        order = self._post(f"{self.API}/quotations/{quote['id']}/convert-to-order/")
        challan = self._post(f"{self.API}/orders/{order['id']}/convert-to-challan/", {})
        resp = self.api.patch(f"{self.API}/challans/{challan['id']}/", {
            "weighbridgeSlip": "WB-5521", "grossWeight": 8420.5, "tareWeight": 8045,
        }, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["weighbridgeSlip"], "WB-5521")
        self.assertEqual(Decimal(str(body["netWeight"])), Decimal("375.500"))
        resp = self.api.patch(f"{self.API}/challans/{challan['id']}/", {"tareWeight": 9000}, format="json")
        self.assertEqual(resp.status_code, 400, resp.content)
