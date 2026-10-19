"""
Unit and integration tests for the provider-agnostic lead ingestion system and Meta adapter.
"""
from datetime import datetime, timezone
import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch
import urllib.error

from django.conf import settings
from django.core.management import call_command
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import Client, Role, User
from apps.accounts.permission_catalogue import seed_roles, sync_permissions
from apps.crm.integrations.base import InboundLeadEvent
from apps.crm.integrations.crypto import decrypt_credentials, encrypt_credentials
from apps.crm.integrations.providers.meta import MetaAuthError, MetaLeadSourceProvider
from apps.crm.integrations.services import (
    clean_email_safe,
    clean_phone_safe,
    create_crm_lead,
    process_lead_event,
)
from apps.crm.models import (
    Deal,
    Lead,
    LeadSourceIntegration,
    LeadWebhookEvent,
    Source,
    Stage,
    StageTask,
    Task,
)


@override_settings(
    META_APP_SECRET="test_meta_app_secret_98765",
    META_WEBHOOK_VERIFY_TOKEN="test_meta_verify_token_12345",
    META_GRAPH_API_VERSION="v21.0",
    LEAD_WEBHOOK_ASYNC=False,  # Run synchronously in tests
)
class LeadIntegrationsTests(TestCase):
    def setUp(self):
        sync_permissions()
        self.tenant = Client.objects.create(slug="meta-test", name="Meta Test Tenant")
        seed_roles(self.tenant)
        self.admin = User.objects.create_user(
            email="admin@meta-test.test",
            password="pass-12345",
            client=self.tenant,
            name="Meta Admin",
            role=Role.objects.get(client=self.tenant, code="AD"),
        )
        self.stage_new = Stage.objects.create(
            client=self.tenant, name="New Lead", sequence=1, is_active=True
        )
        self.stage_qualified = Stage.objects.create(
            client=self.tenant, name="Qualified", sequence=2, is_active=True
        )

        # Create a StageTask template for stage automation test
        self.auto_task_template = StageTask.objects.create(
            client=self.tenant,
            stage=self.stage_new,
            title="Initial Outreach Call",
            assignee_role="Admin",
            auto_create=True,
            sort_order=1,
        )

        # Page ID & Integration
        self.page_id = "page_123456789"
        self.encrypted_token = encrypt_credentials({"access_token": "EAAB_test_page_access_token"})
        self.integration = LeadSourceIntegration.objects.create(
            client=self.tenant,
            provider="meta",
            name="Test Facebook Page",
            external_account_id=self.page_id,
            encrypted_credentials=self.encrypted_token,
            settings={
                "default_stage_id": str(self.stage_new.id),
                "default_owner_id": str(self.admin.id),
                "field_mapping": {"budget_question": "custom_values.budget"},
            },
            is_active=True,
        )

        self.client = APIClient()

    def _sign_payload(self, body_bytes: bytes, secret: str = "test_meta_app_secret_98765") -> str:
        sig = hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
        return f"sha256={sig}"

    # --------------------------------------------------------------------------
    # 1. Verification Handshake (GET)
    # --------------------------------------------------------------------------
    def test_meta_webhook_verification_success(self):
        challenge = "random_challenge_string_9988"
        resp = self.client.get(
            "/webhooks/leads/meta/",
            {
                "hub.mode": "subscribe",
                "hub.verify_token": "test_meta_verify_token_12345",
                "hub.challenge": challenge,
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content.decode("utf-8"), challenge)

    def test_meta_webhook_verification_invalid_token(self):
        resp = self.client.get(
            "/webhooks/leads/meta/",
            {
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong_token",
                "hub.challenge": "12345",
            },
        )
        self.assertEqual(resp.status_code, 403)

    def test_unknown_provider_returns_404(self):
        resp_get = self.client.get("/webhooks/leads/tiktok/")
        self.assertEqual(resp_get.status_code, 404)

        resp_post = self.client.post("/webhooks/leads/tiktok/", {}, format="json")
        self.assertEqual(resp_post.status_code, 404)

    # --------------------------------------------------------------------------
    # 2. Authenticity & Signature (POST)
    # --------------------------------------------------------------------------
    def test_meta_post_missing_signature_fails_403(self):
        payload = json.dumps({"entry": []}).encode("utf-8")
        resp = self.client.post(
            "/webhooks/leads/meta/",
            data=payload,
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)

    def test_meta_post_invalid_signature_fails_403(self):
        payload = json.dumps({"entry": []}).encode("utf-8")
        resp = self.client.post(
            "/webhooks/leads/meta/",
            data=payload,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256="sha256=invalidhash000000000000000000000000000000000000000000000000000000",
        )
        self.assertEqual(resp.status_code, 403)

    # --------------------------------------------------------------------------
    # 3. Payload Parsing
    # --------------------------------------------------------------------------
    def test_parse_events_multiple_entries_and_changes(self):
        adapter = MetaLeadSourceProvider()
        raw_payload = {
            "object": "page",
            "entry": [
                {
                    "id": "page_123456789",
                    "time": 1729330000,
                    "changes": [
                        {
                            "field": "leadgen",
                            "value": {
                                "ad_id": "ad_111",
                                "form_id": "form_222",
                                "leadgen_id": "lead_1001",
                                "created_time": 1729330000,
                                "page_id": "page_123456789",
                                "adgroup_id": "adset_333",
                            },
                        },
                        {
                            "field": "leadgen",
                            "value": {
                                "ad_id": "ad_112",
                                "form_id": "form_222",
                                "leadgen_id": "lead_1002",
                                "created_time": 1729330010,
                                "page_id": "page_123456789",
                                "adgroup_id": "adset_333",
                            },
                        },
                        {
                            "field": "other_field",
                            "value": {"test": 123},
                        },
                    ],
                }
            ],
        }

        request = MagicMock()
        request.body = json.dumps(raw_payload).encode("utf-8")
        events = adapter.parse_events(request)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].external_lead_id, "lead_1001")
        self.assertEqual(events[0].external_account_id, "page_123456789")
        self.assertEqual(events[0].external_form_id, "form_222")
        self.assertEqual(events[1].external_lead_id, "lead_1002")

    # --------------------------------------------------------------------------
    # 4. Normalization & Graph API Details
    # --------------------------------------------------------------------------
    def test_normalization_and_field_mapping(self):
        adapter = MetaLeadSourceProvider()
        details = {
            "id": "lead_999",
            "created_time": "2026-10-19T10:00:00+0000",
            "campaign_id": "camp_456",
            "campaign_name": "Autumn Campaign",
            "adset_id": "adset_789",
            "adset_name": "Delhi Region",
            "ad_id": "ad_101",
            "ad_name": "Banner 1",
            "form_id": "form_202",
            "platform": "fb",
            "field_data": [
                {"name": "full_name", "values": ["Rajesh Sharma"]},
                {"name": "email", "values": ["Rajesh.Sharma@Example.COM "]},
                {"name": "phone_number", "values": ["+91 98765 43210"]},
                {"name": "company_name", "values": ["Sharma Fabrications"]},
                {"name": "city", "values": ["Mumbai"]},
                {"name": "job_title", "values": ["Procurement Head"]},
                {"name": "preferred_time", "values": ["Afternoon"]},
            ],
        }

        normalized = adapter.normalize(details)
        self.assertEqual(normalized.provider, "meta")
        self.assertEqual(normalized.external_lead_id, "lead_999")
        self.assertEqual(normalized.name, "Rajesh Sharma")
        self.assertEqual(normalized.email, "rajesh.sharma@example.com")
        self.assertEqual(normalized.phone, "+91 98765 43210")
        self.assertEqual(normalized.company, "Sharma Fabrications")
        self.assertEqual(normalized.city, "Mumbai")
        self.assertEqual(normalized.job_title, "Procurement Head")
        self.assertEqual(normalized.custom_fields.get("preferred_time"), "Afternoon")
        self.assertEqual(normalized.attribution.get("platform"), "fb")
        self.assertEqual(normalized.attribution.get("campaignName"), "Autumn Campaign")

    # --------------------------------------------------------------------------
    # 5. End-to-End Webhook Ingestion & CRM Lead Creation with Side Effects
    # --------------------------------------------------------------------------
    @patch("apps.crm.integrations.providers.meta.urllib.request.urlopen")
    def test_webhook_post_creates_crm_lead_and_fires_side_effects(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.__enter__.return_value = mock_response
        mock_response.read.return_value = json.dumps({
            "id": "lead_meta_888",
            "created_time": "2026-10-19T11:00:00+0000",
            "campaign_id": "camp_1",
            "campaign_name": "Industrial Gates Promo",
            "platform": "ig",
            "field_data": [
                {"name": "full_name", "values": ["Anita Desai"]},
                {"name": "email", "values": ["anita@desai-ind.com"]},
                {"name": "phone_number", "values": ["+919876512345"]},
                {"name": "company_name", "values": ["Desai Industries"]},
                {"name": "city", "values": ["Bengaluru"]},
            ],
        }).encode("utf-8")
        mock_urlopen.return_value = mock_response

        webhook_body = json.dumps({
            "object": "page",
            "entry": [
                {
                    "id": self.page_id,
                    "changes": [
                        {
                            "field": "leadgen",
                            "value": {
                                "ad_id": "ad_55",
                                "form_id": "form_77",
                                "leadgen_id": "lead_meta_888",
                                "created_time": 1729332000,
                                "page_id": self.page_id,
                            },
                        }
                    ],
                }
            ],
        }).encode("utf-8")

        sig = self._sign_payload(webhook_body)
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(
                "/webhooks/leads/meta/",
                data=webhook_body,
                content_type="application/json",
                HTTP_X_HUB_SIGNATURE_256=sig,
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["received"], True)
        self.assertEqual(resp.json()["events"], 1)

        # 1. Verify Event Record in DB
        event_row = LeadWebhookEvent.objects.filter(
            provider="meta", external_lead_id="lead_meta_888"
        ).first()
        self.assertIsNotNone(event_row)
        self.assertEqual(event_row.status, "processed")
        self.assertIsNotNone(event_row.lead)

        # 2. Verify CRM Lead created
        lead = event_row.lead
        self.assertEqual(lead.name, "Anita Desai")
        self.assertEqual(lead.email, "anita@desai-ind.com")
        self.assertEqual(lead.phone, "+919876512345")
        self.assertEqual(lead.company, "Desai Industries")
        self.assertEqual(lead.city, "Bengaluru")
        self.assertEqual(lead.source_provider, "meta")
        self.assertEqual(lead.source_platform, "ig")
        self.assertEqual(lead.external_lead_id, "lead_meta_888")
        self.assertEqual(lead.stage, self.stage_new)
        self.assertEqual(lead.owner, self.admin)
        self.assertTrue(lead.lead_number.startswith("L"))

        # 3. Verify Deal Side Effect (lead_opens_a_deal)
        deal = Deal.objects.filter(lead=lead).first()
        self.assertIsNotNone(deal, "Deal should be created automatically on commit")
        self.assertEqual(deal.stage, "Draft")

        # 4. Verify Stage Automation Side Effect
        auto_task = Task.objects.filter(lead=lead, stage_task=self.auto_task_template).first()
        self.assertIsNotNone(auto_task, "Stage automation task should be created")
        self.assertEqual(auto_task.title, "Initial Outreach Call")

    # --------------------------------------------------------------------------
    # 6. Idempotency (Duplicate Prevention)
    # --------------------------------------------------------------------------
    @patch("apps.crm.integrations.providers.meta.urllib.request.urlopen")
    def test_duplicate_webhook_is_idempotent(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.__enter__.return_value = mock_response
        mock_response.read.return_value = json.dumps({
            "id": "lead_meta_dup_1",
            "field_data": [
                {"name": "full_name", "values": ["Sunil Kumar"]},
                {"name": "email", "values": ["sunil@kumar.com"]},
            ],
        }).encode("utf-8")
        mock_urlopen.return_value = mock_response

        webhook_body = json.dumps({
            "entry": [
                {
                    "id": self.page_id,
                    "changes": [
                        {
                            "field": "leadgen",
                            "value": {
                                "leadgen_id": "lead_meta_dup_1",
                                "page_id": self.page_id,
                            },
                        }
                    ],
                }
            ]
        }).encode("utf-8")

        sig = self._sign_payload(webhook_body)

        # First delivery
        resp1 = self.client.post(
            "/webhooks/leads/meta/",
            data=webhook_body,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=sig,
        )
        self.assertEqual(resp1.status_code, 200)
        leads_count_1 = Lead.objects.filter(external_lead_id="lead_meta_dup_1").count()
        self.assertEqual(leads_count_1, 1)

        # Second delivery of same webhook
        resp2 = self.client.post(
            "/webhooks/leads/meta/",
            data=webhook_body,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=sig,
        )
        self.assertEqual(resp2.status_code, 200)

        # Verify no duplicate Lead was created
        leads_count_2 = Lead.objects.filter(external_lead_id="lead_meta_dup_1").count()
        self.assertEqual(leads_count_2, 1)

        # Event status should be duplicate or processed
        event = LeadWebhookEvent.objects.get(provider="meta", external_lead_id="lead_meta_dup_1")
        self.assertIn(event.status, ("processed", "duplicate"))

    # --------------------------------------------------------------------------
    # 7. Token Error 190 Handling
    # --------------------------------------------------------------------------
    @patch("apps.crm.integrations.providers.meta.urllib.request.urlopen")
    def test_meta_auth_error_190_marks_needs_reauth(self, mock_urlopen):
        err_json = json.dumps({
            "error": {
                "message": "Error validating access token: Session has expired",
                "type": "OAuthException",
                "code": 190,
                "error_subcode": 463,
            }
        }).encode("utf-8")

        mock_http_error = urllib.error.HTTPError(
            url="https://graph.facebook.com/...",
            code=400,
            msg="Bad Request",
            hdrs={},
            fp=MagicMock(read=lambda: err_json),
        )
        mock_urlopen.side_effect = mock_http_error

        adapter = MetaLeadSourceProvider()
        event = InboundLeadEvent(external_lead_id="lead_expired_tok")

        with self.assertRaises(MetaAuthError):
            adapter.fetch_lead_details(event, self.integration)

        self.integration.refresh_from_db()
        self.assertTrue(self.integration.settings.get("needs_reauth"))

    # --------------------------------------------------------------------------
    # 8. Management Commands
    # --------------------------------------------------------------------------
    def test_setup_meta_integration_command(self):
        call_command(
            "setup_meta_integration",
            tenant="meta-test",
            page_id="page_cli_999",
            token="EAAB_cli_token_secret",
            name="CLI Configured Page",
            stage="New Lead",
            owner="admin@meta-test.test",
            forms="form_1,form_2",
        )

        created_integ = LeadSourceIntegration.objects.filter(external_account_id="page_cli_999").first()
        self.assertIsNotNone(created_integ)
        self.assertEqual(created_integ.name, "CLI Configured Page")
        credentials = decrypt_credentials(created_integ.encrypted_credentials)
        self.assertEqual(credentials.get("access_token"), "EAAB_cli_token_secret")
        self.assertEqual(created_integ.settings.get("form_ids"), ["form_1", "form_2"])

    def test_sync_lead_integrations_reprocess_failed(self):
        failed_event = LeadWebhookEvent.objects.create(
            client=self.tenant,
            integration=self.integration,
            provider="meta",
            external_lead_id="failed_evt_123",
            payload={"page_id": self.page_id},
            status="failed",
            attempts=1,
        )

        with patch("apps.crm.integrations.services.process_lead_event") as mock_process:
            mock_process.return_value = True
            call_command("sync_lead_integrations", "--reprocess-failed")
            mock_process.assert_called_with(str(failed_event.id))
