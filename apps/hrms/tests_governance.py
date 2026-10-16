"""Tests for HR Admin Setup & Governance: Offer Letters, Grievances, Announcements, and Travel Requisitions."""
from datetime import date, timedelta
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, Role, User
from apps.accounts.permission_catalogue import seed_roles, sync_permissions
from apps.core.models import AuditLog
from apps.hrms.models import (
    Announcement,
    Candidate,
    Complaint,
    Department,
    Designation,
    Employee,
    Job,
    Offer,
    OnboardingTask,
    TravelRequest,
)


class HRGovernanceTests(TestCase):
    def setUp(self):
        sync_permissions()
        self.tenant = Client.objects.create(slug="gov-corp", name="Governance Corp")
        seed_roles(self.tenant)
        self.admin = User.objects.create_user(
            email="hradmin@gov.test",
            password="pass-12345",
            client=self.tenant,
            name="HR Admin",
            role=Role.objects.get(client=self.tenant, code="AD"),
        )
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.admin)['access']}")

        self.dept_eng = Department.objects.create(client=self.tenant, name="Engineering")
        self.dept_sales = Department.objects.create(client=self.tenant, name="Sales")
        self.desig = Designation.objects.create(client=self.tenant, name="Staff Engineer")

    def create_employee_and_user(self, name, email, dept=None, role_code="EM"):
        dept = dept or self.dept_eng
        emp = Employee.objects.create(
            client=self.tenant,
            employee_code=f"EMP-{name.upper()[:4]}",
            name=name,
            email=email,
            joining_date="2026-01-01",
            status="Active",
            department=dept,
            designation=self.desig,
        )
        user = User.objects.create_user(
            email=email,
            password="pass-user-123",
            client=self.tenant,
            name=name,
            role=Role.objects.get(client=self.tenant, code=role_code),
            employee=emp,
        )
        return emp, user

    # ── 1. OFFER LETTERS & RECRUITMENT LIFECYCLE ─────────────────────────

    def test_offer_creation_advances_candidate_to_offer_stage(self):
        candidate = Candidate.objects.create(
            client=self.tenant,
            name="Bob Candidate",
            email="bob@candidates.test",
            stage="Interview",
        )
        res = self.api.post(
            "/api/v1/hrms/offers/",
            {
                "candidateId": str(candidate.id),
                "position": "Backend Developer",
                "department": "Engineering",
                "salary": "$90,000 / year",
                "offeredCtc": 90000,
                "joiningDate": (date.today() + timedelta(days=20)).isoformat(),
                "expiryDate": (date.today() + timedelta(days=7)).isoformat(),
                "status": "Draft",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201)
        candidate.refresh_from_db()
        self.assertEqual(candidate.stage, "Offer")

    def test_send_and_accept_offer_actions_advance_candidate_to_hired(self):
        candidate = Candidate.objects.create(
            client=self.tenant,
            name="Carol Candidate",
            email="carol@candidates.test",
            stage="Interview",
        )
        offer = Offer.objects.create(
            client=self.tenant,
            candidate=candidate,
            candidate_name="Carol Candidate",
            position="QA Lead",
            status="Draft",
            offered_ctc=80000,
            joining_date=date.today() + timedelta(days=14),
            expiry_date=date.today() + timedelta(days=5),
        )

        # 1. Send offer action
        send_res = self.api.post(f"/api/v1/hrms/offers/{offer.id}/send_offer/")
        self.assertEqual(send_res.status_code, 200)
        offer.refresh_from_db()
        self.assertEqual(offer.status, "Sent")
        self.assertIsNotNone(offer.sent_at)
        candidate.refresh_from_db()
        self.assertEqual(candidate.stage, "Offer")

        # 2. Accept offer action
        accept_res = self.api.post(f"/api/v1/hrms/offers/{offer.id}/accept_offer/")
        self.assertEqual(accept_res.status_code, 200)
        offer.refresh_from_db()
        self.assertEqual(offer.status, "Accepted")
        self.assertIsNotNone(offer.responded_at)
        candidate.refresh_from_db()
        self.assertEqual(candidate.stage, "Hired")

    def test_declined_offer_updates_candidate_stage_to_rejected(self):
        candidate = Candidate.objects.create(
            client=self.tenant,
            name="Dan Candidate",
            email="dan@candidates.test",
            stage="Offer",
        )
        offer = Offer.objects.create(
            client=self.tenant,
            candidate=candidate,
            candidate_name="Dan Candidate",
            position="Designer",
            status="Sent",
        )
        # Update offer status to Declined
        res = self.api.patch(f"/api/v1/hrms/offers/{offer.id}/", {"status": "Declined"}, format="json")
        self.assertEqual(res.status_code, 200)
        candidate.refresh_from_db()
        self.assertEqual(candidate.stage, "Rejected")

    def test_onboarding_completion_discovers_direct_candidate_offer(self):
        candidate = Candidate.objects.create(
            client=self.tenant,
            name="Elena Candidate",
            email="elena@candidates.test",
            stage="Hired",
        )
        offer = Offer.objects.create(
            client=self.tenant,
            candidate=candidate,
            candidate_name="Elena Candidate",
            position="DevOps Engineer",
            department="Engineering",
            location="HQ Remote",
            status="Accepted",
            offered_ctc=95000,
            joining_date=date.today(),
        )
        # Complete onboarding conversion
        res = self.api.post(f"/api/v1/hrms/onboarding/{candidate.id}/complete/")
        self.assertIn(res.status_code, [200, 201])
        candidate.refresh_from_db()
        self.assertIsNotNone(candidate.employee)
        emp = candidate.employee
        self.assertEqual(emp.name, "Elena Candidate")
        self.assertEqual(emp.status, "Probation")
        self.assertEqual(float(emp.standard_salary), round(95000 / 12, 2))

    # ── 2. GRIEVANCES & COMPLAINTS ───────────────────────────────────────

    def test_grievance_submission_comments_and_resolution_flow(self):
        emp_complainant, _ = self.create_employee_and_user("Fiona", "fiona@gov.test")
        emp_against, _ = self.create_employee_and_user("George", "george@gov.test")

        # 1. Create complaint
        res = self.api.post(
            "/api/v1/hrms/complaints/",
            {
                "raisedByEmployeeId": str(emp_complainant.id),
                "complainant": "Fiona",
                "againstEmployeeId": str(emp_against.id),
                "against": "George",
                "category": "Physical Workplace Environment",
                "priority": "High",
                "summary": "Office temperature and ventilation issues on 3rd floor.",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201)
        complaint_id = res.data["id"]

        # 2. Append investigation comment
        comment_res = self.api.post(
            f"/api/v1/hrms/complaints/{complaint_id}/comment/",
            {"text": "Facilities manager notified to inspect the ventilation dampers."},
            format="json",
        )
        self.assertEqual(comment_res.status_code, 200)
        self.assertEqual(len(comment_res.data["comments"]), 1)
        self.assertEqual(
            comment_res.data["comments"][0]["text"],
            "Facilities manager notified to inspect the ventilation dampers.",
        )

        # 3. Resolve complaint
        resolve_res = self.api.post(
            f"/api/v1/hrms/complaints/{complaint_id}/resolve/",
            {"resolution": "Ventilation dampers repaired and thermostat calibrated."},
            format="json",
        )
        self.assertEqual(resolve_res.status_code, 200)
        complaint = Complaint.objects.get(id=complaint_id)
        self.assertEqual(complaint.status, "Resolved")
        self.assertEqual(complaint.resolution, "Ventilation dampers repaired and thermostat calibrated.")
        self.assertIsNotNone(complaint.resolved_at)

        # Verify audit trail
        audit = AuditLog.objects.filter(
            client=self.tenant,
            entity_type="Complaint",
            entity_id=complaint_id,
            action="resolve",
        ).first()
        self.assertIsNotNone(audit)

    def test_anonymous_grievance_sanitizes_complainant_for_non_staff(self):
        emp_complainant, _ = self.create_employee_and_user("Hannah", "hannah@gov.test")
        emp_against, _ = self.create_employee_and_user("George", "george2@gov.test")
        emp_viewer, user_viewer = self.create_employee_and_user("Ian", "ian@gov.test")

        # Create anonymous complaint where Hannah is complainant but Ian is assigned handler
        complaint = Complaint.objects.create(
            client=self.tenant,
            raised_by_employee=emp_complainant,
            against_employee=emp_against,
            category="Ethics & Compliance Violation",
            description="Alleged conflict of interest in vendor procurement.",
            is_anonymous=True,
            assigned_to=user_viewer,
        )

        # Ian views complaint (does not have view_staff / edit_staff)
        viewer_api = APIClient()
        viewer_api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(user_viewer)['access']}")
        res = viewer_api.get(f"/api/v1/hrms/complaints/{complaint.id}/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["complainant"], "Anonymous")

    # ── 3. ANNOUNCEMENTS & TARGETED AUDIENCES ─────────────────────────────

    def test_announcements_draft_publish_and_archive_lifecycle(self):
        emp_eng, user_eng = self.create_employee_and_user("Jack", "jack@gov.test", dept=self.dept_eng)
        emp_sales, user_sales = self.create_employee_and_user("Karen", "karen@gov.test", dept=self.dept_sales)

        eng_api = APIClient()
        eng_api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(user_eng)['access']}")
        sales_api = APIClient()
        sales_api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(user_sales)['access']}")

        def get_items(resp):
            if isinstance(resp.data, dict) and "results" in resp.data:
                return resp.data["results"]
            if isinstance(resp.data, list):
                return resp.data
            return []

        # 1. Admin creates a Draft announcement targeted to Engineering
        res = self.api.post(
            "/api/v1/hrms/announcements/",
            {
                "title": "Engineering All-Hands",
                "content": "Quarterly sprint alignment this Thursday.",
                "priority": "High",
                "publish_date": date.today().isoformat(),
                "expiry_date": (date.today() + timedelta(days=10)).isoformat(),
                "targetDepartmentId": str(self.dept_eng.id),
                "status": "Draft",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201)
        ann_id = res.data["id"]

        # 2. While in Draft, regular employee in Engineering cannot see it
        eng_view = eng_api.get("/api/v1/hrms/announcements/")
        self.assertEqual(len(get_items(eng_view)), 0)

        # 3. Admin publishes the announcement
        pub_res = self.api.post(f"/api/v1/hrms/announcements/{ann_id}/publish/")
        self.assertEqual(pub_res.status_code, 200)
        self.assertEqual(pub_res.data["status"], "Published")

        # 4. Now Engineering employee can see it
        eng_view_after = eng_api.get("/api/v1/hrms/announcements/")
        items_after = get_items(eng_view_after)
        self.assertEqual(len(items_after), 1)
        self.assertEqual(items_after[0]["id"], ann_id)

        # 5. Sales employee cannot see it (targeted to Engineering)
        sales_view = sales_api.get("/api/v1/hrms/announcements/")
        self.assertEqual(len(get_items(sales_view)), 0)

        # 6. Admin archives the announcement
        arch_res = self.api.post(f"/api/v1/hrms/announcements/{ann_id}/archive/")
        self.assertEqual(arch_res.status_code, 200)
        self.assertEqual(arch_res.data["status"], "Archived")

        # 7. Once archived, Engineering employee no longer sees it
        eng_view_archived = eng_api.get("/api/v1/hrms/announcements/")
        self.assertEqual(len(get_items(eng_view_archived)), 0)

    # ── 4. TRAVEL REQUISITIONS & SETTLEMENT ────────────────────────────────

    def test_travel_requisition_date_validation_and_exited_employee_guard(self):
        emp, _ = self.create_employee_and_user("Leo", "leo@gov.test")

        # Invalid dates: end_date before start_date
        res = self.api.post(
            "/api/v1/hrms/travel-requests/",
            {
                "employeeId": str(emp.id),
                "purpose": "Client meeting in Chicago",
                "destination": "Chicago, IL",
                "start_date": "2026-06-15",
                "end_date": "2026-06-10",
                "advance_requested": 500,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 400)
        field_errors = res.data.get("field_errors") or res.data
        self.assertTrue("end_date" in field_errors or "endDate" in field_errors)

        # Employee with effective exit date in the past
        emp.status = "Terminated"
        emp.effective_exit_date = date.today() - timedelta(days=5)
        emp.save()

        res_exited = self.api.post(
            "/api/v1/hrms/travel-requests/",
            {
                "employeeId": str(emp.id),
                "purpose": "Post-exit trip",
                "destination": "Austin, TX",
                "start_date": date.today().isoformat(),
                "end_date": (date.today() + timedelta(days=2)).isoformat(),
                "advance_requested": 400,
            },
            format="json",
        )
        self.assertEqual(res_exited.status_code, 409)

    def test_travel_requisition_approval_and_settlement_guard(self):
        emp, _ = self.create_employee_and_user("Mia", "mia@gov.test")

        # 1. Create submitted travel request
        create_res = self.api.post(
            "/api/v1/hrms/travel-requests/",
            {
                "employeeId": str(emp.id),
                "purpose": "AWS Summit Keynote",
                "destination": "Las Vegas, NV",
                "start_date": date.today().isoformat(),
                "end_date": (date.today() + timedelta(days=3)).isoformat(),
                "advance_requested": 1200,
            },
            format="json",
        )
        self.assertEqual(create_res.status_code, 201)
        travel_id = create_res.data["id"]

        # 2. Cannot settle an unapproved travel request (Guard check)
        settle_guard_res = self.api.post(
            f"/api/v1/hrms/travel-requests/{travel_id}/settle/",
            {
                "advance_disbursed": 1200,
                "actual_expenses": 1150,
                "settlement_notes": "Flight + hotel settled.",
            },
            format="json",
        )
        self.assertEqual(settle_guard_res.status_code, 409)

        # Direct PATCH to Settled is also blocked
        patch_guard_res = self.api.patch(
            f"/api/v1/hrms/travel-requests/{travel_id}/",
            {"status": "Settled"},
            format="json",
        )
        self.assertEqual(patch_guard_res.status_code, 409)

        # 3. Approve the travel request
        approve_res = self.api.post(f"/api/v1/hrms/travel-requests/{travel_id}/approve/")
        self.assertEqual(approve_res.status_code, 200)
        self.assertEqual(approve_res.data["status"], "Approved")

        # 4. Now settle the travel request
        settle_res = self.api.post(
            f"/api/v1/hrms/travel-requests/{travel_id}/settle/",
            {
                "advance_disbursed": 1200,
                "actual_expenses": 1150,
                "settlement_notes": "All receipts verified; $50 refunded to company.",
            },
            format="json",
        )
        self.assertEqual(settle_res.status_code, 200)
        self.assertEqual(settle_res.data["status"], "Settled")
        adv = settle_res.data.get("advanceDisbursed") or settle_res.data.get("advance_disbursed")
        exp = settle_res.data.get("actualExpenses") or settle_res.data.get("actual_expenses")
        self.assertEqual(float(adv), 1200.0)
        self.assertEqual(float(exp), 1150.0)

        # Verify audit trail
        audit = AuditLog.objects.filter(
            client=self.tenant,
            entity_type="TravelRequest",
            entity_id=travel_id,
            action="settle",
        ).first()
        self.assertIsNotNone(audit)

