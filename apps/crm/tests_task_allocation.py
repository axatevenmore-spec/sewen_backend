"""Task Allocation: internal work handed to a team member, with a server-written
audit trail. Managers allocate and edit; an assignee sees their own work and
can only move its status along."""
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, Role, User
from apps.accounts.permission_catalogue import seed_roles, sync_permissions
from apps.crm.models import TaskAllocation

API = "/api/v1/crm/task-allocations"


class TaskAllocationTests(TestCase):
    def setUp(self):
        sync_permissions()
        self.tenant = Client.objects.create(slug="alloc", name="Alloc")
        seed_roles(self.tenant)
        self.manager = self._user("sm@alloc.test", "Sam Manager", "SM")
        self.alice = self._user("alice@alloc.test", "Alice", "EM")
        self.bob = self._user("bob@alloc.test", "Bob", "EM")

    def _user(self, email, name, role_code):
        return User.objects.create_user(
            email=email, password="pass-12345", client=self.tenant, name=name,
            role=Role.objects.get(client=self.tenant, code=role_code),
        )

    def _as(self, user):
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(user)['access']}")
        return api

    def _assign(self, **extra):
        body = {
            "title": "Market analysis", "assigneeId": str(self.alice.id),
            "priority": "High", "department": "Sales", **extra,
        }
        resp = self._as(self.manager).post(f"{API}/", body, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        return resp.json()

    def test_manager_assigns_and_the_server_writes_the_audit(self):
        row = self._assign(deadline="2026-10-05T10:30:00Z")
        self.assertEqual((row["assignee"], row["assignedBy"], row["status"]),
                         ("Alice", "Sam Manager", "Pending"))
        self.assertEqual([a["text"] for a in row["audit"]], ["Sam Manager assigned this to Alice"])

        resp = self._as(self.manager).patch(
            f"{API}/{row['id']}/", {"assigneeId": str(self.bob.id), "note": "Alice on leave"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["assignee"], "Bob")
        self.assertEqual(resp.json()["audit"][-1]["text"],
                         "Sam Manager reassigned this to Bob — Alice on leave")

    def test_assignee_sees_only_their_work_and_may_only_change_status(self):
        row = self._assign()
        alice, bob = self._as(self.alice), self._as(self.bob)

        self.assertEqual(len(alice.get(f"{API}/").json()["results"]), 1)
        self.assertEqual(len(bob.get(f"{API}/").json()["results"]), 0)
        self.assertEqual(bob.patch(f"{API}/{row['id']}/", {"status": "Completed"},
                                   format="json").status_code, 404)

        resp = alice.patch(f"{API}/{row['id']}/", {"status": "In Progress", "note": "Started"},
                           format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["audit"][-1]["text"], "Alice moved this to In Progress — Started")

        resp = alice.patch(f"{API}/{row['id']}/", {"priority": "Low"}, format="json")
        self.assertEqual(resp.status_code, 403, resp.content)
        self.assertEqual(alice.post(f"{API}/", {"title": "Self-assigned"}, format="json").status_code, 403)
        self.assertEqual(alice.delete(f"{API}/{row['id']}/").status_code, 403)

    def test_delete(self):
        row = self._assign()
        self.assertEqual(self._as(self.manager).delete(f"{API}/{row['id']}/").status_code, 204)
        self.assertFalse(TaskAllocation.objects.filter(pk=row["id"], deleted_at__isnull=True).exists())
