"""Serializers for auth and administration (api.md §2 and §3)."""
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from apps.core.exceptions import ValidationFailed
from apps.core.serializers import (
    BaseModelSerializer,
    BaseSerializer,
    TenantPrimaryKeyRelatedField,
)

from .models import Client, Permission, Role, RolePermission, User, UserPermission, UserSession


# ---------------------------------------------------------------------------
# Identity (api.md §2)
# ---------------------------------------------------------------------------
class RoleRefSerializer(BaseModelSerializer):
    """The nested role shape in ``/auth/me/``: ``{ id, code, name }``."""

    class Meta:
        model = Role
        fields = ["id", "code", "name"]


class MeUserSerializer(BaseModelSerializer):
    """api.md §2 -- drives sidebar visibility and every permission gate."""

    role = RoleRefSerializer(read_only=True)
    employeeId = serializers.SerializerMethodField()
    avatar = serializers.SerializerMethodField()
    reportingManager = serializers.SerializerMethodField()
    lastLogin = serializers.DateTimeField(source="last_login_at", read_only=True)
    partyId = serializers.CharField(source="party_id", read_only=True, allow_null=True)
    partyName = serializers.SerializerMethodField()
    isCustomer = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id", "name", "email", "phone", "avatar", "employeeId", "role",
            "department", "location", "reportingManager", "status", "lastLogin",
            "partyId", "partyName", "isCustomer",
        ]

    def get_employeeId(self, user):
        return user.employee.employee_code if user.employee_id else None

    def get_avatar(self, user):
        return user.avatar_url

    def get_reportingManager(self, user):
        return user.reporting_manager.name if user.reporting_manager_id else None

    def get_partyName(self, user):
        return user.party.name if user.party_id else None

    def get_isCustomer(self, user):
        return user.is_customer


class TenantSerializer(BaseModelSerializer):
    class Meta:
        model = Client
        fields = ["id", "name", "plan"]


class LoginSerializer(BaseSerializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)


class RefreshSerializer(BaseSerializer):
    refresh = serializers.CharField()


class ChangePasswordSerializer(BaseSerializer):
    currentPassword = serializers.CharField(write_only=True, trim_whitespace=False)
    newPassword = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_newPassword(self, value):
        try:
            validate_password(value, user=self.context.get("user"))
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages))
        return value


class ForgotPasswordSerializer(BaseSerializer):
    email = serializers.EmailField()


class VerifyOTPSerializer(BaseSerializer):
    email = serializers.EmailField()
    otp = serializers.CharField(min_length=6, max_length=6, trim_whitespace=True)


class ResetPasswordSerializer(BaseSerializer):
    token = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    resetToken = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    email = serializers.EmailField(required=False, allow_blank=True, allow_null=True)
    otp = serializers.CharField(required=False, allow_blank=True, allow_null=True, min_length=6, max_length=6, trim_whitespace=True)
    newPassword = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_newPassword(self, value):
        try:
            validate_password(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages))
        return value

    def validate(self, attrs):
        token = attrs.get("resetToken") or attrs.get("token")
        email = attrs.get("email")
        otp = attrs.get("otp")

        if not token and not (email and otp):
            raise serializers.ValidationError(
                "Either a reset token or both email and OTP are required to reset password."
            )
        return attrs



class UpdateMeSerializer(BaseModelSerializer):
    """``PATCH /auth/me/`` -- own profile only: name, phone, avatar."""

    avatar = serializers.CharField(source="avatar_url", required=False, allow_null=True)

    class Meta:
        model = User
        fields = ["name", "phone", "avatar"]


class UserSessionSerializer(BaseModelSerializer):
    isCurrent = serializers.SerializerMethodField()
    device = serializers.CharField(source="device_label", read_only=True)

    class Meta:
        model = UserSession
        fields = [
            "id", "device", "user_agent", "ip", "issued_at", "last_seen_at",
            "expires_at", "revoked_at", "isCurrent",
        ]

    def get_isCurrent(self, session):
        return str(session.id) == str(self.context.get("current_session_id") or "")


# ---------------------------------------------------------------------------
# Administration -- users (api.md §3.1)
# ---------------------------------------------------------------------------
class UserSerializer(BaseModelSerializer):
    """The exact row shape api.md §3.1 documents.

    ``role`` is the role *name* (the UI renders it as text) while ``roleId`` is
    the writable relation -- both appear on the row in api.md.
    """

    role = serializers.SerializerMethodField()
    # Tenant-scoped: a role or manager id from another tenant must not resolve.
    roleId = TenantPrimaryKeyRelatedField(
        source="role", queryset=Role.objects.all(), allow_null=True, required=False
    )
    #: The linked HRMS employee's code (read) -- or, written, the employee to
    #: link by id or code; ``""`` / ``null`` unlinks.
    employeeId = serializers.SerializerMethodField()
    employeeRecordId = serializers.SerializerMethodField()
    employeeName = serializers.SerializerMethodField()
    reportingManager = serializers.SerializerMethodField()
    reportingManagerId = TenantPrimaryKeyRelatedField(
        source="reporting_manager",
        queryset=User.objects.all(),
        allow_null=True,
        required=False,
    )
    avatar = serializers.CharField(source="avatar_url", required=False, allow_null=True)
    joinedDate = serializers.DateField(source="joined_date", required=False, allow_null=True)
    lastLogin = serializers.DateTimeField(source="last_login_at", read_only=True)
    partyId = TenantPrimaryKeyRelatedField(
        source="party", model="masters.Party", required=False, allow_null=True
    )
    partyName = serializers.CharField(source="party.name", read_only=True, allow_null=True)
    isCustomer = serializers.SerializerMethodField()
    permissions = serializers.SerializerMethodField()
    password = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = User
        fields = [
            "id", "name", "email", "phone", "role", "roleId", "department", "status",
            "joinedDate", "lastLogin", "employeeId", "employeeRecordId", "employeeName",
            "location", "reportingManager",
            "reportingManagerId", "avatar", "permissions", "crm_roles", "password",
            "partyId", "partyName", "isCustomer",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def get_role(self, user):
        return user.role.name if user.role_id else None

    def _employee(self, user):
        from apps.hrms.user_link import linked_employee

        return linked_employee(user)

    def get_employeeId(self, user):
        employee = self._employee(user)
        return employee.employee_code if employee else None

    def get_employeeRecordId(self, user):
        employee = self._employee(user)
        return str(employee.id) if employee else None

    def get_employeeName(self, user):
        employee = self._employee(user)
        return employee.name if employee else None

    def get_reportingManager(self, user):
        return user.reporting_manager.name if user.reporting_manager_id else None

    def get_isCustomer(self, user):
        return user.is_customer

    def get_permissions(self, user):
        """Role-derived plus overrides (api.md §3.1: "permissions[]" on detail)."""
        return sorted(user.effective_permissions())

    _NO_LINK_INPUT = object()

    def to_internal_value(self, data):
        ret = super().to_internal_value(data)
        if isinstance(data, dict) and "employeeId" in data:
            value = data["employeeId"]
            ret["_employee_link"] = str(value).strip() if value not in (None, "") else ""
        return ret

    def _find_employee(self, client_id, value):
        from uuid import UUID

        from apps.hrms.models import Employee

        live = Employee.objects.filter(client_id=client_id, deleted_at__isnull=True)
        try:
            found = live.filter(id=UUID(value)).first()
        except (ValueError, TypeError):
            found = None
        return found or live.filter(employee_code__iexact=value).first()

    def validate(self, attrs):
        """Resolve ``employeeId`` to the HRMS record the login belongs to.

        Given: link that employee (by id or code), or unlink when blank.
        Not given: a login without an employee is matched to the employee with
        the same email, if nobody else holds that record. Customer logins
        never link -- they are not staff.
        """
        from apps.hrms.user_link import assert_linkable, linked_employee, linked_user

        attrs = super().validate(attrs)
        link = attrs.pop("_employee_link", self._NO_LINK_INPUT)
        client_id = self.context.get("client_id")
        if not client_id:
            return attrs

        is_customer = bool(
            attrs.get("party") or (self.instance is not None and self.instance.party_id)
        )
        if link is not self._NO_LINK_INPUT:
            if not link:
                attrs["employee"] = None
                return attrs
            employee = self._find_employee(client_id, link)
            if employee is None:
                raise ValidationFailed(
                    "No employee with that ID.",
                    field_errors={"employeeId": [f"'{link}' is not an HRMS employee."]},
                )
            assert_linkable(employee, self.instance)
            attrs["employee"] = employee
            return attrs

        already = linked_employee(self.instance) if self.instance is not None else None
        email = (attrs.get("email") or "").strip()
        if already is None and email and not is_customer:
            from apps.hrms.models import Employee

            match = Employee.objects.filter(
                client_id=client_id, email__iexact=email, deleted_at__isnull=True
            ).first()
            if match is not None and linked_user(match) is None:
                attrs["employee"] = match
        return attrs

    def validate_email(self, value):
        value = value.lower()
        client_id = self.context.get("client_id")
        existing = User.objects.filter(client_id=client_id, email=value, deleted_at__isnull=True)
        if self.instance is not None:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return value

    def create(self, validated_data):
        password = validated_data.pop("password", None)
        user = User(**validated_data)
        if password and str(password).strip():
            user.set_password(str(password).strip())
        else:
            user.set_password("Password@123")

        if not user.status or user.status == "Invited":
            user.status = "Active"
        user.is_active = True
        user.save()
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        user = super().update(instance, validated_data)
        if password and str(password).strip():
            user.set_password(str(password).strip())
            user.save(update_fields=["password"])
        return user


class UserPermissionOverrideSerializer(BaseSerializer):
    """``POST /admin/users/{id}/permissions/`` -- overrides on top of the role."""

    grant = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    deny = serializers.ListField(child=serializers.CharField(), required=False, default=list)

    def validate(self, attrs):
        known = set(Permission.objects.values_list("id", flat=True))
        unknown = [p for p in attrs.get("grant", []) + attrs.get("deny", []) if p not in known]
        if unknown:
            raise ValidationFailed(
                "Unknown permission ids.",
                field_errors={"permissions": [f"Not in the catalogue: {', '.join(unknown)}"]},
            )
        overlap = set(attrs.get("grant", [])) & set(attrs.get("deny", []))
        if overlap:
            raise ValidationFailed(
                "A permission cannot be both granted and denied.",
                field_errors={"deny": [", ".join(sorted(overlap))]},
            )
        return attrs


# ---------------------------------------------------------------------------
# Administration -- roles (api.md §3.2)
# ---------------------------------------------------------------------------
class RoleSerializer(BaseModelSerializer):
    userCount = serializers.SerializerMethodField()
    selectedPermissions = serializers.ListField(
        child=serializers.CharField(), required=False, write_only=True
    )
    permissions = serializers.SerializerMethodField()

    class Meta:
        model = Role
        fields = [
            "id", "code", "name", "description", "is_system", "userCount",
            "permissions", "selectedPermissions", "created_at", "updated_at",
        ]
        read_only_fields = ["is_system", "created_at", "updated_at"]
        # The Roles screen asks for a name only; the code is derived from it.
        extra_kwargs = {"code": {"required": False}}

    def get_userCount(self, role):
        cached = getattr(role, "user_count", None)
        if cached is not None:
            return cached
        return role.users.filter(deleted_at__isnull=True).count()

    def get_permissions(self, role):
        return sorted(
            RolePermission.objects.filter(role=role).values_list("permission_id", flat=True)
        )

    def validate_code(self, value):
        client_id = self.context.get("client_id")
        existing = Role.objects.filter(client_id=client_id, code=value, deleted_at__isnull=True)
        if self.instance is not None:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise serializers.ValidationError("A role with this code already exists.")
        return value

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if self.instance is None and not (attrs.get("code") or "").strip():
            attrs["code"] = self._derive_code(attrs.get("name") or "")
        return attrs

    def _derive_code(self, name):
        """Initials of the name ("Regional Sales Lead" -> "RSL"), made unique."""
        words = [w for w in "".join(c if c.isalnum() else " " for c in name).split() if w]
        if len(words) > 1:
            base = "".join(w[0] for w in words[:4]).upper()
        else:
            base = (words[0][:3] if words else "ROLE").upper()
        taken = set(
            Role.objects.filter(
                client_id=self.context.get("client_id"), deleted_at__isnull=True
            ).values_list("code", flat=True)
        )
        code, suffix = base, 1
        while code in taken:
            suffix += 1
            code = f"{base}{suffix}"
        return code

    def _apply_permissions(self, role, permission_ids):
        permission_ids = list(dict.fromkeys(permission_ids))
        known = set(Permission.objects.values_list("id", flat=True))
        unknown = [p for p in permission_ids if p not in known]
        if unknown:
            raise ValidationFailed(
                "Unknown permission ids.",
                field_errors={
                    "selectedPermissions": [f"Not in the catalogue: {', '.join(unknown)}"]
                },
            )
        RolePermission.objects.filter(role=role).delete()
        RolePermission.objects.bulk_create(
            [RolePermission(role=role, permission_id=p) for p in permission_ids]
        )

    def create(self, validated_data):
        permission_ids = validated_data.pop("selectedPermissions", [])
        role = super().create(validated_data)
        self._apply_permissions(role, permission_ids)
        return role

    def update(self, instance, validated_data):
        permission_ids = validated_data.pop("selectedPermissions", None)
        role = super().update(instance, validated_data)
        if permission_ids is not None:
            self._apply_permissions(role, permission_ids)
        return role


class PermissionSerializer(BaseModelSerializer):
    class Meta:
        model = Permission
        fields = ["id", "module", "group", "label", "description", "sort_order"]


# ---------------------------------------------------------------------------
# Administration -- clients / tenants (api.md §3.3)
# ---------------------------------------------------------------------------
class ClientSerializer(BaseModelSerializer):
    userCount = serializers.SerializerMethodField()

    class Meta:
        model = Client
        fields = [
            "id", "name", "slug", "plan", "status", "timezone", "currency",
            "fy_start_month", "onboarded_on", "contact_name", "contact_email",
            "contact_phone", "industry", "notes", "is_demo", "userCount",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def get_userCount(self, client):
        return client.users.filter(deleted_at__isnull=True).count()
