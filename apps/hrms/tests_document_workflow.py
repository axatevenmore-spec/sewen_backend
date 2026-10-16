"""Tests verifying the complete employee-to-admin document workflow (Scenarios 1-10)."""
import base64
from datetime import date
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, Role, User
from apps.accounts.permission_catalogue import seed_roles, sync_permissions
from apps.core.models import File
from apps.core.files import local_path, read_bytes
from apps.hrms.models import Department, Designation, Employee, HrDocument


class EmployeeDocumentWorkflowVerificationTests(TestCase):
    def setUp(self):
        sync_permissions()
        self.tenant = Client.objects.create(slug="docflow-corp", name="Docflow Corp")
        seed_roles(self.tenant)

        # Admin user with full HR Admin roles
        self.admin = User.objects.create_user(
            email="admin@docflow.test",
            password="pass-admin-123",
            client=self.tenant,
            name="HR Administrator",
            role=Role.objects.get(client=self.tenant, code="AD"),
        )
        self.admin_client = APIClient()
        self.admin_client.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.admin)['access']}")

        self.dept = Department.objects.create(client=self.tenant, name="Operations")
        self.desig = Designation.objects.create(client=self.tenant, name="Specialist")

        # Employee A
        self.emp_a, self.user_a = self._create_employee("Employee A", "emp_a@docflow.test")
        self.client_a = APIClient()
        self.client_a.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.user_a)['access']}")

        # Employee B
        self.emp_b, self.user_b = self._create_employee("Employee B", "emp_b@docflow.test")
        self.client_b = APIClient()
        self.client_b.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.user_b)['access']}")

        # Sample valid PDF base64
        self.sample_pdf_bytes = b"%PDF-1.4\n1 0 obj\n<< /Title (Test PDF) >>\nendobj\ntrailer\n<<>>\n%%EOF"
        self.sample_pdf_b64 = base64.b64encode(self.sample_pdf_bytes).decode("utf-8")

    def _create_employee(self, name, email):
        emp = Employee.objects.create(
            client=self.tenant,
            employee_code=f"EMP-{name.replace(' ', '').upper()}",
            name=name,
            email=email,
            joining_date="2026-01-01",
            status="Active",
            department=self.dept,
            designation=self.desig,
        )
        user = User.objects.create_user(
            email=email,
            password="pass-employee-123",
            client=self.tenant,
            name=name,
            role=Role.objects.get(client=self.tenant, code="EM"),
            employee=emp,
        )
        return emp, user

    def test_complete_scenarios_1_to_10(self):
        # =========================================================================
        # Scenario 1: Employee A uploads an Identity document.
        # It appears in Employee A's portal, the admin's All Documents tab,
        # Employee-wise Documents under Employee A, and the Identity filter.
        # =========================================================================
        resp_a1 = self.client_a.post(
            "/api/v1/hrms/documents/",
            {
                "title": "Passport ID Copy",
                "category": "Identity",
                "fileName": "passport_emp_a.pdf",
                "fileData": f"data:application/pdf;base64,{self.sample_pdf_b64}",
                "description": "Employee A passport scan for identity verification.",
            },
            format="json",
        )
        self.assertEqual(resp_a1.status_code, 201, resp_a1.data)
        doc_a1_id = resp_a1.json()["id"]

        # Check Employee A portal view
        emp_a_docs = self.client_a.get("/api/v1/hrms/documents/").json()
        a_list = emp_a_docs.get("results", emp_a_docs) if isinstance(emp_a_docs, dict) else emp_a_docs
        self.assertTrue(any(d["id"] == doc_a1_id for d in a_list))
        self.assertEqual(next(d for d in a_list if d["id"] == doc_a1_id)["category"], "Identity")

        # Check Admin All Documents tab
        admin_all = self.admin_client.get("/api/v1/hrms/documents/").json()
        admin_list = admin_all.get("results", admin_all) if isinstance(admin_all, dict) else admin_all
        self.assertTrue(any(d["id"] == doc_a1_id for d in admin_list))

        # Check Admin Employee-wise filter under Employee A
        admin_emp_a = self.admin_client.get(f"/api/v1/hrms/documents/?employeeId={self.emp_a.id}").json()
        emp_a_filtered = admin_emp_a.get("results", admin_emp_a) if isinstance(admin_emp_a, dict) else admin_emp_a
        self.assertTrue(any(d["id"] == doc_a1_id for d in emp_a_filtered))

        # Check Identity category filter
        admin_cat_identity = self.admin_client.get("/api/v1/hrms/documents/?category=Identity").json()
        identity_filtered = admin_cat_identity.get("results", admin_cat_identity) if isinstance(admin_cat_identity, dict) else admin_cat_identity
        self.assertTrue(any(d["id"] == doc_a1_id for d in identity_filtered))

        # =========================================================================
        # Scenario 2: Employee A uploads a Contract document.
        # It appears under Contract and All, but not under Identity.
        # =========================================================================
        resp_a2 = self.client_a.post(
            "/api/v1/hrms/documents/",
            {
                "title": "Employment Agreement 2026",
                "category": "Contract",
                "fileName": "contract_emp_a.pdf",
                "fileData": f"data:application/pdf;base64,{self.sample_pdf_b64}",
                "description": "Signed employment contract.",
            },
            format="json",
        )
        self.assertEqual(resp_a2.status_code, 201, resp_a2.data)
        doc_a2_id = resp_a2.json()["id"]

        # Check All Documents
        admin_all_2 = self.admin_client.get("/api/v1/hrms/documents/").json()
        all_list_2 = admin_all_2.get("results", admin_all_2) if isinstance(admin_all_2, dict) else admin_all_2
        self.assertTrue(any(d["id"] == doc_a2_id for d in all_list_2))

        # Check Contract filter contains it
        admin_cat_contract = self.admin_client.get("/api/v1/hrms/documents/?category=Contract").json()
        contract_filtered = admin_cat_contract.get("results", admin_cat_contract) if isinstance(admin_cat_contract, dict) else admin_cat_contract
        self.assertTrue(any(d["id"] == doc_a2_id for d in contract_filtered))

        # Check Identity filter DOES NOT contain it
        admin_cat_id_2 = self.admin_client.get("/api/v1/hrms/documents/?category=Identity").json()
        id_filtered_2 = admin_cat_id_2.get("results", admin_cat_id_2) if isinstance(admin_cat_id_2, dict) else admin_cat_id_2
        self.assertFalse(any(d["id"] == doc_a2_id for d in id_filtered_2))

        # =========================================================================
        # Scenario 3: Employee B uploads an Educational document.
        # It appears in Employee B's records and the authorized admin's views
        # without being incorrectly assigned to Employee A.
        # =========================================================================
        resp_b1 = self.client_b.post(
            "/api/v1/hrms/documents/",
            {
                "title": "Master Degree Certificate",
                "category": "Educational",
                "fileName": "degree_emp_b.pdf",
                "fileData": f"data:application/pdf;base64,{self.sample_pdf_b64}",
                "description": "Educational certificate.",
            },
            format="json",
        )
        self.assertEqual(resp_b1.status_code, 201, resp_b1.data)
        doc_b1_id = resp_b1.json()["id"]

        # Verify assigned to Employee B and NOT Employee A
        doc_b_obj = HrDocument.objects.get(id=doc_b1_id)
        self.assertEqual(doc_b_obj.employee_id, self.emp_b.id)
        self.assertNotEqual(doc_b_obj.employee_id, self.emp_a.id)

        # Check Employee B sees it in their records
        emp_b_docs = self.client_b.get("/api/v1/hrms/documents/").json()
        b_list = emp_b_docs.get("results", emp_b_docs) if isinstance(emp_b_docs, dict) else emp_b_docs
        self.assertTrue(any(d["id"] == doc_b1_id for d in b_list))

        # Check Admin sees it in Employee B dossier
        admin_emp_b = self.admin_client.get(f"/api/v1/hrms/documents/?employeeId={self.emp_b.id}").json()
        emp_b_filtered = admin_emp_b.get("results", admin_emp_b) if isinstance(admin_emp_b, dict) else admin_emp_b
        self.assertTrue(any(d["id"] == doc_b1_id for d in emp_b_filtered))

        # Check Employee A dossier DOES NOT include Employee B's document
        admin_emp_a_check = self.admin_client.get(f"/api/v1/hrms/documents/?employeeId={self.emp_a.id}").json()
        a_filtered_check = admin_emp_a_check.get("results", admin_emp_a_check) if isinstance(admin_emp_a_check, dict) else admin_emp_a_check
        self.assertFalse(any(d["id"] == doc_b1_id for d in a_filtered_check))

        # =========================================================================
        # Scenario 4: Employee A cannot view or download Employee B's documents.
        # =========================================================================
        # 1. Listing isolation
        emp_a_list = self.client_a.get("/api/v1/hrms/documents/").json()
        a_visible = emp_a_list.get("results", emp_a_list) if isinstance(emp_a_list, dict) else emp_a_list
        self.assertFalse(any(d["id"] == doc_b1_id for d in a_visible))

        # 2. Direct ID access isolation
        direct_get_b = self.client_a.get(f"/api/v1/hrms/documents/{doc_b1_id}/")
        self.assertIn(direct_get_b.status_code, [403, 404])

        # 3. Direct download isolation
        direct_dl_b = self.client_a.get(f"/api/v1/hrms/documents/{doc_b1_id}/download/")
        self.assertIn(direct_dl_b.status_code, [403, 404])

        # =========================================================================
        # Scenario 5: The admin can view employee-submitted documents
        # according to existing permissions.
        # =========================================================================
        admin_doc_a = self.admin_client.get(f"/api/v1/hrms/documents/{doc_a1_id}/")
        self.assertEqual(admin_doc_a.status_code, 200)
        self.assertEqual(admin_doc_a.json()["employeeId"], str(self.emp_a.id))

        admin_dl_a = self.admin_client.get(f"/api/v1/hrms/documents/{doc_a1_id}/download/")
        self.assertEqual(admin_dl_a.status_code, 200)
        self.assertIn("downloadUrl", admin_dl_a.json())

        admin_doc_b = self.admin_client.get(f"/api/v1/hrms/documents/{doc_b1_id}/")
        self.assertEqual(admin_doc_b.status_code, 200)
        self.assertEqual(admin_doc_b.json()["employeeId"], str(self.emp_b.id))

        # =========================================================================
        # Scenario 6: Existing administrator-uploaded PDFs continue to work.
        # =========================================================================
        admin_upload_resp = self.admin_client.post(
            "/api/v1/hrms/documents/",
            {
                "title": "Corporate Code of Ethics 2026",
                "category": "Compliance",
                "fileName": "code_of_ethics.pdf",
                "fileData": f"data:application/pdf;base64,{self.sample_pdf_b64}",
                "employeeId": "ALL",
                "description": "Company-wide compliance handbook.",
            },
            format="json",
        )
        self.assertEqual(admin_upload_resp.status_code, 201, admin_upload_resp.data)
        admin_doc_id = admin_upload_resp.json()["id"]

        admin_doc_obj = HrDocument.objects.get(id=admin_doc_id)
        self.assertIsNone(admin_doc_obj.employee)
        self.assertEqual(admin_doc_obj.category, "Compliance")

        # Company-wide doc visible to employees and downloadable
        emp_a_all = self.client_a.get("/api/v1/hrms/documents/").json()
        a_all_list = emp_a_all.get("results", emp_a_all) if isinstance(emp_a_all, dict) else emp_a_all
        self.assertTrue(any(d["id"] == admin_doc_id for d in a_all_list))

        emp_a_dl = self.client_a.get(f"/api/v1/hrms/documents/{admin_doc_id}/download/")
        self.assertEqual(emp_a_dl.status_code, 200)
        self.assertIn("downloadUrl", emp_a_dl.json())

        # =========================================================================
        # Scenario 7: Reloading the page preserves document records and categories.
        # =========================================================================
        # Simulate clean reload / re-hydration from database
        refreshed_docs = HrDocument.objects.filter(client=self.tenant).order_by("created_at")
        self.assertEqual(refreshed_docs.count(), 4)

        doc_a1_refreshed = HrDocument.objects.get(id=doc_a1_id)
        self.assertEqual(doc_a1_refreshed.category, "Identity")
        self.assertEqual(doc_a1_refreshed.title, "Passport ID Copy")

        doc_a2_refreshed = HrDocument.objects.get(id=doc_a2_id)
        self.assertEqual(doc_a2_refreshed.category, "Contract")

        doc_b1_refreshed = HrDocument.objects.get(id=doc_b1_id)
        self.assertEqual(doc_b1_refreshed.category, "Educational")

        admin_doc_refreshed = HrDocument.objects.get(id=admin_doc_id)
        self.assertEqual(admin_doc_refreshed.category, "Compliance")

        # =========================================================================
        # Scenario 8: Dashboard totals and category counts remain consistent
        # with the underlying records.
        # =========================================================================
        total_count = HrDocument.objects.filter(client=self.tenant).count()
        identity_count = HrDocument.objects.filter(client=self.tenant, category="Identity").count()
        contract_count = HrDocument.objects.filter(client=self.tenant, category="Contract").count()
        educational_count = HrDocument.objects.filter(client=self.tenant, category="Educational").count()
        compliance_count = HrDocument.objects.filter(client=self.tenant, category="Compliance").count()

        self.assertEqual(total_count, 4)
        self.assertEqual(identity_count, 1)
        self.assertEqual(contract_count, 1)
        self.assertEqual(educational_count, 1)
        self.assertEqual(compliance_count, 1)
        # Sum of category counts equals total count
        self.assertEqual(identity_count + contract_count + educational_count + compliance_count, total_count)

        # =========================================================================
        # Scenario 9: Failed uploads display an error and do not create
        # incomplete or duplicate records.
        # =========================================================================
        before_count = HrDocument.objects.filter(client=self.tenant).count()

        # Non-PDF file attempt
        fail_non_pdf = self.client_a.post(
            "/api/v1/hrms/documents/",
            {
                "title": "Malicious Script",
                "category": "Identity",
                "fileName": "script.exe",
                "fileData": f"data:application/octet-stream;base64,{base64.b64encode(b'MZ...not_a_pdf').decode('utf-8')}",
            },
            format="json",
        )
        self.assertEqual(fail_non_pdf.status_code, 400)
        self.assertIn("fileData", str(fail_non_pdf.data))

        # Invalid category attempt
        fail_bad_cat = self.client_a.post(
            "/api/v1/hrms/documents/",
            {
                "title": "Unknown Cat Document",
                "category": "InvalidCategoryNameXYZ",
                "fileName": "test.pdf",
                "fileData": f"data:application/pdf;base64,{self.sample_pdf_b64}",
            },
            format="json",
        )
        self.assertEqual(fail_bad_cat.status_code, 400)
        self.assertIn("category", str(fail_bad_cat.data).lower())

        # Assert no phantom or incomplete records created
        after_count = HrDocument.objects.filter(client=self.tenant).count()
        self.assertEqual(before_count, after_count)

        # =========================================================================
        # Scenario 10: Verify frontend-to-API-to-backend-to-database persistence
        # and file access.
        # =========================================================================
        doc_record = HrDocument.objects.get(id=doc_a1_id)
        self.assertIsNotNone(doc_record.file)
        self.assertEqual(doc_record.file.content_type, "application/pdf")
        self.assertEqual(doc_record.file.file_name, "passport_emp_a.pdf")
        self.assertEqual(doc_record.file.status, "Committed")

        # Verify disk file persistence and read access
        disk_path = local_path(doc_record.file.storage_key)
        self.assertTrue(disk_path.exists())
        stored_bytes = read_bytes(doc_record.file.storage_key)
        self.assertTrue(stored_bytes.startswith(b"%PDF-1.4"))
        self.assertEqual(stored_bytes, self.sample_pdf_bytes)
