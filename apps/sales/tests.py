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
