"""
Company profile, backup / restore and data reset (api.md §3.4).

These replace ``exportDatabaseSnapshot()``, ``importDatabaseSnapshot()`` and
``resetDemoData()``, which today serialise the whole of ``localStorage``.
"""
from django.conf import settings as django_settings
from django.db import transaction
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .exceptions import PermissionDenied, ValidationFailed
from .files import public_url
from .models import CompanyProfile, File
from .permissions import HasModulePermission
from .serializers import BaseModelSerializer, TenantPrimaryKeyRelatedField


class CompanyProfileSerializer(BaseModelSerializer):
    logo = serializers.SerializerMethodField()
    signature = serializers.SerializerMethodField()
    logoFileId = TenantPrimaryKeyRelatedField(
        source="logo_file",
        model="core.File",
        required=False,
        allow_null=True,
        write_only=True,
    )
    signatureFileId = TenantPrimaryKeyRelatedField(
        source="signature_file",
        model="core.File",
        required=False,
        allow_null=True,
        write_only=True,
    )
    bankAccountId = TenantPrimaryKeyRelatedField(
        source="bank_account",
        model="accounting.BankAccount",
        required=False,
        allow_null=True,
    )

    class Meta:
        model = CompanyProfile
        fields = [
            "legal_name", "trade_name", "gstin", "pan", "cin", "address", "state",
            "state_code", "currency", "phone", "email", "website", "logo", "signature",
            "logoFileId", "signatureFileId", "bankAccountId", "updated_at",
        ]

    def get_logo(self, profile):
        return public_url(profile.logo_file, self.context.get("request"))

    def get_signature(self, profile):
        return public_url(profile.signature_file, self.context.get("request"))


class CompanyProfileView(APIView):
    """``GET/PUT /settings/company-profile/``.

    ``state`` here is what decides CGST+SGST vs IGST on every document
    (api.md §5.7), so it is the one field worth getting right on day one.
    """

    permission_classes = [HasModulePermission]
    permission_map = {"read": [], "write": ["manage_company_profile"]}

    def _profile(self, request):
        profile, _ = CompanyProfile.objects.get_or_create(
            client_id=request.client_id,
            defaults={"legal_name": request.user.client.name, "state": ""},
        )
        return profile

    def get(self, request):
        profile = self._profile(request)
        return Response(
            CompanyProfileSerializer(profile, context={"request": request}).data
        )

    def put(self, request):
        profile = self._profile(request)
        serializer = CompanyProfileSerializer(
            profile, data=request.data, partial=True, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    patch = put


class BackupView(APIView):
    """``POST /settings/backup/`` -- replaces ``exportDatabaseSnapshot()``."""

    permission_classes = [HasModulePermission]
    required_permissions = ["system_backup"]

    def post(self, request):
        from .snapshot import export_snapshot

        payload = export_snapshot(request.client_id)
        return Response(payload)


class RestoreView(APIView):
    """``POST /settings/restore/`` -- replaces ``importDatabaseSnapshot()``."""

    permission_classes = [HasModulePermission]
    required_permissions = ["system_backup"]

    def post(self, request):
        from .snapshot import import_snapshot

        snapshot = request.data.get("snapshot") or request.data
        if not isinstance(snapshot, dict) or "tables" not in snapshot:
            raise ValidationFailed(
                "That does not look like a snapshot.",
                field_errors={"snapshot": ["Expected an object with a 'tables' key."]},
            )
        result = import_snapshot(request.client_id, snapshot, user=request.user)
        return Response(result)


class ResetDemoDataView(APIView):
    """``POST /settings/reset-demo-data/`` -- non-production only (api.md §3.4).

    Erases the tenant's business data and leaves an empty workspace: users,
    roles, permissions, company profile, settings and stage catalogues stay.
    Nothing is re-seeded.
    """

    permission_classes = [HasModulePermission]
    required_permissions = ["system_backup"]

    def post(self, request):
        client = request.user.client
        if not client.is_demo and not django_settings.DEBUG:
            raise PermissionDenied(
                "Data can only be reset on a demo or development workspace.",
                code="NOT_A_DEMO_TENANT",
            )

        from .tenant_setup import bootstrap_configuration, clear_business_data

        deleted = clear_business_data(client)
        with transaction.atomic():
            bootstrap_configuration(client)
        return Response(
            {"message": "All business data has been erased.", "deleted": sum(deleted.values())}
        )
