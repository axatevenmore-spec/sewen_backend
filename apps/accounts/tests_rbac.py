"""RBAC: the permission engine, role administration, no-escalation rules,
report gating and record-level scope.

Permission (can this user use the feature?) and data scope (whose rows?) are
tested separately on purpose -- they are different rules.
"""
from datetime import date

from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, Permission, Role, RolePermission, User
from apps.accounts.permission_catalogue import all_permission_ids, seed_roles, sync_permissions
from apps.core.models import File
from apps.core.permissions import missing_permissions
from apps.crm.models import Lead, Stage
from apps.crm.models import Task as CrmTask
from apps.hrms.models import Attendance, Employee, LeaveRequest, LeaveType, SalaryAdvance

API = "/api/v1"


class RbacTestCase(TestCase):
    def setUp(self):
        sync_permissions()
        self.tenant = Client.objects.create(slug="rbac", name="RBAC Tenant")
        seed_roles(self.tenant)
        self.roles = {role.code: role for role in Role.objects.filter(client=self.tenant)}

    def user(self, email, role_code=None, **extra):
        return User.objects.create_user(
            email=email, password="pass-12345", client=self.tenant, name=email.split("@")[0],
            role=self.roles.get(role_code), **extra,
        )

    def role(self, code, *permission_ids):
        role = Role.objects.create(client=self.tenant, code=code, name=code)
        for permission_id in permission_ids:
            RolePermission.objects.create(role=role, permission_id=permission_id)
        return role

    def api(self, user=None, token=None):
        client = APIClient()
        if user is not None or token is not None:
            token = token or build_tokens(user)["access"]
            client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return client


class PermissionEngineTests(RbacTestCase):
    def test_unauthenticated_is_401(self):
        self.assertEqual(self.api().get(f"{API}/sales/invoices/").status_code, 401)

    def test_user_without_role_is_403_but_can_still_see_themselves(self):
        nobody = self.user("nobody@rbac.test")
        resp = self.api(nobody).get(f"{API}/parties/")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "NO_ROLE")
        self.assertEqual(self.api(nobody).get(f"{API}/auth/me/").status_code, 200)

    def test_missing_permission_is_403_with_the_id_as_code(self):
        employee = self.user("em@rbac.test", "EM")
        resp = self.api(employee).get(f"{API}/sales/invoices/")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "view_sales")

    def test_role_permission_grants_access(self):
        sales = self.user("sm@rbac.test", "SM")
        self.assertEqual(self.api(sales).get(f"{API}/sales/invoices/").status_code, 200)

    def test_superuser_bypasses_and_me_lists_the_whole_catalogue(self):
        root = User.objects.create_superuser(
            email="root@rbac.test", password="pass-12345", client=self.tenant, name="Root"
        )
        self.assertIsNone(root.role_id)
        self.assertEqual(self.api(root).get(f"{API}/sales/invoices/").status_code, 200)
        me = self.api(root).get(f"{API}/auth/me/").json()
        self.assertEqual(set(me["permissions"]), set(Permission.objects.values_list("id", flat=True)))

    def test_removing_a_permission_takes_effect_on_the_next_request(self):
        sales = self.user("sm@rbac.test", "SM")
        token = build_tokens(sales)["access"]  # minted while the grant exists
        self.assertEqual(self.api(token=token).get(f"{API}/sales/invoices/").status_code, 200)
        RolePermission.objects.filter(role=self.roles["SM"], permission_id="view_sales").delete()
        self.assertEqual(self.api(token=token).get(f"{API}/sales/invoices/").status_code, 403)

    def test_any_of_entries(self):
        self.assertEqual(missing_permissions({"b"}, [("a", "b")]), [])
        self.assertEqual(missing_permissions(set(), [("a", "b"), "c"]), [("a", "b"), ("c",)])

    def test_every_mapped_permission_exists_in_the_catalogue(self):
        """No view may require an id that no role can hold (was: manage_deals)."""
        from django.urls import URLPattern, URLResolver, get_resolver

        from apps.core.permissions import HasModulePermission

        catalogue = set(all_permission_ids())
        unknown = set()

        def walk(patterns):
            for pattern in patterns:
                if isinstance(pattern, URLResolver):
                    walk(pattern.url_patterns)
                elif isinstance(pattern, URLPattern):
                    view = getattr(pattern.callback, "cls", None)
                    if view is None or not issubclass(view, object):
                        continue
                    classes = getattr(view, "permission_classes", None) or []
                    if not any(issubclass(c, HasModulePermission) for c in classes):
                        continue
                    entries = list((getattr(view, "permission_map", None) or {}).values())
                    entries.append(getattr(view, "required_permissions", None) or [])
                    for required in entries:
                        for entry in required:
                            options = entry if isinstance(entry, (tuple, list)) else (entry,)
                            unknown.update(o for o in options if o not in catalogue)

        walk(get_resolver().url_patterns)
        self.assertEqual(unknown, set())


class RoleAdministrationTests(RbacTestCase):
    def setUp(self):
        super().setUp()
        self.admin = self.user("admin@rbac.test", "AD")
        self.employee = self.user("em@rbac.test", "EM")
        self.hr = self.user("hr@rbac.test", "HR")

    def test_administrator_holds_every_permission(self):
        self.assertEqual(
            self.roles["AD"].permission_ids(), set(Permission.objects.values_list("id", flat=True))
        )
        self.assertIn("manage_deals", self.roles["AD"].permission_ids())

    def test_role_detail_patch_requires_manage_roles(self):
        url = f"{API}/admin/roles/{self.roles['EM'].id}/"
        payload = {"selectedPermissions": ["apply_leave"]}
        self.assertEqual(self.api(self.employee).patch(url, payload, format="json").status_code, 403)
        self.assertEqual(self.api(self.hr).patch(url, payload, format="json").status_code, 403)
        self.assertEqual(self.api(self.admin).patch(url, payload, format="json").status_code, 200)

    def test_role_create_delete_and_catalogue_require_manage_roles(self):
        client = self.api(self.employee)
        self.assertEqual(
            client.post(f"{API}/admin/roles/", {"code": "X", "name": "X"}, format="json").status_code,
            403,
        )
        self.assertEqual(client.delete(f"{API}/admin/roles/{self.roles['EM'].id}/").status_code, 403)
        self.assertEqual(client.get(f"{API}/admin/permissions/").status_code, 403)
        self.assertEqual(self.api(self.admin).get(f"{API}/admin/permissions/").status_code, 200)

    def test_user_permission_overrides_require_manage_roles(self):
        url = f"{API}/admin/users/{self.employee.id}/permissions/"
        body = {"grant": ["manage_roles"]}
        self.assertEqual(self.api(self.hr).post(url, body, format="json").status_code, 403)
        self.assertEqual(self.api(self.admin).post(url, body, format="json").status_code, 200)

    def test_changing_role_permissions_changes_access_immediately(self):
        employee_token = build_tokens(self.employee)["access"]
        self.assertEqual(self.api(token=employee_token).get(f"{API}/sales/invoices/").status_code, 403)
        resp = self.api(self.admin).patch(
            f"{API}/admin/roles/{self.roles['EM'].id}/",
            {"selectedPermissions": ["view_sales"]},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.api(token=employee_token).get(f"{API}/sales/invoices/").status_code, 200)

    def test_assigning_a_role_gives_its_permissions(self):
        resp = self.api(self.admin).patch(
            f"{API}/admin/users/{self.employee.id}/", {"roleId": str(self.roles["SM"].id)},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("view_sales", resp.json()["permissions"])
        self.assertEqual(self.api(self.employee).get(f"{API}/sales/invoices/").status_code, 200)

    # -- no escalation ----------------------------------------------------------
    def test_staff_editor_cannot_grant_themselves_administrator(self):
        resp = self.api(self.hr).patch(
            f"{API}/admin/users/{self.hr.id}/", {"roleId": str(self.roles["AD"].id)}, format="json"
        )
        self.assertEqual(resp.status_code, 403)
        self.hr.refresh_from_db()
        self.assertEqual(self.hr.role_id, self.roles["HR"].id)

    def test_staff_editor_can_assign_a_role_within_their_own_permissions(self):
        narrow = self.role("VIEWER", "view_staff")
        resp = self.api(self.hr).patch(
            f"{API}/admin/users/{self.employee.id}/", {"roleId": str(narrow.id)}, format="json"
        )
        self.assertEqual(resp.status_code, 200)

    def test_staff_editor_cannot_touch_an_administrator_account(self):
        client = self.api(self.hr)
        url = f"{API}/admin/users/{self.admin.id}/"
        self.assertEqual(client.patch(url, {"password": "Taken-over-123"}, format="json").status_code, 403)
        self.assertEqual(client.patch(url, {"name": "Renamed"}, format="json").status_code, 403)
        self.assertEqual(client.post(f"{url}deactivate/").status_code, 403)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.check_password("pass-12345"))

    def test_setting_another_users_password_needs_reset_permission(self):
        url = f"{API}/admin/users/{self.employee.id}/"
        resp = self.api(self.hr).patch(url, {"password": "New-pass-123"}, format="json")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "reset_staff_password")
        self.assertEqual(
            self.api(self.admin).patch(url, {"password": "New-pass-123"}, format="json").status_code,
            200,
        )

    def test_role_from_another_tenant_is_rejected(self):
        other = Client.objects.create(slug="other", name="Other")
        foreign = Role.objects.create(client=other, code="AD", name="Their admin")
        resp = self.api(self.admin).patch(
            f"{API}/admin/users/{self.employee.id}/", {"roleId": str(foreign.id)}, format="json"
        )
        self.assertEqual(resp.status_code, 400)
        self.employee.refresh_from_db()
        self.assertEqual(self.employee.role_id, self.roles["EM"].id)

    def test_user_stats_require_view_staff(self):
        self.assertEqual(self.api(self.employee).get(f"{API}/admin/users/stats/").status_code, 403)
        self.assertEqual(self.api(self.hr).get(f"{API}/admin/users/stats/").status_code, 200)


class EndpointMappingTests(RbacTestCase):
    def test_reports_follow_their_module_permission(self):
        employee = self.user("em@rbac.test", "EM")
        accountant = self.user("ac@rbac.test", "AC")
        sales = self.user("sm@rbac.test", "SM")
        for key in ("profit-and-loss", "balance-sheet", "hrms-payroll-summary", "sales-register"):
            self.assertEqual(
                self.api(employee).get(f"{API}/reports/{key}/").status_code, 403, key
            )
        self.assertEqual(self.api(accountant).get(f"{API}/reports/profit-and-loss/").status_code, 200)
        # export_excel alone does not open a report the role cannot read.
        resp = self.api(sales).post(
            f"{API}/reports/hrms-payroll-summary/export/", {"format": "csv"}, format="json"
        )
        self.assertEqual(resp.status_code, 403)
        keys = {row["key"] for row in self.api(employee).get(f"{API}/reports/").json()["results"]}
        self.assertNotIn("profit-and-loss", keys)

    def test_deals_are_writable_by_administrator(self):
        admin = self.user("admin@rbac.test", "AD")
        viewer = self.user("viewer@rbac.test")
        viewer.role = self.role("LV", "view_lead")
        viewer.save()
        resp = self.api(viewer).post(f"{API}/crm/deals/", {"title": "X"}, format="json")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "manage_deals")
        resp = self.api(admin).post(f"{API}/crm/deals/", {"title": "X"}, format="json")
        self.assertNotEqual(resp.status_code, 403)

    def test_lead_bulk_delete_requires_delete_lead(self):
        viewer = self.user("viewer@rbac.test")
        viewer.role = self.role("LV", "view_lead")
        viewer.save()
        resp = self.api(viewer).post(f"{API}/crm/leads/bulk-delete/", {"ids": ["x"]}, format="json")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "delete_lead")

    def test_parties_are_readable_but_maintained_by_trading_roles(self):
        employee = self.user("em@rbac.test", "EM")
        purchase = self.user("pu@rbac.test", "PU")
        body = {"name": "Acme", "type": "Customer"}
        self.assertEqual(self.api(employee).get(f"{API}/parties/").status_code, 200)
        self.assertEqual(self.api(employee).post(f"{API}/parties/", body, format="json").status_code, 403)
        self.assertNotEqual(
            self.api(purchase).post(f"{API}/parties/", body, format="json").status_code, 403
        )

    def test_writes_are_not_gated_by_view_permissions(self):
        accountant = self.user("ac@rbac.test", "AC")  # view_sales, no create_quotation
        resp = self.api(accountant).post(f"{API}/sales/estimates/", {}, format="json")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["code"], "create_quotation")


class DataScopeTests(RbacTestCase):
    """Permission says the screen is usable; scope says whose rows it shows."""

    def setUp(self):
        super().setUp()
        self.alice_emp = Employee.objects.create(
            client=self.tenant, employee_code="E1", name="Alice", joining_date=date(2025, 1, 1)
        )
        self.bob_emp = Employee.objects.create(
            client=self.tenant, employee_code="E2", name="Bob", joining_date=date(2025, 1, 1)
        )
        self.alice = self.user("alice@rbac.test", "EM", employee=self.alice_emp)
        self.bob = self.user("bob@rbac.test", "EM", employee=self.bob_emp)
        self.hr = self.user("hr@rbac.test", "HR")
        self.leave_type = LeaveType.objects.create(client=self.tenant, name="Casual")
        for emp in (self.alice_emp, self.bob_emp):
            LeaveRequest.objects.create(
                client=self.tenant, employee=emp, leave_type=self.leave_type,
                from_date=date(2026, 10, 1), to_date=date(2026, 10, 2),
            )
            SalaryAdvance.objects.create(
                client=self.tenant, employee=emp, amount=1000, issued_on=date(2026, 9, 1)
            )

    def ids(self, resp):
        self.assertEqual(resp.status_code, 200, resp.content)
        return {row["employeeId"] for row in resp.json()["results"]}

    def test_employee_sees_only_their_own_leave_and_advances(self):
        own = {str(self.alice_emp.id)}
        self.assertEqual(self.ids(self.api(self.alice).get(f"{API}/hrms/leave/")), own)
        self.assertEqual(self.ids(self.api(self.alice).get(f"{API}/hrms/payroll/advances/")), own)
        bob_leave = LeaveRequest.objects.get(employee=self.bob_emp)
        self.assertEqual(
            self.api(self.alice).get(f"{API}/hrms/leave/{bob_leave.id}/").status_code, 404
        )

    def test_approver_sees_the_team(self):
        everyone = {str(self.alice_emp.id), str(self.bob_emp.id)}
        self.assertEqual(self.ids(self.api(self.hr).get(f"{API}/hrms/leave/")), everyone)
        self.assertEqual(self.ids(self.api(self.hr).get(f"{API}/hrms/payroll/advances/")), everyone)

    def test_employee_cannot_file_leave_for_someone_else(self):
        body = {
            "employeeId": str(self.bob_emp.id), "leaveTypeId": str(self.leave_type.id),
            "fromDate": "2026-11-01", "toDate": "2026-11-01",
        }
        self.assertEqual(self.api(self.alice).post(f"{API}/hrms/leave/", body, format="json").status_code, 403)

    def test_employee_marks_only_their_own_attendance(self):
        def mark(emp):
            return self.api(self.alice).post(
                f"{API}/hrms/attendance/",
                {"employeeId": str(emp.id), "date": "2026-09-24", "status": "Present"},
                format="json",
            )

        self.assertEqual(mark(self.bob_emp).status_code, 403)
        self.assertEqual(mark(self.alice_emp).status_code, 201)
        self.assertFalse(Attendance.objects.filter(employee=self.bob_emp).exists())
        self.assertEqual(
            self.api(self.alice).post(f"{API}/hrms/attendance/bulk/", {}, format="json").status_code,
            403,
        )

    def test_employee_payroll_totals_are_their_own(self):
        resp = self.api(self.alice).get(f"{API}/hrms/payroll/summary/")
        self.assertEqual(resp.status_code, 403)

    def test_crm_tasks_are_scoped_to_the_assignee(self):
        stage = Stage.objects.create(client=self.tenant, name="New", sequence=1)
        lead = Lead.objects.create(client=self.tenant, lead_number="L-1", name="Lead", stage=stage)
        mine = CrmTask.objects.create(
            client=self.tenant, title="Mine", assignee=self.alice, lead=lead
        )
        CrmTask.objects.create(client=self.tenant, title="Bob's", assignee=self.bob, lead=lead)
        rows = self.api(self.alice).get(f"{API}/crm/tasks/").json()["results"]
        self.assertEqual([row["id"] for row in rows], [str(mine.id)])
        manager = self.user("sm@rbac.test", "SM")  # assign_task: sees the team
        self.assertEqual(len(self.api(manager).get(f"{API}/crm/tasks/").json()["results"]), 2)

    def test_file_list_shows_only_own_uploads(self):
        for owner in (self.alice, self.bob):
            File.objects.create(
                client=self.tenant, scope="attachment", storage_key=f"k/{owner.email}",
                file_name=f"{owner.email}.pdf", uploaded_by=owner, status="committed",
            )
        rows = self.api(self.alice).get(f"{API}/files/").json()["results"]
        self.assertEqual([row["fileName"] for row in rows], ["alice@rbac.test.pdf"])
