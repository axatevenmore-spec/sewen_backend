"""The Leads section as one system: Tasks Master -> Stage Tasks -> stage
automation -> lead tasks that open the right Task Form, and lead custom fields."""
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, Role, User
from apps.accounts.permission_catalogue import seed_roles, sync_permissions
from apps.crm.models import Deal, Form, Lead, MasterTask, Stage, StageTask, Task

API = "/api/v1/crm"


class LeadSectionLinkTests(TestCase):
    def setUp(self):
        sync_permissions()
        self.tenant = Client.objects.create(slug="links", name="Links")
        seed_roles(self.tenant)
        self.admin = User.objects.create_user(
            email="admin@links.test", password="pass-12345", client=self.tenant, name="Admin",
            role=Role.objects.get(client=self.tenant, code="AD"),
        )
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.admin)['access']}")
        self.new = Stage.objects.create(client=self.tenant, name="New Lead", sequence=1)
        self.demo = Stage.objects.create(client=self.tenant, name="Demo Pending", sequence=2)
        self.call_form = Form.objects.create(client=self.tenant, name="Call report", kind="task")
        self.lead_form = Form.objects.create(client=self.tenant, name="Lead capture", kind="lead")

    def post(self, path, body):
        return self.api.post(f"{API}{path}", body, format="json")

    def patch(self, path, body):
        return self.api.patch(f"{API}{path}", body, format="json")

    def linked(self, master_id):
        return StageTask.objects.filter(master_task_id=master_id, deleted_at__isnull=True)

    # -- Tasks Master -> Stage Tasks -------------------------------------------
    def test_used_in_stages_creates_and_retires_linked_stage_tasks(self):
        resp = self.post("/master-tasks/", {
            "title": "Site visit", "role": "Field Executive", "dueIn": 2, "priority": "High",
            "stages": [str(self.new.id), str(self.demo.id)],
        })
        self.assertEqual(resp.status_code, 201, resp.content)
        master_id = resp.json()["id"]
        rows = {row.stage_id: row for row in self.linked(master_id)}
        self.assertEqual(set(rows), {self.new.id, self.demo.id})
        row = rows[self.new.id]
        self.assertEqual((row.title, row.assignee_role, row.offset_days, row.priority),
                         ("Site visit", "Field Executive", 2, "High"))

        # Editing the master updates its linked rows; unticking a stage retires one.
        resp = self.patch(f"/master-tasks/{master_id}/", {"role": "Sales Executive", "stages": [str(self.new.id)]})
        self.assertEqual(resp.status_code, 200, resp.content)
        rows = list(self.linked(master_id))
        self.assertEqual([(r.stage_id, r.assignee_role) for r in rows], [(self.new.id, "Sales Executive")])

        # Per-stage settings survive a master edit.
        StageTask.objects.filter(pk=rows[0].pk).update(required=True, repeats=True, sort_order=9)
        self.patch(f"/master-tasks/{master_id}/", {"priority": "Low"})
        row = self.linked(master_id).get()
        self.assertEqual((row.required, row.repeats, row.sort_order, row.priority), (True, True, 9, "Low"))

        # Inactive or deleted: nothing left for the automation to generate.
        self.patch(f"/master-tasks/{master_id}/", {"isActive": False})
        self.assertFalse(self.linked(master_id).exists())
        self.patch(f"/master-tasks/{master_id}/", {"isActive": True})
        self.assertTrue(self.linked(master_id).exists())
        self.assertEqual(self.api.delete(f"{API}/master-tasks/{master_id}/").status_code, 204)
        self.assertFalse(self.linked(master_id).exists())

    def test_master_task_stages_are_tenant_scoped(self):
        other = Client.objects.create(slug="other", name="Other")
        foreign = Stage.objects.create(client=other, name="Theirs", sequence=1)
        resp = self.post("/master-tasks/", {"title": "Probe", "stages": [str(foreign.id)]})
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_a_master_task_joins_no_stage_until_it_is_added_to_one(self):
        # Created from Task Roles: the library row only. In particular it must
        # not turn up in the first stage just because that is where it sits.
        resp = self.post("/master-tasks/", {"title": "Whastapp ping", "role": "BDE", "stages": []})
        self.assertEqual(resp.status_code, 201, resp.content)
        master_id = resp.json()["id"]
        self.assertFalse(self.linked(master_id).exists())
        self.assertEqual(self.api.get(f"{API}/stage-tasks/").json()["count"], 0)

        # Adding it to a stage is the deliberate step that puts it there.
        resp = self.post("/stage-tasks/", {"stageId": str(self.demo.id), "masterTaskId": master_id})
        self.assertEqual(resp.status_code, 201, resp.content)
        stage_task_id = resp.json()["id"]
        self.assertEqual([row.stage_id for row in self.linked(master_id)], [self.demo.id])
        self.assertEqual(
            list(MasterTask.objects.get(pk=master_id).stages.values_list("id", flat=True)),
            [self.demo.id],
        )

        # Taking it out of the stage takes it off the master too, so saving the
        # master afterwards cannot put the row back.
        self.assertEqual(self.api.delete(f"{API}/stage-tasks/{stage_task_id}/").status_code, 204)
        master = MasterTask.objects.get(pk=master_id)
        self.assertEqual(list(master.stages.values_list("id", flat=True)), [])
        self.patch(f"/master-tasks/{master_id}/", {"role": "Area Sales Manager"})
        self.assertFalse(self.linked(master_id).exists())

    def test_editing_a_task_role_leaves_the_stages_alone(self):
        master_id = self.post("/master-tasks/", {"title": "Follow up", "role": "BDE"}).json()["id"]
        template = self.post(
            "/stage-tasks/", {"stageId": str(self.demo.id), "masterTaskId": master_id}
        ).json()
        # The stage task is given per-stage settings of its own.
        self.patch(f"/stage-tasks/{template['id']}/", {"required": True, "maxRepeats": 4})

        # Saving the library row -- what Task Roles does -- leaves it in place.
        self.patch(f"/master-tasks/{master_id}/", {"role": "Sales Support Executive"})
        row = self.linked(master_id).get()
        self.assertEqual(
            (row.stage_id, row.assignee_role, row.required, row.max_repeats),
            (self.demo.id, "Sales Support Executive", True, 4),
        )

    def test_a_master_task_joins_task_roles_only_when_picked(self):
        resp = self.post("/master-tasks/", {"title": "Detail collection", "role": "BDE"})
        self.assertEqual(resp.status_code, 201, resp.content)
        master_id = resp.json()["id"]
        # A new master is in the library only: not in Task Roles, in no stage.
        self.assertIs(resp.json()["inTaskRoles"], False)
        self.assertFalse(self.linked(master_id).exists())

        # Task Roles' Add Task + Save marks it, and still puts it in no stage.
        resp = self.patch(f"/master-tasks/{master_id}/", {"inTaskRoles": True, "dueIn": 3})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIs(resp.json()["inTaskRoles"], True)
        self.assertFalse(self.linked(master_id).exists())

        # Removing it from Task Roles keeps the library row.
        self.patch(f"/master-tasks/{master_id}/", {"inTaskRoles": False})
        master = MasterTask.objects.get(pk=master_id)
        self.assertIsNone(master.deleted_at)
        self.assertFalse(master.in_task_roles)

    def test_lead_task_forms_are_gone_from_the_api(self):
        # The existing task form stays in the table, but is not listed ...
        names = [row["name"] for row in self.api.get(f"{API}/forms/").json()["results"]]
        self.assertIn("Lead capture", names)
        self.assertNotIn("Call report", names)
        # ... and no new one can be made.
        resp = self.post("/forms/", {"name": "Visit report", "kind": "task", "schema": {}})
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertTrue(Form.objects.filter(pk=self.call_form.pk).exists())

    def test_stage_task_picked_from_master_copies_its_defaults(self):
        master = MasterTask.objects.create(
            client=self.tenant, title="Demo", role="Demo Executive", department="Sales",
            duration_days=3, priority="Urgent",
        )
        resp = self.post("/stage-tasks/", {"stageId": str(self.demo.id), "masterTaskId": str(master.id)})
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        self.assertEqual((body["name"], body["role"], body["dueIn"], body["priority"], body["masterTaskId"]),
                         ("Demo", "Demo Executive", 3, "Urgent", str(master.id)))

    # -- Stage automation -> lead tasks with the right form ---------------------
    def test_moving_a_lead_generates_its_stage_tasks(self):
        # A template still linked to a (removed) task form generates a plain task.
        master = MasterTask.objects.create(
            client=self.tenant, title="Demo call", role="", form=self.call_form,
        )
        template = StageTask.objects.create(
            client=self.tenant, stage=self.demo, master_task=master, title="Demo call",
        )
        lead = self.post("/leads/", {"name": "Acme", "stageId": str(self.new.id),
                                     "customValues": {"budget-1": 50000}}).json()
        self.assertEqual(lead["customValues"], {"budget-1": 50000})

        resp = self.patch(f"/leads/{lead['id']}/", {"stageId": str(self.demo.id)})
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["lead"]["stageId"], str(self.demo.id))
        self.assertEqual(len(body["createdTasks"]), 1)
        created = body["createdTasks"][0]
        self.assertEqual(created["stageTaskId"], str(template.id))
        self.assertNotIn("taskFormId", created["extra"])
        self.assertEqual(str(Task.objects.get(pk=created["id"]).lead_id), lead["id"])

    def test_max_repeats_caps_tasks_per_lead(self):
        template = StageTask.objects.create(
            client=self.tenant, stage=self.demo, title="Follow up", max_repeats=2,
        )
        lead = self.post("/leads/", {"name": "Gamma", "stageId": str(self.new.id)}).json()
        for _ in range(3):
            self.patch(f"/leads/{lead['id']}/", {"stageId": str(self.demo.id)})
            self.patch(f"/leads/{lead['id']}/", {"stageId": str(self.new.id)})
        self.assertEqual(Task.objects.filter(lead_id=lead["id"], stage_task=template).count(), 2)

        resp = self.patch(f"/stage-tasks/{template.id}/", {"maxRepeats": 5})
        self.assertEqual((resp.status_code, resp.json()["maxRepeats"]), (200, 5))

    # -- Complete Task form -----------------------------------------------------
    def lead_with_tasks(self, name, *titles, required=()):
        """A lead in New Lead with one open task per title, from New Lead templates."""
        lead = self.post("/leads/", {"name": name, "stageId": str(self.new.id)}).json()
        tasks = []
        for title in titles:
            template = StageTask.objects.create(
                client=self.tenant, stage=self.new, title=title, required=title in required,
            )
            tasks.append(Task.objects.create(
                client=self.tenant, lead_id=lead["id"], stage_task=template, title=title,
                extra={"stage": "New Lead"},
            ))
        return lead, tasks

    def complete(self, task, body):
        return self.post(f"/tasks/{task.id}/complete/", body)

    def test_needs_another_attempt_reschedules_the_same_task(self):
        _, (call,) = self.lead_with_tasks("Delta", "Detail collection")
        resp = self.complete(call, {"nextAction": "call-again", "note": "No answer",
                                    "followUpAt": "2026-10-13T10:00:00"})
        self.assertEqual(resp.status_code, 200, resp.content)
        follow_up = resp.json()["followUpTask"]
        self.assertEqual(follow_up["title"], "Detail collection")
        self.assertEqual(follow_up["dueDate"], "2026-10-13")
        self.assertEqual((follow_up["extra"]["attempt"], follow_up["extra"]["dueAt"]),
                         (2, "2026-10-13T10:00"))
        self.assertEqual(follow_up["stageTaskId"], str(call.stage_task_id))

    def test_moving_on_waits_for_the_required_tasks(self):
        lead, (call, collect) = self.lead_with_tasks(
            "Echo", "Call", "Detail collection", required=("Detail collection",)
        )
        resp = self.complete(call, {"nextAction": "move-next-stage"})
        self.assertEqual(resp.status_code, 422, resp.content)
        self.assertIn("Detail collection", resp.content.decode())
        call.refresh_from_db()
        self.assertEqual(call.status, "Open")  # refused as a whole

        self.complete(collect, {"nextAction": "finish"})
        resp = self.complete(call, {"nextAction": "move-next-stage"})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()["stageChanged"])

    def test_finished_with_this_lead_closes_its_open_tasks(self):
        lead, (call, demo) = self.lead_with_tasks("Foxtrot", "Call", "Demo")
        resp = self.complete(call, {"nextAction": "finish", "closeOpenTasks": True})
        self.assertEqual(resp.status_code, 200, resp.content)
        demo.refresh_from_db()
        self.assertEqual(demo.status, "Cancelled")
        self.assertEqual(self.api.get(f"{API}/leads/{lead['id']}/").json()["stageId"], str(self.new.id))

    def test_ready_to_move_on_takes_the_stage_the_form_picked(self):
        """A lead that skipped ahead lands where it was filed, not one step on."""
        quoted = Stage.objects.create(client=self.tenant, name="Quotation Shared", sequence=5)
        template = StageTask.objects.create(
            client=self.tenant, stage=quoted, title="Send quotation",
        )
        _, (call,) = self.lead_with_tasks("Golf", "Call")

        resp = self.complete(call, {"nextAction": "move-next-stage", "targetStageId": str(quoted.id)})
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertTrue(body["stageChanged"])
        self.assertEqual(body["movedToStage"], "Quotation Shared")
        self.assertEqual([t["stageTaskId"] for t in body["createdTasks"]], [str(template.id)])

    def test_the_moved_to_tasks_are_due_on_the_chosen_date(self):
        template = StageTask.objects.create(client=self.tenant, stage=self.demo, title="Demo")
        _, (call,) = self.lead_with_tasks("Hotel", "Call")

        resp = self.complete(call, {"nextAction": "move-next-stage",
                                    "followUpAt": "2026-10-14T09:30:00"})
        self.assertEqual(resp.status_code, 200, resp.content)
        created = resp.json()["createdTasks"][0]
        self.assertEqual(created["stageTaskId"], str(template.id))
        self.assertEqual(created["dueDate"], "2026-10-14")
        self.assertEqual(created["extra"]["dueAt"], "2026-10-14T09:30")
        self.assertEqual(created["extra"]["dueTime"], "09:30")

    def test_moving_on_rejects_a_stage_the_lead_is_already_in(self):
        lead, (call,) = self.lead_with_tasks("India", "Call")
        resp = self.complete(call, {"nextAction": "move-next-stage", "targetStageId": str(self.new.id)})
        self.assertEqual(resp.status_code, 400, resp.content)
        call.refresh_from_db()
        self.assertEqual(call.status, "Open")  # refused as a whole

    def test_moving_on_ignores_a_stage_from_another_tenant(self):
        elsewhere = Client.objects.create(slug="elsewhere", name="Elsewhere")
        other = Stage.objects.create(client=elsewhere, name="Quotation Shared", sequence=5)
        _, (call,) = self.lead_with_tasks("Japan", "Call")
        resp = self.complete(call, {"nextAction": "move-next-stage", "targetStageId": str(other.id)})
        self.assertEqual(resp.status_code, 400, resp.content)
        call.refresh_from_db()
        self.assertEqual(call.status, "Open")

    # -- Lead status <-> Deals pipeline -----------------------------------------
    def closing_stages(self):
        won = Stage.objects.create(client=self.tenant, name="Won", sequence=7, is_won=True)
        lost = Stage.objects.create(client=self.tenant, name="Lost", sequence=8, is_lost=True)
        return won, lost

    def test_mark_lost_puts_the_lead_and_a_deal_in_lost(self):
        _, lost = self.closing_stages()
        lead = self.post("/leads/", {"name": "Hotel", "company": "Hotel Co", "stageId": str(self.demo.id)}).json()
        resp = self.post("/leads/mark-lost/", {"ids": [lead["id"]]})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["results"][0]["stageId"], str(lost.id))

        deal = Deal.objects.get(lead_id=lead["id"])
        self.assertEqual((deal.stage, deal.title), ("Lost", "Hotel Co opportunity"))
        self.assertIsNotNone(deal.closed_at)
        # Not deleted: the lead is still listed.
        self.assertIsNone(Lead.objects.get(pk=lead["id"]).deleted_at)

    def test_admin_reopens_a_lost_lead_from_the_deal(self):
        _, lost = self.closing_stages()
        lead = self.post("/leads/", {"name": "India", "stageId": str(self.demo.id)}).json()
        self.post("/leads/mark-lost/", {"ids": [lead["id"]]})
        deal = Deal.objects.get(lead_id=lead["id"])

        resp = self.patch(f"/deals/{deal.id}/", {"stage": "Open"})
        self.assertEqual(resp.status_code, 200, resp.content)
        row = Lead.objects.get(pk=lead["id"])
        # Back in the stage it was lost from.
        self.assertEqual((row.stage_id, row.reopen_stage_id), (self.demo.id, None))

    def test_only_an_admin_can_reopen(self):
        _, lost = self.closing_stages()
        manager = User.objects.create_user(
            email="sm@links.test", password="pass-12345", client=self.tenant, name="Manager",
            role=Role.objects.get(client=self.tenant, code="SM"),
        )
        lead = self.post("/leads/", {"name": "Juliet", "stageId": str(self.new.id)}).json()
        self.post("/leads/mark-lost/", {"ids": [lead["id"]]})
        deal = Deal.objects.get(lead_id=lead["id"])

        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(manager)['access']}")
        self.assertEqual(self.patch(f"/deals/{deal.id}/", {"stage": "Open"}).status_code, 403)
        self.assertEqual(self.patch(f"/leads/{lead['id']}/", {"stageId": str(self.new.id)}).status_code, 403)
        self.assertEqual(Lead.objects.get(pk=lead["id"]).stage_id, lost.id)

    def test_deal_won_or_lost_moves_its_lead(self):
        won, lost = self.closing_stages()
        lead = self.post("/leads/", {"name": "Kilo", "stageId": str(self.demo.id)}).json()
        deal = self.post(f"/leads/{lead['id']}/convert/", {"createCustomer": False}).json()["deal"]

        self.patch(f"/deals/{deal['id']}/", {"stage": "Won"})
        self.assertEqual(Lead.objects.get(pk=lead["id"]).stage_id, won.id)
        self.patch(f"/deals/{deal['id']}/", {"stage": "Lost"})
        self.assertEqual(Lead.objects.get(pk=lead["id"]).stage_id, lost.id)

        # And the other way: a lead moved to Won takes its deal along.
        self.patch(f"/leads/{lead['id']}/", {"stageId": str(won.id)})
        self.assertEqual(Deal.objects.get(pk=deal["id"]).stage, "Won")

    # -- Who sees a task ----------------------------------------------------------
    def test_a_task_is_seen_by_its_assignee_not_other_managers(self):
        role = Role.objects.get(client=self.tenant, code="SM")
        owner = User.objects.create_user(
            email="owner@links.test", password="pass-12345", client=self.tenant, name="Owner", role=role,
        )
        other = User.objects.create_user(
            email="other@links.test", password="pass-12345", client=self.tenant, name="Other", role=role,
        )
        lead = self.post("/leads/", {"name": "Lima", "stageId": str(self.new.id)}).json()
        task = Task.objects.create(
            client=self.tenant, lead_id=lead["id"], title="Call", assignee=owner, created_by=self.admin,
        )

        def visible(user):
            self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(user)['access']}")
            rows = self.api.get(f"{API}/tasks/").json()["results"]
            return {row["id"]: row for row in rows}

        mine = visible(owner)
        self.assertIn(str(task.id), mine)
        self.assertEqual(mine[str(task.id)]["leadNumber"], lead["leadNumber"])
        self.assertEqual(mine[str(task.id)]["createdByName"], "Admin")
        self.assertNotIn(str(task.id), visible(other))
        self.assertIn(str(task.id), visible(self.admin))

    # -- The Deals pipeline follows the lead's documents ------------------------
    def test_new_lead_then_estimate_sent_then_accepted(self):
        from datetime import date

        from apps.sales.models import Estimate, Quotation

        with self.captureOnCommitCallbacks(execute=True):
            lead = self.post("/leads/", {"name": "Mike", "company": "Mike Co", "stageId": str(self.new.id)}).json()
        deal = Deal.objects.get(lead_id=lead["id"])
        self.assertEqual(deal.stage, "Draft")

        estimate = Estimate.objects.create(client=self.tenant, doc_date=date.today(), crm_lead_id=lead["id"])
        deal.refresh_from_db()
        self.assertEqual(deal.stage, "Draft")  # a draft estimate moves nothing
        estimate.status = "Sent"
        estimate.save(update_fields=["status", "updated_at"])
        deal.refresh_from_db()
        self.assertEqual(deal.stage, "Sent")

        quotation = Quotation.objects.create(
            client=self.tenant, doc_date=date.today(), crm_lead_id=lead["id"], status="Sent",
        )
        quotation.status = "Accepted"
        quotation.save(update_fields=["status", "updated_at"])
        deal.refresh_from_db()
        self.assertEqual(deal.stage, "Won")
        # No Won stage was configured: one is made, and the lead is in it.
        row = Lead.objects.get(pk=lead["id"])
        self.assertTrue(row.stage.is_won)
        self.assertEqual(Deal.objects.filter(lead_id=lead["id"]).count(), 1)

    def test_mark_lost_makes_a_lost_stage_when_there_is_none(self):
        lead = self.post("/leads/", {"name": "November", "stageId": str(self.new.id)}).json()
        resp = self.post("/leads/mark-lost/", {"ids": [lead["id"]]})
        self.assertEqual(resp.status_code, 200, resp.content)
        stage = Stage.objects.get(pk=resp.json()["results"][0]["stageId"])
        self.assertEqual((stage.name, stage.is_lost), ("Lost", True))
        self.assertEqual(Deal.objects.get(lead_id=lead["id"]).stage, "Lost")

    def test_an_estimate_for_a_lead_is_addressed_to_the_lead(self):
        lead = self.post("/leads/", {"name": "Oscar", "phone": "9876543210",
                                     "email": "oscar@links.test", "stageId": str(self.new.id)}).json()
        resp = self.api.post("/api/v1/sales/estimates/", {
            "crmLead": lead["id"], "date": "2026-10-12",
            "lineItems": [{"description": "Service", "qty": 1, "rate": 100}],
        }, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        self.assertEqual((body["partyName"], body["isOneTimeParty"], body["partyPhone"]),
                         ("Oscar", True, "9876543210"))

    def test_a_converted_lead_carries_its_customer_name(self):
        lead = self.post("/leads/", {"name": "Papa", "company": "Papa Ltd", "stageId": str(self.new.id)}).json()
        self.assertIsNone(lead["customerName"])
        self.post(f"/leads/{lead['id']}/convert/", {"createCustomer": True})
        row = self.api.get(f"{API}/leads/{lead['id']}/").json()
        self.assertEqual(row["customerName"], "Papa Ltd")
