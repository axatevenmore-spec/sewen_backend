"""The Leads section as one system: Tasks Master -> Stage Tasks -> stage
automation -> lead tasks that open the right Task Form, and lead custom fields."""
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, Role, User
from apps.accounts.permission_catalogue import seed_roles, sync_permissions
from apps.crm.models import Form, MasterTask, Stage, StageTask, Task

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
            "stages": [str(self.new.id), str(self.demo.id)], "formId": str(self.call_form.id),
        })
        self.assertEqual(resp.status_code, 201, resp.content)
        master_id = resp.json()["id"]
        self.assertEqual(resp.json()["formName"], "Call report")
        rows = {row.stage_id: row for row in self.linked(master_id)}
        self.assertEqual(set(rows), {self.new.id, self.demo.id})
        row = rows[self.new.id]
        self.assertEqual((row.title, row.assignee_role, row.offset_days, row.priority, row.form_id),
                         ("Site visit", "Field Executive", 2, "High", self.call_form.id))

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

    def test_task_templates_accept_task_forms_only(self):
        resp = self.post("/master-tasks/", {"title": "Bad", "formId": str(self.lead_form.id)})
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_stage_task_picked_from_master_copies_its_defaults(self):
        master = MasterTask.objects.create(
            client=self.tenant, title="Demo", role="Demo Executive", department="Sales",
            duration_days=3, priority="Urgent", form=self.call_form,
        )
        resp = self.post("/stage-tasks/", {"stageId": str(self.demo.id), "masterTaskId": str(master.id)})
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        self.assertEqual((body["name"], body["role"], body["dueIn"], body["priority"], body["masterTaskId"]),
                         ("Demo", "Demo Executive", 3, "Urgent", str(master.id)))

    # -- Stage automation -> lead tasks with the right form ---------------------
    def test_moving_a_lead_generates_tasks_that_open_their_form(self):
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
        self.assertEqual(created["extra"]["taskFormId"], str(self.call_form.id))
        self.assertEqual(created["extra"]["taskFormName"], "Call report")
        self.assertEqual(str(Task.objects.get(pk=created["id"]).lead_id), lead["id"])

    def test_stage_task_form_overrides_the_masters(self):
        own = Form.objects.create(client=self.tenant, name="Demo checklist", kind="task")
        master = MasterTask.objects.create(client=self.tenant, title="Demo", form=self.call_form)
        StageTask.objects.create(client=self.tenant, stage=self.demo, master_task=master, title="Demo", form=own)
        lead = self.post("/leads/", {"name": "Beta", "stageId": str(self.new.id)}).json()
        created = self.patch(f"/leads/{lead['id']}/", {"stageId": str(self.demo.id)}).json()["createdTasks"]
        self.assertEqual(created[0]["extra"]["taskFormName"], "Demo checklist")

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
