"""
Management command to register or update a Meta Lead Ads integration.
"""
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import Client, User
from apps.crm.integrations.crypto import encrypt_credentials
from apps.crm.models import LeadSourceIntegration, Stage


class Command(BaseCommand):
    help = "Creates or updates a Meta Lead Ads integration record for a tenant."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", required=True, help="Tenant slug or UUID")
        parser.add_argument("--page-id", required=True, help="Meta Facebook Page ID")
        parser.add_argument("--token", required=True, help="Meta Page Access Token")
        parser.add_argument("--name", default="", help="Label for this integration")
        parser.add_argument("--stage", default="", help="Default Stage name or UUID")
        parser.add_argument("--owner", default="", help="Default Owner user email or UUID")
        parser.add_argument("--forms", default="", help="Comma-separated Form IDs for backfill")

    def handle(self, *args, **options):
        tenant_ident = options["tenant"]
        page_id = str(options["page_id"]).strip()
        token = str(options["token"]).strip()
        label = options["name"].strip() or f"Meta Lead Ads ({page_id})"

        client = Client.objects.filter(slug=tenant_ident).first() or Client.objects.filter(id=tenant_ident).first()
        if not client:
            raise CommandError(f"Tenant '{tenant_ident}' not found.")

        settings_dict = {}

        if options["stage"]:
            st = Stage.objects.filter(client=client, name__iexact=options["stage"], deleted_at__isnull=True).first()
            if not st:
                st = Stage.objects.filter(client=client, id=options["stage"]).first()
            if st:
                settings_dict["default_stage_id"] = str(st.id)
                self.stdout.write(f"Default stage mapped to: {st.name} ({st.id})")
            else:
                self.stdout.write(self.style.WARNING(f"Stage '{options['stage']}' not found. Using system default."))

        if options["owner"]:
            u = User.objects.filter(client=client, email__iexact=options["owner"], deleted_at__isnull=True).first()
            if not u:
                u = User.objects.filter(client=client, id=options["owner"]).first()
            if u:
                settings_dict["default_owner_id"] = str(u.id)
                self.stdout.write(f"Default owner mapped to: {u.name} ({u.id})")
            else:
                self.stdout.write(self.style.WARNING(f"Owner '{options['owner']}' not found."))

        if options["forms"]:
            forms_list = [f.strip() for f in options["forms"].split(",") if f.strip()]
            settings_dict["form_ids"] = forms_list
            self.stdout.write(f"Configured {len(forms_list)} form(s) for backfill.")

        encrypted = encrypt_credentials({"access_token": token, "page_access_token": token})

        integration, created = LeadSourceIntegration.objects.update_or_create(
            client=client,
            provider="meta",
            external_account_id=page_id,
            defaults={
                "name": label,
                "encrypted_credentials": encrypted,
                "settings": settings_dict,
                "is_active": True,
            },
        )

        action = "Created" if created else "Updated"
        self.stdout.write(
            self.style.SUCCESS(
                f"{action} Meta Lead Ads integration for tenant '{client.name}' (Page ID: {page_id}, ID: {integration.id})"
            )
        )
