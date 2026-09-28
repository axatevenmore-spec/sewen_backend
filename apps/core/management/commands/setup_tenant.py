"""
Provision a tenant with its administrator and configuration -- no sample data.

    python manage.py setup_tenant --tenant acme --name "Acme Pvt Ltd" \
        --email admin@acme.in --password '...'

Re-running is safe: every step is idempotent and never overwrites. ``--reset``
first deletes the tenant's business data (documents, masters, CRM, PMS, HRMS
records, files) and keeps its users, roles and configuration.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.core.tenant_setup import bootstrap_configuration, clear_business_data


class Command(BaseCommand):
    help = "Create or refresh a tenant with its administrator and default configuration."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", required=True, help="Tenant slug.")
        parser.add_argument("--name", help="Tenant name (required when creating).")
        parser.add_argument("--email", help="Administrator email (required when creating).")
        parser.add_argument("--password", help="Administrator password for a new administrator.")
        parser.add_argument("--currency", default="INR", help="Tenant currency for a new tenant.")
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete the tenant's business data first; users, roles and settings stay.",
        )

    def handle(self, *args, **options):
        from apps.accounts.models import Client

        slug = options["tenant"]
        client = Client.objects.filter(slug=slug).first()
        if client is None:
            if not options["name"] or not options["email"] or not options["password"]:
                raise CommandError("A new tenant needs --name, --email and --password.")
            client = Client.objects.create(
                slug=slug,
                name=options["name"],
                plan="Enterprise",
                currency=options["currency"],
                fy_start_month=4,
                onboarded_on=timezone.localdate(),
            )
            self.stdout.write(self.style.SUCCESS(f"Created tenant {client.name}"))
        else:
            self.stdout.write(f"Using tenant {client.name}")

        with tenant_context(client.id, push_to_db=False):
            if options["reset"]:
                deleted = clear_business_data(client)
                self.stdout.write(f"  cleared {sum(deleted.values())} business row(s)")

            with transaction.atomic():
                self._seed_access(client, options)
                bootstrap_configuration(client)

        self.stdout.write(self.style.SUCCESS("Tenant ready."))

    def _seed_access(self, client, options):
        from apps.accounts.models import Role, User
        from apps.accounts.permission_catalogue import seed_roles, sync_permissions

        created, updated = sync_permissions()
        self.stdout.write(f"  permissions: +{created} ~{updated}")
        seed_roles(client)

        if not options["email"]:
            return
        admin, was_created = User.objects.get_or_create(
            client=client,
            email=options["email"].lower(),
            defaults={
                "name": "Administrator",
                "role": Role.objects.filter(client=client, code="AD").first(),
                "status": "Active",
                "joined_date": timezone.localdate(),
                "department": "Administration",
            },
        )
        if was_created:
            if not options["password"]:
                raise CommandError("A new administrator needs --password.")
            admin.set_password(options["password"])
            admin.save()
            self.stdout.write(f"  administrator: {admin.email}")
