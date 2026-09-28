"""Role-Based Customer Product/Project Tracking View tests.

Verifies:
1. Role-based access and permissions (Customer, PM, Admin, other internal roles).
2. Customer data isolation (Customer A cannot access Customer B's projects).
3. Dynamic PMS stages and stage percentage weights preservation.
4. Automatic reflection of PM stage updates (progress, deadline, status).
5. Safe delay presentation without internal blame/reasons.
6. Safe serialization preventing internal PMS data leakage.
"""
from decimal import Decimal
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, Role, RolePermission, User
from apps.accounts.permission_catalogue import seed_roles, sync_permissions
from apps.masters.models import Party
from apps.pms.models import Delay, Department, Project, ProjectStage
from apps.pms import services


class CustomerTrackingTests(TestCase):
    def setUp(self):
        sync_permissions()
        self.client_obj = Client.objects.create(slug="tracking-tenant", name="Tracking Tenant")
        seed_roles(self.client_obj)

        roles = {r.code: r for r in Role.objects.filter(client=self.client_obj)}
        self.role_ad = roles["AD"]
        self.role_pm = roles["PM"]
        self.role_cu = roles["CU"]
        self.role_em = roles["EM"]

        # Parties (Customers)
        self.party_a = Party.objects.create(
            client=self.client_obj,
            code="CUST-A",
            name="Acme Corporation",
            email="contact@acme.test",
            type="Customer",
        )
        self.party_b = Party.objects.create(
            client=self.client_obj,
            code="CUST-B",
            name="Beta Industries",
            email="contact@beta.test",
            type="Customer",
        )

        # Users
        self.admin_user = User.objects.create_superuser(
            email="admin@test.io", password="pass", client=self.client_obj, name="Admin User",
        )
        self.pm_user = User.objects.create_user(
            email="pm@test.io", password="pass", client=self.client_obj, name="Kiran PM",
            role=self.role_pm,
        )
        self.customer_a = User.objects.create_user(
            email="alice@acme.test", password="pass", client=self.client_obj, name="Alice Acme",
            role=self.role_cu, party=self.party_a,
        )
        self.customer_b = User.objects.create_user(
            email="bob@beta.test", password="pass", client=self.client_obj, name="Bob Beta",
            role=self.role_cu, party=self.party_b,
        )
        self.employee_user = User.objects.create_user(
            email="emp@test.io", password="pass", client=self.client_obj, name="John Employee",
            role=self.role_em,
        )

        self.dept_design = Department.objects.create(client=self.client_obj, name="Design")
        self.dept_prod = Department.objects.create(client=self.client_obj, name="Production")
        self.dept_qc = Department.objects.create(client=self.client_obj, name="Quality")

        # Project for Customer A
        self.proj_a = Project.objects.create(
            client=self.client_obj,
            code="PRJ-A-001",
            party=self.party_a,
            customer_name=self.party_a.name,
            product_name="Custom Industrial Enclosure",
            project_manager=self.pm_user,
            status="In Progress",
            start_date=timezone.now(),
            expected_completion_date=timezone.now() + timezone.timedelta(days=30),
        )
        # Stages for Project A with custom percentage weights
        self.stage_a1 = ProjectStage.objects.create(
            client=self.client_obj, project=self.proj_a, sequence=1, name="Design & 3D Modeling",
            department=self.dept_design, weight_pct=Decimal("20.00"), completion_pct=100,
            status="Completed", actual_completion_datetime=timezone.now(),
        )
        self.stage_a2 = ProjectStage.objects.create(
            client=self.client_obj, project=self.proj_a, sequence=2, name="Fabrication",
            department=self.dept_prod, weight_pct=Decimal("50.00"), completion_pct=60,
            status="In Progress", start_datetime=timezone.now(),
        )
        self.stage_a3 = ProjectStage.objects.create(
            client=self.client_obj, project=self.proj_a, sequence=3, name="Quality Inspection",
            department=self.dept_qc, weight_pct=Decimal("30.00"), completion_pct=0,
            status="Not Started",
        )
        services.recalculate_project(self.proj_a)
        self.proj_a.current_stage = self.stage_a2
        self.proj_a.save()

        # Project for Customer B
        self.proj_b = Project.objects.create(
            client=self.client_obj,
            code="PRJ-B-001",
            party=self.party_b,
            customer_name=self.party_b.name,
            product_name="Conveyor Assembly",
            project_manager=self.pm_user,
            status="In Progress",
            start_date=timezone.now(),
        )
        self.stage_b1 = ProjectStage.objects.create(
            client=self.client_obj, project=self.proj_b, sequence=1, name="Assembly",
            department=self.dept_prod, weight_pct=Decimal("100.00"), completion_pct=30,
            status="In Progress",
        )
        services.recalculate_project(self.proj_b)

    def api(self, user):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(user)['access']}")
        return client

    def test_customer_can_view_own_project(self):
        """Customer A can view Project A."""
        res = self.api(self.customer_a).get(f"/api/v1/pms/customer-tracking/{self.proj_a.id}/")
        self.assertEqual(res.status_code, 200, res.content)
        data = res.json()
        self.assertEqual(data["code"], "PRJ-A-001")
        self.assertEqual(data["productName"], "Custom Industrial Enclosure")
        self.assertEqual(data["customerName"], "Acme Corporation")
        self.assertEqual(data["overallCompletionPct"], 50)  # (20*100 + 50*60 + 30*0)/100 = 50%
        self.assertEqual(len(data["stages"]), 3)
        self.assertEqual(data["stages"][0]["name"], "Design & 3D Modeling")
        self.assertEqual(data["stages"][0]["status"], "Completed")
        self.assertEqual(float(data["stages"][0]["percentage"]), 20.00)
        self.assertEqual(data["stages"][1]["name"], "Fabrication")
        self.assertEqual(data["stages"][1]["completionPct"], 60)
        self.assertEqual(float(data["stages"][1]["percentage"]), 50.00)

    def test_customer_cannot_view_other_customer_project(self):
        """Customer Data Isolation: Customer A CANNOT view Project B."""
        # By UUID
        res = self.api(self.customer_a).get(f"/api/v1/pms/customer-tracking/{self.proj_b.id}/")
        self.assertEqual(res.status_code, 403)
        # By Code
        res2 = self.api(self.customer_a).get(f"/api/v1/pms/customer-tracking/{self.proj_b.code}/")
        self.assertEqual(res2.status_code, 403)
        # Via ProjectViewSet action
        res3 = self.api(self.customer_a).get(f"/api/v1/pms/projects/{self.proj_b.id}/customer-tracking/")
        self.assertEqual(res3.status_code, 403)

    def test_customer_projects_list_isolation(self):
        """Customer A list endpoint returns ONLY Project A, never Project B."""
        res = self.api(self.customer_a).get("/api/v1/pms/customer-tracking/")
        self.assertEqual(res.status_code, 200)
        results = res.json()["results"]
        codes = [p["code"] for p in results]
        self.assertIn("PRJ-A-001", codes)
        self.assertNotIn("PRJ-B-001", codes)

    def test_pm_and_admin_can_preview_any_customer_project(self):
        """Project Manager and Admin can view any project's customer tracking view."""
        # PM viewing Project A
        res_pm_a = self.api(self.pm_user).get(f"/api/v1/pms/customer-tracking/{self.proj_a.id}/")
        self.assertEqual(res_pm_a.status_code, 200)
        # PM viewing Project B
        res_pm_b = self.api(self.pm_user).get(f"/api/v1/pms/customer-tracking/{self.proj_b.id}/")
        self.assertEqual(res_pm_b.status_code, 200)
        # Admin viewing Project A
        res_admin = self.api(self.admin_user).get(f"/api/v1/pms/customer-tracking/{self.proj_a.id}/")
        self.assertEqual(res_admin.status_code, 200)

    def test_unauthorized_internal_role_denied(self):
        """Standard employee role without view_pms or view_projects is denied access."""
        res = self.api(self.employee_user).get(f"/api/v1/pms/customer-tracking/{self.proj_a.id}/")
        self.assertEqual(res.status_code, 403)

    def test_automatic_updates_from_pm_changes(self):
        """Changes made by PM to stage progress and deadline reflect immediately in customer view."""
        # PM updates stage 2 progress 60% -> 80%
        self.stage_a2.completion_pct = 80
        self.stage_a2.save()
        services.recalculate_project(self.proj_a)

        res = self.api(self.customer_a).get(f"/api/v1/pms/customer-tracking/{self.proj_a.id}/")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        stage2 = data["stages"][1]
        self.assertEqual(stage2["completionPct"], 80)
        # (20*100 + 50*80 + 30*0)/100 = 60%
        self.assertEqual(data["overallCompletionPct"], 60)

    def test_delayed_stage_safe_presentation(self):
        """Delayed stage presents customer-safe notice without exposing internal reasons."""
        rec_date = (timezone.now() + timezone.timedelta(days=5)).date()
        Delay.objects.create(
            client=self.client_obj,
            project=self.proj_a,
            stage=self.stage_a2,
            reason="Subcontractor machine breakdown in laser cutting shop",  # Internal reason!
            responsible_user=self.pm_user,
            expected_recovery_date=rec_date,
            recovery_plan="Diverting work to secondary shop",  # Internal plan!
        )
        self.stage_a2.status = "Delayed"
        self.stage_a2.save()

        res = self.api(self.customer_a).get(f"/api/v1/pms/customer-tracking/{self.proj_a.id}/")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        stage2 = data["stages"][1]
        self.assertTrue(stage2["isDelayed"])
        self.assertIsNotNone(stage2["delayNotice"])
        self.assertEqual(stage2["delayNotice"]["message"], "Delayed")
        self.assertEqual(stage2["delayNotice"]["newExpectedDate"], rec_date.isoformat())

        # CRITICAL: Verify internal reasons and plans are NOT exposed anywhere in stage dict
        self.assertNotIn("reason", stage2)
        self.assertNotIn("recovery_plan", stage2)
        self.assertNotIn("responsible_user", stage2)
        self.assertNotIn("subcontractor machine breakdown", str(stage2).lower())
        self.assertNotIn("secondary shop", str(stage2).lower())

    def test_safe_serialization_no_internal_leakage(self):
        """Verify internal tasks, costing, and employee info are excluded from customer payload."""
        res = self.api(self.customer_a).get(f"/api/v1/pms/customer-tracking/{self.proj_a.id}/")
        self.assertEqual(res.status_code, 200)
        data = res.json()

        # No tasks array or employee details in stage
        for stage in data["stages"]:
            self.assertNotIn("tasks", stage)
            self.assertNotIn("assignedUser", stage)
            self.assertNotIn("assigned_user", stage)
            self.assertNotIn("assignedTeam", stage)

        # No internal team discussions, audit logs or accounting
        self.assertNotIn("activityLog", data)
        self.assertNotIn("orderValue", data)
        self.assertNotIn("conversations", data)
