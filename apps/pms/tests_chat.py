"""Messenger: team chats come from stage/task assignments, access follows them,
and messages, read state, mentions, attachments and search stay scoped."""
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, Role, RolePermission, User
from apps.accounts.permission_catalogue import sync_permissions
from apps.core.models import AuditLog, File, Notification
from apps.pms.models import Conversation, Department, Project, ProjectStage, Task


class MessengerTests(TestCase):
    def setUp(self):
        sync_permissions()
        self.tenant = Client.objects.create(slug="chat-tenant", name="Chat Tenant")
        viewer = Role.objects.create(client=self.tenant, code="PV", name="PMS Viewer")
        RolePermission.objects.create(role=viewer, permission_id="view_pms")

        def person(email, name, **extra):
            return User.objects.create_user(
                email=email, password="pass-12345", client=self.tenant, name=name,
                role=viewer, **extra,
            )

        self.pm = person("pm@chat.test", "Priya PM")
        self.designer = person("rahul@chat.test", "Rahul")
        self.qc = person("dhruv@chat.test", "Dhruv")
        self.outsider = person("out@chat.test", "Olive Outsider")
        self.admin = User.objects.create_superuser(
            email="admin@chat.test", password="pass-12345", client=self.tenant, name="Admin",
        )

        self.design = Department.objects.create(client=self.tenant, name="Design")
        self.quality = Department.objects.create(client=self.tenant, name="QC")
        self.project = Project.objects.create(
            client=self.tenant, code="PRJ-CHAT-001", project_manager=self.pm,
            customer_name="ABC Furniture", status="In Progress",
        )
        self.design_stage = ProjectStage.objects.create(
            client=self.tenant, project=self.project, name="Design", sequence=1,
            department=self.design, assigned_user=self.designer,
        )
        self.qc_stage = ProjectStage.objects.create(
            client=self.tenant, project=self.project, name="Quality Check", sequence=2,
            department=self.quality,
        )
        Task.objects.create(
            client=self.tenant, project=self.project, stage=self.qc_stage,
            task_name="Inspect joints", assigned_user=self.qc,
        )
        self.base = f"/api/v1/pms/projects/{self.project.id}"

    # -- helpers ---------------------------------------------------------------
    def api(self, user):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(user)['access']}")
        return client

    def conversations(self, user):
        resp = self.api(user).get(f"{self.base}/conversations/")
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    def conv_id(self, user, title):
        rows = self.conversations(user)["results"]
        return next(r["id"] for r in rows if r["title"] == title)

    def send(self, user, conv_id, text="", expect=201, **extra):
        resp = self.api(user).post(
            f"{self.base}/conversations/{conv_id}/messages/",
            {"text": text, **extra}, format="json",
        )
        self.assertEqual(resp.status_code, expect, resp.content)
        return resp.json()

    def committed_file(self, owner, name="design-v3.pdf"):
        return File.objects.create(
            client=self.tenant, storage_key=f"chat-tests/{name}-{owner.id}", file_name=name,
            content_type="application/pdf", file_size=2048, status="committed",
            uploaded_by=owner,
        )

    # -- structure and access -------------------------------------------------
    def test_project_and_team_chats_created_once(self):
        titles = [r["title"] for r in self.conversations(self.pm)["results"]]
        self.assertEqual(titles, ["Project Chat", "Design Team", "QC Team"])
        self.conversations(self.pm)
        self.conversations(self.designer)
        self.assertEqual(Conversation.objects.filter(project=self.project).count(), 3)

    def test_team_members_come_from_assignments(self):
        rows = {r["title"]: r for r in self.conversations(self.pm)["results"]}
        design = {m["name"]: m["role"] for m in rows["Design Team"]["members"]}
        self.assertEqual(design, {"Priya PM": "Project Manager", "Rahul": "Member"})
        qc = [m["name"] for m in rows["QC Team"]["members"]]
        self.assertEqual(qc, ["Priya PM", "Dhruv"])

    def test_visibility_follows_team_membership(self):
        designer = [r["title"] for r in self.conversations(self.designer)["results"]]
        self.assertEqual(designer, ["Project Chat", "Design Team"])
        admin = [r["title"] for r in self.conversations(self.admin)["results"]]
        self.assertEqual(admin, ["Project Chat", "Design Team", "QC Team"])
        body = self.conversations(self.outsider)
        self.assertEqual(body["results"], [])
        self.assertFalse(body["aggregates"]["isParticipant"])

        qc_chat = self.conv_id(self.pm, "QC Team")
        resp = self.api(self.designer).get(f"{self.base}/conversations/{qc_chat}/messages/")
        self.assertEqual(resp.status_code, 404)
        self.send(self.outsider, qc_chat, "hello", expect=404)

    def test_reassigning_a_stage_moves_chat_access(self):
        design_chat = self.conv_id(self.pm, "Design Team")
        self.design_stage.assigned_user = self.qc
        self.design_stage.save()
        self.assertNotIn(
            "Design Team", [r["title"] for r in self.conversations(self.designer)["results"]]
        )
        self.assertIn("Design Team", [r["title"] for r in self.conversations(self.qc)["results"]])
        self.send(self.designer, design_chat, "still here?", expect=404)

    def test_assign_stage_creates_the_team_chat(self):
        packing = Department.objects.create(client=self.tenant, name="Packaging")
        stage = ProjectStage.objects.create(
            client=self.tenant, project=self.project, name="Packing", sequence=3,
        )
        resp = self.api(self.admin).post(
            f"{self.base}/stages/{stage.id}/assign/",
            {"departmentId": str(packing.id), "assignedUserId": str(self.qc.id)},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(
            Conversation.objects.filter(project=self.project, kind="Team", department=packing).exists()
        )

    # -- messages, unread, isolation ------------------------------------------
    def test_send_persist_unread_and_isolation(self):
        design_chat = self.conv_id(self.designer, "Design Team")
        sent = self.send(self.designer, design_chat, "Client revision uploaded")
        self.assertEqual(sent["sender"]["name"], "Rahul")
        self.assertTrue(sent["createdAt"].endswith("Z"))

        listed = self.api(self.pm).get(f"{self.base}/conversations/{design_chat}/messages/").json()
        self.assertEqual([m["text"] for m in listed["results"]], ["Client revision uploaded"])

        pm_rows = {r["title"]: r for r in self.conversations(self.pm)["results"]}
        self.assertEqual(pm_rows["Design Team"]["unreadCount"], 1)
        self.assertEqual(pm_rows["Design Team"]["lastMessage"]["senderName"], "Rahul")
        self.assertEqual(pm_rows["QC Team"]["unreadCount"], 0)
        self.assertEqual(self.conversations(self.pm)["aggregates"]["totalUnread"], 1)
        # The sender has nothing unread in their own chat.
        mine = {r["title"]: r for r in self.conversations(self.designer)["results"]}
        self.assertEqual(mine["Design Team"]["unreadCount"], 0)

        resp = self.api(self.pm).post(f"{self.base}/conversations/{design_chat}/read/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(self.conversations(self.pm)["aggregates"]["totalUnread"], 0)

        qc_chat = self.conv_id(self.pm, "QC Team")
        qc_msgs = self.api(self.pm).get(f"{self.base}/conversations/{qc_chat}/messages/").json()
        self.assertEqual(qc_msgs["results"], [])

    def test_reply_and_stage_context(self):
        design_chat = self.conv_id(self.designer, "Design Team")
        first = self.send(self.designer, design_chat, "Client requested another revision.",
                          stageId=str(self.design_stage.id))
        self.assertEqual(first["stageName"], "Design")
        reply = self.send(self.pm, design_chat, "I will update the PDF.", replyToId=first["id"])
        self.assertEqual(reply["replyTo"]["senderName"], "Rahul")

        other = Project.objects.create(client=self.tenant, code="PRJ-CHAT-002")
        foreign = ProjectStage.objects.create(
            client=self.tenant, project=other, name="Design", sequence=1,
        )
        self.send(self.designer, design_chat, "wrong stage", expect=400, stageId=str(foreign.id))
        self.send(self.designer, design_chat, "", expect=400)

    def test_edit_and_delete_own_messages_only(self):
        design_chat = self.conv_id(self.designer, "Design Team")
        msg = self.send(self.designer, design_chat, "Draft v1 ready")
        url = f"{self.base}/conversations/{design_chat}/messages/{msg['id']}/"

        resp = self.api(self.pm).patch(url, {"text": "hijack"}, format="json")
        self.assertEqual(resp.status_code, 403)
        resp = self.api(self.designer).patch(url, {"text": "Draft v2 ready"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()["isEdited"])

        cursor = self.api(self.pm).get(
            f"{self.base}/conversations/{design_chat}/messages/"
        ).json()["aggregates"]["cursor"]
        self.assertEqual(self.api(self.pm).delete(url).status_code, 403)
        resp = self.api(self.designer).delete(url)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()["isDeleted"])
        self.assertEqual(resp.json()["text"], "")

        # A poller holding the old cursor learns about the deletion.
        polled = self.api(self.pm).get(
            f"{self.base}/conversations/{design_chat}/messages/", {"since": cursor}
        ).json()["results"]
        self.assertEqual([(m["id"], m["isDeleted"]) for m in polled], [(msg["id"], True)])
        self.assertTrue(
            AuditLog.objects.filter(action="CHAT_MESSAGE_DELETED", entity_id=self.project.id).exists()
        )

    # -- attachments -----------------------------------------------------------
    def test_attachments_use_committed_files_of_the_sender(self):
        design_chat = self.conv_id(self.designer, "Design Team")
        mine = self.committed_file(self.designer)
        sent = self.send(self.designer, design_chat, "Updated design attached.",
                         attachmentIds=[str(mine.id)])
        self.assertEqual(sent["attachments"][0]["fileName"], "design-v3.pdf")
        self.assertIn("/files/", sent["attachments"][0]["url"])

        audit = AuditLog.objects.get(action="CHAT_FILE_SHARED", entity_id=self.project.id)
        self.assertNotIn("Updated design attached", audit.description)

        theirs = self.committed_file(self.pm, "secret.pdf")
        self.send(self.designer, design_chat, "", expect=400, attachmentIds=[str(theirs.id)])
        # A file alone is a valid message.
        self.send(self.designer, design_chat, "", attachmentIds=[str(mine.id)])

    # -- mentions and notifications -------------------------------------------
    def test_mentions_are_validated_and_notify(self):
        design_chat = self.conv_id(self.designer, "Design Team")
        sent = self.send(
            self.designer, design_chat, "@Priya PM please check. @Dhruv FYI",
            mentions=[{"type": "user", "id": str(self.pm.id)},
                      {"type": "user", "id": str(self.qc.id)}],
        )
        # Dhruv is not in the Design team chat, so that mention is dropped.
        self.assertEqual([m["name"] for m in sent["mentions"]], ["Priya PM"])
        self.assertTrue(
            Notification.objects.filter(recipient=self.pm, type="pms.chat_mention").exists()
        )
        self.assertFalse(Notification.objects.filter(recipient=self.qc).exists())

        project_chat = self.conv_id(self.pm, "Project Chat")
        sent = self.send(
            self.pm, project_chat, "@QC Team please be ready.",
            mentions=[{"type": "team", "id": str(self.quality.id)}],
        )
        self.assertEqual(sent["mentions"][0]["name"], "QC Team")
        self.assertTrue(
            Notification.objects.filter(recipient=self.qc, type="pms.chat_mention").exists()
        )

    def test_message_notifications_coalesce_and_clear_on_read(self):
        design_chat = self.conv_id(self.designer, "Design Team")
        for text in ("one", "two", "three"):
            self.send(self.designer, design_chat, text)
        unread = Notification.objects.filter(
            recipient=self.pm, type="pms.chat_message", read_at__isnull=True
        )
        self.assertEqual(unread.count(), 1)
        self.assertIn("?tab=messenger&conversation=", unread.first().payload["path"])
        self.api(self.pm).post(f"{self.base}/conversations/{design_chat}/read/")
        self.assertEqual(unread.count(), 0)

    # -- search and direct messages -------------------------------------------
    def test_search_is_scoped_to_visible_chats(self):
        design_chat = self.conv_id(self.designer, "Design Team")
        qc_chat = self.conv_id(self.qc, "QC Team")
        self.send(self.designer, design_chat, "Design revision two is up",
                  attachmentIds=[str(self.committed_file(self.designer, "layout-final.pdf").id)])
        self.send(self.qc, qc_chat, "Revision of the weld checklist")

        def search(user, q):
            resp = self.api(user).get(f"{self.base}/messages/search/", {"q": q})
            self.assertEqual(resp.status_code, 200, resp.content)
            return [m["text"] for m in resp.json()["results"]]

        self.assertEqual(len(search(self.pm, "revision")), 2)
        self.assertEqual(search(self.designer, "revision"), ["Design revision two is up"])
        self.assertEqual(search(self.pm, "layout-final"), ["Design revision two is up"])
        self.assertEqual(search(self.pm, "Dhruv"), ["Revision of the weld checklist"])

    def test_direct_messages(self):
        resp = self.api(self.designer).post(
            f"{self.base}/conversations/", {"kind": "Direct", "userId": str(self.pm.id)},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["title"], "Priya PM")
        again = self.api(self.pm).post(
            f"{self.base}/conversations/", {"kind": "Direct", "userId": str(self.designer.id)},
            format="json",
        )
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["id"], resp.json()["id"])
        self.assertEqual(again.json()["title"], "Rahul")

        self.assertNotIn(resp.json()["id"], [r["id"] for r in self.conversations(self.qc)["results"]])
        denied = self.api(self.outsider).post(
            f"{self.base}/conversations/", {"kind": "Direct", "userId": str(self.pm.id)},
            format="json",
        )
        self.assertEqual(denied.status_code, 403)
