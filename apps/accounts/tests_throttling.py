"""
Tests for production-ready rate limiting and throttling.

Covers:
1. Login within the allowed limit succeeds.
2. Login exceeding the limit returns 429.
3. Failed login rate limit (10 failed attempts / 10 min / IP) returns 429.
4. Successful login clears failed attempts counter and is not blocked by previous failures.
5. Authenticated user API calls throttled after configured limit (120/min).
6. Anonymous API calls throttled after configured limit (30/min).
7. Different authenticated users have independent limits.
8. Different IP addresses have independent anonymous limits.
9. Rate limiting cannot be bypassed by spoofing X-Forwarded-For headers.
10. Retry-After header is returned on 429 responses.
11. Response format is {"detail": "Request rate limit exceeded. Please try again later."}.
12. Rate-limit configuration can be overridden in tests.
13. Sensitive endpoints (forgot-password, verify-otp, refresh) have stricter throttling.
"""
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, PasswordResetOTP, User
from apps.accounts.views import _hash_otp
from apps.core.throttling import FailedLoginThrottle

API = "/api/v1"


class ThrottlingTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.tenant = Client.objects.create(slug="throttle-test", name="Throttle Tenant")
        self.user_a = User.objects.create_user(
            email="usera@test.com",
            password="StrongPassword123!",
            client=self.tenant,
            name="User A",
            status="Active",
        )
        self.user_b = User.objects.create_user(
            email="userb@test.com",
            password="StrongPassword123!",
            client=self.tenant,
            name="User B",
            status="Active",
        )
        self.tokens_a = build_tokens(self.user_a)
        self.tokens_b = build_tokens(self.user_b)

    def tearDown(self):
        cache.clear()


class LoginThrottlingTests(ThrottlingTestCase):
    @override_settings(LOGIN_RATE_LIMIT="5/minute")
    def test_login_within_allowed_limit_succeeds(self):
        """1. Login within the allowed limit succeeds."""
        resp = self.client.post(
            f"{API}/auth/login/",
            {"email": "usera@test.com", "password": "StrongPassword123!"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("access", resp.json())

    @override_settings(LOGIN_RATE_LIMIT="3/minute")
    def test_login_exceeding_limit_returns_429(self):
        """2. Login exceeding the limit returns 429 with Retry-After and exact detail."""
        for i in range(3):
            resp = self.client.post(
                f"{API}/auth/login/",
                {"email": "usera@test.com", "password": "WrongPassword!"},
                format="json",
            )
            self.assertEqual(resp.status_code, 401)

        # 4th request exceeds LOGIN_RATE_LIMIT
        resp = self.client.post(
            f"{API}/auth/login/",
            {"email": "usera@test.com", "password": "StrongPassword123!"},
            format="json",
        )
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(
            resp.json(),
            {"detail": "Request rate limit exceeded. Please try again later."},
        )
        self.assertTrue(resp.has_header("Retry-After"))
        self.assertTrue(int(resp["Retry-After"]) >= 1)

    @override_settings(LOGIN_RATE_LIMIT="20/minute", LOGIN_FAILED_RATE_LIMIT="3/10minute")
    def test_failed_login_throttling_after_max_failures(self):
        """Failed logins exceeding the threshold return 429."""
        for i in range(3):
            resp = self.client.post(
                f"{API}/auth/login/",
                {"email": "usera@test.com", "password": "WrongPassword!"},
                format="json",
            )
            self.assertEqual(resp.status_code, 401)

        # 4th request from same IP is blocked due to excessive failed attempts
        resp = self.client.post(
            f"{API}/auth/login/",
            {"email": "usera@test.com", "password": "StrongPassword123!"},
            format="json",
        )
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(
            resp.json(),
            {"detail": "Request rate limit exceeded. Please try again later."},
        )
        self.assertTrue(resp.has_header("Retry-After"))

    @override_settings(LOGIN_RATE_LIMIT="10/minute", LOGIN_FAILED_RATE_LIMIT="5/10minute")
    def test_successful_login_clears_failed_attempts(self):
        """7. Successful login works normally and clears previous failed attempt records."""
        # 2 failed attempts
        for i in range(2):
            resp = self.client.post(
                f"{API}/auth/login/",
                {"email": "usera@test.com", "password": "WrongPassword!"},
                format="json",
            )
            self.assertEqual(resp.status_code, 401)

        # 3rd attempt is successful with correct credentials
        success_resp = self.client.post(
            f"{API}/auth/login/",
            {"email": "usera@test.com", "password": "StrongPassword123!"},
            format="json",
        )
        self.assertEqual(success_resp.status_code, 200)

        # Failed login history was cleared; user is not blocked
        next_resp = self.client.post(
            f"{API}/auth/login/",
            {"email": "usera@test.com", "password": "StrongPassword123!"},
            format="json",
        )
        self.assertEqual(next_resp.status_code, 200)

    def test_login_does_not_reveal_user_existence_on_failure(self):
        """Does not reveal whether an email exists when credentials fail."""
        resp_known = self.client.post(
            f"{API}/auth/login/",
            {"email": "usera@test.com", "password": "WrongPassword!"},
            format="json",
        )
        resp_unknown = self.client.post(
            f"{API}/auth/login/",
            {"email": "nonexistent@test.com", "password": "WrongPassword!"},
            format="json",
        )
        self.assertEqual(resp_known.status_code, 401)
        self.assertEqual(resp_unknown.status_code, 401)
        self.assertEqual(resp_known.json().get("code"), resp_unknown.json().get("code"))

    @override_settings(
        ACCOUNT_LOCKOUT_ATTEMPTS=3,
        ACCOUNT_LOCKOUT_MINUTES=15,
        LOGIN_RATE_LIMIT="20/minute",
        LOGIN_FAILED_RATE_LIMIT="20/10minute",
    )
    def test_wrong_password_limit_and_user_lockout(self):
        """Wrong password counter locks user account after configured attempts."""
        # Attempt 1: wrong password, 2 attempts remaining
        resp1 = self.client.post(
            f"{API}/auth/login/",
            {"email": "usera@test.com", "password": "WrongPassword!"},
            format="json",
        )
        self.assertEqual(resp1.status_code, 401)
        self.assertEqual(resp1.json()["code"], "INVALID_CREDENTIALS")
        self.assertIn("2 attempts remaining", resp1.json()["message"])

        # Attempt 2: wrong password, 1 attempt remaining
        resp2 = self.client.post(
            f"{API}/auth/login/",
            {"email": "usera@test.com", "password": "WrongPassword!"},
            format="json",
        )
        self.assertEqual(resp2.status_code, 401)
        self.assertIn("1 attempt remaining", resp2.json()["message"])

        # Attempt 3: 3rd wrong password reaches limit, locks the user!
        resp3 = self.client.post(
            f"{API}/auth/login/",
            {"email": "usera@test.com", "password": "WrongPassword!"},
            format="json",
        )
        self.assertEqual(resp3.status_code, 401)
        self.assertEqual(resp3.json()["code"], "ACCOUNT_LOCKED")
        self.assertIn("temporarily locked", resp3.json()["message"])

        # User is locked in the database
        self.user_a.refresh_from_db()
        self.assertTrue(self.user_a.is_locked())

        # Attempt 4: Even with CORRECT password, user is blocked while locked
        resp4 = self.client.post(
            f"{API}/auth/login/",
            {"email": "usera@test.com", "password": "StrongPassword123!"},
            format="json",
        )
        self.assertEqual(resp4.status_code, 401)
        self.assertEqual(resp4.json()["code"], "ACCOUNT_LOCKED")

    @override_settings(
        ACCOUNT_LOCKOUT_ATTEMPTS=2,
        ACCOUNT_LOCKOUT_MINUTES=10,
        LOGIN_RATE_LIMIT="20/minute",
        LOGIN_FAILED_RATE_LIMIT="20/10minute",
    )
    def test_user_unlocked_after_time_elapsed(self):
        """Once the lockout duration expires, user can log in with correct credentials."""
        # Lock user
        for _ in range(2):
            self.client.post(
                f"{API}/auth/login/",
                {"email": "usera@test.com", "password": "WrongPassword!"},
                format="json",
            )
        self.user_a.refresh_from_db()
        self.assertTrue(self.user_a.is_locked())

        # Simulate time passing beyond lockout period
        from django.utils import timezone
        from datetime import timedelta
        self.user_a.locked_until = timezone.now() - timedelta(minutes=1)
        self.user_a.save(update_fields=["locked_until"])

        self.assertFalse(self.user_a.is_locked())

        # Correct login succeeds and resets failure count
        resp = self.client.post(
            f"{API}/auth/login/",
            {"email": "usera@test.com", "password": "StrongPassword123!"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.user_a.refresh_from_db()
        self.assertEqual(self.user_a.failed_login_count, 0)
        self.assertIsNone(self.user_a.locked_until)


class AuthenticatedApiThrottlingTests(ThrottlingTestCase):
    @override_settings(AUTHENTICATED_RATE_LIMIT="3/minute")
    def test_authenticated_user_throttled_after_limit(self):
        """3. Authenticated user API calls are throttled after the configured limit."""
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.tokens_a['access']}")

        # 3 allowed requests
        for i in range(3):
            resp = client.get(f"{API}/auth/me/")
            self.assertEqual(resp.status_code, 200)

        # 4th request exceeds AUTHENTICATED_RATE_LIMIT
        resp = client.get(f"{API}/auth/me/")
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(
            resp.json(),
            {"detail": "Request rate limit exceeded. Please try again later."},
        )
        self.assertTrue(resp.has_header("Retry-After"))

    @override_settings(AUTHENTICATED_RATE_LIMIT="2/minute")
    def test_different_authenticated_users_have_independent_limits(self):
        """5. Different authenticated users have independent limits."""
        client_a = APIClient()
        client_a.credentials(HTTP_AUTHORIZATION=f"Bearer {self.tokens_a['access']}")

        client_b = APIClient()
        client_b.credentials(HTTP_AUTHORIZATION=f"Bearer {self.tokens_b['access']}")

        # User A makes 2 requests and gets throttled on the 3rd
        self.assertEqual(client_a.get(f"{API}/auth/me/").status_code, 200)
        self.assertEqual(client_a.get(f"{API}/auth/me/").status_code, 200)
        self.assertEqual(client_a.get(f"{API}/auth/me/").status_code, 429)

        # User B has independent limit and can still make requests
        resp_b = client_b.get(f"{API}/auth/me/")
        self.assertEqual(resp_b.status_code, 200)


class AnonymousApiThrottlingTests(ThrottlingTestCase):
    @override_settings(ANONYMOUS_RATE_LIMIT="3/minute")
    def test_anonymous_api_throttled_after_limit(self):
        """4. Anonymous API calls are throttled after configured limit."""
        # Using a public endpoint (e.g. form endpoint)
        url = f"{API}/public/forms/sample-form/"
        for i in range(3):
            resp = self.client.get(url)
            # Response might be 404 if form not found, but it reaches the view and throttle applies
            self.assertIn(resp.status_code, [200, 404])

        # 4th request is throttled
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(
            resp.json(),
            {"detail": "Request rate limit exceeded. Please try again later."},
        )
        self.assertTrue(resp.has_header("Retry-After"))

    @override_settings(ANONYMOUS_RATE_LIMIT="2/minute")
    def test_different_ips_have_independent_anonymous_limits(self):
        """6. Different IP addresses have independent anonymous limits."""
        url = f"{API}/public/forms/sample-form/"

        client_1 = APIClient(REMOTE_ADDR="198.51.100.1")
        client_2 = APIClient(REMOTE_ADDR="198.51.100.2")

        # IP 1 uses up its limit
        self.assertIn(client_1.get(url).status_code, [200, 404])
        self.assertIn(client_1.get(url).status_code, [200, 404])
        self.assertEqual(client_1.get(url).status_code, 429)

        # IP 2 is not affected
        self.assertIn(client_2.get(url).status_code, [200, 404])


class SecurityRequirementsTests(ThrottlingTestCase):
    @override_settings(LOGIN_RATE_LIMIT="2/minute", NUM_PROXIES=0)
    def test_spoofed_x_forwarded_for_cannot_bypass_rate_limiting(self):
        """9. Rate limiting cannot be bypassed simply by changing X-Forwarded-For header."""
        for i in range(2):
            resp = self.client.post(
                f"{API}/auth/login/",
                {"email": "usera@test.com", "password": "WrongPassword!"},
                HTTP_X_FORWARDED_FOR=f"10.0.0.{i+1}",
                format="json",
            )
            self.assertEqual(resp.status_code, 401)

        # Attacker sends a different spoofed header; must still be throttled based on REMOTE_ADDR
        resp = self.client.post(
            f"{API}/auth/login/",
            {"email": "usera@test.com", "password": "StrongPassword123!"},
            HTTP_X_FORWARDED_FOR="198.51.100.99",
            format="json",
        )
        self.assertEqual(resp.status_code, 429)

    @override_settings(
        LOGIN_RATE_LIMIT="2/minute",
        NUM_PROXIES=1,
        REST_FRAMEWORK={"NUM_PROXIES": 1},
    )
    def test_trusted_proxy_configuration_honors_forwarded_ip(self):
        """When NUM_PROXIES is configured, client IP from trusted proxy is honored."""
        # Client 1 through reverse proxy
        for i in range(2):
            resp = self.client.post(
                f"{API}/auth/login/",
                {"email": "usera@test.com", "password": "WrongPassword!"},
                HTTP_X_FORWARDED_FOR="203.0.113.10",
                format="json",
            )
            self.assertEqual(resp.status_code, 401)

        # Client 1 is now throttled
        resp = self.client.post(
            f"{API}/auth/login/",
            {"email": "usera@test.com", "password": "WrongPassword!"},
            HTTP_X_FORWARDED_FOR="203.0.113.10",
            format="json",
        )
        self.assertEqual(resp.status_code, 429)

        # Client 2 through reverse proxy is NOT throttled
        resp2 = self.client.post(
            f"{API}/auth/login/",
            {"email": "usera@test.com", "password": "WrongPassword!"},
            HTTP_X_FORWARDED_FOR="203.0.113.20",
            format="json",
        )
        self.assertEqual(resp2.status_code, 401)


class SensitiveEndpointsThrottlingTests(ThrottlingTestCase):
    @override_settings(PASSWORD_RESET_RATE_LIMIT="2/15minute")
    def test_password_reset_throttled_after_limit(self):
        """Sensitive password reset endpoint is throttled according to PASSWORD_RESET_RATE_LIMIT."""
        for i in range(2):
            resp = self.client.post(
                f"{API}/auth/forgot-password/",
                {"email": "usera@test.com"},
                format="json",
            )
            self.assertEqual(resp.status_code, 200)

        # 3rd request is throttled
        resp = self.client.post(
            f"{API}/auth/forgot-password/",
            {"email": "usera@test.com"},
            format="json",
        )
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(
            resp.json(),
            {"detail": "Request rate limit exceeded. Please try again later."},
        )

    @override_settings(OTP_VERIFY_RATE_LIMIT="2/10minute")
    def test_otp_verify_throttled_after_limit(self):
        """Sensitive OTP verify endpoint is throttled according to OTP_VERIFY_RATE_LIMIT."""
        for i in range(2):
            resp = self.client.post(
                f"{API}/auth/verify-otp/",
                {"email": "usera@test.com", "otp": "000000"},
                format="json",
            )
            self.assertIn(resp.status_code, [400, 200])

        # 3rd request is throttled
        resp = self.client.post(
            f"{API}/auth/verify-otp/",
            {"email": "usera@test.com", "otp": "000000"},
            format="json",
        )
        self.assertEqual(resp.status_code, 429)

    @override_settings(TOKEN_REFRESH_RATE_LIMIT="2/10minute")
    def test_token_refresh_throttled_after_limit(self):
        """Sensitive token refresh endpoint is throttled according to TOKEN_REFRESH_RATE_LIMIT."""
        login_resp = self.client.post(
            f"{API}/auth/login/",
            {"email": "usera@test.com", "password": "StrongPassword123!"},
            format="json",
        )
        self.assertEqual(login_resp.status_code, 200)
        refresh_token = login_resp.json()["refresh"]

        for i in range(2):
            resp = self.client.post(
                f"{API}/auth/refresh/",
                {"refresh": refresh_token},
                format="json",
            )
            self.assertEqual(resp.status_code, 200)

        # 3rd request is throttled
        resp = self.client.post(
            f"{API}/auth/refresh/",
            {"refresh": refresh_token},
            format="json",
        )
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(
            resp.json(),
            {"detail": "Request rate limit exceeded. Please try again later."},
        )
