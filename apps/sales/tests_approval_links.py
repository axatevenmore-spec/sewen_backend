"""Customer approval links for estimates, quotations, proforma and sales invoices
(apps/sales/approval_links.py) -- the sales twin of the PMS proof link."""
from datetime import date, timedelta
from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, Role, User
from apps.accounts.permission_catalogue import seed_roles, sync_permissions
from apps.core.models import Notification
from apps.masters.models import Party
from apps.sales.models import (
    Estimate,
    ProformaInvoice,
    Quotation,
    QuotationActivity,
    QuotationLine,
    SalesApprovalLink,
    SalesInvoice,
)

API = "/api/v1"
PUBLIC = f"{API}/public/sales/approve"


class ApprovalLinkTests(TestCase):
    def setUp(self):
        cache.clear()
        sync_permissions()
        self.tenant = Client.objects.create(slug="approve", name="Approve Tenant")
        seed_roles(self.tenant)
        self.staff = User.objects.create_user(
            email="sales@approve.test", password="pass-12345", client=self.tenant, name="Sales Rep",
            role=Role.objects.get(client=self.tenant, code="AD"), status="Active",
        )
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.staff)['access']}")
        self.public = APIClient()
        self.party = Party.objects.create(
            client=self.tenant, code="C-1", type="Customer", name="Acme Corp"
        )

    def tearDown(self):
        cache.clear()

    def _doc(self, model, number_field, number, status="Draft", **extra):
        return model.objects.create(
            client=self.tenant, party=self.party, party_name="Acme Corp", doc_date=date.today(),
            subtotal=Decimal("1000"), taxable_value=Decimal("1000"), total_tax=Decimal("180"),
            total=Decimal("1180"), status=status, created_by=self.staff,
            **{number_field: number}, **extra,
        )

    def _share(self, path, **body):
        resp = self.api.post(f"{API}/sales/{path}/approval-links/", body, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        return resp.json()

    # -- quotation: the full round trip ---------------------------------------
    def test_quotation_link_view_comment_and_approve(self):
        quote = self._doc(Quotation, "quotation_number", "QT-1")
        QuotationLine.objects.create(
            client=self.tenant, quotation=quote, line_no=1, item_name="Steel rack",
            qty=Decimal("2"), rate=Decimal("500"), amount=Decimal("1000"),
            tax_pct=Decimal("18"), tax_amount=Decimal("180"), line_total=Decimal("1180"),
        )
        issued = self._share(f"quotations/{quote.id}", recipientName="Ravi", message="Please review")
        self.assertTrue(issued["url"].startswith("/sales/approve/"))
        quote.refresh_from_db()
        self.assertEqual(quote.status, "Sent")  # issuing a link sends it

        page = self.public.get(f"{PUBLIC}/{issued['token']}/")
        self.assertEqual(page.status_code, 200, page.content)
        body = page.json()
        self.assertEqual(body["document"]["number"], "QT-1")
        self.assertEqual(body["document"]["lineItems"][0]["itemName"], "Steel rack")
        self.assertEqual(body["message"], "Please review")
        self.assertTrue(body["canDecide"])
        quote.refresh_from_db()
        self.assertEqual(quote.status, "Viewed")

        thread = self.public.post(
            f"{PUBLIC}/{issued['token']}/comments/", {"text": "Can you do 10 days delivery?"}, format="json"
        )
        self.assertEqual(thread.status_code, 201)
        staff = self.api.post(
            f"{API}/sales/quotations/{quote.id}/approval-comments/", {"text": "Yes"}, format="json"
        )
        self.assertEqual(
            [(c["authorType"], c["text"]) for c in staff.json()["results"]],
            [("Client", "Can you do 10 days delivery?"), ("Staff", "Yes")],
        )

        decided = self.public.post(
            f"{PUBLIC}/{issued['token']}/decide/", {"decision": "Approved", "decidedBy": "Ravi K"},
            format="json",
        )
        self.assertEqual(decided.status_code, 200, decided.content)
        quote.refresh_from_db()
        self.assertEqual(quote.status, "Accepted")
        self.assertTrue(QuotationActivity.objects.filter(quotation=quote, event="accepted").exists())
        self.assertTrue(
            Notification.objects.filter(recipient=self.staff, type="sales.approval_decided").exists()
        )

        again = self.public.post(
            f"{PUBLIC}/{issued['token']}/decide/", {"decision": "Rejected", "rejectionReason": "x"},
            format="json",
        )
        self.assertEqual(again.status_code, 409)
        self.assertFalse(self.public.get(f"{PUBLIC}/{issued['token']}/").json()["canDecide"])

    def test_reject_requires_a_reason(self):
        estimate = self._doc(Estimate, "estimate_number", "EST-1")
        issued = self._share(f"estimates/{estimate.id}")
        resp = self.public.post(f"{PUBLIC}/{issued['token']}/decide/", {"decision": "Rejected"}, format="json")
        self.assertEqual(resp.status_code, 400)
        resp = self.public.post(
            f"{PUBLIC}/{issued['token']}/decide/",
            {"decision": "Rejected", "rejectionReason": "Price too high"}, format="json",
        )
        self.assertEqual(resp.status_code, 200)
        estimate.refresh_from_db()
        self.assertEqual(estimate.status, "Rejected")
        link = SalesApprovalLink.objects.get(document_id=estimate.id)
        self.assertEqual(link.rejection_reason, "Price too high")

    # -- per-type effects -------------------------------------------------------
    def test_proforma_approval_accepts_and_rejection_leaves_status(self):
        approved = self._doc(ProformaInvoice, "proforma_number", "PI-1")
        rejected = self._doc(ProformaInvoice, "proforma_number", "PI-2")
        for doc, decision in ((approved, "Approved"), (rejected, "Rejected")):
            token = self._share(f"proforma-invoices/{doc.id}")["token"]
            resp = self.public.post(
                f"{PUBLIC}/{token}/decide/", {"decision": decision, "rejectionReason": "Change terms"},
                format="json",
            )
            self.assertEqual(resp.status_code, 200, resp.content)
        approved.refresh_from_db()
        rejected.refresh_from_db()
        self.assertEqual((approved.status, rejected.status), ("Accepted", "Draft"))

    def test_invoice_decision_never_touches_payment_status_or_shows_cash_split(self):
        invoice = self._doc(
            SalesInvoice, "invoice_number", "INV-1", status="Unpaid",
            due_date=date.today(), cash_amount=Decimal("250"),
        )
        token = self._share(f"invoices/{invoice.id}")["token"]
        body = self.public.get(f"{PUBLIC}/{token}/").json()
        self.assertNotIn("cashAmount", body["document"])
        self.assertNotIn("250", str(body["document"]["totals"]))
        resp = self.public.post(f"{PUBLIC}/{token}/decide/", {"decision": "Approved"}, format="json")
        self.assertEqual(resp.status_code, 200)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, "Unpaid")

    # -- link lifecycle ---------------------------------------------------------
    def test_new_link_supersedes_the_old_and_revoke_closes_it(self):
        quote = self._doc(Quotation, "quotation_number", "QT-2")
        first = self._share(f"quotations/{quote.id}")
        second = self._share(f"quotations/{quote.id}")
        self.assertEqual(self.public.get(f"{PUBLIC}/{first['token']}/").status_code, 409)
        self.assertEqual(self.public.get(f"{PUBLIC}/{second['token']}/").status_code, 200)

        listed = self.api.get(f"{API}/sales/quotations/{quote.id}/approval-links/").json()["results"]
        self.assertEqual([row["status"] for row in listed], ["Active", "Revoked"])
        self.assertNotIn("token", listed[0])

        link_id = second["link"]["id"]
        revoked = self.api.post(
            f"{API}/sales/quotations/{quote.id}/approval-links/{link_id}/revoke/", {}, format="json"
        )
        self.assertEqual(revoked.json()["status"], "Revoked")
        self.assertEqual(self.public.get(f"{PUBLIC}/{second['token']}/").json()["code"], "TOKEN_REVOKED")

    def test_expired_and_unknown_links(self):
        quote = self._doc(Quotation, "quotation_number", "QT-3")
        token = self._share(f"quotations/{quote.id}")["token"]
        SalesApprovalLink.objects.update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.public.get(f"{PUBLIC}/{token}/").json()["code"], "TOKEN_EXPIRED")
        self.assertEqual(self.public.get(f"{PUBLIC}/not-a-real-token/").status_code, 404)

    def test_cancelled_document_cannot_be_shared_or_decided(self):
        quote = self._doc(Quotation, "quotation_number", "QT-4", status="Cancelled")
        resp = self.api.post(f"{API}/sales/quotations/{quote.id}/approval-links/", {}, format="json")
        self.assertEqual(resp.status_code, 409)

        live = self._doc(Quotation, "quotation_number", "QT-5")
        token = self._share(f"quotations/{live.id}")["token"]
        Quotation.objects.filter(pk=live.pk).update(status="Converted")
        self.assertFalse(self.public.get(f"{PUBLIC}/{token}/").json()["canDecide"])
        resp = self.public.post(f"{PUBLIC}/{token}/decide/", {"decision": "Approved"}, format="json")
        self.assertEqual(resp.status_code, 409)

    def test_sharing_needs_edit_permission(self):
        viewer_role = Role.objects.create(client=self.tenant, name="Viewer", code="VW")
        from apps.accounts.models import RolePermission

        RolePermission.objects.create(role=viewer_role, permission_id="view_sales")
        viewer = User.objects.create_user(
            email="viewer@approve.test", password="pass-12345", client=self.tenant, name="V",
            role=viewer_role, status="Active",
        )
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(viewer)['access']}")
        quote = self._doc(Quotation, "quotation_number", "QT-6")
        self.assertEqual(api.post(f"{API}/sales/quotations/{quote.id}/approval-links/", {}, format="json").status_code, 403)
        self.assertEqual(api.get(f"{API}/sales/quotations/{quote.id}/approval-links/").status_code, 200)
