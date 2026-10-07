"""
Find logins still on the old built-in default password and disable them.

    python manage.py revoke_default_passwords --dry-run
    python manage.py revoke_default_passwords [--tenant acme] [--notify]

Accounts created without a password used to get ``Password@123`` (or an empty
password). Anyone who knew the address could sign in as them. This sets an
unusable password on every such login and signs out its sessions; the owner
then sets a new one through "Forgot password" on the sign-in page. ``--notify``
also emails each owner the activation link (apps/accounts/invites.py).

Checking a password hashes it, so this takes a moment per user.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.core.tenancy import tenant_context

LEGACY_DEFAULTS = ("Password@123", "")


class Command(BaseCommand):
    help = "Disable logins that still use the old built-in default password."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", help="Only this tenant slug (default: every tenant).")
        parser.add_argument(
            "--dry-run", action="store_true", help="List the affected logins; change nothing."
        )
        parser.add_argument(
            "--notify", action="store_true", help="Email each affected user the activation link."
        )

    def handle(self, *args, **options):
        from apps.accounts.models import Client

        clients = Client.objects.all().order_by("slug")
        if options["tenant"]:
            clients = clients.filter(slug=options["tenant"])
            if not clients.exists():
                raise CommandError(f"No tenant '{options['tenant']}'.")

        dry = options["dry_run"]
        total = 0
        for client in clients:
            with tenant_context(client.id, push_to_db=False):
                total += self._revoke_tenant(client, dry, options["notify"])

        verb = "would be disabled" if dry else "disabled"
        self.stdout.write(f"{total} login(s) {verb}.")
        if dry:
            self.stdout.write(self.style.WARNING("Dry run: nothing was changed."))

    def _revoke_tenant(self, client, dry, notify):
        from apps.accounts.invites import send_invite_on_commit
        from apps.accounts.models import User, UserSession

        count = 0
        users = User.objects.filter(client=client, deleted_at__isnull=True).exclude(status="Deleted")
        for user in users:
            if not user.has_usable_password():
                continue
            if not any(user.check_password(p) for p in LEGACY_DEFAULTS):
                continue
            count += 1
            self.stdout.write(f"  {client.slug}: {user.email}")
            if dry:
                continue
            with transaction.atomic():
                user.set_unusable_password()
                user.save(update_fields=["password"])
                UserSession.objects.filter(user=user, revoked_at__isnull=True).update(
                    revoked_at=timezone.now(), revoked_reason="default_password_revoked"
                )
                if notify:
                    send_invite_on_commit(user)
        return count
