"""Tests for forgot password, OTP email verification, and password reset flows."""
from datetime import timedelta
from unittest.mock import patch

from django.core import mail
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Client, PasswordResetOTP, PasswordResetToken, User
from apps.accounts.views import _hash, _hash_otp

API = "/api/v1"


class PasswordResetOTPTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.tenant = Client.objects.create(slug="otp-test", name="OTP Test Tenant")
        self.user = User.objects.create_user(
            email="priya@test.com",
            password="OldPassword123!",
            client=self.tenant,
            name="Priya Patel",
            status="Active",
        )

    def test_forgot_password_sends_email_and_creates_otp(self):
        with self.settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"):
            mail.outbox = []
            resp = self.client.post(
                f"{API}/auth/forgot-password/",
                {"email": "priya@test.com"},
                format="json",
            )
            self.assertEqual(resp.status_code, 200)
            self.assertIn("message", resp.json())
            self.assertEqual(resp.json()["email"], "priya@test.com")

            # Email outbox check
            self.assertEqual(len(mail.outbox), 1)
            sent_email = mail.outbox[0]
            self.assertEqual(sent_email.to, ["priya@test.com"])
            self.assertIn("verification code", sent_email.subject.lower())

            # OTP record check
            otp_record = PasswordResetOTP.objects.filter(user=self.user, used_at__isnull=True).first()
            self.assertIsNotNone(otp_record)
            self.assertTrue(otp_record.is_usable)

    def test_forgot_password_unknown_email_returns_success_without_leak(self):
        with self.settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"):
            mail.outbox = []
            resp = self.client.post(
                f"{API}/auth/forgot-password/",
                {"email": "unknown@nowhere.com"},
                format="json",
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(len(mail.outbox), 0)

    def test_verify_otp_success_returns_reset_token(self):
        otp = "654321"
        PasswordResetOTP.objects.create(
            user=self.user,
            email=self.user.email,
            otp_hash=_hash_otp(self.user.id, otp),
            expires_at=timezone.now() + timedelta(minutes=15),
        )

        resp = self.client.post(
            f"{API}/auth/verify-otp/",
            {"email": "priya@test.com", "otp": otp},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data.get("valid"))
        self.assertIn("resetToken", data)

        # OTP is now marked used
        otp_record = PasswordResetOTP.objects.filter(user=self.user).first()
        self.assertIsNotNone(otp_record.used_at)

        # PasswordResetToken was generated
        reset_token = data["resetToken"]
        token_record = PasswordResetToken.objects.filter(token_hash=_hash(reset_token)).first()
        self.assertIsNotNone(token_record)
        self.assertTrue(token_record.is_usable)

    def test_verify_otp_wrong_code_increments_attempts(self):
        otp = "654321"
        record = PasswordResetOTP.objects.create(
            user=self.user,
            email=self.user.email,
            otp_hash=_hash_otp(self.user.id, otp),
            expires_at=timezone.now() + timedelta(minutes=15),
        )

        resp = self.client.post(
            f"{API}/auth/verify-otp/",
            {"email": "priya@test.com", "otp": "000000"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        record.refresh_from_db()
        self.assertEqual(record.attempts, 1)

    def test_verify_otp_expired_code_fails(self):
        otp = "654321"
        PasswordResetOTP.objects.create(
            user=self.user,
            email=self.user.email,
            otp_hash=_hash_otp(self.user.id, otp),
            expires_at=timezone.now() - timedelta(minutes=1),
        )

        resp = self.client.post(
            f"{API}/auth/verify-otp/",
            {"email": "priya@test.com", "otp": otp},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_reset_password_with_reset_token(self):
        # 1. User verifies OTP and gets resetToken
        otp = "789123"
        PasswordResetOTP.objects.create(
            user=self.user,
            email=self.user.email,
            otp_hash=_hash_otp(self.user.id, otp),
            expires_at=timezone.now() + timedelta(minutes=15),
        )
        verify_resp = self.client.post(
            f"{API}/auth/verify-otp/",
            {"email": "priya@test.com", "otp": otp},
            format="json",
        )
        reset_token = verify_resp.json()["resetToken"]

        # 2. User submits reset password
        new_password = "NewStrongPassword456!"
        resp = self.client.post(
            f"{API}/auth/reset-password/",
            {"resetToken": reset_token, "newPassword": new_password},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)

        # 3. User can log in with new password
        login_resp = self.client.post(
            f"{API}/auth/login/",
            {"email": "priya@test.com", "password": new_password},
            format="json",
        )
        self.assertEqual(login_resp.status_code, 200)

        # 4. Old password fails
        old_login_resp = self.client.post(
            f"{API}/auth/login/",
            {"email": "priya@test.com", "password": "OldPassword123!"},
            format="json",
        )
        self.assertEqual(old_login_resp.status_code, 401)

    def test_reset_password_directly_with_email_and_otp(self):
        otp = "321654"
        PasswordResetOTP.objects.create(
            user=self.user,
            email=self.user.email,
            otp_hash=_hash_otp(self.user.id, otp),
            expires_at=timezone.now() + timedelta(minutes=15),
        )

        new_password = "AnotherStrongPass789!"
        resp = self.client.post(
            f"{API}/auth/reset-password/",
            {
                "email": "priya@test.com",
                "otp": otp,
                "newPassword": new_password,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 200)

        # Check login works
        login_resp = self.client.post(
            f"{API}/auth/login/",
            {"email": "priya@test.com", "password": new_password},
            format="json",
        )
        self.assertEqual(login_resp.status_code, 200)
