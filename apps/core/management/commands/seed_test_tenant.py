"""
Build a separate TEST tenant full of sample data, for trying the app end to end.

    python manage.py seed_test_tenant                  # creates "sweven-test"
    python manage.py seed_test_tenant --reset          # wipe it and build again
    python manage.py seed_test_tenant --password "..." # choose the password

Real tenants stay empty by design (no demo data); this never touches them:

- it only writes to a tenant whose slug ends in ``-test``, and only to one it
  created itself (marked with the ``test_tenant`` setting);
- its logins are on their own domain (``admin@sweven-test.example`` ...), because
  login refuses an address that exists in two tenants -- reusing the real
  addresses would lock the real users out;
- the password is generated unless given, and printed once at the end.

The base rows (company, roles, people, items, parties, stock, leads, quotations,
orders, purchase orders, projects, bank accounts) come from ``seed_project_data``.
On top of that, documents are pushed through the same services and endpoints a
user would trigger, so stock, ledger, numbering and statuses come out real:
invoices with part payments, purchase bills with goods received and paid, leave,
punch-based attendance, a payroll run, and PMS tasks / team / timesheets / chat.
"""
import secrets
import string
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Client, User, UserSession
from apps.core.tenancy import tenant_context

TEST_FLAG = "test_tenant"
#: The test logins' password, kept in the TEST tenant only, for the login
#: page's test-account picker (``TEST_LOGIN_PICKER``).
TEST_PASSWORD_KEY = "test_tenant_password"


def _generate_password():
    alphabet = string.ascii_letters + string.digits
    core = "".join(secrets.choice(alphabet) for _ in range(10))
    return f"Test-{core}9!"


class Command(BaseCommand):
    help = "Create (or --reset) a separate TEST tenant filled with sample data across every module."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", default="sweven-test", help="Slug; must end in '-test'.")
        parser.add_argument("--password", default=None, help="Password for every test login (default: generated).")
        parser.add_argument("--reset", action="store_true", help="Wipe the test tenant and build it again.")

    # ------------------------------------------------------------------ main
    def handle(self, *args, **options):
        slug = options["tenant"].strip().lower()
        if not slug.endswith("-test"):
            raise CommandError("Refusing: the test tenant's slug must end in '-test'.")
        password = options["password"] or _generate_password()
        domain = f"{slug}.example"

        client = Client.objects.filter(slug=slug).first()
        if client is not None:
            if not self._is_test_tenant(client):
                raise CommandError(
                    f"Refusing: tenant '{slug}' exists and was not created by this command."
                )
            if not options["reset"]:
                raise CommandError(
                    f"Tenant '{slug}' already has test data. Run with --reset to rebuild it."
                )
            self._wipe(client)
        else:
            client = Client.objects.create(
                slug=slug,
                name="Sweven Fabricators (TEST)",
                plan="Enterprise",
                currency="INR",
                fy_start_month=4,
                contact_name="Test Tenant",
                contact_email=f"admin@{domain}",
                industry="Metal Fabrication & Machinery",
                onboarded_on=timezone.localdate(),
            )
        self._mark_test_tenant(client, password)

        self.stdout.write(self.style.MIGRATE_HEADING(f"Base data -> {slug}"))
        call_command(
            "seed_project_data", tenant=slug, email_domain=domain, password=password,
            stdout=self.stdout,
        )

        self.client_obj = client
        self.domain = domain
        self.problems = []
        with tenant_context(client.id, push_to_db=False):
            self.admin = User.objects.get(client=client, email=f"admin@{domain}")
            self.api = self._api_client(self.admin)
            try:
                self.stdout.write(self.style.MIGRATE_HEADING("Extra test data"))
                self._section("Sales invoices & payments", self._sales)
                self._section("Purchase bills, goods receipt & payments", self._purchase)
                self._section("HR: leave types, salary structure", self._hr_setup)
                self._section("HR: attendance from punches", self._attendance)
                self._section("HR: leave requests", self._leave)
                self._section("HR: payroll run", self._payroll)
                self._section("PMS: tasks", self._pms_tasks)
                self._section("PMS: project teams", self._pms_team)
                self._section("PMS: timesheets", self._pms_timesheets)
                self._section("PMS: chat", self._pms_chat)
            finally:
                UserSession.objects.filter(pk=self.session.pk).delete()

        self._summary(slug, password)

    # ---------------------------------------------------------------- safety
    def _is_test_tenant(self, client):
        from apps.core.models import Setting

        with tenant_context(client.id, push_to_db=False):
            row = Setting.objects.filter(client=client, key=TEST_FLAG).first()
            return bool(row and row.value)

    def _mark_test_tenant(self, client, password):
        from apps.core.models import Setting

        with tenant_context(client.id, push_to_db=False):
            Setting.objects.update_or_create(
                client=client, key=TEST_FLAG, defaults={"value": True}
            )
            Setting.objects.update_or_create(
                client=client, key=TEST_PASSWORD_KEY, defaults={"value": password}
            )

    @transaction.atomic
    def _wipe(self, client):
        from apps.core.tenant_setup import clear_business_data

        self.stdout.write(f"Wiping test tenant '{client.slug}'...")
        with tenant_context(client.id, push_to_db=False):
            clear_business_data(client)
            UserSession.objects.filter(client=client).delete()
            User.objects.filter(client=client).delete()

    # --------------------------------------------------------------- helpers
    def _section(self, label, fn):
        try:
            with transaction.atomic():
                detail = fn()
            self.stdout.write(f"  [ok] {label}" + (f" -- {detail}" if detail else ""))
        except Exception as exc:  # report and carry on: one stale section must not sink the rest
            self.problems.append((label, exc))
            self.stdout.write(self.style.WARNING(f"  [skipped] {label}: {exc}"))

    def _api_client(self, user):
        from rest_framework.test import APIClient

        from apps.accounts.views import _hash, build_tokens

        self.session = UserSession.objects.create(
            user=user,
            client=user.client,
            refresh_token_hash=_hash(secrets.token_hex(32)),
            user_agent="seed_test_tenant",
            device_label="Test data seeder",
            expires_at=timezone.now() + timedelta(hours=1),
        )
        tokens = build_tokens(user, session=self.session)
        api = APIClient(SERVER_NAME="localhost")
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        return api

    def _post(self, path, data=None):
        response = self.api.post(f"/api/v1{path}", data or {}, format="json")
        if response.status_code >= 400:
            raise RuntimeError(f"POST {path} -> {response.status_code}: {response.content[:300]!r}")
        return response.json()

    def _user(self, local_part):
        return User.objects.get(client=self.client_obj, email=f"{local_part}@{self.domain}")

    def _bank(self):
        from apps.accounting.models import BankAccount

        return BankAccount.objects.filter(client=self.client_obj, deleted_at__isnull=True).first()

    def _at(self, day, hh, mm):
        return timezone.make_aware(datetime.combine(day, time(hh, mm)))

    def _work_days_back(self, count):
        """The last ``count`` working days (Mon-Sat) before today, oldest first."""
        days, day = [], timezone.localdate()
        while len(days) < count:
            day -= timedelta(days=1)
            if day.weekday() != 6:
                days.append(day)
        return list(reversed(days))

    # ----------------------------------------------------------------- sales
    def _sales(self):
        from apps.sales import services as sales
        from apps.sales.models import SalesInvoice, SalesOrder

        from apps.inventory.services import post_movement
        from apps.masters.models import Location

        made = []
        location_id = sales._default_location_id(self.client_obj.id)
        for order in SalesOrder.objects.filter(client=self.client_obj, deleted_at__isnull=True).order_by("doc_date"):
            # The fabricated goods come off the shop floor first: finished stock
            # at the dispatch location, so invoicing can post it out.
            location = Location.objects.get(pk=getattr(order, "location_id", None) or location_id)
            for line in order.line_items.filter(deleted_at__isnull=True, item__isnull=False):
                post_movement(
                    client_id=self.client_obj.id, item=line.item, location=location,
                    type="ADJUSTMENT", quantity=Decimal(line.qty),
                    unit_cost=Decimal(line.rate or 0), movement_date=timezone.localdate() - timedelta(days=3),
                    notes=f"Production output for {order.order_number}", user=self.admin,
                )
            body = self._post(f"/sales/orders/{order.id}/convert-to-invoice/")
            invoice = SalesInvoice.objects.get(pk=body.get("id") or body.get("invoice", {}).get("id"))
            invoice = sales.finalize_invoice(invoice, user=self.admin, override_credit_limit=True)
            made.append(invoice)

        bank = self._bank()
        today = timezone.localdate()
        if made:  # first invoice half paid, the rest left open
            first = made[0]
            sales.record_payment_in(
                client=self.client_obj, party=first.party, amount=round(first.total / 2, 2),
                payment_date=today - timedelta(days=2), mode="Bank", bank_account=bank,
                reference_number="NEFT-TEST-001", invoice=first, user=self.admin,
            )
        return f"{len(made)} invoice(s), 1 part payment"

    # -------------------------------------------------------------- purchase
    def _purchase(self):
        from apps.purchase import services as purchase
        from apps.purchase.models import PurchaseBill, PurchaseOrder

        bank = self._bank()
        bills = []
        for order in PurchaseOrder.objects.filter(client=self.client_obj, deleted_at__isnull=True).order_by("doc_date"):
            body = self._post(f"/purchase/orders/{order.id}/convert-to-bill/")
            bill = PurchaseBill.objects.get(pk=body.get("id") or body.get("bill", {}).get("id"))
            purchase.receive_bill_goods(bill, lines_payload=[], qc_status="Approved", user=self.admin)
            bill.refresh_from_db()
            bills.append(bill)
        if bills:
            first = bills[0]
            purchase.record_payment_out(
                client=self.client_obj, party=first.party, amount=first.total,
                payment_date=timezone.localdate() - timedelta(days=1), mode="Bank",
                bank_account=bank, reference_number="RTGS-TEST-001", bill=first, user=self.admin,
            )
        return f"{len(bills)} bill(s) received, 1 paid"

    # -------------------------------------------------------------------- HR
    def _staff(self):
        """Employees with a working login (not customers)."""
        from apps.hrms.models import Employee

        linked = User.objects.filter(
            client=self.client_obj, employee__isnull=False, deleted_at__isnull=True
        ).values_list("employee_id", flat=True)
        return list(Employee.objects.filter(pk__in=list(linked)).select_related("designation"))

    def _hr_setup(self):
        from apps.hrms.models import LeaveType, SalaryStructure

        for name, code, days, encash, carry in (
            ("Annual Leave", "AL", 18, True, 12),
            ("Sick Leave", "SL", 10, False, None),
            ("Casual Leave", "CL", 7, False, None),
        ):
            LeaveType.objects.update_or_create(
                client=self.client_obj, name=name,
                defaults={
                    "code": code, "annual_entitlement": days, "accrual": "Yearly",
                    "carry_forward_cap": carry, "is_encashable": encash, "is_paid": True,
                },
            )
        structure, _ = SalaryStructure.objects.update_or_create(
            client=self.client_obj, name="Standard Staff",
            defaults={"basic_pct": Decimal("50"), "hra_pct": Decimal("20"), "components": [], "is_active": True},
        )
        pay = {"General Manager": 150000, "Project Manager": 90000, "Sales Manager": 85000,
               "HR Manager": 80000, "Accounts Manager": 80000}
        for employee in self._staff():
            title = employee.designation.name if employee.designation_id else ""
            employee.standard_salary = Decimal(
                pay.get(title, 45000 if "Executive" in title or "Officer" in title else 28000)
            )
            employee.salary_structure = structure
            employee.save(update_fields=["standard_salary", "salary_structure", "updated_at"])
        self._leave_balances()
        return "3 leave types with this year's balances, salary structure on every employee"

    def _leave_balances(self):
        from apps.hrms.models import LeaveBalance, LeaveType

        year = timezone.localdate().year
        types = list(LeaveType.objects.filter(client=self.client_obj, deleted_at__isnull=True))
        for employee in self._staff():
            for leave_type in types:
                LeaveBalance.objects.update_or_create(
                    client=self.client_obj, employee=employee, leave_type=leave_type, period_year=year,
                    defaults={"entitlement": leave_type.annual_entitlement},
                )

    def _attendance(self):
        from apps.hrms import services as hr

        staff = self._staff()
        days = self._work_days_back(10)
        punches = 0
        for index, employee in enumerate(staff):
            user = User.objects.filter(employee=employee).first()
            for d_index, day in enumerate(days):
                if (index + d_index) % 11 == 5:  # an absent day here and there
                    continue
                late = (index + d_index) % 7 == 3
                early = (index + d_index) % 9 == 4
                hr.record_punch(
                    client=self.client_obj, employee=employee, punch_type="IN",
                    punch_time=self._at(day, 10 if late else 9, 12 if late else 20 + (index % 10)),
                    source="web", user=user,
                )
                hr.record_punch(
                    client=self.client_obj, employee=employee, punch_type="OUT",
                    punch_time=self._at(day, 16 if early else 18, 5 if early else 35 + (index % 15)),
                    source="web", user=user,
                    early_reason="Doctor's appointment" if early else None,
                )
                punches += 2
            # Today: most are in already (only if the morning has passed).
            now = timezone.localtime()
            if index % 3 != 2 and now.hour >= 10:
                hr.record_punch(
                    client=self.client_obj, employee=employee, punch_type="IN",
                    punch_time=self._at(now.date(), 9, 25), source="web", user=user,
                )
                punches += 1
        return f"{punches} punches for {len(staff)} employees over {len(days)} days"

    def _leave(self):
        from apps.hrms import services as hr
        from apps.hrms.models import LeaveRequest, LeaveType

        types = {t.code: t for t in LeaveType.objects.filter(client=self.client_obj)}
        hr_user = self._user("hr.manager")
        today = timezone.localdate()
        welder = self._user("employee.welder").employee
        assembly = self._user("employee.assembly").employee
        fabrication = self._user("employee.fabrication").employee

        LeaveRequest.objects.create(
            client=self.client_obj, employee=welder, leave_type=types["SL"],
            from_date=today + timedelta(days=5), to_date=today + timedelta(days=6), days=2,
            reason="Medical check-up", delegate_employee=fabrication,
        )
        approved = LeaveRequest.objects.create(
            client=self.client_obj, employee=assembly, leave_type=types["CL"],
            from_date=today + timedelta(days=9), to_date=today + timedelta(days=9), days=1,
            reason="Family function", delegate_employee=welder,
        )
        hr.approve_leave(approved, user=hr_user, remark="Approved")
        LeaveRequest.objects.create(
            client=self.client_obj, employee=fabrication, leave_type=types["AL"],
            from_date=today + timedelta(days=20), to_date=today + timedelta(days=24), days=5,
            reason="Annual vacation", delegate_employee=assembly,
        )
        return "1 approved, 2 pending (with work handover)"

    def _payroll(self):
        from apps.hrms import services as hr

        last_month = (timezone.localdate().replace(day=1) - timedelta(days=1)).replace(day=1)
        result = hr.process_payroll(
            client=self.client_obj, period_month=last_month, user=self._user("hr.manager")
        )
        slips = list(result["payslips"])
        bank = self._bank()
        for slip in slips[: len(slips) // 2]:
            slip.status = "Approved"  # the approve step, as on the payroll screen
            slip.save(update_fields=["status", "updated_at"])
            hr.mark_payslip_paid(slip, payment_date=timezone.localdate() - timedelta(days=3),
                                 bank_account=bank, user=self.admin)
        return f"{len(slips)} payslips for {last_month:%B %Y}, {len(slips) // 2} paid"

    # ------------------------------------------------------------------- PMS
    def _pms_tasks(self):
        from apps.pms.models import Project, Task

        made = 0
        plans = {
            "Design & Drawing": ["Prepare GA drawing", "Client sign-off on design"],
            "Fabrication": ["Cut MS plates to size", "Weld frame & stiffeners", "Grind & finish welds"],
            "Quality Inspection": ["Dimensional check", "Weld inspection report"],
            "Packaging": ["Crate & label"],
            "Installation": ["Site installation", "Commissioning trial"],
        }
        stage_workers = {
            "Design & Drawing": [self._user("project.manager")],
            "Fabrication": [self._user("employee.fabrication"), self._user("employee.welder")],
            "Quality Inspection": [self._user("project.manager")],
            "Packaging": [self._user("store.executive"), self._user("employee.assembly")],
            "Installation": [self._user("employee.assembly")],
        }
        today = timezone.localdate()
        for project in Project.objects.filter(client=self.client_obj, deleted_at__isnull=True):
            for stage in project.stages.filter(deleted_at__isnull=True).order_by("sequence"):
                # Ensure stage assignee matches the stage role
                workers = stage_workers.get(stage.name, [stage.assigned_user or self._user("project.manager")])
                if stage.assigned_user != workers[0]:
                    stage.assigned_user = workers[0]
                    stage.save(update_fields=["assigned_user"])

                for n, name in enumerate(plans.get(stage.name, [])):
                    started = stage.status not in ("Not Started",)
                    done = stage.status == "Completed" or (started and n == 0 and stage.completion_pct >= 50)
                    Task.objects.create(
                        client=self.client_obj, project=project, stage=stage, task_name=name,
                        assigned_user=workers[n % len(workers)],
                        department=stage.department,
                        start_date=today - timedelta(days=5) if started else today + timedelta(days=7),
                        due_date=today + timedelta(days=2 + n) if started else today + timedelta(days=14),
                        completion_pct=100 if done else (40 if started else 0),
                        status="Completed" if done else ("In Progress" if started else "Not Started"),
                        priority="High" if stage.name in ("Fabrication", "Quality Inspection") else "Medium",
                    )
                    made += 1
        return f"{made} tasks"

    def _pms_team(self):
        from apps.pms import team
        from apps.pms.models import Project

        pm = self._user("project.manager")
        added = 0
        for project in Project.objects.filter(client=self.client_obj, deleted_at__isnull=True):
            production = project.stages.filter(department__name="Production", deleted_at__isnull=True).first()
            for local in ("employee.welder", "employee.assembly"):
                team.add_member(project, pm, str(self._user(local).id),
                                str(production.department_id) if production else None)
                added += 1
            team.add_member(project, pm, str(self._user("store.executive").id), None)
            added += 1
        return f"{added} people added to project teams"

    def _pms_timesheets(self):
        from apps.pms.models import Project, TimesheetEntry
        from apps.pms.views import recount_timesheet, week_timesheet

        project = Project.objects.filter(client=self.client_obj, deleted_at__isnull=True).order_by("code").first()
        made, sheets = 0, set()
        for local, rate in (("employee.welder", 350), ("employee.fabrication", 400), ("project.manager", 900)):
            user = self._user(local)
            for day in self._work_days_back(5):
                sheet = week_timesheet(user, day)
                TimesheetEntry.objects.create(
                    client=self.client_obj, timesheet=sheet, user=user, project=project, date=day,
                    duration_hours=Decimal("7.5") if local != "project.manager" else Decimal("2.0"),
                    hourly_rate=Decimal(rate), is_billable=True,
                    description="Fabrication work" if local != "project.manager" else "Project review",
                )
                sheets.add(sheet.pk)
                made += 1
        from apps.pms.models import Timesheet

        for sheet in Timesheet.objects.filter(pk__in=sheets):
            recount_timesheet(sheet)
        # One week already submitted, waiting for the PM.
        first = Timesheet.objects.filter(pk__in=sheets).order_by("week_start").first()
        if first:
            first.status = "Submitted"
            first.save(update_fields=["status", "updated_at"])
        return f"{made} entries on {len(sheets)} weekly timesheets"

    def _pms_chat(self):
        from apps.pms import chat
        from apps.pms.models import Conversation, Project

        project = Project.objects.filter(client=self.client_obj, deleted_at__isnull=True).order_by("code").first()
        chat.ensure_conversations(project)
        conv_project = Conversation.objects.get(project=project, kind="Project", deleted_at__isnull=True)
        lines = [
            ("project.manager", conv_project, f"Kick-off for {project.code}: please check your tasks on the Fabrication stage."),
            ("employee.welder", conv_project, "Plates are cut, starting the frame welding today."),
            ("employee.fabrication", conv_project, "Grinding station is free from 2 pm."),
        ]
        team_conv = Conversation.objects.filter(
            project=project, kind="Team", department__name="Production", deleted_at__isnull=True
        ).first()
        if team_conv:
            lines.append(("employee.assembly", team_conv, "Need 20 more M16 bolts for the frame, can stores send them?"))
        for local, conversation, text in lines:
            user = self._user(local)
            chat.post_message(chat.ChatScope(project, user), conversation, {"text": text})
        return f"{len(lines)} messages in {project.code}"

    # --------------------------------------------------------------- summary
    def _summary(self, slug, password):
        with tenant_context(self.client_obj.id, push_to_db=False):
            users = list(
                User.objects.filter(client=self.client_obj).select_related("role").order_by("email")
            )
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"Test tenant '{slug}' is ready."))
        self.stdout.write(f"Password for every login below: {password}")
        for user in users:
            self.stdout.write(f"  {user.email:<42} {user.role.name if user.role_id else '-'}")
        if self.problems:
            self.stdout.write(self.style.WARNING(
                f"{len(self.problems)} section(s) were skipped (see above); the rest is in place."
            ))
