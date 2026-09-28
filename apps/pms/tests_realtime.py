"""Socket.IO wiring: who may connect, and which writes push which events where.

The transport itself is python-socketio's; these tests pin down our side of it
-- token checks, rooms, and that every event is sent only after commit.
"""
import asyncio
from unittest import mock

import socketio
from django.test import SimpleTestCase, TestCase

from apps.accounts.authentication import build_tokens
from apps.core import realtime
from apps.core.audit import notify
from apps.pms.tests_chat import MessengerFixture


class RealtimeTests(MessengerFixture, TestCase):
    """Uses the messenger fixture: PM, designer, QC member, outsider, admin."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(realtime.sio, "emit", new_callable=mock.AsyncMock)
        self.sio_emit = patcher.start()
        self.addCleanup(patcher.stop)

    def emitted(self):
        return [(c.args[0], c.kwargs.get("room"), c.args[1]) for c in self.sio_emit.call_args_list]

    def events(self, name):
        return [(room, data) for event, room, data in self.emitted() if event == name]

    # -- connecting ------------------------------------------------------------
    def test_token_authentication(self):
        identity = realtime.authenticate_token(build_tokens(self.designer)["access"])
        self.assertEqual(identity["user_id"], str(self.designer.id))
        self.assertEqual(identity["client_id"], str(self.tenant.id))
        self.assertTrue(identity["can_view_pms"])

        self.assertIsNone(realtime.authenticate_token(None))
        self.assertIsNone(realtime.authenticate_token("not-a-jwt"))
        token = build_tokens(self.qc)["access"]
        self.qc.status = "Inactive"
        self.qc.save()
        self.assertIsNone(realtime.authenticate_token(token))

    def test_origin_rule(self):
        with self.settings(CORS_ALLOWED_ORIGINS=["http://localhost:5173"]):
            self.assertTrue(realtime._origin_allowed("http://localhost:5173", {}))
            self.assertTrue(realtime._origin_allowed("https://erp.example.com", {"HTTP_HOST": "erp.example.com"}))
            self.assertFalse(realtime._origin_allowed("https://evil.example", {"HTTP_HOST": "erp.example.com"}))

    def test_watch_is_limited_to_the_tenant(self):
        self.assertTrue(realtime._project_in_tenant(str(self.project.id), str(self.tenant.id)))
        self.assertFalse(realtime._project_in_tenant(str(self.project.id), str(self.qc.id)))
        self.assertFalse(realtime._project_in_tenant("garbage", str(self.tenant.id)))

    # -- PMS project changes ---------------------------------------------------
    def test_project_writes_announce_after_commit(self):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            resp = self.api(self.admin).post(
                f"{self.base}/stages/{self.design_stage.id}/progress/", {"pct": 40},
                format="json",
            )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(self.sio_emit.call_count, 0, "nothing is pushed before commit")
        for callback in callbacks:
            callback()
        changed = self.events("pms:project_changed")
        self.assertEqual(len(changed), 1)
        room, data = changed[0]
        self.assertEqual(room, f"tenant:{self.tenant.id}:pms")
        self.assertEqual(data["projectId"], str(self.project.id))
        self.assertEqual(data["action"], "stage_progress")

    def test_detail_reads_are_revalidated(self):
        """A re-read after a push must not come from the browser's cache."""
        resp = self.api(self.pm).get(f"{self.base}/")
        self.assertIn("Last-Modified", resp)
        self.assertIn("no-cache", resp["Cache-Control"])

    def test_reads_and_chat_do_not_announce_project_changes(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.api(self.pm).get(f"{self.base}/")
            conv = self.conv_id(self.designer, "Design Team")
            self.send(self.designer, conv, "hello")
        self.assertEqual(self.events("pms:project_changed"), [])

    # -- chat ------------------------------------------------------------------
    def test_team_message_goes_to_the_project_room(self):
        conv = self.conv_id(self.designer, "Design Team")
        with self.captureOnCommitCallbacks(execute=True):
            msg = self.send(self.designer, conv, "Revision uploaded")
        activity = self.events("chat:activity")
        self.assertEqual(activity, [(
            f"pms:project:{self.project.id}",
            {"projectId": str(self.project.id), "conversationId": conv,
             "messageId": msg["id"], "change": "created"},
        )])
        # The PM was notified, so the PM's bell is woken too.
        self.assertIn((f"user:{self.pm.id}", {}), self.events("notification:new"))

    def test_direct_chat_goes_only_to_its_members(self):
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.api(self.designer).post(
                f"{self.base}/conversations/", {"kind": "Direct", "userId": str(self.pm.id)},
                format="json",
            )
            self.send(self.designer, resp.json()["id"], "private note")
        rooms = {room for room, data in self.events("chat:activity") if data["change"] == "created"}
        self.assertEqual(rooms, {f"user:{self.designer.id}", f"user:{self.pm.id}"})

    def test_edit_delete_and_read_are_announced(self):
        conv = self.conv_id(self.designer, "Design Team")
        msg = self.send(self.designer, conv, "draft")
        url = f"{self.base}/conversations/{conv}/messages/{msg['id']}/"
        with self.captureOnCommitCallbacks(execute=True):
            self.api(self.designer).patch(url, {"text": "final"}, format="json")
            self.api(self.designer).delete(url)
            self.api(self.pm).post(f"{self.base}/conversations/{conv}/read/")
        changes = [data["change"] for _, data in self.events("chat:activity")]
        self.assertEqual(changes, ["updated", "deleted"])
        self.assertEqual(self.events("chat:read"), [
            (f"user:{self.pm.id}", {"projectId": str(self.project.id), "conversationId": conv})
        ])

    def test_notify_wakes_each_recipient(self):
        with self.captureOnCommitCallbacks(execute=True):
            notify(client=self.tenant, recipients=[self.pm, self.qc, None, self.pm],
                   type="pms.test", category="pms", title="hello")
        self.assertEqual(
            sorted(room for room, _ in self.events("notification:new")),
            sorted([f"user:{self.pm.id}", f"user:{self.qc.id}"]),
        )


class SocketHandshakeTests(SimpleTestCase):
    def test_connect_refuses_without_valid_token(self):
        with self.assertRaises(socketio.exceptions.ConnectionRefusedError):
            asyncio.run(realtime.connect("sid-1", {}, {"token": "nope"}))
        self.assertEqual(realtime._online, {})
