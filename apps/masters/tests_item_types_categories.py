"""
Comprehensive Unit & Integration Tests for Phase 2:
Item Type and Category Master Upgrade.
"""
from decimal import Decimal
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.accounting.services import seed_chart_of_accounts
from apps.masters.models import Item, ItemCategory, ItemType, Location, MaterialGrade, Unit


class ItemTypeAndCategoryMasterTests(TestCase):
    def setUp(self):
        self.client_obj, _ = Client.objects.get_or_create(
            slug="test-phase2-tenant", defaults={"name": "Test Phase 2 Tenant"}
        )
        seed_chart_of_accounts(self.client_obj)
        self.user = User.objects.filter(email="phase2_tester@example.com").first()
        if not self.user:
            self.user = User.objects.create_superuser(
                email="phase2_tester@example.com",
                password="password123",
                client=self.client_obj,
            )
        tokens = build_tokens(self.user)
        self.api_client = APIClient()
        self.api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

        self.unit_kg = Unit.objects.create(client=self.client_obj, code="KG", label="Kilogram")
        self.location = Location.objects.create(
            client=self.client_obj, code="WH-01", name="Main Plant", type="Warehouse"
        )

    def test_item_type_crud_and_uniqueness(self):
        """Create, read, update, activate/deactivate ItemType, and prevent duplicate name."""
        # 1. Create ItemType
        res = self.api_client.post(
            "/api/v1/inventory/item-types/",
            {
                "name": "Metal Sheet",
                "code": "SHEET",
                "shape_profile": "Flat",
                "description": "Flat rolled metal plate/sheet",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        sheet_id = res.data["id"]

        # 2. Prevent duplicate ItemType name
        res_dup = self.api_client.post(
            "/api/v1/inventory/item-types/",
            {"name": "Metal Sheet", "code": "SHEET-2"},
            format="json",
        )
        self.assertEqual(res_dup.status_code, 400)
        self.assertIn("name", res_dup.data.get("field_errors", res_dup.data))

        # 3. Deactivate ItemType
        res_deact = self.api_client.post(f"/api/v1/inventory/item-types/{sheet_id}/deactivate/")
        self.assertEqual(res_deact.status_code, 200)
        self.assertFalse(res_deact.data.get("isActive", res_deact.data.get("is_active")))

        # 4. Activate ItemType
        res_act = self.api_client.post(f"/api/v1/inventory/item-types/{sheet_id}/activate/")
        self.assertEqual(res_act.status_code, 200)
        self.assertTrue(res_act.data.get("isActive", res_act.data.get("is_active")))


    def test_category_item_type_link_and_duplicate_prevention(self):
        """Requirement 8: Category names must be unique within the relevant item type."""
        # Create two item types
        type_sheet = ItemType.objects.create(
            client=self.client_obj, code="SHEET", name="Metal Sheet"
        )
        type_rod = ItemType.objects.create(
            client=self.client_obj, code="ROD", name="Rod"
        )

        # Create "Mild Steel" under Metal Sheet
        res1 = self.api_client.post(
            "/api/v1/inventory/categories/",
            {
                "name": "Mild Steel",
                "itemTypeId": str(type_sheet.id),
                "defaultUnitId": str(self.unit_kg.id),
                "leadTimeDays": 5,
            },
            format="json",
        )
        self.assertEqual(res1.status_code, 201, res1.data)
        cat_sheet_ms_id = res1.data["id"]

        # Attempt duplicate "Mild Steel" under the SAME item type (Metal Sheet) -> Reject 400
        res_dup = self.api_client.post(
            "/api/v1/inventory/categories/",
            {
                "name": "Mild Steel",
                "itemTypeId": str(type_sheet.id),
            },
            format="json",
        )
        self.assertEqual(res_dup.status_code, 400)
        self.assertIn("name", res_dup.data.get("field_errors", res_dup.data))

        # Create "Mild Steel" under a DIFFERENT item type (Rod) -> Allow 201
        res_rod_ms = self.api_client.post(
            "/api/v1/inventory/categories/",
            {
                "name": "Mild Steel",
                "itemTypeId": str(type_rod.id),
            },
            format="json",
        )
        self.assertEqual(res_rod_ms.status_code, 201, res_rod_ms.data)

    def test_category_filtering_by_item_type_and_active_status(self):
        """Requirement 6: Filter categories by item type and active status."""
        type_pipe = ItemType.objects.create(client=self.client_obj, code="PIPE", name="Pipe")
        type_bar = ItemType.objects.create(client=self.client_obj, code="BAR", name="Bar")

        cat1 = ItemCategory.objects.create(
            client=self.client_obj, code="CAT-PIPE-GI", name="GI Pipe", item_type=type_pipe, is_active=True
        )
        cat2 = ItemCategory.objects.create(
            client=self.client_obj, code="CAT-PIPE-SS", name="SS Pipe", item_type=type_pipe, is_active=False
        )
        cat3 = ItemCategory.objects.create(
            client=self.client_obj, code="CAT-BAR-HEX", name="Hex Bar", item_type=type_bar, is_active=True
        )

        # Filter by itemType
        res_type = self.api_client.get(f"/api/v1/inventory/categories/?itemTypeId={type_pipe.id}")
        self.assertEqual(res_type.status_code, 200)
        names = [c["name"] for c in res_type.data.get("results", res_type.data)]
        self.assertIn("GI Pipe", names)
        self.assertIn("SS Pipe", names)
        self.assertNotIn("Hex Bar", names)

        # Filter by isActive=true
        res_active = self.api_client.get(f"/api/v1/inventory/categories/?itemTypeId={type_pipe.id}&isActive=true")
        self.assertEqual(res_active.status_code, 200)
        active_names = [c["name"] for c in res_active.data.get("results", res_active.data)]
        self.assertIn("GI Pipe", active_names)
        self.assertNotIn("SS Pipe", active_names)

    def test_item_category_compatibility_validation(self):
        """Requirement 9: Backend rejects incompatible category and item_type, rejects inactive categories."""
        type_angle = ItemType.objects.create(client=self.client_obj, code="ANG", name="Angle", is_active=True)
        type_beam = ItemType.objects.create(client=self.client_obj, code="BM", name="Beam", is_active=True)
        type_inactive = ItemType.objects.create(client=self.client_obj, code="INACT", name="Inactive Type", is_active=False)

        cat_angle = ItemCategory.objects.create(
            client=self.client_obj, code="CAT-ANG-MS", name="MS Angle", item_type=type_angle, is_active=True
        )
        cat_inactive = ItemCategory.objects.create(
            client=self.client_obj, code="CAT-INACT", name="Old Angle", item_type=type_angle, is_active=False
        )

        # 1. Reject incompatible combination: Category belongs to Angle, itemTypeId passed as Beam
        res_mismatch = self.api_client.post(
            "/api/v1/inventory/items/",
            {
                "sku": "ANG-MS-001",
                "name": "MS Angle 50x50x5",
                "categoryId": str(cat_angle.id),
                "itemTypeId": str(type_beam.id),
                "costPrice": "100",
                "sellingPrice": "120",
            },
            format="json",
        )
        self.assertEqual(res_mismatch.status_code, 400)
        self.assertIn("itemTypeId", res_mismatch.data.get("field_errors", res_mismatch.data))

        # 2. Reject inactive category
        res_inact_cat = self.api_client.post(
            "/api/v1/inventory/items/",
            {
                "sku": "ANG-OLD-001",
                "name": "Old MS Angle",
                "categoryId": str(cat_inactive.id),
                "costPrice": "100",
                "sellingPrice": "120",
            },
            format="json",
        )
        self.assertEqual(res_inact_cat.status_code, 400)
        self.assertIn("categoryId", res_inact_cat.data.get("field_errors", res_inact_cat.data))


        # 3. Valid compatible item creation with auto-resolution of item_type from category
        res_valid = self.api_client.post(
            "/api/v1/inventory/items/",
            {
                "sku": "ANG-MS-VALID",
                "name": "MS Angle 50x50x6",
                "categoryId": str(cat_angle.id),
                "costPrice": "100",
                "sellingPrice": "120",
            },
            format="json",
        )
        self.assertEqual(res_valid.status_code, 201, res_valid.data)
        self.assertEqual(res_valid.data["itemTypeId"], str(type_angle.id))

    def test_prevent_deletion_of_category_in_use(self):
        """Requirement 10: Prevent deletion of categories in active use; enforce deactivation."""
        type_tube = ItemType.objects.create(client=self.client_obj, code="TUBE", name="Tube")
        cat_tube = ItemCategory.objects.create(
            client=self.client_obj, code="CAT-TUBE-SS", name="SS Tube", item_type=type_tube
        )
        item = Item.objects.create(
            client=self.client_obj,
            sku="TUBE-SS-001",
            name="SS Tube 25mm",
            category=cat_tube,
            item_type=type_tube,
            cost_price=Decimal("200"),
            selling_price=Decimal("250"),
        )

        # Delete category in use -> 409 Conflict
        res_del = self.api_client.delete(f"/api/v1/inventory/categories/{cat_tube.id}/")
        self.assertEqual(res_del.status_code, 409)
        self.assertEqual(res_del.data.get("code"), "CATEGORY_IN_USE")

        # Deactivate works smoothly
        res_deact = self.api_client.post(f"/api/v1/inventory/categories/{cat_tube.id}/deactivate/")
        self.assertEqual(res_deact.status_code, 200)
        self.assertFalse(res_deact.data["isActive"])
