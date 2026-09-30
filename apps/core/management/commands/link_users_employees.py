"""
Link existing Administration users to the HRMS employees with the same email.

    python manage.py link_users_employees --dry-run
    python manage.py link_users_employees [--tenant acme]

Records made before the link was kept in step (apps/hrms/user_link.py) may be
one person held twice. A pair is linked only when it is unambiguous: a live
staff login with no employee, and exactly one live employee with that email
that no other login holds. On linking, HR's values win and the login fills
what HR left blank -- the same rule as a link made from either screen.

It never creates records: a login with no matching employee (or the reverse)
is listed, for someone to decide.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.core.tenancy import tenant_context


class Command(BaseCommand):
    help = "Link Administration users to HRMS employees with the same email."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", help="Only this tenant slug (default: every tenant).")
        parser.add_argument(
            "--dry-run", action="store_true", help="Report what would be linked; change nothing."
        )

    def handle(self, *args, **options):
        from apps.accounts.models import Client

        clients = Client.objects.all().order_by("slug")
        if options["tenant"]:
            clients = clients.filter(slug=options["tenant"])
            if not clients.exists():
                raise CommandError(f"No tenant '{options['tenant']}'.")

        dry = options["dry_run"]
        for client in clients:
            with tenant_context(client.id, push_to_db=False):
                self._link_tenant(client, dry)
        if dry:
            self.stdout.write(self.style.WARNING("Dry run: nothing was changed."))

    def _link_tenant(self, client, dry):
        from apps.accounts.models import User
        from apps.hrms import user_link
        from apps.hrms.models import Employee

        users = (
            User.objects.filter(client=client, deleted_at__isnull=True)
            .exclude(status="Deleted")
            .select_related("role", "employee")
        )
        employees = list(Employee.objects.filter(client=client, deleted_at__isnull=True))
        by_email = {}
        for employee in employees:
            if employee.email:
                by_email.setdefault(employee.email.strip().lower(), []).append(employee)

        held = {u.employee_id for u in users if user_link.linked_employee(u)}
        linked, unmatched = [], []
        for user in users:
            if user_link.linked_employee(user) or user.is_customer:
                continue
            matches = [e for e in by_email.get(user.email.lower(), []) if e.pk not in held]
            if len(matches) != 1:
                unmatched.append((user, "ambiguous" if matches else "no employee"))
                continue
            employee = matches[0]
            held.add(employee.pk)
            linked.append((user, employee))
            if not dry:
                with transaction.atomic():
                    user.employee = employee
                    user.save(update_fields=["employee", "updated_at"])
                    user_link.reconcile(user, employee)

        no_login = [e for e in employees if e.pk not in held]
        self.stdout.write(self.style.MIGRATE_HEADING(f"{client.slug} ({client.name})"))
        for user, employee in linked:
            self.stdout.write(f"  link   {user.email} -> {employee.employee_code} {employee.name}")
        for user, why in unmatched:
            self.stdout.write(f"  skip   {user.email}: {why}")
        for employee in no_login:
            self.stdout.write(f"  alone  {employee.employee_code} {employee.name} <{employee.email or '-'}>: no login")
        self.stdout.write(
            f"  {len(linked)} linked, {len(unmatched)} logins without an employee, "
            f"{len(no_login)} employees without a login"
        )
