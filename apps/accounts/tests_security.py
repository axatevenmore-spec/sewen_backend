"""Regression tests for SECURITY_VULNERABILITIES_FIX_PLAN.md.

SEC-03  no default password for accounts created without one
SEC-04  uploaded files cannot run script in the app's origin
SEC-05  a signed-out session's access token stops working at once
SEC-02  errors outside DRF still answer in the JSON envelope
"""
import shutil
import tempfile

from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, Role, User
from apps.accounts.permission_catalogue import seed_roles, sync_permissions
from apps.core import files as file_service
from apps.core.models import File

API = "/api/v1"
LEGACY_DEFAULT = "Password@123"


class SecurityTestCase(TestCase):
    def setUp(self):
        cache.clear()
        sync_permissions()
        self.tenant = Client.objects.create(slug="sec", name="Security Tenant")
        seed_roles(self.tenant)
        self.admin = User.objects.create_user(
            email="admin@sec.test", password="Adm1n-Strong-Pass", client=self.tenant,
            name="Admin", role=Role.objects.get(client=self.tenant, code="AD"), status="Active",
        )
        self.em_role = Role.objects.get(client=self.tenant, code="EM")

    def tearDown(self):
        cache.clear()

    def _as(self, user):
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(user)['access']}")
        return api

    def _login(self, email, password):
        return APIClient().post(
            f"{API}/auth/login/", {"email": email, "password": password}, format="json"
        )


class NoDefaultPasswordTests(SecurityTestCase):
    def test_manager_creates_an_unusable_password(self):
        user = User.objects.create_user(email="x@sec.test", client=self.tenant, name="X")
        self.assertFalse(user.has_usable_password())
        self.assertFalse(user.check_password(""))
        self.assertFalse(user.check_password(LEGACY_DEFAULT))

    def test_admin_created_user_without_password_cannot_sign_in_and_is_invited(self):
        api = self._as(self.admin)
        with self.captureOnCommitCallbacks(execute=True):
            resp = api.post(
                f"{API}/admin/users/",
                {"name": "Ravi", "email": "ravi@sec.test", "roleId": str(self.em_role.id)},
                format="json",
            )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertFalse(User.objects.get(email="ravi@sec.test").has_usable_password())
        self.assertEqual(self._login("ravi@sec.test", LEGACY_DEFAULT).status_code, 401)

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["ravi@sec.test"])
        self.assertIn("action=forgot-password", mail.outbox[0].body)
        self.assertIn("email=ravi%40sec.test", mail.outbox[0].body)

    def test_hr_created_login_without_password_cannot_sign_in(self):
        api = self._as(self.admin)
        with self.captureOnCommitCallbacks(execute=True):
            resp = api.post(
                f"{API}/hrms/employees/", {"name": "Asha", "email": "asha@sec.test"}, format="json"
            )
        self.assertEqual(resp.status_code, 201, resp.content)
        user = User.objects.get(email="asha@sec.test")
        self.assertFalse(user.has_usable_password())
        self.assertEqual(self._login("asha@sec.test", LEGACY_DEFAULT).status_code, 401)
        self.assertEqual([m.to for m in mail.outbox], [["asha@sec.test"]])

    def test_an_explicit_password_still_works_and_sends_no_invite(self):
        api = self._as(self.admin)
        with self.captureOnCommitCallbacks(execute=True):
            resp = api.post(
                f"{API}/admin/users/",
                {"name": "Meera", "email": "meera@sec.test", "roleId": str(self.em_role.id),
                 "password": "Meera-Own-Pass-1"},
                format="json",
            )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(self._login("meera@sec.test", "Meera-Own-Pass-1").status_code, 200)
        self.assertEqual(mail.outbox, [])


class SessionRevocationTests(SecurityTestCase):
    def test_access_token_is_rejected_after_logout(self):
        resp = self._login("admin@sec.test", "Adm1n-Strong-Pass")
        self.assertEqual(resp.status_code, 200, resp.content)
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {resp.json()['access']}")

        self.assertEqual(api.get(f"{API}/auth/me/").status_code, 200)
        self.assertEqual(api.post(f"{API}/auth/logout/", {}, format="json").status_code, 204)

        after = api.get(f"{API}/auth/me/")
        self.assertEqual(after.status_code, 401)
        self.assertEqual(after.json()["code"], "TOKEN_REVOKED")

    def test_logout_on_one_device_leaves_the_other_signed_in(self):
        first = self._login("admin@sec.test", "Adm1n-Strong-Pass").json()["access"]
        second = self._login("admin@sec.test", "Adm1n-Strong-Pass").json()["access"]
        api_first, api_second = APIClient(), APIClient()
        api_first.credentials(HTTP_AUTHORIZATION=f"Bearer {first}")
        api_second.credentials(HTTP_AUTHORIZATION=f"Bearer {second}")

        api_first.post(f"{API}/auth/logout/", {}, format="json")
        self.assertEqual(api_first.get(f"{API}/auth/me/").status_code, 401)
        self.assertEqual(api_second.get(f"{API}/auth/me/").status_code, 200)

    def test_realtime_handshake_refuses_a_revoked_token(self):
        from apps.core.realtime import authenticate_token

        access = self._login("admin@sec.test", "Adm1n-Strong-Pass").json()["access"]
        self.assertIsNotNone(authenticate_token(access))
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        api.post(f"{API}/auth/logout/", {}, format="json")
        self.assertIsNone(authenticate_token(access))


class FileDownloadHeaderTests(SecurityTestCase):
    def setUp(self):
        super().setUp()
        # Never write into the real Backend/media.
        self.media = tempfile.mkdtemp()
        self.override = override_settings(MEDIA_ROOT=self.media)
        self.override.enable()

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.media, ignore_errors=True)
        super().tearDown()

    def _download(self, file_name, content_type, body):
        key = file_service.build_storage_key(self.tenant.id, "other", file_name)
        file_service.local_path(key).write_bytes(body)
        row = File.objects.create(
            client=self.tenant, storage_key=key, file_name=file_name,
            content_type=content_type, file_size=len(body), status="committed",
        )
        token = file_service.sign_download(row.id)
        return APIClient().get(f"{API}/files/{row.id}/download/", {"token": token})

    def test_svg_is_forced_to_download_and_sandboxed(self):
        resp = self._download("evil.svg", "image/svg+xml", b"<svg><script>alert(1)</script></svg>")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp["Content-Disposition"].startswith("attachment"))
        self.assertEqual(resp["X-Content-Type-Options"], "nosniff")
        self.assertIn("sandbox", resp["Content-Security-Policy"])

    def test_png_and_pdf_stay_inline(self):
        png = self._download("photo.png", "image/png", b"\x89PNG\r\n")
        self.assertTrue(png["Content-Disposition"].startswith("inline"))
        self.assertEqual(png["X-Content-Type-Options"], "nosniff")
        pdf = self._download("proof.pdf", "application/pdf", b"%PDF-1.4")
        self.assertTrue(pdf["Content-Disposition"].startswith("inline"))
        # Chrome will not render a PDF in a sandboxed document.
        self.assertNotIn("Content-Security-Policy", pdf)

    def test_file_name_cannot_inject_header_parameters(self):
        resp = self._download('a".html; filename="b.png', "text/html", b"<script>1</script>")
        disposition = resp["Content-Disposition"]
        self.assertTrue(disposition.startswith("attachment"))
        self.assertNotIn('filename="b.png"', disposition)


class ErrorEnvelopeTests(TestCase):
    def test_unknown_url_answers_json_not_html(self):
        resp = self.client.get("/no-such-page/")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp["Content-Type"], "application/json")
        self.assertEqual(resp.json()["code"], "NOT_FOUND")
