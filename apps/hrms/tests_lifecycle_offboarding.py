"""Tests for Employee Lifecycle integration: Terminations, Resignations, and Offboarding."""
from datetime import date, timedelta
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, Role, User
from apps.accounts.permission_catalogue import seed_roles, sync_permissions
from apps.core.models import AuditLog
from apps.hrms.models import (
    Asset,
    AssetAssignment,
    Department,
    Designation,
    Employee,
    LeaveType,
    Resignation,
    ResignationChecklistItem,
    Team,
    Termination,
)
from apps.hrms.lifecycle_service import (
    cancel_exit_process,
    execute_employee_offboarding,
    initialize_clearance_checklist,
    process_due_exits,
)
from apps.hrms.services import mark_attendance


class EmployeeLifecycleOffboardingTests(TestCase):
    def setUp(self):
        sync_permissions()
        self.tenant = Client.objects.create(slug="lifecycle-corp", name="Lifecycle Corp")
        seed_roles(self.tenant)
        self.admin = User.objects.create_user(
            email="hradmin@lifecycle.test",
            password="pass-12345",
            client=self.tenant,
            name="HR Admin",
            role=Role.objects.get(client=self.tenant, code="AD"),
        )
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.admin)['access']}")

        self.dept = Department.objects.create(client=self.tenant, name="Engineering")
        self.desig = Designation.objects.create(client=self.tenant, name="Software Engineer")

    def create_employee(self, name, email, status="Active"):
        emp = Employee.objects.create(
            client=self.tenant,
            employee_code=f"EMP-{name.upper()}",
            name=name,
            email=email,
            joining_date="2026-01-01",
            status=status,
            department=self.dept,
            designation=self.desig,
        )
        user = User.objects.create_user(
            email=email,
            password="pass-user-123",
            client=self.tenant,
            name=name,
            role=Role.objects.get(client=self.tenant, code="EM"),
            employee=emp,
        )
        return emp, user

    def test_future_dated_resignation_sets_notice_period(self):
        """Future-dated resignation moves employee to Notice Period without terminating prematurely."""
        emp, user = self.create_employee("Alice", "alice@lifecycle.test")
        future_date = date.today() + timedelta(days=30)

        resig = Resignation.objects.create(
            client=self.tenant,
            employee=emp,
            submitted_on=date.today(),
            last_working_day=future_date,
            reason="Better opportunity",
            status="Approved",
        )

        exit_inst, is_completed, msg = execute_employee_offboarding(resig, user=self.admin)
        self.assertFalse(is_completed)
        self.assertIn("Notice Period", msg)

        emp.refresh_from_db()
        user.refresh_from_db()
        self.assertEqual(emp.status, "Notice Period")
        self.assertTrue(user.is_active)
        self.assertEqual(user.status, "Active")

        # Verify clearance checklist was initialized
        checklist = ResignationChecklistItem.objects.filter(resignation=resig)
        self.assertEqual(checklist.count(), 5)

    def test_effective_date_termination_offboarding(self):
        """On or past effective last working day with clearance, employee is fully offboarded."""
        emp, user = self.create_employee("Bob", "bob@lifecycle.test")
        today = date.today()

        # Add Bob to a team
        team = Team.objects.create(
            client=self.tenant,
            code="ENG-ALPHA",
            name="Alpha Squad",
            department=self.dept,
        )
        team.members.add(emp)
        team.headcount = team.members.count()
        team.save()
        self.assertEqual(team.headcount, 1)

        # Assign an asset to Bob
        asset = Asset.objects.create(
            client=self.tenant,
            asset_code="AST-MAC-01",
            name="MacBook Pro",
            assigned_employee=emp,
            status="Assigned",
        )
        assignment = AssetAssignment.objects.create(
            client=self.tenant,
            asset=asset,
            employee=emp,
            assigned_by=self.admin,
        )

        term = Termination.objects.create(
            client=self.tenant,
            employee=emp,
            notice_date=today - timedelta(days=15),
            last_working_day=today,
            termination_type="Involuntary",
            reason="Performance",
            status="Approved",
        )

        # Initialize and complete clearance checklist
        initialize_clearance_checklist(term)
        ResignationChecklistItem.objects.filter(termination=term).update(
            is_completed=True, completed_at=timezone.now()
        )

        # Execute offboarding
        exit_inst, is_completed, msg = execute_employee_offboarding(term, user=self.admin)
        self.assertTrue(is_completed)

        emp.refresh_from_db()
        user.refresh_from_db()
        team.refresh_from_db()
        term.refresh_from_db()

        # Employee status updated to Terminated
        self.assertEqual(emp.status, "Terminated")
        # User login deactivated
        self.assertFalse(user.is_active)
        self.assertEqual(user.status, "Inactive")
        # Team membership removed and headcount updated
        self.assertNotIn(emp, team.members.all())
        self.assertEqual(team.headcount, 0)
        # Termination status updated
        self.assertEqual(term.status, "Completed")
        # Outstanding asset remains intact (not deleted, not falsely completed)
        asset.refresh_from_db()
        assignment.refresh_from_db()
        self.assertEqual(asset.status, "Assigned")
        self.assertIsNone(assignment.returned_at)

        # Audit log verification
        audit = AuditLog.objects.filter(client=self.tenant, action="execute_offboarding").first()
        self.assertIsNotNone(audit)
        self.assertEqual(str(audit.entity_id), str(emp.id))

    def test_idempotent_offboarding(self):
        """Calling execute_employee_offboarding multiple times does not duplicate or fail."""
        emp, user = self.create_employee("Charlie", "charlie@lifecycle.test")
        resig = Resignation.objects.create(
            client=self.tenant,
            employee=emp,
            submitted_on=date.today() - timedelta(days=30),
            last_working_day=date.today(),
            reason="Relocation",
            status="Relieved & Completed",
        )
        emp.status = "Resigned"
        emp.save()

        # Call offboarding again
        exit_inst, is_completed, msg = execute_employee_offboarding(resig, user=self.admin)
        self.assertFalse(is_completed)
        self.assertIn("Already offboarded", msg)

    def test_post_exit_attendance_guardrail(self):
        """Attendance punches cannot be registered after employee's effective exit date."""
        emp, user = self.create_employee("Dana", "dana@lifecycle.test", status="Terminated")
        term = Termination.objects.create(
            client=self.tenant,
            employee=emp,
            notice_date=date.today() - timedelta(days=10),
            last_working_day=date.today() - timedelta(days=2),
            termination_type="Mutual",
            reason="Contract End",
            status="Completed",
        )

        with self.assertRaises(Exception) as ctx:
            mark_attendance(
                client=self.tenant,
                employee=emp,
                work_date=date.today(),
                check_in="09:00:00",
            )
        self.assertIn("employee is terminated", str(ctx.exception))
        self.assertEqual(getattr(ctx.exception, "code", None), "EMPLOYEE_EXITED")

    def test_post_exit_leave_guardrail(self):
        """Leave applications cannot extend past effective exit date."""
        emp, user = self.create_employee("Eve", "eve@lifecycle.test")
        term = Termination.objects.create(
            client=self.tenant,
            employee=emp,
            notice_date=date.today(),
            last_working_day=date.today() + timedelta(days=5),
            termination_type="Resignation",
            reason="Moving abroad",
            status="Approved",
        )

        leave_type = LeaveType.objects.create(
            client=self.tenant,
            name="Casual Leave",
            code="CL",
            annual_entitlement=12,
        )

        # Login as Eve
        eve_client = APIClient()
        eve_client.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(user)['access']}")

        # Attempt to apply for leave spanning past exit date
        resp = eve_client.post(
            "/api/v1/hrms/leave/",
            {
                "employeeId": str(emp.id),
                "leaveTypeId": str(leave_type.id),
                "fromDate": str(date.today() + timedelta(days=4)),
                "toDate": str(date.today() + timedelta(days=8)),
                "days": 4,
                "reason": "Vacation before leaving",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 409)
        self.assertIn("EMPLOYEE_EXITED", str(resp.content))

    def test_cancel_exit_process_restores_active_status(self):
        """Cancelling a pending exit restores employee from Notice Period back to Active."""
        emp, user = self.create_employee("Frank", "frank@lifecycle.test", status="Notice Period")
        resig = Resignation.objects.create(
            client=self.tenant,
            employee=emp,
            submitted_on=date.today(),
            last_working_day=date.today() + timedelta(days=20),
            reason="Personal",
            status="Under Review",
        )

        cancel_exit_process(resig, user=self.admin, reason="Retention offer accepted")

        emp.refresh_from_db()
        resig.refresh_from_db()
        self.assertEqual(emp.status, "Active")
        self.assertEqual(resig.status, "Cancelled")

        # Verify audit log recorded
        audit = AuditLog.objects.filter(client=self.tenant, action="cancel_exit").first()
        self.assertIsNotNone(audit)
        self.assertEqual(str(audit.entity_id), str(emp.id))

    def test_scheduled_process_due_exits(self):
        """Scheduled batch runner executes offboarding on exits due today."""
        emp, user = self.create_employee("Grace", "grace@lifecycle.test", status="Notice Period")
        resig = Resignation.objects.create(
            client=self.tenant,
            employee=emp,
            submitted_on=date.today() - timedelta(days=30),
            last_working_day=date.today(),
            reason="Higher Studies",
            status="Approved",
        )
        # Initialize and complete checklist
        initialize_clearance_checklist(resig)
        ResignationChecklistItem.objects.filter(resignation=resig).update(
            is_completed=True, completed_at=timezone.now()
        )

        results = process_due_exits(client_id=str(self.tenant.id))
        self.assertEqual(results["processed"], 1)
        self.assertEqual(results["errors"], 0)

        emp.refresh_from_db()
        resig.refresh_from_db()
        user.refresh_from_db()
        self.assertEqual(emp.status, "Resigned")
        self.assertEqual(resig.status, "Relieved & Completed")
        self.assertFalse(user.is_active)

    def test_employee_directory_default_exclusion_and_historical_filters(self):
        """Active employee listing excludes exited employees by default, but allows historical queries."""
        emp_active, _ = self.create_employee("Henry", "henry@lifecycle.test", status="Active")
        emp_resigned, _ = self.create_employee("Ian", "ian@lifecycle.test", status="Resigned")
        emp_terminated, _ = self.create_employee("Jack", "jack@lifecycle.test", status="Terminated")

        # Default GET /employees/
        resp = self.api.get("/api/v1/hrms/employees/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        names = [e["name"] for e in (data["results"] if isinstance(data, dict) and "results" in data else data)]
        self.assertIn("Henry", names)
        self.assertNotIn("Ian", names)
        self.assertNotIn("Jack", names)

        # Explicit filter ?status=Resigned
        resp_resigned = self.api.get("/api/v1/hrms/employees/?status=Resigned")
        self.assertEqual(resp_resigned.status_code, 200)
        data_res = resp_resigned.json()
        resigned_names = [e["name"] for e in (data_res["results"] if isinstance(data_res, dict) and "results" in data_res else data_res)]
        self.assertIn("Ian", resigned_names)
        self.assertNotIn("Henry", resigned_names)
        self.assertNotIn("Jack", resigned_names)

        # Historical query ?status=All
        resp_all = self.api.get("/api/v1/hrms/employees/?status=All")
        self.assertEqual(resp_all.status_code, 200)
        data_all = resp_all.json()
        all_names = [e["name"] for e in (data_all["results"] if isinstance(data_all, dict) and "results" in data_all else data_all)]
        self.assertIn("Henry", all_names)
        self.assertIn("Ian", all_names)
        self.assertIn("Jack", all_names)
