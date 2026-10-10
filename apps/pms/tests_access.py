"""Who sees which PMS project: everyone assigned to it, PMS access or not;
``view_pms`` alone reaches only the projects that user works on."""
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, Role, RolePermission, User
from apps.accounts.permission_catalogue import sync_permissions
from apps.pms.models import Department, Project, ProjectMember, ProjectStage, Task


class AssignedProjectAccessTests(TestCase):
    def setUp(self):
        sync_permissions()
        self.tenant = Client.objects.create(slug="access-tenant", name="Access Tenant")

        def role(code, *perms):
            r = Role.objects.create(client=self.tenant, code=code, name=code)
            for perm in perms:
                RolePermission.objects.create(role=r, permission_id=perm)
            return r

        employee = role("EM", "apply_leave", "view_pms")
        store = role("ST", "menu_inventory", "view_inventory")  # no PMS access at all
        manager = role("PM", "menu_pms", "view_pms", "create_pms_project", "assign_stage")

        def person(email, r):
            return User.objects.create_user(
                email=email, password="pass-12345", client=self.tenant, name=email, role=r,
            )

        self.pm = person("pm@access.test", manager)
        self.stage_owner = person("stage@access.test", employee)
        self.task_owner = person("task@access.test", store)
        self.member = person("member@access.test", store)
        self.unassigned = person("idle@access.test", employee)

        dept = Department.objects.create(client=self.tenant, name="Fabrication")
        self.project = Project.objects.create(
            client=self.tenant, code="PRJ-ACC-001", project_manager=self.pm, status="In Progress",
        )
        self.other = Project.objects.create(client=self.tenant, code="PRJ-ACC-002", status="In Progress")
        self.stage = ProjectStage.objects.create(
            client=self.tenant, project=self.project, name="Fabrication", sequence=1,
            department=dept, assigned_user=self.stage_owner,
        )
        self.task = Task.objects.create(
            client=self.tenant, project=self.project, stage=self.stage,
            task_name="Weld frame", assigned_user=self.task_owner,
        )
        ProjectMember.objects.create(client=self.tenant, project=self.project, user=self.member)

    def api(self, user):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(user)['access']}")
        return client

    def codes(self, user, path="/api/v1/pms/projects/"):
        resp = self.api(user).get(path)
        self.assertEqual(resp.status_code, 200, resp.content)
        return sorted(r["code"] for r in resp.json()["results"])

    def test_every_assignee_sees_the_project_and_nothing_else(self):
        for user in (self.stage_owner, self.task_owner, self.member):
            self.assertEqual(self.codes(user), ["PRJ-ACC-001"], user.email)
            self.assertEqual(self.codes(user, "/api/v1/pms/my-projects/"), ["PRJ-ACC-001"], user.email)
            detail = self.api(user).get(f"/api/v1/pms/projects/{self.project.code}/")
            self.assertEqual(detail.status_code, 200, user.email)
            self.assertEqual(self.api(user).get(f"/api/v1/pms/projects/{self.other.code}/").status_code, 404)

    def test_view_pms_alone_is_not_every_project(self):
        self.assertEqual(self.codes(self.unassigned), [])
        self.assertEqual(self.api(self.unassigned).get(f"/api/v1/pms/projects/{self.project.code}/").status_code, 404)

    def test_project_managers_see_every_project(self):
        self.assertEqual(self.codes(self.pm), ["PRJ-ACC-001", "PRJ-ACC-002"])
        self.assertEqual(self.codes(self.pm, "/api/v1/pms/my-projects/"), ["PRJ-ACC-001"])

    def test_my_tasks_without_pms_access(self):
        resp = self.api(self.task_owner).get("/api/v1/pms/my-tasks/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual([r["taskName"] for r in resp.json()["results"]], ["Weld frame"])

    def test_assignee_updates_only_their_own_task(self):
        url = f"/api/v1/pms/projects/{self.project.code}/stages/{self.stage.id}/tasks/{self.task.id}/"
        ok = self.api(self.task_owner).patch(url, {"status": "In Progress"}, format="json")
        self.assertEqual(ok.status_code, 200, ok.content)
        self.assertEqual(self.api(self.task_owner).delete(url).status_code, 403)
        self.assertEqual(self.api(self.member).patch(url, {"status": "Completed"}, format="json").status_code, 403)

    def test_customer_tracking_is_not_for_employees(self):
        # Assigned or not, an employee does not get the customer's tracking view.
        mine = self.api(self.stage_owner).get(f"/api/v1/pms/customer-tracking/{self.project.id}/")
        self.assertEqual(mine.status_code, 403)
        self.assertEqual(self.api(self.stage_owner).get("/api/v1/pms/customer-tracking/").status_code, 403)
        self.assertEqual(
            self.api(self.pm).get(f"/api/v1/pms/customer-tracking/{self.project.id}/").status_code, 200
        )
