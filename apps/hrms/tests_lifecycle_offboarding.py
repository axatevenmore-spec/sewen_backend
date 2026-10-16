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

    def test_employee_portal_resignation_submission_and_hr_admin_visibility(self):
        """Employee submits resignation in portal, sees only own resignation, HR Admin sees all."""
        emp, user = self.create_employee("Zara", "zara@lifecycle.test")
        from apps.accounts.models import Role
        em_role = Role.objects.filter(client=self.tenant, code="EM").first()
        if em_role:
            user.role = em_role
            user.save()

        # Authenticate as employee Zara
        from rest_framework.test import APIClient
        emp_client = APIClient()
        emp_client.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(user)['access']}")

        lwd = (date.today() + timedelta(days=30)).isoformat()
        payload = {
            "noticePeriod": "30 Days",
            "noticePeriodDays": 30,
            "lastWorkingDay": lwd,
            "reason": "Pursuing master's degree abroad",
        }

        # 1. Employee submits resignation via API
        resp = emp_client.post("/api/v1/hrms/resignations/", payload, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        res_data = resp.json()
        self.assertEqual(res_data["employeeName"], "Zara")
        self.assertIn(res_data["status"], ("Submitted", "Pending Review", "Pending"))
        res_id = res_data["id"]

        # 2. Duplicate submission by same employee rejected
        dup_resp = emp_client.post("/api/v1/hrms/resignations/", payload, format="json")
        self.assertEqual(dup_resp.status_code, 409)

        # 3. Employee list query returns ONLY Zara's resignation
        my_res = emp_client.get("/api/v1/hrms/resignations/")
        self.assertEqual(my_res.status_code, 200)
        items = my_res.json()["results"] if isinstance(my_res.json(), dict) and "results" in my_res.json() else my_res.json()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], res_id)

        # 4. HR Admin sees Zara's resignation in HR Admin view
        admin_res = self.api.get("/api/v1/hrms/resignations/")
        self.assertEqual(admin_res.status_code, 200)
        admin_items = admin_res.json()["results"] if isinstance(admin_res.json(), dict) and "results" in admin_res.json() else admin_res.json()
        admin_item_ids = [r["id"] for r in admin_items]
        self.assertIn(res_id, admin_item_ids)

        # 5. Employee can cancel/withdraw their own pending resignation
        cancel_resp = emp_client.post(f"/api/v1/hrms/resignations/{res_id}/cancel/", {"reason": "Decided to stay"}, format="json")
        self.assertEqual(cancel_resp.status_code, 200)
        self.assertEqual(cancel_resp.json()["status"], "Cancelled")

    def test_employee_document_submission_and_hr_admin_sync(self):
        """Test employee PDF document submission, category enforcement, session derivation, and HR Admin sync."""
        import base64
        from apps.hrms.models import HrDocument

        emp, user = self.create_employee("Devon", "devon@lifecycle.test")

        emp_client = APIClient()
        emp_client.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(user)['access']}")

        # 1. Non-PDF upload rejection
        txt_b64 = base64.b64encode(b"This is a text file").decode("utf-8")
        bad_payload = {
            "title": "Plain Text Notes",
            "category": "Identity",
            "fileData": f"data:text/plain;base64,{txt_b64}",
            "fileName": "notes.txt",
        }
        bad_resp = emp_client.post("/api/v1/hrms/documents/", bad_payload, format="json")
        self.assertEqual(bad_resp.status_code, 400)
        self.assertIn("PDF", str(bad_resp.data))

        # 2. Invalid category rejection
        pdf_bytes = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF"
        pdf_b64 = base64.b64encode(pdf_bytes).decode("utf-8")
        invalid_cat_payload = {
            "title": "Invalid Category Doc",
            "category": "RandomUnapprovedCategory",
            "fileData": f"data:application/pdf;base64,{pdf_b64}",
            "fileName": "document.pdf",
        }
        inv_cat_resp = emp_client.post("/api/v1/hrms/documents/", invalid_cat_payload, format="json")
        self.assertEqual(inv_cat_resp.status_code, 400)
        self.assertIn("category", str(inv_cat_resp.data).lower())

        # 3. Valid PDF submission with allowed category (e.g. Identity)
        valid_payload = {
            "title": "Passport Identification",
            "category": "Identity",
            "fileName": "Devon_Passport.pdf",
            "fileData": f"data:application/pdf;base64,{pdf_b64}",
            "expiry": "2030-12-31",
            "description": "Government-issued passport copy for compliance.",
        }
        create_resp = emp_client.post("/api/v1/hrms/documents/", valid_payload, format="json")
        self.assertEqual(create_resp.status_code, 201, create_resp.data)
        doc_data = create_resp.json()
        self.assertEqual(doc_data["title"], "Passport Identification")
        self.assertEqual(doc_data["category"], "Identity")
        self.assertEqual(doc_data["fileName"], "Devon_Passport.pdf")
        # Employee identity derived from session
        self.assertEqual(doc_data["employee"], "Devon")
        self.assertEqual(doc_data["employeeId"], str(emp.id))
        self.assertEqual(doc_data["employeeCode"], emp.employee_code)
        self.assertEqual(doc_data["status"], "Valid")
        doc_id = doc_data["id"]

        # 4. Verify employee list only returns their own documents + company wide
        other_emp, other_user = self.create_employee("OtherStaff", "other@lifecycle.test")
        HrDocument.objects.create(
            client=self.tenant,
            employee=other_emp,
            title="Other Staff Secret Doc",
            category="Legal",
        )
        emp_list = emp_client.get("/api/v1/hrms/documents/")
        self.assertEqual(emp_list.status_code, 200)
        emp_items = emp_list.json()["results"] if isinstance(emp_list.json(), dict) and "results" in emp_list.json() else emp_list.json()
        emp_doc_titles = [d["title"] for d in emp_items]
        self.assertIn("Passport Identification", emp_doc_titles)
        self.assertNotIn("Other Staff Secret Doc", emp_doc_titles)

        # 5. HR Admin sees Devon's document in All Documents & Employee dossier
        admin_list = self.api.get("/api/v1/hrms/documents/")
        self.assertEqual(admin_list.status_code, 200)
        admin_items = admin_list.json()["results"] if isinstance(admin_list.json(), dict) and "results" in admin_list.json() else admin_list.json()
        admin_doc_ids = [d["id"] for d in admin_items]
        self.assertIn(doc_id, admin_doc_ids)

        # 6. Admin can filter by employeeId to view in Employee-wise dossier
        emp_filtered = self.api.get(f"/api/v1/hrms/documents/?employeeId={emp.id}")
        self.assertEqual(emp_filtered.status_code, 200)
        filtered_items = emp_filtered.json()["results"] if isinstance(emp_filtered.json(), dict) and "results" in emp_filtered.json() else emp_filtered.json()
        self.assertTrue(all(d["employeeId"] == str(emp.id) for d in filtered_items))
        self.assertIn(doc_id, [d["id"] for d in filtered_items])

        # 7. Employee cannot access other employee's document details or download URL
        other_doc = HrDocument.objects.get(title="Other Staff Secret Doc")
        # Direct GET /documents/{other_doc.id}/
        direct_resp = emp_client.get(f"/api/v1/hrms/documents/{other_doc.id}/")
        self.assertIn(direct_resp.status_code, [403, 404])

        # Direct GET /documents/{other_doc.id}/download/
        download_resp = emp_client.get(f"/api/v1/hrms/documents/{other_doc.id}/download/")
        self.assertIn(download_resp.status_code, [403, 404])

        # Employee CAN download their own document
        own_download_resp = emp_client.get(f"/api/v1/hrms/documents/{doc_id}/download/")
        self.assertEqual(own_download_resp.status_code, 200)
        self.assertIn("downloadUrl", own_download_resp.json())

        # 8. Employee cannot delete or edit other employee's document
        patch_resp = emp_client.patch(f"/api/v1/hrms/documents/{other_doc.id}/", {"title": "Hacked Title"})
        self.assertIn(patch_resp.status_code, [403, 404])
        delete_resp = emp_client.delete(f"/api/v1/hrms/documents/{other_doc.id}/")
        self.assertIn(delete_resp.status_code, [403, 404])

        # 9. Admin CAN view and download other employee's document
        admin_download = self.api.get(f"/api/v1/hrms/documents/{doc_id}/download/")
        self.assertEqual(admin_download.status_code, 200)
        self.assertIn("downloadUrl", admin_download.json())

        # 10. File size validation check (>25 MB rejected)
        oversized_b64 = base64.b64encode(b"%PDF-1.4\n" + b"A" * (26 * 1024 * 1024)).decode("utf-8")
        oversized_resp = emp_client.post(
            "/api/v1/hrms/documents/",
            {
                "title": "Oversized PDF",
                "category": "Contract",
                "fileData": f"data:application/pdf;base64,{oversized_b64}",
                "fileName": "huge.pdf",
            },
            format="json",
        )
        self.assertEqual(oversized_resp.status_code, 400)
        resp_content = str(oversized_resp.data)
        self.assertIn("fileData", resp_content)
        self.assertIn("exceeds", resp_content.lower())
