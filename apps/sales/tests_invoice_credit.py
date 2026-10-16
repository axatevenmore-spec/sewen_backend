import uuid
from datetime import date
from decimal import Decimal
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.accounting.services import seed_chart_of_accounts
from apps.masters.models import Party
from apps.sales.models import CreditAllocation, PaymentIn, SalesInvoice, SalesReturn
from apps.sales import services


class InvoiceCreditConnectionTests(TestCase):
    def setUp(self):
        self.client_obj, _ = Client.objects.get_or_create(
            slug=f"tenant-{uuid.uuid4().hex[:8]}", defaults={"name": "Test Tenant"}
        )
        seed_chart_of_accounts(self.client_obj)

        self.user = User.objects.create_superuser(
            email=f"sales_admin_{uuid.uuid4().hex[:6]}@example.com",
            password="password123",
            client=self.client_obj,
        )

        self.party_a = Party.objects.create(
            client=self.client_obj,
            code="CUST-CREDIT-A",
            type="Customer",
            name="Party A Industrial Corp",
            email="partya@example.com",
            place_of_supply="Maharashtra (27)",
        )

        self.party_b = Party.objects.create(
            client=self.client_obj,
            code="CUST-CREDIT-B",
            type="Customer",
            name="Party B Enterprises",
            email="partyb@example.com",
            place_of_supply="Maharashtra (27)",
        )

        from apps.accounting.models import Account, BankAccount
        bank_coa = Account.objects.filter(client=self.client_obj, system_key="bank").first() or Account.objects.filter(client=self.client_obj).first()
        BankAccount.objects.create(
            client=self.client_obj,
            account=bank_coa,
            name="Primary Current Account",
            type="Bank",
            bank_name="State Bank of India",
            account_number="SBI-100200300",
            is_default=True,
        )

        self.api_client = APIClient()
        tokens = build_tokens(self.user)
        self.api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

    def test_prompt_example_scenario_full_credit_application(self):
        """
        User Prompt Example:
        - Party available credit: ₹60,000
        - New invoice: ₹50,000
        - Credit applied: ₹50,000
        - Remaining invoice due: ₹0
        - Remaining party credit: ₹10,000
        - Ineligible as payable once due is 0.
        """
        # 1. Record advance payment of ₹60,000 for Party A
        advance_pmt = services.record_payment_in(
            client=self.client_obj,
            party=self.party_a,
            amount=Decimal("60000.00"),
            payment_date=date.today(),
            payment_type="WITH_BILL",
            mode="Cash",
            user=self.user,
            description="Advance from customer",
        )

        # Verify initial available credit
        avail_credit = services.get_party_available_credit(self.client_obj.id, self.party_a.id)
        self.assertEqual(avail_credit, Decimal("60000.00"))

        # 2. Create invoice for ₹50,000
        invoice = SalesInvoice.objects.create(
            client=self.client_obj,
            invoice_number="INV-60K-50K",
            party=self.party_a,
            party_name=self.party_a.name,
            doc_date=date.today(),
            due_date=date.today(),
            subtotal=Decimal("50000.00"),
            taxable_value=Decimal("50000.00"),
            total_tax=Decimal("0.00"),
            total=Decimal("50000.00"),
            amount_paid=Decimal("0.00"),
            status="Unpaid",
            posted_at=timezone.now(),
        )

        # 3. Apply credit to invoice
        services.apply_party_credit_to_invoice(invoice, user=self.user)
        invoice.refresh_from_db()

        # Check results
        self.assertEqual(invoice.applied_credit, Decimal("50000.00"))
        self.assertEqual(invoice.balance_due, Decimal("0.00"))
        self.assertEqual(invoice.remaining_due, Decimal("0.00"))
        self.assertEqual(invoice.status, "Paid")

        # Verify remaining party credit is ₹10,000
        rem_party_credit = services.get_party_available_credit(self.client_obj.id, self.party_a.id)
        self.assertEqual(rem_party_credit, Decimal("10000.00"))

        # Verify invoice does not appear in payable list
        resp = self.api_client.get("/api/v1/sales/invoices/?payable=true")
        self.assertEqual(resp.status_code, 200)
        payable_ids = [item["id"] for item in resp.data.get("results", resp.data)]
        self.assertNotIn(str(invoice.id), payable_ids)

    def test_partial_credit_application_and_payable_status(self):
        """
        If credit < invoice total:
        - Party available credit: ₹20,000
        - New invoice: ₹50,000
        - Credit applied: ₹20,000
        - Remaining invoice due: ₹30,000
        - Remaining party credit: ₹0
        - Invoice appears as payable for ₹30,000.
        """
        services.record_payment_in(
            client=self.client_obj,
            party=self.party_a,
            amount=Decimal("20000.00"),
            payment_date=date.today(),
            payment_type="WITH_BILL",
            mode="Cash",
            user=self.user,
        )

        invoice = SalesInvoice.objects.create(
            client=self.client_obj,
            invoice_number="INV-PARTIAL-01",
            party=self.party_a,
            party_name=self.party_a.name,
            doc_date=date.today(),
            due_date=date.today(),
            subtotal=Decimal("50000.00"),
            taxable_value=Decimal("50000.00"),
            total_tax=Decimal("0.00"),
            total=Decimal("50000.00"),
            amount_paid=Decimal("0.00"),
            status="Unpaid",
            posted_at=timezone.now(),
        )

        services.apply_party_credit_to_invoice(invoice, user=self.user)
        invoice.refresh_from_db()

        self.assertEqual(invoice.applied_credit, Decimal("20000.00"))
        self.assertEqual(invoice.balance_due, Decimal("30000.00"))
        self.assertEqual(invoice.remaining_due, Decimal("30000.00"))
        self.assertEqual(invoice.status, "Partially Paid")

        rem_party_credit = services.get_party_available_credit(self.client_obj.id, self.party_a.id)
        self.assertEqual(rem_party_credit, Decimal("0.00"))

        # Outstanding endpoint check
        resp = self.api_client.get(f"/api/v1/sales/invoices/{invoice.id}/outstanding/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Decimal(str(resp.data["appliedCredit"])), Decimal("20000.00"))
        self.assertEqual(Decimal(str(resp.data["outstanding"])), Decimal("30000.00"))
        self.assertEqual(Decimal(str(resp.data["balanceDue"])), Decimal("30000.00"))

        # Payable query check: invoice must appear as payable
        payable_resp = self.api_client.get("/api/v1/sales/invoices/?payable=true")
        self.assertEqual(payable_resp.status_code, 200)
        payable_ids = [item["id"] for item in payable_resp.data.get("results", payable_resp.data)]
        self.assertIn(str(invoice.id), payable_ids)

    def test_multiple_invoices_consume_credit_without_duplication(self):
        """
        FIFO credit consumption across multiple invoices:
        Party Credit: ₹60,000
        Invoice 1: ₹50,000 -> credit applied ₹50,000, remaining ₹0
        Invoice 2: ₹25,000 -> credit applied ₹10,000, remaining ₹15,000
        Remaining party credit: ₹0
        """
        services.record_payment_in(
            client=self.client_obj,
            party=self.party_a,
            amount=Decimal("60000.00"),
            payment_date=date.today(),
            payment_type="WITH_BILL",
            mode="Cash",
            user=self.user,
        )

        inv1 = SalesInvoice.objects.create(
            client=self.client_obj,
            invoice_number="INV-MULTI-01",
            party=self.party_a,
            party_name=self.party_a.name,
            doc_date=date.today(),
            due_date=date.today(),
            subtotal=Decimal("50000.00"),
            total=Decimal("50000.00"),
            status="Unpaid",
            posted_at=timezone.now(),
        )
        services.apply_party_credit_to_invoice(inv1, user=self.user)
        inv1.refresh_from_db()
        self.assertEqual(inv1.applied_credit, Decimal("50000.00"))
        self.assertEqual(inv1.balance_due, Decimal("0.00"))

        inv2 = SalesInvoice.objects.create(
            client=self.client_obj,
            invoice_number="INV-MULTI-02",
            party=self.party_a,
            party_name=self.party_a.name,
            doc_date=date.today(),
            due_date=date.today(),
            subtotal=Decimal("25000.00"),
            total=Decimal("25000.00"),
            status="Unpaid",
            posted_at=timezone.now(),
        )
        services.apply_party_credit_to_invoice(inv2, user=self.user)
        inv2.refresh_from_db()
        self.assertEqual(inv2.applied_credit, Decimal("10000.00"))
        self.assertEqual(inv2.balance_due, Decimal("15000.00"))

        rem_party_credit = services.get_party_available_credit(self.client_obj.id, self.party_a.id)
        self.assertEqual(rem_party_credit, Decimal("0.00"))

    def test_party_isolation_cannot_cross_allocate(self):
        """Credit of Party A must NEVER be applied to Party B's invoices."""
        services.record_payment_in(
            client=self.client_obj,
            party=self.party_a,
            amount=Decimal("60000.00"),
            payment_date=date.today(),
            payment_type="WITH_BILL",
            mode="Cash",
            user=self.user,
        )

        inv_b = SalesInvoice.objects.create(
            client=self.client_obj,
            invoice_number="INV-PARTY-B",
            party=self.party_b,
            party_name=self.party_b.name,
            doc_date=date.today(),
            due_date=date.today(),
            subtotal=Decimal("40000.00"),
            total=Decimal("40000.00"),
            status="Unpaid",
            posted_at=timezone.now(),
        )

        services.apply_party_credit_to_invoice(inv_b, user=self.user)
        inv_b.refresh_from_db()

        self.assertEqual(inv_b.applied_credit, Decimal("0.00"))
        self.assertEqual(inv_b.balance_due, Decimal("40000.00"))
        self.assertEqual(inv_b.status, "Unpaid")

        party_a_credit = services.get_party_available_credit(self.client_obj.id, self.party_a.id)
        self.assertEqual(party_a_credit, Decimal("60000.00"))

    def test_invoice_cancellation_restores_party_credit(self):
        """Cancelling an invoice that consumed credit must restore the credit to the party."""
        services.record_payment_in(
            client=self.client_obj,
            party=self.party_a,
            amount=Decimal("60000.00"),
            payment_date=date.today(),
            payment_type="WITH_BILL",
            mode="Cash",
            user=self.user,
        )

        invoice = SalesInvoice.objects.create(
            client=self.client_obj,
            invoice_number="INV-CANCEL-RESTORE",
            party=self.party_a,
            party_name=self.party_a.name,
            doc_date=date.today(),
            due_date=date.today(),
            subtotal=Decimal("50000.00"),
            total=Decimal("50000.00"),
            status="Unpaid",
            posted_at=timezone.now(),
        )
        services.apply_party_credit_to_invoice(invoice, user=self.user)
        invoice.refresh_from_db()
        self.assertEqual(services.get_party_available_credit(self.client_obj.id, self.party_a.id), Decimal("10000.00"))

        # Cancel the invoice
        services.cancel_invoice(invoice, reason="Customer requested cancellation", user=self.user)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, "Cancelled")

        # Credit must be fully restored to ₹60,000
        restored_credit = services.get_party_available_credit(self.client_obj.id, self.party_a.id)
        self.assertEqual(restored_credit, Decimal("60000.00"))

    def test_credit_note_sales_return_eligible_source(self):
        """SalesReturn / Credit Note acts as eligible party credit source."""
        parent_inv = SalesInvoice.objects.create(
            client=self.client_obj,
            invoice_number="INV-PARENT-CRN",
            party=self.party_a,
            party_name=self.party_a.name,
            doc_date=date.today(),
            due_date=date.today(),
            subtotal=Decimal("15000.00"),
            total=Decimal("15000.00"),
            amount_paid=Decimal("15000.00"),
            status="Paid",
            posted_at=timezone.now(),
        )

        SalesReturn.objects.create(
            client=self.client_obj,
            sales_invoice=parent_inv,
            return_number="RET-CRN-01",
            credit_note_number="CRN-2026-001",
            party=self.party_a,
            party_name=self.party_a.name,
            doc_date=date.today(),
            status="Posted",
            subtotal=Decimal("15000.00"),
            total_tax=Decimal("0.00"),
            total=Decimal("15000.00"),
        )

        avail_credit = services.get_party_available_credit(self.client_obj.id, self.party_a.id)
        self.assertEqual(avail_credit, Decimal("15000.00"))

        invoice = SalesInvoice.objects.create(
            client=self.client_obj,
            invoice_number="INV-CRN-CONSUME",
            party=self.party_a,
            party_name=self.party_a.name,
            doc_date=date.today(),
            due_date=date.today(),
            subtotal=Decimal("20000.00"),
            total=Decimal("20000.00"),
            status="Unpaid",
            posted_at=timezone.now(),
        )
        services.apply_party_credit_to_invoice(invoice, user=self.user)
        invoice.refresh_from_db()

        self.assertEqual(invoice.applied_credit, Decimal("15000.00"))
        self.assertEqual(invoice.balance_due, Decimal("5000.00"))
        self.assertEqual(services.get_party_available_credit(self.client_obj.id, self.party_a.id), Decimal("0.00"))

    def test_remove_credit_allocation_action(self):
        """Reversing or removing credit allocation from an invoice."""
        services.record_payment_in(
            client=self.client_obj,
            party=self.party_a,
            amount=Decimal("30000.00"),
            payment_date=date.today(),
            payment_type="WITH_BILL",
            mode="Cash",
            user=self.user,
        )

        invoice = SalesInvoice.objects.create(
            client=self.client_obj,
            invoice_number="INV-REMOVE-CREDIT",
            party=self.party_a,
            party_name=self.party_a.name,
            doc_date=date.today(),
            due_date=date.today(),
            subtotal=Decimal("30000.00"),
            total=Decimal("30000.00"),
            status="Unpaid",
            posted_at=timezone.now(),
        )
        services.apply_party_credit_to_invoice(invoice, user=self.user)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, "Paid")
        self.assertEqual(services.get_party_available_credit(self.client_obj.id, self.party_a.id), Decimal("0.00"))

        # Call remove-credit action via API
        resp = self.api_client.post(f"/api/v1/sales/invoices/{invoice.id}/remove-credit/")
        self.assertEqual(resp.status_code, 200)

        invoice.refresh_from_db()
        self.assertEqual(invoice.applied_credit, Decimal("0.00"))
        self.assertEqual(invoice.balance_due, Decimal("30000.00"))
        self.assertEqual(invoice.status, "Unpaid")

        restored_credit = services.get_party_available_credit(self.client_obj.id, self.party_a.id)
        self.assertEqual(restored_credit, Decimal("30000.00"))

    def test_record_with_bill_invoice_payment_prompt_scenario(self):
        """
        User Prompt Exact Example:
        - Invoice total: ₹1,00,000
        - Credit already allocated: ₹20,000
        - Previously allocated payment: ₹30,000
        - Current due: ₹50,000
        - New payment: ₹50,000
        - Final due: ₹0
        - The payment must not be deducted again from party credit if it is already recorded as an invoice payment.
        - Exclude invoice from payable list (?payable=true).
        """
        # 1. Setup party credit advance of ₹20,000
        services.record_payment_in(
            client=self.client_obj,
            party=self.party_a,
            amount=Decimal("20000.00"),
            payment_date=date.today(),
            payment_type="WITH_BILL",
            mode="Cash",
            user=self.user,
        )
        self.assertEqual(services.get_party_available_credit(self.client_obj.id, self.party_a.id), Decimal("20000.00"))

        # 2. Create invoice of ₹1,00,000 and apply the credit of ₹20,000
        invoice = SalesInvoice.objects.create(
            client=self.client_obj,
            invoice_number="INV-WITH-BILL-TEST-1",
            party=self.party_a,
            party_name=self.party_a.name,
            doc_date=date.today(),
            due_date=date.today(),
            subtotal=Decimal("100000.00"),
            total=Decimal("100000.00"),
            status="Unpaid",
            posted_at=timezone.now(),
        )
        services.apply_party_credit_to_invoice(invoice, user=self.user)
        invoice.refresh_from_db()
        self.assertEqual(invoice.applied_credit, Decimal("20000.00"))
        self.assertEqual(invoice.balance_due, Decimal("80000.00"))

        # 3. Record previously allocated payment of ₹30,000
        pay_prev_resp = self.api_client.post(
            "/api/v1/sales/payments/",
            {
                "customerId": str(self.party_a.id),
                "invoiceId": str(invoice.id),
                "paymentType": "WITH_BILL",
                "amount": "30000.00",
                "date": str(date.today()),
                "mode": "Bank Transfer",
                "reference_number": "UTR-PREV-30K",
            },
            format="json",
        )
        self.assertEqual(pay_prev_resp.status_code, 201)

        invoice.refresh_from_db()
        self.assertEqual(invoice.amount_paid, Decimal("30000.00"))
        self.assertEqual(invoice.applied_credit, Decimal("20000.00"))
        self.assertEqual(invoice.balance_due, Decimal("50000.00"))
        self.assertEqual(invoice.status, "Partially Paid")

        # Check payable list includes it with due 50,000
        list_resp = self.api_client.get(f"/api/v1/sales/invoices/?payable=true&customerId={self.party_a.id}")
        self.assertEqual(list_resp.status_code, 200)
        payable_ids = [item["id"] for item in list_resp.data.get("results", list_resp.data)]
        self.assertIn(str(invoice.id), payable_ids)
        matched_item = next(item for item in list_resp.data.get("results", list_resp.data) if item["id"] == str(invoice.id))
        self.assertEqual(Decimal(str(matched_item["balanceDue"])), Decimal("50000.00"))
        self.assertEqual(Decimal(str(matched_item["appliedCredit"])), Decimal("20000.00"))
        self.assertEqual(Decimal(str(matched_item["paidAmount"])), Decimal("30000.00"))

        # 4. Attempt payment exceeding balance (> 50,000) -> must fail validation
        overpay_resp = self.api_client.post(
            "/api/v1/sales/payments/",
            {
                "customerId": str(self.party_a.id),
                "invoiceId": str(invoice.id),
                "paymentType": "WITH_BILL",
                "amount": "50001.00",
                "date": str(date.today()),
                "mode": "Bank Transfer",
            },
            format="json",
        )
        self.assertEqual(overpay_resp.status_code, 400)

        # 5. Record new payment of exact current due: ₹50,000
        pay_final_resp = self.api_client.post(
            "/api/v1/sales/payments/",
            {
                "customerId": str(self.party_a.id),
                "invoiceId": str(invoice.id),
                "paymentType": "WITH_BILL",
                "amount": "50000.00",
                "date": str(date.today()),
                "mode": "Bank Transfer",
                "reference_number": "UTR-FINAL-50K",
            },
            format="json",
        )
        self.assertEqual(pay_final_resp.status_code, 201)

        invoice.refresh_from_db()
        self.assertEqual(invoice.amount_paid, Decimal("80000.00"))
        self.assertEqual(invoice.applied_credit, Decimal("20000.00"))
        self.assertEqual(invoice.balance_due, Decimal("0.00"))
        self.assertEqual(invoice.status, "Paid")

        # 6. Exclude invoice from payable list once due is zero
        list_after = self.api_client.get(f"/api/v1/sales/invoices/?payable=true&customerId={self.party_a.id}")
        self.assertEqual(list_after.status_code, 200)
        payable_after_ids = [item["id"] for item in list_after.data.get("results", list_after.data)]
        self.assertNotIn(str(invoice.id), payable_after_ids)

        # 7. Check that payments recorded against invoices are NOT counted as unused party credit
        # All party credit was allocated (20k used), all payments were allocated (30k + 50k used)
        self.assertEqual(services.get_party_available_credit(self.client_obj.id, self.party_a.id), Decimal("0.00"))

        # 8. Attempting to record any further payment on this paid invoice must be rejected
        extra_pay_resp = self.api_client.post(
            "/api/v1/sales/payments/",
            {
                "customerId": str(self.party_a.id),
                "invoiceId": str(invoice.id),
                "paymentType": "WITH_BILL",
                "amount": "100.00",
                "date": str(date.today()),
                "mode": "Bank Transfer",
            },
            format="json",
        )
        self.assertEqual(extra_pay_resp.status_code, 400)

    def test_cross_customer_and_cancelled_invoice_payment_rejected(self):
        """Cross customer payment or payment against cancelled invoice is strictly blocked."""
        # Party B's invoice
        inv_b = SalesInvoice.objects.create(
            client=self.client_obj,
            invoice_number="INV-PARTY-B",
            party=self.party_b,
            party_name=self.party_b.name,
            doc_date=date.today(),
            due_date=date.today(),
            subtotal=Decimal("15000.00"),
            total=Decimal("15000.00"),
            status="Unpaid",
            posted_at=timezone.now(),
        )

        # Try to pay Party B's invoice using Party A
        cross_resp = self.api_client.post(
            "/api/v1/sales/payments/",
            {
                "customerId": str(self.party_a.id),
                "invoiceId": str(inv_b.id),
                "paymentType": "WITH_BILL",
                "amount": "5000.00",
                "date": str(date.today()),
                "mode": "Bank Transfer",
            },
            format="json",
        )
        self.assertEqual(cross_resp.status_code, 400)

        # Cancel Party B's invoice
        inv_b.status = "Cancelled"
        inv_b.save(update_fields=["status"])

        cancelled_resp = self.api_client.post(
            "/api/v1/sales/payments/",
            {
                "customerId": str(self.party_b.id),
                "invoiceId": str(inv_b.id),
                "paymentType": "WITH_BILL",
                "amount": "5000.00",
                "date": str(date.today()),
                "mode": "Bank Transfer",
            },
            format="json",
        )
        self.assertEqual(cancelled_resp.status_code, 400)

    def test_record_without_bill_cash_receipt_prompt_example(self):
        """
        User Prompt Requirements & Example:
        - Invoice due: ₹40,000
        - Cash receipt recorded as an unallocated advance: ₹10,000
        - Invoice due remains ₹40,000 until the advance is explicitly and validly allocated.
        - After valid allocation of ₹10,000, invoice due becomes ₹30,000.
        - Cancellation/reversal restores balance accurately with audit history.
        - Prevent duplicate allocation or allocation to unrelated parties.
        """
        from apps.sales.models import CashPaymentReceipt

        # 1. Create formal invoice of ₹40,000 for Party A
        invoice = SalesInvoice.objects.create(
            client=self.client_obj,
            invoice_number="INV-CASH-TEST-1",
            party=self.party_a,
            party_name=self.party_a.name,
            doc_date=date.today(),
            due_date=date.today(),
            subtotal=Decimal("40000.00"),
            total=Decimal("40000.00"),
            status="Unpaid",
            posted_at=timezone.now(),
        )
        self.assertEqual(invoice.balance_due, Decimal("40000.00"))

        # 2. Record Without-Bill cash receipt as an unallocated advance: ₹10,000
        cpr_resp = self.api_client.post(
            "/api/v1/sales/cash-receipts/",
            {
                "customerId": str(self.party_a.id),
                "amount": "10000.00",
                "paymentDate": str(date.today()),
                "mode": "Cash",
                "description": "Counter advance from Party A",
                "reference_number": "CASH-ADV-10K",
            },
            format="json",
        )
        self.assertEqual(cpr_resp.status_code, 201)
        receipt_id = cpr_resp.data["id"]
        receipt = CashPaymentReceipt.objects.get(pk=receipt_id)
        self.assertEqual(receipt.status, "RECEIVED")
        self.assertIsNone(receipt.invoice)
        self.assertEqual(receipt.amount, Decimal("10000.00"))

        # 3. Requirement 6: Invoice due MUST REMAIN ₹40,000 (not automatically reduced)
        invoice.refresh_from_db()
        self.assertEqual(invoice.balance_due, Decimal("40000.00"))
        self.assertEqual(invoice.amount_paid, Decimal("0.00"))
        self.assertEqual(invoice.applied_credit, Decimal("0.00"))

        # 4. Attempt to allocate cash advance to another customer's invoice (Party B) -> must fail
        inv_b = SalesInvoice.objects.create(
            client=self.client_obj,
            invoice_number="INV-PARTY-B-CASH",
            party=self.party_b,
            party_name=self.party_b.name,
            doc_date=date.today(),
            due_date=date.today(),
            subtotal=Decimal("20000.00"),
            total=Decimal("20000.00"),
            status="Unpaid",
            posted_at=timezone.now(),
        )
        cross_alloc_resp = self.api_client.post(
            f"/api/v1/sales/cash-receipts/{receipt.id}/allocate-invoice/",
            {"invoiceId": str(inv_b.id), "amount": "10000.00"},
            format="json",
        )
        self.assertEqual(cross_alloc_resp.status_code, 400)

        # 5. Explicitly and validly allocate ₹10,000 cash receipt to Party A's invoice
        alloc_resp = self.api_client.post(
            f"/api/v1/sales/cash-receipts/{receipt.id}/allocate-invoice/",
            {"invoiceId": str(invoice.id), "amount": "10000.00"},
            format="json",
        )
        self.assertEqual(alloc_resp.status_code, 200)

        # 6. Requirement 5 & Example: Invoice due becomes ₹30,000
        invoice.refresh_from_db()
        self.assertEqual(invoice.applied_credit, Decimal("10000.00"))
        self.assertEqual(invoice.balance_due, Decimal("30000.00"))
        self.assertEqual(invoice.status, "Partially Paid")

        # 7. Requirement 7: Prevent the same cash receipt from being allocated more than once
        dup_alloc_resp = self.api_client.post(
            f"/api/v1/sales/cash-receipts/{receipt.id}/allocate-invoice/",
            {"invoiceId": str(invoice.id), "amount": "10000.00"},
            format="json",
        )
        self.assertIn(dup_alloc_resp.status_code, (400, 422))

        # 8. Requirement 10: Cancellation & reversal restores invoice balance and audit trail
        cancel_resp = self.api_client.post(
            f"/api/v1/sales/cash-receipts/{receipt.id}/cancel/",
            {"reason": "Customer requested cash return"},
            format="json",
        )
        self.assertEqual(cancel_resp.status_code, 200)
        self.assertEqual(cancel_resp.data["status"], "CANCELLED")

        # Invoice balance is restored back to ₹40,000
        invoice.refresh_from_db()
        self.assertEqual(invoice.applied_credit, Decimal("0.00"))
        self.assertEqual(invoice.balance_due, Decimal("40000.00"))
        self.assertEqual(invoice.status, "Unpaid")

    def test_direct_cash_receipt_with_linked_invoice_at_creation(self):
        """Recording cash receipt with linked invoice directly at creation."""
        invoice = SalesInvoice.objects.create(
            client=self.client_obj,
            invoice_number="INV-DIRECT-CASH",
            party=self.party_a,
            party_name=self.party_a.name,
            doc_date=date.today(),
            due_date=date.today(),
            subtotal=Decimal("50000.00"),
            total=Decimal("50000.00"),
            status="Unpaid",
            posted_at=timezone.now(),
        )

        resp = self.api_client.post(
            "/api/v1/sales/cash-receipts/",
            {
                "customerId": str(self.party_a.id),
                "invoiceId": str(invoice.id),
                "amount": "15000.00",
                "paymentDate": str(date.today()),
                "mode": "Cash",
                "description": "Counter payment against invoice",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201)

        invoice.refresh_from_db()
        self.assertEqual(invoice.applied_credit, Decimal("15000.00"))
        self.assertEqual(invoice.balance_due, Decimal("35000.00"))
        self.assertEqual(invoice.status, "Partially Paid")

    def test_e2e_accounting_calculations_and_party_summary(self):
        """End-to-end test verifying:
        1. Invoice Gross Amount = subtotal + taxes - discounts.
        2. Allocated Payments = direct payments.
        3. Applied Credit = advance/credit note credit allocations.
        4. Remaining Due = Gross - Allocated Payments - Applied Credit.
        5. Outstanding list, overdue, and payable filters match detail balances.
        6. Party summary outstanding eliminates double-counting.
        7. Multiple invoices for same customer do not cross-contaminate.
        """
        # Create Advance Payment of ₹20,000 for Party A
        adv_resp = self.api_client.post(
            "/api/v1/sales/payments/",
            {
                "customerId": str(self.party_a.id),
                "amount": "20000.00",
                "date": str(date.today()),
                "mode": "Bank Transfer",
                "paymentType": "WITH_BILL",
                "description": "Advance Payment A",
            },
            format="json",
        )
        self.assertEqual(adv_resp.status_code, 201)

        # Create Invoice 1 of ₹50,000 for Party A -> Auto-consumes ₹20,000 credit -> remaining due ₹30,000
        inv1 = SalesInvoice.objects.create(
            client=self.client_obj,
            invoice_number="INV-E2E-001",
            party=self.party_a,
            party_name=self.party_a.name,
            doc_date=date.today(),
            due_date=date.today(),
            subtotal=Decimal("50000.00"),
            total=Decimal("50000.00"),
            status="Unpaid",
            posted_at=timezone.now(),
        )
        services.apply_party_credit_to_invoice(inv1, user=self.user)
        inv1.refresh_from_db()

        self.assertEqual(inv1.applied_credit, Decimal("20000.00"))
        self.assertEqual(inv1.amount_paid, Decimal("0.00"))
        self.assertEqual(inv1.balance_due, Decimal("30000.00"))
        self.assertEqual(inv1.status, "Partially Paid")

        # Create Invoice 2 of ₹40,000 for Party A -> Available credit is 0 -> remaining due ₹40,000
        inv2 = SalesInvoice.objects.create(
            client=self.client_obj,
            invoice_number="INV-E2E-002",
            party=self.party_a,
            party_name=self.party_a.name,
            doc_date=date.today(),
            due_date=date.today(),
            subtotal=Decimal("40000.00"),
            total=Decimal("40000.00"),
            status="Unpaid",
            posted_at=timezone.now(),
        )
        services.apply_party_credit_to_invoice(inv2, user=self.user)
        inv2.refresh_from_db()

        self.assertEqual(inv2.applied_credit, Decimal("0.00"))
        self.assertEqual(inv2.balance_due, Decimal("40000.00"))
        self.assertEqual(inv2.status, "Unpaid")

        # Record partial payment of ₹10,000 against Invoice 1 -> remaining due becomes ₹20,000
        pmt_resp = self.api_client.post(
            "/api/v1/sales/payments/",
            {
                "customerId": str(self.party_a.id),
                "invoiceId": str(inv1.id),
                "amount": "10000.00",
                "date": str(date.today()),
                "mode": "Bank Transfer",
                "paymentType": "WITH_BILL",
                "description": "Partial payment against INV-E2E-001",
            },
            format="json",
        )
        self.assertEqual(pmt_resp.status_code, 201)

        inv1.refresh_from_db()
        self.assertEqual(inv1.amount_paid, Decimal("10000.00"))
        self.assertEqual(inv1.applied_credit, Decimal("20000.00"))
        self.assertEqual(inv1.balance_due, Decimal("20000.00"))
        self.assertEqual(inv1.status, "Partially Paid")

        # Verify Party Summary for Party A: Outstanding = ₹20,000 (inv1) + ₹40,000 (inv2) = ₹60,000
        summary_resp = self.api_client.get(f"/api/v1/masters/parties/{self.party_a.id}/summary/")
        self.assertEqual(summary_resp.status_code, 200)
        self.assertEqual(summary_resp.data["outstanding"], Decimal("60000.00"))
        self.assertEqual(summary_resp.data["openInvoices"], 2)

        # Verify invoice list aggregates: outstanding matches sum of balance_due
        list_resp = self.api_client.get(f"/api/v1/sales/invoices/?customerId={self.party_a.id}")
        self.assertEqual(list_resp.status_code, 200)
        aggregates = list_resp.data.get("aggregates", {})
        self.assertEqual(aggregates.get("outstanding"), Decimal("60000.00"))

        # Verify payable filter only returns invoices with balance_due > 0
        payable_resp = self.api_client.get(f"/api/v1/sales/invoices/?customerId={self.party_a.id}&payable=true")
        self.assertEqual(payable_resp.status_code, 200)
        payable_ids = [item["id"] for item in payable_resp.data.get("results", [])]
        self.assertIn(str(inv1.id), payable_ids)
        self.assertIn(str(inv2.id), payable_ids)

        # Pay off the remaining ₹20,000 on Invoice 1
        full_pay_resp = self.api_client.post(
            "/api/v1/sales/payments/",
            {
                "customerId": str(self.party_a.id),
                "invoiceId": str(inv1.id),
                "amount": "20000.00",
                "date": str(date.today()),
                "mode": "Bank Transfer",
                "paymentType": "WITH_BILL",
            },
            format="json",
        )
        self.assertEqual(full_pay_resp.status_code, 201)

        inv1.refresh_from_db()
        self.assertEqual(inv1.amount_paid, Decimal("30000.00"))
        self.assertEqual(inv1.applied_credit, Decimal("20000.00"))
        self.assertEqual(inv1.balance_due, Decimal("0.00"))
        self.assertEqual(inv1.status, "Paid")

        # Now Invoice 1 MUST NOT appear in payable list
        payable_resp_after = self.api_client.get(f"/api/v1/sales/invoices/?customerId={self.party_a.id}&payable=true")
        payable_ids_after = [item["id"] for item in payable_resp_after.data.get("results", [])]
        self.assertNotIn(str(inv1.id), payable_ids_after)
        self.assertIn(str(inv2.id), payable_ids_after)

