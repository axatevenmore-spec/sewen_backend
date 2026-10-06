"""
Authentication and identity (api.md §2) and Administration (api.md §3).
"""
import hashlib
import math
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import authenticate
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from apps.core.audit import record_audit, request_ip
from apps.core.exceptions import (
    Codes,
    Conflict,
    NotAuthenticated,
    NotFound,
    PermissionDenied,
    ValidationFailed,
)
from apps.core.pagination import envelope
from apps.core.permissions import (
    AllowPublic,
    HasModulePermission,
    granted_permissions,
    has_permission,
    require_permission,
)
from apps.core.throttling import (
    FailedLoginThrottle,
    LoginThrottle,
    OTPVerifyThrottle,
    PasswordResetThrottle,
    SensitiveActionThrottle,
    TokenRefreshThrottle,
)
from apps.core.viewsets import TenantModelViewSet
from apps.core.tenancy import set_current_client_id
from apps.hrms import user_link

from django.conf import settings

from .authentication import build_tokens
from .models import (
    Client,
    PasswordResetOTP,
    PasswordResetToken,
    Permission,
    Role,
    RolePermission,
    User,
    UserPermission,
    UserSession,
)
from .permission_catalogue import grouped_catalogue
from .serializers import (
    ChangePasswordSerializer,
    ClientSerializer,
    ForgotPasswordSerializer,
    LoginSerializer,
    MeUserSerializer,
    PermissionSerializer,
    RefreshSerializer,
    ResetPasswordSerializer,
    RoleSerializer,
    TenantSerializer,
    UpdateMeSerializer,
    UserPermissionOverrideSerializer,
    UserSerializer,
    UserSessionSerializer,
    VerifyOTPSerializer,
)
from apps.core.emails import send_password_reset_otp_email

MAX_FAILED_LOGINS = 8
LOCKOUT = timedelta(minutes=15)


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _hash_otp(user_id, otp):
    secret = getattr(settings, "SECRET_KEY", "fallback-secret")
    return hashlib.sha256(f"{user_id}:{otp}:{secret}".encode()).hexdigest()



def _me_payload(user, request=None):
    """The ``/auth/me/`` body (api.md §2).

    A superuser passes every server check, so the UI is told they hold the
    whole catalogue -- otherwise client-side gating would hide the app from
    the one account that can do everything.
    """
    if user.is_superuser:
        permissions = sorted(Permission.objects.values_list("id", flat=True))
    else:
        permissions = sorted(user.effective_permissions())
    return {
        "user": MeUserSerializer(user).data,
        "permissions": permissions,
        "tenant": TenantSerializer(user.client).data,
    }


# ---------------------------------------------------------------------------
# api.md §2 -- Authentication
# ---------------------------------------------------------------------------
class LoginView(APIView):
    authentication_classes = []
    permission_classes = [AllowPublic]
    throttle_classes = [LoginThrottle, FailedLoginThrottle]

    def post(self, request):
        try:
            serializer = LoginSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            email = serializer.validated_data["email"].lower()
            password = serializer.validated_data["password"]

            candidates = list(
                User.objects.filter(email=email, deleted_at__isnull=True).select_related(
                    "client", "role"
                )
            )
            # A tenant slug disambiguates when the same address exists in more than
            # one tenant. Without it, an ambiguous address is treated as a failure
            # rather than guessing which tenant the caller meant.
            slug = (request.data.get("tenant") or request.data.get("clientSlug") or "").strip()
            if slug:
                candidates = [u for u in candidates if u.client.slug == slug]

            invalid = NotAuthenticated(
                "Incorrect email or password.", code="INVALID_CREDENTIALS"
            )

            if len(candidates) != 1:
                raise invalid

            user = candidates[0]

            lockout_attempts = getattr(settings, "ACCOUNT_LOCKOUT_ATTEMPTS", 5)
            lockout_minutes = getattr(settings, "ACCOUNT_LOCKOUT_MINUTES", 15)
            lockout_duration = timedelta(minutes=lockout_minutes)

            if user.is_locked():
                remaining_sec = int((user.locked_until - timezone.now()).total_seconds())
                remaining_min = max(1, math.ceil(remaining_sec / 60))
                raise NotAuthenticated(
                    f"This account is temporarily locked due to too many failed attempts. Please try again in {remaining_min} minute{'s' if remaining_min > 1 else ''}.",
                    code="ACCOUNT_LOCKED",
                )
            if user.status != "Active":
                raise NotAuthenticated(
                    "This account is not active. Contact your administrator.",
                    code="ACCOUNT_INACTIVE",
                )
            if user.client.status != "Active":
                raise NotAuthenticated(
                    "This workspace is not active.", code="TENANT_INACTIVE"
                )

            if not user.check_password(password):
                user.failed_login_count += 1
                fields = ["failed_login_count"]
                if user.failed_login_count >= lockout_attempts:
                    user.locked_until = timezone.now() + lockout_duration
                    fields.append("locked_until")
                    user.save(update_fields=fields)
                    record_audit(
                        client=user.client_id,
                        actor=user,
                        action="account_locked",
                        entity_type="User",
                        entity_id=user.id,
                        entity_label=user.email,
                        description=f"Account locked for {lockout_minutes} minutes after {user.failed_login_count} failed attempts",
                        ip=request_ip(request),
                    )
                    raise NotAuthenticated(
                        f"This account is temporarily locked due to too many failed login attempts. Please try again in {lockout_minutes} minutes.",
                        code="ACCOUNT_LOCKED",
                    )
                user.save(update_fields=fields)
                remaining = max(0, lockout_attempts - user.failed_login_count)
                raise NotAuthenticated(
                    f"Incorrect email or password. {remaining} attempt{'s' if remaining != 1 else ''} remaining before account lockout.",
                    code="INVALID_CREDENTIALS",
                )

            set_current_client_id(user.client_id)
            with transaction.atomic():
                user.failed_login_count = 0
                user.locked_until = None
                user.last_login_at = timezone.now()
                user.save(update_fields=["failed_login_count", "locked_until", "last_login_at"])

                refresh = RefreshToken.for_user(user)
                session = UserSession.objects.create(
                    user=user,
                    client=user.client,
                    refresh_token_hash=_hash(str(refresh)),
                    user_agent=request.META.get("HTTP_USER_AGENT", "")[:500],
                    ip=request_ip(request),
                    device_label=_device_label(request),
                    expires_at=timezone.now() + settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"],
                )
                tokens = build_tokens(user, session=session)
                # The stored hash must match the token actually handed out.
                session.refresh_token_hash = _hash(tokens["refresh"])
                session.save(update_fields=["refresh_token_hash"])

                record_audit(
                    client=user.client_id,
                    actor=user,
                    action="login",
                    entity_type="User",
                    entity_id=user.id,
                    entity_label=user.email,
                    description="Signed in",
                    ip=request_ip(request),
                )

            FailedLoginThrottle.clear_failures(request)
            return Response({**tokens, **_me_payload(user, request)})
        except Exception:
            FailedLoginThrottle.record_failure(request)
            raise


def _device_label(request):
    agent = request.META.get("HTTP_USER_AGENT", "")
    for needle, label in (
        ("Edg/", "Edge"),
        ("Chrome/", "Chrome"),
        ("Firefox/", "Firefox"),
        ("Safari/", "Safari"),
    ):
        if needle in agent:
            platform = "Windows" if "Windows" in agent else (
                "macOS" if "Mac OS" in agent else ("Android" if "Android" in agent else "")
            )
            return f"{label}{' on ' + platform if platform else ''}"
    return "Unknown device"


class RefreshView(APIView):
    """Single-flight silent refresh (api-integration.md §5.2)."""

    authentication_classes = []
    permission_classes = [AllowPublic]
    throttle_classes = [TokenRefreshThrottle]

    def post(self, request):
        serializer = RefreshSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        raw = serializer.validated_data["refresh"]

        try:
            token = RefreshToken(raw)
        except TokenError as exc:
            raise NotAuthenticated(
                "Your session has expired. Please sign in again.",
                code="TOKEN_INVALID",
                detail=str(exc),
            ) from exc

        session = UserSession.objects.filter(refresh_token_hash=_hash(raw)).first()
        if session is None or not session.is_active:
            raise NotAuthenticated(
                "Your session has been signed out.", code="TOKEN_REVOKED"
            )

        user = User.objects.select_related("client", "role").filter(
            pk=token.get("user_id"), deleted_at__isnull=True
        ).first()
        if user is None or user.status != "Active":
            raise NotAuthenticated("This account is not active.", code="ACCOUNT_INACTIVE")

        set_current_client_id(user.client_id)
        session.last_seen_at = timezone.now()
        session.save(update_fields=["last_seen_at"])
        return Response({"access": build_tokens(user, session=session)["access"]})


class LogoutView(APIView):
    def post(self, request):
        raw = request.data.get("refresh")
        session_id = getattr(request.user, "session_id", None)
        sessions = UserSession.objects.filter(user=request.user, revoked_at__isnull=True)
        if raw:
            sessions = sessions.filter(refresh_token_hash=_hash(raw))
        elif session_id:
            sessions = sessions.filter(pk=session_id)
        sessions.update(revoked_at=timezone.now(), revoked_reason="logout")
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(APIView):
    def get(self, request):
        return Response(_me_payload(request.user, request))

    def patch(self, request):
        before = user_link.capture(request.user, user_link.USER_FIELDS)
        serializer = UpdateMeSerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        # Your own name, phone and photo are also what HR has on file.
        changed = user_link.changed_fields(user, before, user_link.USER_FIELDS)
        if changed:
            user_link.push_user_to_employee(user, changed)
        return Response(_me_payload(request.user, request))


class ChangePasswordView(APIView):
    throttle_classes = [SensitiveActionThrottle]

    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data, context={"user": request.user})
        serializer.is_valid(raise_exception=True)
        if not request.user.check_password(serializer.validated_data["currentPassword"]):
            raise ValidationFailed(
                "Your current password is incorrect.",
                field_errors={"currentPassword": ["Incorrect password."]},
            )
        request.user.set_password(serializer.validated_data["newPassword"])
        request.user.save(update_fields=["password"])
        # Every other session is signed out; the current one keeps working.
        UserSession.objects.filter(user=request.user, revoked_at__isnull=True).exclude(
            pk=getattr(request.user, "session_id", None) or ""
        ).update(revoked_at=timezone.now(), revoked_reason="password_changed")
        record_audit(
            client=request.client_id,
            actor=request.user,
            action="change_password",
            entity_type="User",
            entity_id=request.user.id,
            entity_label=request.user.email,
            description="Password changed",
            ip=request_ip(request),
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class ForgotPasswordView(APIView):
    """Sends a 6-digit OTP code to the user's email if an active account exists."""

    authentication_classes = []
    permission_classes = [AllowPublic]
    throttle_classes = [PasswordResetThrottle]

    def post(self, request):
        serializer = ForgotPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"].lower()

        user = User.objects.filter(
            email=email, deleted_at__isnull=True, status="Active"
        ).first()
        if user is not None:
            # 6-digit numeric OTP (100000 - 999999)
            otp = f"{secrets.randbelow(900000) + 100000:06d}"

            # Invalidate any previously pending OTPs for this user
            PasswordResetOTP.objects.filter(user=user, used_at__isnull=True).update(
                used_at=timezone.now()
            )

            # Store fresh OTP valid for 15 minutes
            PasswordResetOTP.objects.create(
                user=user,
                email=email,
                otp_hash=_hash_otp(user.id, otp),
                expires_at=timezone.now() + timedelta(minutes=15),
            )

            # Also store reset token for fallback / backward compatibility
            token = secrets.token_urlsafe(48)
            PasswordResetToken.objects.create(
                user=user,
                token_hash=_hash(token),
                expires_at=timezone.now() + timedelta(minutes=15),
            )

            # Send OTP email
            send_password_reset_otp_email(
                email=email,
                otp=otp,
                user_name=user.name or "",
                expiry_minutes=15,
            )

            import logging
            logging.getLogger(__name__).info("Password reset OTP requested for %s", email)

        return Response(
            {
                "message": "If that email address is registered with an active account, a 6-digit verification code has been sent.",
                "email": email,
            },
            status=status.HTTP_200_OK,
        )


class VerifyOTPView(APIView):
    """Verify the 6-digit OTP code sent to the user's email."""

    authentication_classes = []
    permission_classes = [AllowPublic]
    throttle_classes = [OTPVerifyThrottle]

    def post(self, request):
        serializer = VerifyOTPSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"].lower()
        otp = serializer.validated_data["otp"].strip()

        user = User.objects.filter(
            email=email, deleted_at__isnull=True, status="Active"
        ).first()

        if user is None:
            raise ValidationFailed(
                "Invalid or expired verification code.",
                code=Codes.INVALID_OTP,
                field_errors={"otp": ["Invalid or expired verification code."]},
            )

        otp_record = (
            PasswordResetOTP.objects.filter(
                user=user,
                used_at__isnull=True,
                expires_at__gt=timezone.now(),
            )
            .order_by("-created_at")
            .first()
        )

        if otp_record is None or not otp_record.is_usable:
            raise ValidationFailed(
                "Verification code has expired or was not requested. Please request a new code.",
                code=Codes.OTP_EXPIRED,
                field_errors={"otp": ["Verification code expired or not found."]},
            )

        if otp_record.attempts >= 5:
            raise ValidationFailed(
                "Too many incorrect attempts. Please request a new verification code.",
                code=Codes.TOO_MANY_ATTEMPTS,
                field_errors={"otp": ["Maximum attempts exceeded. Please request a new code."]},
            )

        expected_hash = _hash_otp(user.id, otp)
        if otp_record.otp_hash != expected_hash:
            otp_record.attempts += 1
            otp_record.save(update_fields=["attempts"])
            remaining = max(0, 5 - otp_record.attempts)
            raise ValidationFailed(
                f"Invalid verification code. {remaining} attempt(s) remaining.",
                code=Codes.INVALID_OTP,
                field_errors={"otp": ["Invalid verification code."]},
            )

        # OTP is verified! Generate a single-use reset token valid for 15 minutes
        reset_token = secrets.token_urlsafe(48)
        PasswordResetToken.objects.create(
            user=user,
            token_hash=_hash(reset_token),
            expires_at=timezone.now() + timedelta(minutes=15),
        )

        # Mark OTP as used
        otp_record.used_at = timezone.now()
        otp_record.save(update_fields=["used_at"])

        return Response(
            {
                "valid": True,
                "message": "Verification code verified successfully.",
                "resetToken": reset_token,
            },
            status=status.HTTP_200_OK,
        )


class ResetPasswordView(APIView):
    authentication_classes = []
    permission_classes = [AllowPublic]
    throttle_classes = [PasswordResetThrottle]

    def post(self, request):
        serializer = ResetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        new_password = serializer.validated_data["newPassword"]
        token_str = serializer.validated_data.get("resetToken") or serializer.validated_data.get("token")
        email = serializer.validated_data.get("email")
        otp = serializer.validated_data.get("otp")

        user = None
        token_record = None
        otp_record = None

        if token_str:
            token_record = PasswordResetToken.objects.filter(
                token_hash=_hash(token_str)
            ).select_related("user").first()

            if token_record is None or not token_record.is_usable:
                raise ValidationFailed(
                    "That reset link or session is no longer valid. Please request a new one.",
                    code=Codes.TOKEN_EXPIRED,
                    field_errors={"token": ["Expired or already used."]},
                )
            user = token_record.user
        elif email and otp:
            email = email.lower().strip()
            otp = otp.strip()
            user = User.objects.filter(
                email=email, deleted_at__isnull=True, status="Active"
            ).first()

            if user is None:
                raise ValidationFailed(
                    "Invalid or expired verification code.",
                    code=Codes.INVALID_OTP,
                    field_errors={"otp": ["Invalid or expired verification code."]},
                )

            otp_record = (
                PasswordResetOTP.objects.filter(
                    user=user,
                    used_at__isnull=True,
                    expires_at__gt=timezone.now(),
                )
                .order_by("-created_at")
                .first()
            )

            if otp_record is None or not otp_record.is_usable:
                raise ValidationFailed(
                    "That verification code is no longer valid. Please request a new one.",
                    code=Codes.OTP_EXPIRED,
                    field_errors={"otp": ["Expired or already used."]},
                )

            if otp_record.otp_hash != _hash_otp(user.id, otp):
                otp_record.attempts += 1
                otp_record.save(update_fields=["attempts"])
                raise ValidationFailed(
                    "Invalid verification code.",
                    code=Codes.INVALID_OTP,
                    field_errors={"otp": ["Invalid verification code."]},
                )
            user = otp_record.user
        else:
            raise ValidationFailed(
                "Either a valid reset token or email and OTP are required.",
                code=Codes.VALIDATION_FAILED,
            )

        with transaction.atomic():
            user.set_password(new_password)
            user.failed_login_count = 0
            user.locked_until = None
            user.save(update_fields=["password", "failed_login_count", "locked_until"])

            if token_record:
                token_record.used_at = timezone.now()
                token_record.save(update_fields=["used_at"])

            if otp_record:
                otp_record.used_at = timezone.now()
                otp_record.save(update_fields=["used_at"])

            UserSession.objects.filter(user=user, revoked_at__isnull=True).update(
                revoked_at=timezone.now(), revoked_reason="password_reset"
            )

        return Response(
            {
                "message": "Password has been reset successfully. You can now sign in with your new password.",
            },
            status=status.HTTP_200_OK,
        )



class SessionListView(APIView):
    """``GET /auth/sessions/`` -- the topbar security panel."""

    def get(self, request):
        sessions = UserSession.objects.filter(user=request.user).order_by("-last_seen_at")
        data = UserSessionSerializer(
            sessions,
            many=True,
            context={"current_session_id": getattr(request.user, "session_id", None)},
        ).data
        return Response(envelope(data))


class SessionDetailView(APIView):
    def delete(self, request, pk):
        session = UserSession.objects.filter(pk=pk, user=request.user).first()
        if session is None:
            raise NotFound("That session no longer exists.")
        session.revoked_at = timezone.now()
        session.revoked_reason = "revoked_by_user"
        session.save(update_fields=["revoked_at", "revoked_reason"])
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# api.md §3.1 -- Users
# ---------------------------------------------------------------------------
class UserViewSet(TenantModelViewSet):
    queryset = User.objects.select_related("role", "reporting_manager", "employee").all()
    serializer_class = UserSerializer
    audit_entity_type = "User"
    audit_label_field = "email"
    search_fields = ["name", "email", "phone", "department"]
    ordering_fields = ["name", "email", "created_at", "last_login_at", "status"]
    ordering = ["name"]
    filter_map = {
        "role": "role__name",
        "roleId": "role_id",
        "department": "department",
        "status": "status",
    }
    status_field = "status"
    default_date_field = "joined_date"
    permission_map = {
        "list": ["view_staff"],
        "retrieve": ["view_staff"],
        "create": ["create_staff"],
        "update": ["edit_staff"],
        "partial_update": ["edit_staff"],
        "destroy": ["delete_staff"],
        "activate": ["edit_staff"],
        "deactivate": ["edit_staff"],
        "reset_password": ["reset_staff_password"],
        "permissions": ["manage_roles"],
        "stats": ["view_staff"],
    }

    # -- no-escalation rules -------------------------------------------------
    # ``edit_staff`` is an HR permission, not an access-control one. Without
    # these checks it could hand out the Administrator role or overwrite an
    # administrator's password and sign in as them.
    def _guard_target(self, target):
        """Only an administrator may change an administrator's account, and
        nobody may change an account that holds more than they do."""
        actor = self.request.user
        if actor.is_superuser:
            return
        if target.is_superuser:
            raise PermissionDenied(
                "Only a superuser can change a superuser account.", code="SUPERUSER_ONLY"
            )
        target_permissions = target.effective_permissions()
        if has_permission(actor, "manage_roles"):
            beyond = sorted(target_permissions - granted_permissions(actor))
            if beyond:
                raise PermissionDenied(
                    "You cannot change an account with permissions you don't hold.",
                    code="manage_roles",
                    detail=f"'{target.name}' also holds: {', '.join(beyond)}.",
                )
            return
        if "manage_roles" in target_permissions:
            raise PermissionDenied(
                "Only an administrator can change an administrator's account.",
                code="manage_roles",
            )

    def _guard_role(self, role):
        """A role may only be handed out by someone who holds all of it --
        ``manage_roles`` included, or a partial administrator could assign
        the Administrator role to themselves."""
        actor = self.request.user
        if role is None or actor.is_superuser:
            return
        beyond = sorted(role.permission_ids() - granted_permissions(actor))
        if beyond:
            raise PermissionDenied(
                "You cannot assign a role with permissions you don't hold.",
                code="manage_roles",
                detail=f"Role '{role.name}' also grants: {', '.join(beyond)}.",
            )

    def _guard_password(self, target, validated_data):
        """Setting someone else's password is a reset, not a profile edit."""
        if not validated_data.get("password"):
            return
        if target is not None and target.pk == self.request.user.pk:
            return
        actor = self.request.user
        if actor.is_superuser or has_permission(actor, "manage_roles"):
            return
        require_permission(self.request.user, "reset_staff_password")

    @transaction.atomic
    def perform_create(self, serializer):
        self._guard_role(serializer.validated_data.get("role"))
        user = super().perform_create(serializer)
        self._link_employee(user, created=True)
        return user

    @transaction.atomic
    def perform_update(self, serializer):
        instance = serializer.instance
        self._guard_target(instance)
        if "role" in serializer.validated_data:
            new_role = serializer.validated_data["role"]
            if (new_role.pk if new_role else None) != instance.role_id:
                self._guard_role(new_role)
        self._guard_password(instance, serializer.validated_data)
        before = user_link.capture(instance, user_link.USER_FIELDS)
        previous_employee = instance.employee_id
        user = super().perform_update(serializer)
        if user.employee_id != previous_employee:
            self._link_employee(user)
        else:
            changed = user_link.changed_fields(user, before, user_link.USER_FIELDS)
            if changed:
                user_link.push_user_to_employee(user, changed)
        return user

    def _link_employee(self, user, *, created=False):
        """Keep the login and its HRMS employee record as one person.

        A new staff login with no matching employee gets one, the way HRMS's
        "Create Administration Login Account" makes a login for a new employee
        (``createEmployee: false`` opts out). A fresh link takes HR's values.
        """
        employee = user_link.linked_employee(user)
        if employee is not None:
            user_link.reconcile(user, employee)
            self.write_audit(
                "link_employee", user,
                description=f"Linked to employee {employee.employee_code}",
            )
            return
        wants_record = self.request.data.get("createEmployee", True) not in (False, "false", "0", 0)
        if created and wants_record and not user.is_customer:
            employee = user_link.create_employee_for(user, actor=self.request.user)
            user.employee = employee
            user.save(update_fields=["employee", "updated_at"])
            self.write_audit(
                "provision_employee", user,
                description=f"Employee record {employee.employee_code} created",
            )

    def get_aggregates(self, queryset):
        """api.md §3.1 -- "KPI tiles: total, active, inactive, admins"."""
        rows = queryset.aggregate(
            total=Count("id"),
            active=Count("id", filter=Q(status="Active")),
            inactive=Count("id", filter=Q(status="Inactive")),
            admins=Count("id", filter=Q(role__code="AD")),
        )
        return rows

    def perform_destroy(self, instance):
        """api.md §3.1 -- soft delete (``status: Deleted``)."""
        self._guard_target(instance)
        instance.status = "Deleted"
        instance.is_active = False
        instance.save(update_fields=["status", "is_active"])
        UserSession.objects.filter(user=instance, revoked_at__isnull=True).update(
            revoked_at=timezone.now(), revoked_reason="user_deleted"
        )
        super().perform_destroy(instance)

    @action(detail=False, methods=["get"], url_path="stats")
    def stats(self, request):
        return Response(self.get_aggregates(self.filter_queryset(self.get_queryset())))

    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        user = self.get_object()
        self._guard_target(user)
        user.status = "Active"
        user.is_active = True
        user.save(update_fields=["status", "is_active"])
        self.write_audit("activate", user, description="Account activated")
        return Response(self.get_serializer(user).data)

    @action(detail=True, methods=["post"])
    def deactivate(self, request, pk=None):
        """api.md §3.1 -- ``status: Inactive``, revoke sessions."""
        user = self.get_object()
        self._guard_target(user)
        if user.id == request.user.id:
            raise Conflict(
                "You cannot deactivate your own account.", code="SELF_DEACTIVATION"
            )
        with transaction.atomic():
            user.status = "Inactive"
            user.is_active = False
            user.save(update_fields=["status", "is_active"])
            UserSession.objects.filter(user=user, revoked_at__isnull=True).update(
                revoked_at=timezone.now(), revoked_reason="user_deactivated"
            )
            self.write_audit("deactivate", user, description="Account deactivated")
        return Response(self.get_serializer(user).data)

    @action(detail=True, methods=["post"], url_path="reset-password")
    def reset_password(self, request, pk=None):
        user = self.get_object()
        self._guard_target(user)

        otp = f"{secrets.randbelow(900000) + 100000:06d}"
        PasswordResetOTP.objects.filter(user=user, used_at__isnull=True).update(used_at=timezone.now())
        PasswordResetOTP.objects.create(
            user=user,
            email=user.email,
            otp_hash=_hash_otp(user.id, otp),
            expires_at=timezone.now() + timedelta(minutes=30),
        )

        token = secrets.token_urlsafe(48)
        PasswordResetToken.objects.create(
            user=user, token_hash=_hash(token), expires_at=timezone.now() + timedelta(hours=24)
        )
        send_password_reset_otp_email(
            email=user.email,
            otp=otp,
            user_name=getattr(user, "name", "") or "",
            expiry_minutes=30,
        )
        self.write_audit("reset_password", user, description="Admin-triggered password reset")
        import logging

        logging.getLogger(__name__).info("Admin reset OTP dispatched for %s", user.email)
        return Response({"message": f"A password reset verification code has been sent to {user.email}."})

    @action(detail=True, methods=["post"], url_path="unlock")
    def unlock(self, request, pk=None):
        """Administrator unlocks a temporarily locked account."""
        user = self.get_object()
        self._guard_target(user)
        user.failed_login_count = 0
        user.locked_until = None
        user.save(update_fields=["failed_login_count", "locked_until"])
        self.write_audit("unlock", user, description="Account unlocked by administrator")
        return Response({"message": f"Account for {user.email} has been unlocked."})

    @action(detail=True, methods=["get", "post"], url_path="permissions")
    def permissions(self, request, pk=None):
        """Per-user overrides on top of the role (api.md §3.1)."""
        user = self.get_object()
        if request.method == "GET":
            overrides = UserPermission.objects.filter(user=user)
            return Response(
                {
                    "effective": sorted(user.effective_permissions()),
                    "role": sorted(user.role.permission_ids()) if user.role_id else [],
                    "grant": sorted(
                        o.permission_id for o in overrides if o.effect == "grant"
                    ),
                    "deny": sorted(o.permission_id for o in overrides if o.effect == "deny"),
                }
            )

        self._guard_target(user)
        serializer = UserPermissionOverrideSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        actor = request.user
        if not actor.is_superuser:
            beyond = sorted(set(serializer.validated_data.get("grant", [])) - granted_permissions(actor))
            if beyond:
                raise PermissionDenied(
                    "You cannot grant permissions you don't hold.",
                    code="manage_roles",
                    detail=f"Not yours to grant: {', '.join(beyond)}.",
                )
        with transaction.atomic():
            UserPermission.objects.filter(user=user).delete()
            UserPermission.objects.bulk_create(
                [
                    UserPermission(user=user, permission_id=p, effect="grant")
                    for p in serializer.validated_data.get("grant", [])
                ]
                + [
                    UserPermission(user=user, permission_id=p, effect="deny")
                    for p in serializer.validated_data.get("deny", [])
                ]
            )
            self.write_audit(
                "permissions", user, after=serializer.validated_data,
                description="Permission overrides updated",
            )
        return Response({"effective": sorted(user.effective_permissions())})


# ---------------------------------------------------------------------------
# api.md §3.2 -- Roles and permissions
# ---------------------------------------------------------------------------
class RoleViewSet(TenantModelViewSet):
    queryset = Role.objects.all()
    serializer_class = RoleSerializer
    audit_entity_type = "Role"
    audit_label_field = "name"
    search_fields = ["name", "code", "description"]
    ordering_fields = ["name", "code", "created_at"]
    ordering = ["name"]
    status_field = None
    permission_map = {
        # Whoever may create or edit staff picks a role on the user form, so
        # they may list the roles; only ``manage_roles`` may change one.
        "list": [("manage_roles", "create_staff", "edit_staff")],
        "read": ["manage_roles"],
        "write": ["manage_roles"],
    }

    def get_queryset(self):
        return super().get_queryset().annotate(
            user_count=Count("users", filter=Q(users__deleted_at__isnull=True))
        )

    # -- no-escalation rules -------------------------------------------------
    # ``manage_roles`` alone must not be a way to every permission: a role
    # can only be given what its editor holds, and a role that grants more
    # than its editor holds is out of their reach.
    def _guard_role_change(self, role, requested):
        actor = self.request.user
        if actor.is_superuser:
            return
        held = granted_permissions(actor)
        if role is not None:
            beyond = sorted(role.permission_ids() - held)
            if beyond:
                raise PermissionDenied(
                    "You cannot change a role with permissions you don't hold.",
                    code="manage_roles",
                    detail=f"Role '{role.name}' also grants: {', '.join(beyond)}.",
                )
        if requested is None:
            return
        beyond = sorted(set(requested) - held)
        if beyond:
            raise PermissionDenied(
                "You cannot grant permissions you don't hold.",
                code="manage_roles",
                detail=f"Not yours to grant: {', '.join(beyond)}.",
            )
        if (
            role is not None
            and role.pk == actor.role_id
            and "manage_roles" in role.permission_ids()
            and "manage_roles" not in requested
            and not UserPermission.objects.filter(
                user=actor, permission_id="manage_roles", effect="grant"
            ).exists()
        ):
            raise Conflict(
                "You cannot remove 'Manage roles' from your own role.",
                code="SELF_LOCKOUT",
                detail="You would lose access to this screen. Ask another administrator.",
            )

    def perform_create(self, serializer):
        self._guard_role_change(None, serializer.validated_data.get("selectedPermissions", []))
        return super().perform_create(serializer)

    def perform_update(self, serializer):
        self._guard_role_change(
            serializer.instance, serializer.validated_data.get("selectedPermissions")
        )
        return super().perform_update(serializer)

    def check_delete_allowed(self, role):
        """api.md §3.2 -- 409 if users are attached unless ``?reassignTo=``."""
        attached = User.objects.filter(role=role, deleted_at__isnull=True)
        count = attached.count()
        if not count:
            return

        reassign_to = self.request.query_params.get("reassignTo")
        if not reassign_to:
            raise Conflict(
                f"{count} user{'s' if count != 1 else ''} still use this role.",
                code=Codes.IN_USE,
                detail="Pass ?reassignTo=<roleId> to move them to another role first.",
                payload={"userCount": count},
            )

        target = Role.objects.filter(
            pk=reassign_to, client_id=self.get_client_id(), deleted_at__isnull=True
        ).first()
        if target is None:
            raise ValidationFailed(
                "The role to reassign to was not found.",
                field_errors={"reassignTo": ["Unknown role id."]},
            )
        if target.pk == role.pk:
            raise ValidationFailed(
                "Reassign to a different role.",
                field_errors={"reassignTo": ["Cannot reassign to the role being deleted."]},
            )
        attached.update(role=target)

    def perform_destroy(self, instance):
        if instance.is_system:
            raise Conflict(
                "System roles cannot be deleted.", code="SYSTEM_ROLE"
            )
        self._guard_role_change(instance, None)
        super().perform_destroy(instance)

    @action(detail=True, methods=["post"])
    def duplicate(self, request, pk=None):
        """The UI's "Copy" action (api.md §3.2)."""
        source = self.get_object()
        self._guard_role_change(None, source.permission_ids())
        base_code = f"{source.code}-COPY"
        code, suffix = base_code, 1
        while Role.objects.filter(
            client_id=self.get_client_id(), code=code, deleted_at__isnull=True
        ).exists():
            suffix += 1
            code = f"{base_code}{suffix}"

        with transaction.atomic():
            clone = Role.objects.create(
                client_id=self.get_client_id(),
                code=code,
                name=request.data.get("name") or f"{source.name} (Copy)",
                description=source.description,
                is_system=False,
                created_by=request.user,
                updated_by=request.user,
            )
            RolePermission.objects.bulk_create(
                [
                    RolePermission(role=clone, permission_id=permission_id)
                    for permission_id in source.permission_ids()
                ]
            )
            self.write_audit("duplicate", clone, description=f"Duplicated from {source.name}")
        clone.user_count = 0
        return Response(self.get_serializer(clone).data, status=status.HTTP_201_CREATED)


class PermissionCatalogueView(APIView):
    """``GET /admin/permissions/`` -- modules -> groups -> permissions."""

    permission_classes = [HasModulePermission]
    required_permissions = ["manage_roles"]

    def get(self, request):
        flat = request.query_params.get("flat") == "true"
        if flat:
            rows = PermissionSerializer(Permission.objects.all(), many=True).data
            return Response(envelope(rows))
        return Response({"modules": grouped_catalogue()})


# ---------------------------------------------------------------------------
# api.md §3.3 -- Clients (tenants)
# ---------------------------------------------------------------------------
class ClientViewSet(TenantModelViewSet):
    """Administration -> Clients.

    A tenant can only see and edit itself; the list is therefore its own row.
    Managing *other* tenants is a platform-operator concern and deliberately
    outside this tenant-scoped API.
    """

    queryset = Client.objects.all()
    serializer_class = ClientSerializer
    audit_entity_type = "Client"
    audit_label_field = "name"
    search_fields = ["name", "contact_name", "contact_email", "industry"]
    ordering = ["name"]
    filter_soft_deleted = False
    permission_map = {"read": ["menu_admin"], "write": ["manage_company_profile"]}

    def get_queryset(self):
        client_id = self.get_client_id()
        if client_id is None:
            return Client.objects.none()
        return Client.objects.filter(pk=client_id)

    def create_defaults(self):
        return {}

    def create(self, request, *args, **kwargs):
        raise PermissionDenied(
            "Tenants are provisioned by the platform operator.",
            code="TENANT_PROVISIONING",
        )

    @action(detail=True, methods=["get"])
    def deals(self, request, pk=None):
        """api.md §3.3 -- linked CRM deals."""
        from apps.crm.models import Deal
        from apps.crm.serializers import DealSerializer

        self.get_object()
        rows = Deal.objects.filter(client_id=pk, deleted_at__isnull=True).select_related(
            "party", "owner"
        )
        return Response(envelope(DealSerializer(rows, many=True).data))

    @action(detail=True, methods=["get"])
    def projects(self, request, pk=None):
        """api.md §3.3 -- linked projects with ``progress``."""
        from apps.pms.models import Project
        from apps.pms.serializers import ProjectListSerializer

        self.get_object()
        rows = Project.objects.filter(client_id=pk, deleted_at__isnull=True).select_related(
            "project_manager"
        )
        return Response(envelope(ProjectListSerializer(rows, many=True).data))
