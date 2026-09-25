from decimal import Decimal
from django.test import TestCase
from apps.accounts.models import Client, User
from apps.pms.models import Project, ProjectStage, Department
from apps.pms import services


class StagePercentageCalculationTests(TestCase):
    def setUp(self):
        self.client_obj, _ = Client.objects.get_or_create(
            slug="test-tenant", defaults={"name": "Test Tenant"}
        )
        self.creator = User.objects.filter(email="creator@example.com").first()
        if not self.creator:
            self.creator = User.objects.create_user(
                email="creator@example.com",
                password="pass",
                client=self.client_obj,
            )
        self.dept, _ = Department.objects.get_or_create(
            client=self.client_obj,
            name="Engineering",
        )

    def test_serializer_percentage_validation(self):
        from apps.pms.serializers import StagePercentagesSerializer
        from rest_framework.exceptions import ValidationError

        # Invalid: 80% total
        invalid_data = {
            "stages": [
                {"id": "1", "percentage": 30},
                {"id": "2", "percentage": 50},
            ]
        }
        s1 = StagePercentagesSerializer(data=invalid_data)
        self.assertFalse(s1.is_valid())
        self.assertIn("stages", s1.errors)

        # Valid: 100% total
        valid_data = {
            "stages": [
                {"id": "1", "percentage": 30},
                {"id": "2", "percentage": 70},
            ]
        }
        s2 = StagePercentagesSerializer(data=valid_data)
        self.assertTrue(s2.is_valid())

    def test_weighted_progress_calculation(self):
        import uuid
        project = Project.objects.create(
            client=self.client_obj,
            code=f"PRJ-2026-WEIGHTED-{uuid.uuid4().hex[:6]}",
            created_by=self.creator,
            status="In Progress",
        )
        specs = [
            ("Design", 15, 100),
            ("Fabrication", 30, 50),
            ("QC", 15, 0),
            ("Coating", 15, 0),
            ("Packaging", 10, 0),
            ("Dispatch", 15, 0),
        ]
        for idx, (name, weight, completion) in enumerate(specs, start=1):
            ProjectStage.objects.create(
                client=self.client_obj,
                project=project,
                name=name,
                sequence=idx,
                department=self.dept,
                weight_pct=Decimal(str(weight)),
                completion_pct=completion,
            )

        services.recalculate_project(project)
        project.refresh_from_db()
        self.assertEqual(project.overall_completion_pct, 30)

    def test_unweighted_fallback_for_legacy_projects(self):
        import uuid
        project = Project.objects.create(
            client=self.client_obj,
            code=f"PRJ-2026-LEGACY-{uuid.uuid4().hex[:6]}",
            created_by=self.creator,
            status="In Progress",
        )
        specs = [
            ("Stage 1", 0, 100),
            ("Stage 2", 0, 50),
            ("Stage 3", 0, 0),
        ]
        for idx, (name, weight, completion) in enumerate(specs, start=1):
            ProjectStage.objects.create(
                client=self.client_obj,
                project=project,
                name=name,
                sequence=idx,
                department=self.dept,
                weight_pct=Decimal(str(weight)),
                completion_pct=completion,
            )

        services.recalculate_project(project)
        project.refresh_from_db()
        # (100 + 50 + 0) / 3 = 50%
        self.assertEqual(project.overall_completion_pct, 50)

    def test_stage_percentages_rebalance(self):
        import uuid
        # Create project with 2 stages: 50% / 50%
        project = Project.objects.create(
            client=self.client_obj,
            code=f"PRJ-2026-REBALANCE-{uuid.uuid4().hex[:6]}",
            created_by=self.creator,
            status="In Progress",
        )
        s1 = ProjectStage.objects.create(
            client=self.client_obj,
            project=project,
            name="Stage 1",
            sequence=1,
            department=self.dept,
            weight_pct=Decimal("50"),
            completion_pct=100,
        )
        s2 = ProjectStage.objects.create(
            client=self.client_obj,
            project=project,
            name="Stage 2",
            sequence=2,
            department=self.dept,
            weight_pct=Decimal("50"),
            completion_pct=0,
        )
        services.recalculate_project(project)
        project.refresh_from_db()
        # 100 * 0.5 = 50%
        self.assertEqual(project.overall_completion_pct, 50)

        # Rebalance: Stage 1 -> 80%, Stage 2 -> 20%
        s1.weight_pct = Decimal("80")
        s1.save()
        s2.weight_pct = Decimal("20")
        s2.save()
        services.recalculate_project(project)
        project.refresh_from_db()
        # 100 * 0.8 = 80%
        self.assertEqual(project.overall_completion_pct, 80)


class ProofShareReviewTests(TestCase):
    """Generate Link → client opens it, comments, decides; the team sees the thread."""

    def setUp(self):
        from rest_framework.test import APIClient

        from apps.accounts.authentication import build_tokens
        from apps.core.models import File
        from apps.pms.models import Document

        self.client_obj, _ = Client.objects.get_or_create(
            slug="proof-tenant", defaults={"name": "Proof Tenant"}
        )
        self.user = User.objects.create_superuser(
            email="proof_pm@example.com", password="pass-12345", client=self.client_obj,
        )
        self.project = Project.objects.create(
            client=self.client_obj, code="PRJ-PROOF-001", customer_name="Acme Engineering",
            product_name="Belt Conveyor 6m", specifications="SS-304 frame, 600 mm belt",
            quantity=Decimal("2"), order_value=Decimal("321095.70"), status="In Progress",
        )
        stage = ProjectStage.objects.create(
            client=self.client_obj, project=self.project, name="Design & Drawing", sequence=1,
        )
        file_row = File.objects.create(
            client=self.client_obj, storage_key="proof-tests/drawing.pdf",
            file_name="drawing.pdf", content_type="application/pdf", status="committed",
        )
        self.document = Document.objects.create(
            client=self.client_obj, project=self.project, stage=stage, doc_key="drawing",
            version=1, file=file_row, file_name="drawing.pdf", is_proof=True,
        )
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.user)['access']}")
        self.public = APIClient()

    def _share(self, **extra):
        resp = self.api.post(
            f"/api/v1/pms/projects/{self.project.code}/documents/{self.document.id}/share/",
            {"recipientName": "Rhea Shah", "recipientEmail": "", "expiryDays": 7, **extra},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        return resp.json()["token"]

    def test_link_without_email_carries_note_and_product(self):
        token = self._share(message="Please confirm the bracket positions.")
        resp = self.public.get(f"/api/v1/public/pms/approve/{token}/")
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["message"], "Please confirm the bracket positions.")
        self.assertEqual(body["project"]["productName"], "Belt Conveyor 6m")
        self.assertEqual(body["project"]["specifications"], "SS-304 frame, 600 mm belt")
        self.assertEqual(body["project"]["items"], [])
        self.assertTrue(body["canDecide"])
        self.assertEqual(body["comments"], [])

    def test_client_and_team_share_one_comment_thread(self):
        token = self._share()
        resp = self.public.post(
            f"/api/v1/public/pms/approve/{token}/comments/",
            {"text": "Can the guard rail be 50 mm higher?", "page": 1},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        thread = resp.json()["results"]
        self.assertEqual(len(thread), 1)
        self.assertEqual(thread[0]["authorType"], "Client")
        self.assertEqual(thread[0]["author"], "Rhea Shah")

        staff_url = f"/api/v1/pms/projects/{self.project.code}/documents/{self.document.id}/comments/"
        resp = self.api.post(staff_url, {"text": "Yes — revised in v2."}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        thread = resp.json()["results"]
        self.assertEqual([c["authorType"] for c in thread], ["Client", "Staff"])

        resp = self.public.get(f"/api/v1/public/pms/approve/{token}/comments/")
        self.assertEqual(len(resp.json()["results"]), 2)

        resp = self.public.post(
            f"/api/v1/public/pms/approve/{token}/comments/", {"text": "   "}, format="json"
        )
        self.assertEqual(resp.status_code, 400)

    def test_client_rejects_with_reason(self):
        token = self._share()
        resp = self.public.post(
            f"/api/v1/public/pms/approve/{token}/decide/",
            {"decision": "Need Improvement", "decidedBy": "Rhea Shah",
             "revisionReason": "Raise the guard rail by 50 mm."},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        body = self.public.get(f"/api/v1/public/pms/approve/{token}/").json()
        self.assertEqual(body["decision"], "Need Improvement")
        self.assertEqual(body["revisionReason"], "Raise the guard rail by 50 mm.")
        self.assertFalse(body["canDecide"])
        self.document.refresh_from_db()
        self.assertEqual(self.document.approval_status, "Need Improvement")
