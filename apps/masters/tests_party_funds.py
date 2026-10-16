from datetime import date
from decimal import Decimal
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.accounting.models import Account, BankAccount
from apps.accounting.services import seed_chart_of_accounts
from apps.masters.models import Party
from apps.sales.models import CashPaymentReceipt, PaymentIn, SalesInvoice


class PartyDualStreamFundsTests(TestCase):
    def setUp(self):
        self.client_obj, _ = Client.objects.get_or_create(
            slug="test-tenant-funds", defaults={"name": "Test Tenant Funds"}
        )
        seed_chart_of_accounts(self.client_obj)
        self.user = User.objects.filter(email="party_funds_user@example.com").first()
        if not self.user:
            self.user = User.objects.create_superuser(
                email="party_funds_user@example.com",
                password="password123",
                client=self.client_obj,
            )
        self.party = Party.objects.create(
            client=self.client_obj,
            code="CUST-FUNDS-01",
            type="Customer",
            name="Alpha Industries",
            email="alpha@example.com",
        )
        self.api_client = APIClient()
        tokens = build_tokens(self.user)
        self.api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

        bank_asset = Account.objects.filter(client=self.client_obj, type="Asset").first()
        self.bank_account = BankAccount.objects.create(
            client=self.client_obj,
            account=bank_asset,
            name="Main Bank",
            account_number="1234567890",
            bank_name="HDFC Bank",
            is_default=True,
        )

        self.invoice = SalesInvoice.objects.create(
            client=self.client_obj,
            invoice_number="INV-FUNDS-001",
            party=self.party,
            party_name="Alpha Industries",
            doc_date=date.today(),
            due_date=date.today(),
            subtotal=Decimal("2000.00"),
            taxable_value=Decimal("2000.00"),
            total_tax=Decimal("360.00"),
            total=Decimal("2360.00"),
            amount_paid=Decimal("0.00"),
            status="Unpaid",
            posted_at=timezone.now(),
        )

    def test_record_dual_formal_and_cash_funds(self):
        url = f"/api/v1/parties/{self.party.id}/record-funds/"
        payload = {
            "formalAmount": "1000.00",
            "cashAmount": "500.00",
            "formalMode": "Bank Transfer",
            "cashMode": "Cash",
            "formalReference": "FT-BANK-999",
            "cashReference": "CR-VOUCHER-111",
            "paymentDate": str(date.today()),
            "invoiceId": str(self.invoice.id),
            "description": "Dual settlement",
        }
        response = self.api_client.post(url, payload, format="json")
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertIn("formalPayment", data)
        self.assertIn("cashPayment", data)
        self.assertEqual(Decimal(str(data["totalRecorded"])), Decimal("1500.00"))

        # Check PaymentIn created
        self.assertTrue(PaymentIn.objects.filter(client=self.client_obj, party=self.party, reference_number="FT-BANK-999").exists())
        # Check Cash receipt created
        self.assertTrue(CashPaymentReceipt.objects.filter(client=self.client_obj, party=self.party, receipt_number=data["cashPayment"]["receiptNumber"]).exists())

        # Check invoice paid amount updated with formal amount
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.amount_paid, Decimal("1000.00"))

    def test_record_formal_only(self):
        url = f"/api/v1/parties/{self.party.id}/record-funds/"
        payload = {
            "formalAmount": "600.00",
            "cashAmount": "0.00",
            "formalMode": "Cheque",
            "formalReference": "CHQ-12345",
            "paymentDate": str(date.today()),
        }
        response = self.api_client.post(url, payload, format="json")
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertIsNotNone(data["formalPayment"])
        self.assertIsNone(data["cashPayment"])
        self.assertEqual(Decimal(str(data["totalRecorded"])), Decimal("600.00"))

    def test_record_cash_only(self):
        url = f"/api/v1/parties/{self.party.id}/record-funds/"
        payload = {
            "formalAmount": "0.00",
            "cashAmount": "750.00",
            "cashMode": "Cash",
            "cashReference": "CASH-REC-777",
            "paymentDate": str(date.today()),
        }
        response = self.api_client.post(url, payload, format="json")
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertIsNone(data["formalPayment"])
        self.assertIsNotNone(data["cashPayment"])
        self.assertEqual(Decimal(str(data["totalRecorded"])), Decimal("750.00"))

    def test_record_funds_validation_zero_and_negative(self):
        url = f"/api/v1/parties/{self.party.id}/record-funds/"
        # Both zero
        resp = self.api_client.post(url, {"formalAmount": "0.00", "cashAmount": "0.00"}, format="json")
        self.assertEqual(resp.status_code, 400)

        # Negative formal
        resp = self.api_client.post(url, {"formalAmount": "-100.00", "cashAmount": "0.00"}, format="json")
        self.assertEqual(resp.status_code, 400)

        # Negative cash
        resp = self.api_client.post(url, {"formalAmount": "0.00", "cashAmount": "-50.00"}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_party_summary_financial_segregation(self):
        # Post dual funds
        record_url = f"/api/v1/parties/{self.party.id}/record-funds/"
        self.api_client.post(
            record_url,
            {
                "formalAmount": "1000.00",
                "cashAmount": "400.00",
                "paymentDate": str(date.today()),
                "invoiceId": str(self.invoice.id),
            },
            format="json",
        )

        summary_url = f"/api/v1/parties/{self.party.id}/summary/"
        summary_resp = self.api_client.get(summary_url)
        self.assertEqual(summary_resp.status_code, 200)
        sdata = summary_resp.json()

        self.assertEqual(Decimal(str(sdata["formalAmountReceived"])), Decimal("1000.00"))
        self.assertEqual(Decimal(str(sdata["cashAmountReceived"])), Decimal("400.00"))
        self.assertEqual(Decimal(str(sdata["totalReceived"])), Decimal("1400.00"))
        # Invoice was 2360, paid 1000 formal -> remaining outstanding is 1360.00
        self.assertEqual(Decimal(str(sdata["outstanding"])), Decimal("1360.00"))

    def test_party_documents_includes_both_streams(self):
        record_url = f"/api/v1/parties/{self.party.id}/record-funds/"
        self.api_client.post(
            record_url,
            {
                "formalAmount": "800.00",
                "cashAmount": "300.00",
                "formalReference": "FORMAL-REF-1",
                "cashReference": "CASH-REF-1",
                "paymentDate": str(date.today()),
            },
            format="json",
        )

        docs_url = f"/api/v1/parties/{self.party.id}/documents/"
        docs_resp = self.api_client.get(docs_url)
        self.assertEqual(docs_resp.status_code, 200)
        results = docs_resp.json().get("results", [])
        doc_types = [d["documentType"] for d in results]
        self.assertIn("PaymentIn", doc_types)
        self.assertIn("CashPaymentReceipt", doc_types)
