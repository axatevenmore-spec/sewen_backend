"""Administration user <-> HRMS employee: one person, two screens, one set of
shared fields. Saving either side updates the other."""
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, Role, User
from apps.accounts.permission_catalogue import seed_roles, sync_permissions
from apps.hrms.models import Department, Employee

USERS = "/api/v1/admin/users"
EMPLOYEES = "/api/v1/hrms/employees"


class UserEmployeeLinkTests(TestCase):
    def setUp(self):
        sync_permissions()
        self.tenant = Client.objects.create(slug="link", name="Link")
        seed_roles(self.tenant)
        self.admin = User.objects.create_user(
            email="admin@link.test", password="pass-12345", client=self.tenant, name="Admin",
            role=Role.objects.get(client=self.tenant, code="AD"),
        )
        self.api = self._as(self.admin)
        self.em_role = Role.objects.get(client=self.tenant, code="EM")

    def _as(self, user):
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(user)['access']}")
        return api

    def _create_user(self, **body):
        resp = self.api.post(f"{USERS}/", {"roleId": str(self.em_role.id), **body}, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        return resp.json()

    def _create_employee(self, **body):
        resp = self.api.post(f"{EMPLOYEES}/", body, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        return resp.json()

    # -- creating on either side ---------------------------------------------
    def test_new_user_gets_an_employee_record(self):
        row = self._create_user(
            name="Ravi", email="Ravi@link.test", phone="98765", department="Sales",
            location="Pune", joinedDate="2026-09-01",
        )
        employee = Employee.objects.get(pk=row["employeeRecordId"])
        self.assertEqual(row["employeeId"], employee.employee_code)
        self.assertEqual(
            (employee.name, employee.email, employee.phone, employee.department.name,
             employee.location.name, str(employee.joining_date)),
            ("Ravi", "ravi@link.test", "98765", "Sales", "Pune", "2026-09-01"),
        )

    def test_opting_out_of_the_employee_record(self):
        row = self._create_user(name="Vendor rep", email="v@link.test", createEmployee=False)
        self.assertIsNone(row["employeeRecordId"])
        self.assertFalse(Employee.objects.filter(email="v@link.test").exists())

    def test_new_user_links_the_employee_with_their_email(self):
        employee = self._create_employee(
            name="Meera", email="meera@link.test", phone="111", createUserAccount=False,
        )
        row = self._create_user(name="Meera K", email="meera@link.test")
        self.assertEqual(row["employeeRecordId"], employee["id"])
        # HR's record wins on a fresh link.
        self.assertEqual((row["name"], row["phone"]), ("Meera", "111"))
        self.assertEqual(Employee.objects.filter(email="meera@link.test").count(), 1)

    def test_new_employee_gets_a_login_with_the_hr_fields(self):
        row = self._create_employee(
            name="Asha", email="asha@link.test", department="Accounts",
            location="Mumbai", joining="2026-08-15",
        )
        self.assertEqual(row["login"]["email"], "asha@link.test")
        user = User.objects.get(pk=row["login"]["id"])
        self.assertEqual(
            (user.name, user.department, user.location, str(user.joined_date), user.employee_id),
            ("Asha", "Accounts", "Mumbai", "2026-08-15", Employee.objects.get(pk=row["id"]).pk),
        )

    # -- editing either side ----------------------------------------------------
    def test_editing_the_user_updates_the_employee(self):
        manager = self._create_user(name="Boss", email="boss@link.test")
        row = self._create_user(name="Ravi", email="ravi@link.test", department="Sales")
        resp = self.api.patch(f"{USERS}/{row['id']}/", {
            "name": "Ravi Kumar", "email": "ravi.k@link.test", "department": "Service",
            "reportingManagerId": manager["id"],
        }, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        employee = Employee.objects.get(pk=row["employeeRecordId"])
        self.assertEqual(
            (employee.name, employee.email, employee.department.name, str(employee.manager_id)),
            ("Ravi Kumar", "ravi.k@link.test", "Service", manager["employeeRecordId"]),
        )

    def test_editing_the_employee_updates_the_user(self):
        row = self._create_employee(name="Asha", email="asha@link.test", department="Accounts")
        resp = self.api.patch(f"{EMPLOYEES}/{row['id']}/", {
            "name": "Asha Rao", "phone": "222", "department": "Finance",
        }, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        user = User.objects.get(pk=row["login"]["id"])
        self.assertEqual((user.name, user.phone, user.department), ("Asha Rao", "222", "Finance"))
        self.assertTrue(Department.objects.filter(client=self.tenant, name="Finance").exists())

    def test_an_unchanged_field_never_overwrites_the_other_side(self):
        row = self._create_employee(name="Asha", email="asha@link.test", phone="111")
        User.objects.filter(pk=row["login"]["id"]).update(phone="999")
        self.api.patch(f"{EMPLOYEES}/{row['id']}/", {"name": "Asha R"}, format="json")
        self.assertEqual(User.objects.get(pk=row["login"]["id"]).phone, "999")

    def test_own_profile_edit_reaches_hr(self):
        row = self._create_employee(name="Asha", email="asha@link.test")
        me = self._as(User.objects.get(pk=row["login"]["id"]))
        self.assertEqual(me.patch("/api/v1/auth/me/", {"phone": "555"}, format="json").status_code, 200)
        self.assertEqual(Employee.objects.get(pk=row["id"]).phone, "555")

    def test_employee_leaving_deactivates_the_login(self):
        row = self._create_employee(name="Asha", email="asha@link.test")
        self.api.patch(f"{EMPLOYEES}/{row['id']}/", {"status": "Resigned"}, format="json")
        user = User.objects.get(pk=row["login"]["id"])
        self.assertEqual((user.status, user.is_active), ("Inactive", False))

    # -- linking and unlinking ---------------------------------------------------
    def test_link_by_code_unlink_and_one_login_per_employee(self):
        employee = self._create_employee(name="Kiran", email="kiran@link.test", createUserAccount=False)
        a = self._create_user(name="A", email="a@link.test", createEmployee=False)
        b = self._create_user(name="B", email="b@link.test", createEmployee=False)

        resp = self.api.patch(f"{USERS}/{a['id']}/", {"employeeId": employee["employeeCode"]}, format="json")
        self.assertEqual(resp.json()["employeeRecordId"], employee["id"])
        self.assertEqual(resp.json()["name"], "Kiran")

        resp = self.api.patch(f"{USERS}/{b['id']}/", {"employeeId": employee["id"]}, format="json")
        self.assertEqual(resp.status_code, 400, resp.content)

        resp = self.api.patch(f"{USERS}/{a['id']}/", {"employeeId": ""}, format="json")
        self.assertIsNone(resp.json()["employeeRecordId"])
        self.assertIsNone(self.api.get(f"{EMPLOYEES}/{employee['id']}/").json()["login"])

        resp = self.api.patch(f"{USERS}/{a['id']}/", {"employeeId": "EMP-NOPE"}, format="json")
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_deleting_the_employee_unlinks_the_login(self):
        row = self._create_employee(name="Asha", email="asha@link.test")
        self.assertEqual(self.api.delete(f"{EMPLOYEES}/{row['id']}/").status_code, 204)
        self.assertIsNone(User.objects.get(pk=row["login"]["id"]).employee_id)
