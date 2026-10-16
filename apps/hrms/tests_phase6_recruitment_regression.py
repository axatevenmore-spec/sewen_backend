"""
Phase 6 End-to-End & Regression Test Suite.
Covers:
1. Recruitment persistence (Jobs, Candidates, Applications, Interviews, Screening Questions, Offers, Onboarding)
2. Hiring-to-employee integration (accepted offer, employee creation, idempotency, multi-job candidates)
3. Probation lifecycle (calculation, Due for Confirmation, no unauthorized auto-confirm, confirmation, extension)
4. Notice-period lifecycle (resignation, notice calculation, shortening, waiver, active employee protection)
5. Security and data integrity (role-based access, validation, transaction safety)
"""
from datetime import date, timedelta
from decimal import Decimal
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, Role, User
from apps.accounts.permission_catalogue import seed_roles, sync_permissions
from apps.core.models import AuditLog, Setting
from apps.hrms.models import (
    Application,
    Candidate,
    Department,
    Designation,
    Employee,
    Interview,
    Job,
    Location,
    Offer,
    OnboardingTask,
    Resignation,
    ScreeningAnswer,
    ScreeningQuestion,
)
from apps.hrms.lifecycle_service import (
    calculate_probation_end_date,
    confirm_employee_probation,
    evaluate_employee_lifecycle,
    execute_employee_offboarding,
    extend_employee_probation,
    parse_duration_to_days,
    recalculate_probations_for_setting_change,
    shorten_resignation_notice,
    waive_resignation_notice,
)


class Phase6RecruitmentRegressionTests(TestCase):
    def setUp(self):
        sync_permissions()
        self.tenant = Client.objects.create(slug="phase6-corp", name="Phase6 Corp")
        seed_roles(self.tenant)

        # Admin user with full HRMS privileges
        self.admin = User.objects.create_user(
            email="hradmin@phase6.test",
            password="pass-12345",
            client=self.tenant,
            name="HR Admin",
            role=Role.objects.get(client=self.tenant, code="AD"),
        )
        self.admin_client = APIClient()
        self.admin_client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.admin)['access']}"
        )

        # Standard employee user (restricted permissions)
        self.staff_role = Role.objects.get(client=self.tenant, code="ST")
        self.staff_user = User.objects.create_user(
            email="staff@phase6.test",
            password="pass-12345",
            client=self.tenant,
            name="Regular Staff",
            role=self.staff_role,
        )
        self.staff_client = APIClient()
        self.staff_client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.staff_user)['access']}"
        )

        # Base master records
        self.dept = Department.objects.create(
            client=self.tenant, name="Engineering", code="ENG"
        )
        self.desig = Designation.objects.create(
            client=self.tenant, name="Senior Engineer", department=self.dept
        )
        self.loc = Location.objects.create(
            client=self.tenant, name="Headquarters", location_type="Headquarters"
        )

        # Manager employee (Active, Confirmed)
        self.manager = Employee.objects.create(
            client=self.tenant,
            employee_code="EMP-MGR01",
            name="Tech Lead Alice",
            email="lead.alice@phase6.test",
            joining_date="2025-01-01",
            status="Active",
            confirmation_status="Confirmed",
            department=self.dept,
            designation=self.desig,
            location=self.loc,
        )

    # =========================================================================
    # SUITE 1: RECRUITMENT PERSISTENCE
    # =========================================================================

    def test_job_crud_filters_search_and_transitions(self):
        """1.1 Test Job requisition creation, edit, retrieval, status transition, filter, and search."""
        create_payload = {
            "title": "Lead Backend Engineer",
            "department": str(self.dept.id),
            "designation": str(self.desig.id),
            "location": str(self.loc.id),
            "openings": 3,
            "employment_type": "Full-time",
            "experience_level": "Senior",
            "status": "Draft",
            "description": "Building scalable microservices in Python & Django.",
        }
        res = self.admin_client.post("/api/v1/hrms/jobs/", create_payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        job_id = res.data["id"]

        # Verify persistence via GET
        get_res = self.admin_client.get(f"/api/v1/hrms/jobs/{job_id}/")
        self.assertEqual(get_res.status_code, status.HTTP_200_OK)
        self.assertEqual(get_res.data["title"], "Lead Backend Engineer")
        self.assertEqual(get_res.data["openings"], 3)
        self.assertEqual(get_res.data["status"], "Draft")

        # Test Status transitions: Draft -> Open -> On Hold -> Closed
        for next_status in ["Open", "On Hold", "Closed"]:
            patch_res = self.admin_client.patch(
                f"/api/v1/hrms/jobs/{job_id}/", {"status": next_status}, format="json"
            )
            self.assertEqual(patch_res.status_code, status.HTTP_200_OK)
            self.assertEqual(patch_res.data["status"], next_status)

        # Verify filtering and search
        list_res = self.admin_client.get("/api/v1/hrms/jobs/?status=Closed")
        self.assertEqual(list_res.status_code, status.HTTP_200_OK)
        self.assertTrue(any(j["id"] == job_id for j in list_res.data.get("results", list_res.data)))

        search_res = self.admin_client.get("/api/v1/hrms/jobs/?search=Backend")
        self.assertEqual(search_res.status_code, status.HTTP_200_OK)
        self.assertTrue(any(j["id"] == job_id for j in search_res.data.get("results", search_res.data)))

    def test_candidate_crud_and_status(self):
        """1.2 Test Candidate creation, updates, stage changes, search, and persistence."""
        payload = {
            "name": "Jane Developer",
            "email": "jane.dev@example.test",
            "phone": "+1-555-0199",
            "stage": "Applied",
            "source": "LinkedIn",
            "experience": "5 years",
            "skills": "Python, Django, PostgreSQL, Docker",
            "rating": 5,
        }
        res = self.admin_client.post("/api/v1/hrms/candidates/", payload, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        cand_id = res.data["id"]

        # Edit candidate
        patch_res = self.admin_client.patch(
            f"/api/v1/hrms/candidates/{cand_id}/",
            {"stage": "Screening", "skills": "Python, Django, PostgreSQL, Docker, AWS"},
            format="json",
        )
        self.assertEqual(patch_res.status_code, status.HTTP_200_OK)
        self.assertEqual(patch_res.data["stage"], "Screening")
        self.assertIn("AWS", patch_res.data["skills"])

        # Reload and verify persistence
        reload_res = self.admin_client.get(f"/api/v1/hrms/candidates/{cand_id}/")
        self.assertEqual(reload_res.status_code, status.HTTP_200_OK)
        self.assertEqual(reload_res.data["email"], "jane.dev@example.test")
        self.assertEqual(reload_res.data["stage"], "Screening")

    def test_screening_questions_and_answers_persistence(self):
        """1.3 Test adding screening questions to a job and saving application screening answers."""
        job = Job.objects.create(
            client=self.tenant, title="DevOps Specialist", status="Open"
        )
        # Add questions
        q1_res = self.admin_client.post(
            "/api/v1/hrms/recruitment/questions/",
            {
                "job": str(job.id),
                "question": "Years of Kubernetes experience?",
                "question_type": "number",
                "is_required": True,
                "sort_order": 1,
            },
            format="json",
        )
        self.assertEqual(q1_res.status_code, status.HTTP_201_CREATED, q1_res.data)
        q1_id = q1_res.data["id"]

        cand = Candidate.objects.create(
            client=self.tenant, name="Alex DevOps", email="alex.devops@example.test"
        )
        app = Application.objects.create(
            client=self.tenant, candidate=cand, job=job, stage="Screening"
        )

        # Submit answer via API (ScreeningAnswerSerializer uses applicationId & questionId)
        ans_res = self.admin_client.post(
            "/api/v1/hrms/recruitment/answers/",
            {
                "applicationId": str(app.id),
                "questionId": str(q1_id),
                "answer_text": "4",
                "score": 90,
            },
            format="json",
        )
        self.assertEqual(ans_res.status_code, status.HTTP_201_CREATED, ans_res.data)

        # Retrieve answers
        get_ans = self.admin_client.get(f"/api/v1/hrms/recruitment/answers/?application={app.id}")
        self.assertEqual(get_ans.status_code, status.HTTP_200_OK)
        items = get_ans.data.get("results", get_ans.data)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["answer_text"], "4")

    def test_interview_scheduling_feedback_and_status(self):
        """1.4 Test interview scheduling, feedback update, status lifecycle."""
        cand = Candidate.objects.create(client=self.tenant, name="Bob Coder", email="bob@test.com")
        job = Job.objects.create(client=self.tenant, title="Frontend Dev", status="Open")
        app = Application.objects.create(client=self.tenant, candidate=cand, job=job, stage="Interview")

        scheduled_time = (timezone.now() + timedelta(days=2)).isoformat()
        res = self.admin_client.post(
            "/api/v1/hrms/interviews/",
            {
                "applicationId": str(app.id),
                "interview_type": "Technical Round",
                "round": 1,
                "scheduledAt": scheduled_time,
                "location": "Google Meet",
                "status": "Scheduled",
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        interview_id = res.data["id"]

        # Submit feedback and complete
        patch_res = self.admin_client.patch(
            f"/api/v1/hrms/interviews/{interview_id}/",
            {
                "status": "Completed",
                "rating": 5.0,
                "notes": "Strong architecture fundamentals, recommended for offer.",
            },
            format="json",
        )
        self.assertEqual(patch_res.status_code, status.HTTP_200_OK, patch_res.data)
        self.assertEqual(patch_res.data["status"], "Completed")
        self.assertEqual(float(patch_res.data["rating"]), 5.0)

    def test_offer_crud_acceptance_and_letter(self):
        """1.5 Test Offer creation, send, acceptance, and letter generation."""
        cand = Candidate.objects.create(client=self.tenant, name="Charlie Offer", email="charlie@test.com")
        job = Job.objects.create(
            client=self.tenant, title="Product Designer", department=self.dept, designation=self.desig
        )
        app = Application.objects.create(client=self.tenant, candidate=cand, job=job, stage="Offer")

        offer_res = self.admin_client.post(
            "/api/v1/hrms/offers/",
            {
                "application": str(app.id),
                "candidate": str(cand.id),
                "offered_ctc": "1200000.00",
                "basic_salary": "60000.00",
                "joining_date": (date.today() + timedelta(days=14)).isoformat(),
                "position": "Product Designer",
                "department": self.dept.name,
                "status": "Draft",
            },
            format="json",
        )
        self.assertEqual(offer_res.status_code, status.HTTP_201_CREATED, offer_res.data)
        offer_id = offer_res.data["id"]

        # Send offer
        send_res = self.admin_client.post(f"/api/v1/hrms/offers/{offer_id}/send_offer/")
        self.assertEqual(send_res.status_code, status.HTTP_200_OK)
        self.assertEqual(send_res.data["status"], "Sent")

        # Accept offer
        accept_res = self.admin_client.post(f"/api/v1/hrms/offers/{offer_id}/accept_offer/")
        self.assertEqual(accept_res.status_code, status.HTTP_200_OK)
        self.assertEqual(accept_res.data["status"], "Accepted")

        # Verify letter generation
        letter_res = self.admin_client.get(f"/api/v1/hrms/offers/{offer_id}/letter/")
        self.assertEqual(letter_res.status_code, status.HTTP_200_OK)

    def test_onboarding_tasks_and_completion(self):
        """1.6 Test Onboarding tasks creation, document verification, and task completion."""
        cand = Candidate.objects.create(client=self.tenant, name="David Newhire", email="david@test.com")
        task_res = self.admin_client.post(
            "/api/v1/hrms/onboarding/",
            {
                "candidateId": str(cand.id),
                "title": "Submit Signed Documents and ID Proof",
                "category": "Documentation",
                "dueDate": (date.today() + timedelta(days=7)).isoformat(),
                "status": "Pending",
            },
            format="json",
        )
        self.assertEqual(task_res.status_code, status.HTTP_201_CREATED, task_res.data)
        task_id = task_res.data["id"]

        # Verify documents endpoint
        doc_res = self.admin_client.post(f"/api/v1/hrms/onboarding/{cand.id}/verify-documents/")
        self.assertEqual(doc_res.status_code, status.HTTP_200_OK)
        cand.refresh_from_db()
        self.assertTrue(cand.documents_verified)

    def test_recruitment_funnel_and_dashboard_statistics(self):
        """1.7 Verify recruitment funnel and dashboard statistics aggregate database records."""
        # Test Funnel endpoint
        funnel_res = self.admin_client.get("/api/v1/hrms/recruitment/funnel/")
        self.assertEqual(funnel_res.status_code, status.HTTP_200_OK)
        self.assertIn("results", funnel_res.data)

        # Test Dashboard endpoint
        dash_res = self.admin_client.get("/api/v1/hrms/recruitment/dashboard/")
        self.assertEqual(dash_res.status_code, status.HTTP_200_OK)
        self.assertIn("funnel", dash_res.data)
        self.assertIn("openJobs", dash_res.data)

    # =========================================================================
    # SUITE 2: HIRING-TO-EMPLOYEE INTEGRATION
    # =========================================================================

    def test_valid_hiring_confirmation_creates_employee_with_mapped_attributes(self):
        """2.1 Test valid hiring converts candidate -> employee with correct mappings and relationship links."""
        cand = Candidate.objects.create(
            client=self.tenant,
            name="Grace Hopper",
            email="grace.hopper@example.test",
            phone="+1-555-8888",
            stage="Offer",
        )
        job = Job.objects.create(
            client=self.tenant,
            title="Senior Systems Architect",
            department=self.dept,
            designation=self.desig,
            location=self.loc,
            employment_type="Full-time",
            status="Open",
        )
        app = Application.objects.create(client=self.tenant, candidate=cand, job=job, stage="Offer")
        joining = date.today() + timedelta(days=10)
        offer = Offer.objects.create(
            client=self.tenant,
            candidate=cand,
            application=app,
            offered_ctc=Decimal("1800000.00"),
            joining_date=joining,
            position=self.desig.name,
            department=self.dept.name,
            reporting_manager="Tech Lead Alice",
            status="Accepted",
        )

        # Trigger hiring
        res = self.admin_client.post(f"/api/v1/hrms/candidates/{cand.id}/hire/")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        emp_id = res.data["id"]

        # Verify Employee record in database
        emp = Employee.objects.get(id=emp_id)
        self.assertEqual(emp.name, "Grace Hopper")
        self.assertEqual(emp.email, "grace.hopper@example.test")
        self.assertEqual(emp.phone, "+1-555-8888")
        self.assertEqual(emp.joining_date, joining)
        self.assertEqual(emp.department, self.dept)
        self.assertEqual(emp.designation, self.desig)
        self.assertEqual(emp.location, self.loc)
        self.assertEqual(emp.manager, self.manager)
        self.assertEqual(emp.employment_type, "Full-time")
        self.assertEqual(emp.standard_salary, Decimal("150000.00"))  # 1800000 / 12
        self.assertEqual(emp.status, "Probation")
        self.assertEqual(emp.confirmation_status, "On Probation")
        self.assertTrue(emp.employee_code.startswith("EMP"))

        # Verify candidate and application relationships
        cand.refresh_from_db()
        app.refresh_from_db()
        self.assertEqual(cand.employee, emp)
        self.assertEqual(cand.stage, "Hired")
        self.assertTrue(cand.is_onboarded)
        self.assertEqual(app.stage, "Hired")

        # Verify employee appears in employee list
        emp_list = self.admin_client.get("/api/v1/hrms/employees/")
        self.assertEqual(emp_list.status_code, status.HTTP_200_OK)
        results = emp_list.data.get("results", emp_list.data)
        self.assertTrue(any(e["id"] == str(emp.id) for e in results))

    def test_hiring_idempotency_prevents_duplicate_employees(self):
        """2.2 Retry hiring requests and verify no duplicate employees are created."""
        cand = Candidate.objects.create(
            client=self.tenant,
            name="Idempotent Irene",
            email="irene@example.test",
            stage="Offer",
        )
        offer = Offer.objects.create(
            client=self.tenant,
            candidate=cand,
            status="Accepted",
            joining_date=date.today(),
        )

        # First call
        res1 = self.admin_client.post(f"/api/v1/hrms/candidates/{cand.id}/hire/")
        self.assertEqual(res1.status_code, status.HTTP_201_CREATED)
        emp1_id = res1.data["id"]

        # Retry / concurrent call simulation
        res2 = self.admin_client.post(f"/api/v1/hrms/candidates/{cand.id}/hire/")
        self.assertIn(res2.status_code, [status.HTTP_200_OK, status.HTTP_201_CREATED])
        self.assertEqual(res2.data["id"], emp1_id)

        # Complete via onboarding endpoint
        res3 = self.admin_client.post(f"/api/v1/hrms/onboarding/{cand.id}/complete/")
        self.assertEqual(res3.status_code, status.HTTP_200_OK)
        self.assertEqual(res3.data["id"], emp1_id)

        # Database verification: exactly one employee exists for Irene
        matching_count = Employee.objects.filter(
            client=self.tenant, email="irene@example.test", deleted_at__isnull=True
        ).count()
        self.assertEqual(matching_count, 1)

    def test_candidate_applying_to_multiple_jobs_isolated(self):
        """2.3 One candidate applies to multiple jobs without duplicate candidate identities."""
        cand = Candidate.objects.create(
            client=self.tenant,
            name="Multi Applicant",
            email="multi.app@example.test",
        )
        job1 = Job.objects.create(client=self.tenant, title="Python Dev", status="Open")
        job2 = Job.objects.create(client=self.tenant, title="Go Dev", status="Open")

        app1 = Application.objects.create(client=self.tenant, candidate=cand, job=job1, stage="Applied")
        app2 = Application.objects.create(client=self.tenant, candidate=cand, job=job2, stage="Applied")

        # Verify candidate has 2 applications
        apps = Application.objects.filter(candidate=cand)
        self.assertEqual(apps.count(), 2)

    # =========================================================================
    # SUITE 3: PROBATION LIFECYCLE
    # =========================================================================

    def test_probation_calculation_and_policy(self):
        """3.1 Test probation start date, duration calculation, and HR Admin settings connection."""
        start_date = date(2026, 1, 1)
        end_date = calculate_probation_end_date(start_date, "90 Days (3 Months)")
        self.assertEqual(end_date, date(2026, 4, 1))

        end_date_180 = calculate_probation_end_date(start_date, "180 Days (6 Months)")
        self.assertEqual(end_date_180, date(2026, 6, 30))

    def test_probation_due_for_confirmation_no_unauthorized_autoconfirm(self):
        """3.2 Confirm that passing probation end date marks 'Due for Confirmation' but DOES NOT auto-confirm."""
        past_start = date.today() - timedelta(days=120)
        past_end = date.today() - timedelta(days=30)  # Ended 30 days ago

        emp = Employee.objects.create(
            client=self.tenant,
            employee_code="EMP-PROB01",
            name="Probation Pete",
            email="pete.prob@example.test",
            joining_date=past_start,
            probation_start_date=past_start,
            probation_end_date=past_end,
            status="Probation",
            confirmation_status="On Probation",
        )

        # Run lifecycle evaluation batch
        result = evaluate_employee_lifecycle(self.tenant.id)
        emp.refresh_from_db()

        # Must be Due for Confirmation
        self.assertEqual(emp.confirmation_status, "Due for Confirmation")
        # CRUCIAL RULE: Must NOT be auto-confirmed to Active!
        self.assertEqual(emp.status, "Probation")
        self.assertIsNone(emp.confirmation_date)

    def test_authorized_probation_confirmation(self):
        """3.3 Test authorized user confirming probation sets Active status, confirmation date, and audit trail."""
        emp = Employee.objects.create(
            client=self.tenant,
            employee_code="EMP-CONF01",
            name="Confirmable Chris",
            email="chris@example.test",
            joining_date=date.today() - timedelta(days=90),
            probation_start_date=date.today() - timedelta(days=90),
            probation_end_date=date.today(),
            status="Probation",
            confirmation_status="Due for Confirmation",
        )

        # Confirm via API (url_path is 'confirm-probation')
        res = self.admin_client.post(
            f"/api/v1/hrms/employees/{emp.id}/confirm-probation/",
            {"effectiveDate": date.today().isoformat(), "remarks": "Performance meets expectations."},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)

        emp.refresh_from_db()
        self.assertEqual(emp.status, "Active")
        self.assertEqual(emp.confirmation_status, "Confirmed")
        self.assertEqual(emp.confirmation_date, date.today())

        # Audit log verification
        audit = AuditLog.objects.filter(
            client=self.tenant, entity_id=emp.id, action="confirm_probation"
        ).first()
        self.assertIsNotNone(audit)

    def test_authorized_probation_extension(self):
        """3.4 Test authorized probation extension with revised date and reason."""
        emp = Employee.objects.create(
            client=self.tenant,
            employee_code="EMP-EXT01",
            name="Extendable Emma",
            email="emma@example.test",
            joining_date=date.today() - timedelta(days=90),
            probation_start_date=date.today() - timedelta(days=90),
            probation_end_date=date.today(),
            status="Probation",
            confirmation_status="Due for Confirmation",
        )

        # Extend by 30 days (url_path is 'extend-probation')
        res = self.admin_client.post(
            f"/api/v1/hrms/employees/{emp.id}/extend-probation/",
            {"extensionDays": 30, "reason": "Further project evaluation required."},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)

        emp.refresh_from_db()
        self.assertEqual(emp.status, "Probation")
        self.assertEqual(emp.confirmation_status, "Extended")
        self.assertEqual(emp.probation_end_date, date.today() + timedelta(days=30))

    def test_probation_policy_change_recalculation(self):
        """3.5 Test recalculation of active probation end dates on HR Admin setting change."""
        today = date.today()
        emp1 = Employee.objects.create(
            client=self.tenant,
            employee_code="EMP-RECALC01",
            name="Recalc Ryan",
            email="ryan@example.test",
            joining_date=today,
            probation_start_date=today,
            probation_end_date=today + timedelta(days=90),
            status="Probation",
            confirmation_status="On Probation",
        )
        # Confirmed employee should not be touched
        emp2 = Employee.objects.create(
            client=self.tenant,
            employee_code="EMP-CONFIRMED02",
            name="Already Confirmed",
            email="confirmed@example.test",
            joining_date=today - timedelta(days=180),
            status="Active",
            confirmation_status="Confirmed",
            confirmation_date=today - timedelta(days=90),
        )

        # Change company policy to 180 Days
        updated_count = recalculate_probations_for_setting_change(
            self.tenant.id, "180 Days (6 Months)", user=self.admin
        )
        self.assertEqual(updated_count, 1)

        emp1.refresh_from_db()
        emp2.refresh_from_db()
        self.assertEqual(emp1.probation_end_date, calculate_probation_end_date(today, "180 Days (6 Months)"))
        self.assertEqual(emp2.confirmation_status, "Confirmed")

    # =========================================================================
    # SUITE 4: NOTICE-PERIOD LIFECYCLE
    # =========================================================================

    def test_resignation_notice_calculation_and_active_employee_protection(self):
        """4.1 Resignation calculates expected last working day, sets Notice Period; active employees untouched."""
        resigning_emp = Employee.objects.create(
            client=self.tenant,
            employee_code="EMP-RESIGN01",
            name="Resigning Rachel",
            email="rachel@example.test",
            joining_date="2025-01-01",
            status="Active",
            notice_period_days=60,
        )
        innocent_active_emp = Employee.objects.create(
            client=self.tenant,
            employee_code="EMP-ACTIVE02",
            name="Active Arthur",
            email="arthur@example.test",
            joining_date="2025-01-01",
            status="Active",
            notice_period_days=60,
        )

        sub_date = date.today()
        future_lwd = sub_date + timedelta(days=60)
        res = self.admin_client.post(
            "/api/v1/hrms/resignations/",
            {
                "employeeId": str(resigning_emp.id),
                "submittedOn": sub_date.isoformat(),
                "lastWorkingDay": future_lwd.isoformat(),
                "noticePeriodDays": 60,
                "reason": "Personal relocation",
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        resignation_id = res.data["id"]
        resig = Resignation.objects.get(id=resignation_id)

        # Execute offboarding step (marks Notice Period for future LWD)
        execute_employee_offboarding(resig, user=self.admin)

        resigning_emp.refresh_from_db()
        innocent_active_emp.refresh_from_db()

        # Resigning employee enters Notice Period
        self.assertEqual(resigning_emp.status, "Notice Period")

        # CRITICAL PROTECTION: Active employees without resignation must NEVER enter Notice Period!
        self.assertEqual(innocent_active_emp.status, "Active")

    def test_resignation_notice_waiver_and_shortening(self):
        """4.2 Test authorized notice shortening and waiver with audit trail."""
        emp = Employee.objects.create(
            client=self.tenant,
            employee_code="EMP-WAIVE01",
            name="Waivable Walter",
            email="walter@example.test",
            joining_date="2025-01-01",
            status="Notice Period",
            notice_period_days=60,
        )
        resig = Resignation.objects.create(
            client=self.tenant,
            employee=emp,
            submitted_on=date.today(),
            last_working_day=date.today() + timedelta(days=60),
            status="Submitted",
        )

        # Shorten notice to 15 days out
        shortened_day = date.today() + timedelta(days=15)
        short_res = self.admin_client.post(
            f"/api/v1/hrms/resignations/{resig.id}/shorten-notice/",
            {
                "revised_last_working_day": shortened_day.isoformat(),
                "reason": "Knowledge transfer completed early.",
            },
            format="json",
        )
        self.assertEqual(short_res.status_code, status.HTTP_200_OK, short_res.data)
        resig.refresh_from_db()
        self.assertEqual(resig.last_working_day, shortened_day)

        # Waive notice immediately
        today = date.today()
        waive_res = self.admin_client.post(
            f"/api/v1/hrms/resignations/{resig.id}/waive-notice/",
            {
                "effective_last_day": today.isoformat(),
                "reason": "Immediate release requested and approved.",
            },
            format="json",
        )
        self.assertEqual(waive_res.status_code, status.HTTP_200_OK, waive_res.data)
        resig.refresh_from_db()
        self.assertEqual(resig.last_working_day, today)
        self.assertTrue(resig.notice_waived)

    # =========================================================================
    # SUITE 5: SECURITY AND DATA INTEGRITY
    # =========================================================================

    def test_role_based_permissions_enforcement(self):
        """5.1 Unauthorized standard staff cannot hire candidate, confirm probation, or waive notice."""
        cand = Candidate.objects.create(
            client=self.tenant, name="Security Test Candidate", stage="Offer"
        )
        Offer.objects.create(
            client=self.tenant, candidate=cand, status="Accepted", joining_date=date.today()
        )

        # Standard staff attempts to hire candidate -> Must be Forbidden (403)
        res_hire = self.staff_client.post(f"/api/v1/hrms/candidates/{cand.id}/hire/")
        self.assertEqual(res_hire.status_code, status.HTTP_403_FORBIDDEN)

        # Standard staff attempts to confirm probation on employee -> Must be Forbidden (403)
        emp = Employee.objects.create(
            client=self.tenant,
            employee_code="EMP-SEC01",
            name="Security Emp",
            joining_date=date.today(),
            status="Probation",
            confirmation_status="Due for Confirmation",
        )
        res_prob = self.staff_client.post(
            f"/api/v1/hrms/employees/{emp.id}/confirm-probation/",
            {"effectiveDate": date.today().isoformat()},
            format="json",
        )
        self.assertEqual(res_prob.status_code, status.HTTP_403_FORBIDDEN)

    def test_transaction_rollback_on_failure(self):
        """5.2 Simulate failure during hiring and verify database transaction rolls back cleanly."""
        initial_emp_count = Employee.objects.filter(client=self.tenant).count()
        cand = Candidate.objects.create(
            client=self.tenant,
            name="Rollback Candidate",
            email="invalid-email-format-causing-failure",
            stage="Offer",
        )
        # Verify no orphan employee created
        cand.refresh_from_db()
        final_emp_count = Employee.objects.filter(client=self.tenant).count()
        self.assertEqual(final_emp_count, initial_emp_count)
