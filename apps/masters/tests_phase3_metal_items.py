"""
Comprehensive Unit & Integration Tests for Phase 3:
Metal Inventory Item Master Upgrade.
"""
from decimal import Decimal
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.accounting.services import seed_chart_of_accounts
from apps.masters.models import Item, ItemCategory, ItemType, Location, MaterialGrade, Unit


class Phase3MetalItemMasterTests(TestCase):
    def setUp(self):
        self.client_obj, _ = Client.objects.get_or_create(
            slug="test-phase3-metal-tenant", defaults={"name": "Test Phase 3 Metal Tenant"}
        )
        seed_chart_of_accounts(self.client_obj)
        self.user = User.objects.filter(email="phase3_tester@example.com").first()
        if not self.user:
            self.user = User.objects.create_superuser(
                email="phase3_tester@example.com",
                password="password123",
                client=self.client_obj,
            )
        tokens = build_tokens(self.user)
        self.api_client = APIClient()
        self.api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

        self.unit_kg = Unit.objects.create(client=self.client_obj, code="KG", label="Kilogram")
        self.unit_pcs = Unit.objects.create(client=self.client_obj, code="PCS", label="Pieces")
        self.location = Location.objects.create(
            client=self.client_obj, code="WH-01", name="Metal Yard", type="Warehouse"
        )

        # Standard Phase 2 Item Types
        self.type_sheet = ItemType.objects.create(
            client=self.client_obj, code="SHEET", name="Metal Sheet", shape_profile="SHEET"
        )
        self.type_tube = ItemType.objects.create(
            client=self.client_obj, code="TUBE", name="Tube", shape_profile="HOLLOW_SECTION"
        )
        self.type_rod = ItemType.objects.create(
            client=self.client_obj, code="ROD", name="Rod", shape_profile="ROUND_SOLID"
        )
        self.type_angle = ItemType.objects.create(
            client=self.client_obj, code="ANGLE", name="Angle", shape_profile="EQUAL_ANGLE"
        )
        self.type_flat = ItemType.objects.create(
            client=self.client_obj, code="FLAT", name="Flat", shape_profile="FLAT_BAR"
        )
        self.type_channel = ItemType.objects.create(
            client=self.client_obj, code="CHANNEL", name="Channel", shape_profile="CHANNEL"
        )

        # Categories
        self.cat_sheet_ms = ItemCategory.objects.create(
            client=self.client_obj, code="CAT-SHEET-MS", name="MS Sheet", item_type=self.type_sheet
        )
        self.cat_sheet_ss = ItemCategory.objects.create(
            client=self.client_obj, code="CAT-SHEET-SS", name="SS Sheet", item_type=self.type_sheet
        )
        self.cat_tube_ss = ItemCategory.objects.create(
            client=self.client_obj, code="CAT-TUBE-SS", name="SS Tube", item_type=self.type_tube
        )
        self.cat_rod_ms = ItemCategory.objects.create(
            client=self.client_obj, code="CAT-ROD-MS", name="MS Rod", item_type=self.type_rod
        )
        self.cat_angle_ms = ItemCategory.objects.create(
            client=self.client_obj, code="CAT-ANG-MS", name="MS Angle", item_type=self.type_angle
        )
        self.cat_flat_ms = ItemCategory.objects.create(
            client=self.client_obj, code="CAT-FLAT-MS", name="MS Flat", item_type=self.type_flat
        )
        self.cat_channel_ms = ItemCategory.objects.create(
            client=self.client_obj, code="CAT-CHAN-MS", name="MS Channel", item_type=self.type_channel
        )

        # Grades
        self.grade_ms = MaterialGrade.objects.create(
            client=self.client_obj, code="MS", name="Mild Steel IS 2062", density=Decimal("7.8500")
        )
        self.grade_ss304 = MaterialGrade.objects.create(
            client=self.client_obj, code="SS 304", name="Stainless Steel 304", density=Decimal("7.9300")
        )
        self.grade_ss316 = MaterialGrade.objects.create(
            client=self.client_obj, code="SS 316", name="Stainless Steel 316", density=Decimal("7.9800")
        )

    def test_create_metal_sheet_item_with_weight_calculation(self):
        """Metal Sheet — Mild Steel — 2 mm — 8 × 4 ft (approx 2438 x 1219 mm)."""
        payload = {
            "sku": "SHT-MS-2MM-8X4",
            "name": "MS Sheet 2.0mm 8x4ft",
            "itemTypeId": str(self.type_sheet.id),
            "categoryId": str(self.cat_sheet_ms.id),
            "gradeId": str(self.grade_ms.id),
            "metalGrade": "MS",
            "finishCoating": "2B",
            "hasSheetSpec": True,
            "sheetThickness": 2.0,
            "sheetThicknessUnit": "mm",
            "sheetLength": 2438.0,
            "sheetWidth": 1219.0,
            "isWeightItem": True,
            "tolerancePct": 2.0,
            "uom": "Pcs",
            "costPrice": "3500.00",
            "sellingPrice": "4200.00",
        }
        res = self.api_client.post("/api/v1/inventory/items/", payload, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        data = res.data

        # Expected weight = (2438 * 1219 * 2.0 * 7.85) / 1,000,000 ≈ 46.6601 kg
        self.assertIsNotNone(data["weightPerPiece"])
        self.assertAlmostEqual(float(data["weightPerPiece"]), 46.6601, places=2)
        self.assertAlmostEqual(float(data["theoreticalWeight"]), 46.6601, places=2)
        self.assertEqual(data["finishCoating"], "2B")
        self.assertEqual(data["itemType"], "Metal Sheet")
        self.assertEqual(data["gradeName"], "Mild Steel IS 2062")

    def test_create_tube_round_and_square_with_weight_calculation(self):
        """Tube — SS304 — 25 × 25 × 2 mm — 6 m."""
        payload = {
            "sku": "TUB-SS304-25X25X2-6M",
            "name": "SS304 Square Tube 25x25x2mm 6m",
            "itemTypeId": str(self.type_tube.id),
            "categoryId": str(self.cat_tube_ss.id),
            "gradeId": str(self.grade_ss304.id),
            "metalGrade": "SS 304",
            "finishCoating": "No. 4",
            "hasTubeSpec": True,
            "tubeProfile": "Square",
            "outerWidth": 25.0,
            "wallThickness": 2.0,
            "tubeLength": 6000.0,
            "isWeightItem": True,
            "uom": "Length",
            "costPrice": "1200.00",
            "sellingPrice": "1650.00",
        }
        res = self.api_client.post("/api/v1/inventory/items/", payload, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        data = res.data

        # Square tube area = 4 * 2.0 * (25 - 2) = 184 mm²
        # Weight per meter = 184 * 7.93 / 1000 ≈ 1.4591 kg/m
        # Weight per piece (6m) = 1.4591 * 6 ≈ 8.7547 kg
        self.assertIsNotNone(data["weightPerMeter"])
        self.assertAlmostEqual(float(data["weightPerMeter"]), 1.4591, places=2)
        self.assertAlmostEqual(float(data["weightPerPiece"]), 8.7547, places=2)
        self.assertEqual(data["finishCoating"], "No. 4")

    def test_create_rod_item_with_weight_calculation(self):
        """Rod — Mild Steel — 12 mm diameter — 6 m."""
        payload = {
            "sku": "ROD-MS-12MM-6M",
            "name": "MS Round Bar 12mm 6m",
            "itemTypeId": str(self.type_rod.id),
            "categoryId": str(self.cat_rod_ms.id),
            "gradeId": str(self.grade_ms.id),
            "metalGrade": "MS",
            "diameter": 12.0,
            "tubeLength": 6000.0,
            "finishCoating": "Mill Finish",
            "isWeightItem": True,
            "uom": "Length",
            "costPrice": "450.00",
            "sellingPrice": "580.00",
        }
        res = self.api_client.post("/api/v1/inventory/items/", payload, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        data = res.data

        # Rod Area = π * (6)² = 113.0973 mm²
        # Weight per meter = 113.0973 * 7.85 / 1000 ≈ 0.8878 kg/m
        # Weight per piece (6m) ≈ 5.3269 kg
        self.assertIsNotNone(data["weightPerMeter"])
        self.assertAlmostEqual(float(data["weightPerMeter"]), 0.8878, places=2)
        self.assertAlmostEqual(float(data["weightPerPiece"]), 5.3269, places=2)
        self.assertEqual(float(data["diameter"]), 12.0)

    def test_create_angle_item_with_weight_calculation(self):
        """Angle — Mild Steel — 40 × 40 × 5 mm — 6 m."""
        payload = {
            "sku": "ANG-MS-40X40X5-6M",
            "name": "MS Angle 40x40x5mm 6m",
            "itemTypeId": str(self.type_angle.id),
            "categoryId": str(self.cat_angle_ms.id),
            "gradeId": str(self.grade_ms.id),
            "metalGrade": "MS",
            "legA": 40.0,
            "legB": 40.0,
            "sheetThickness": 5.0,
            "tubeLength": 6000.0,
            "finishCoating": "Mill Finish",
            "isWeightItem": True,
            "uom": "Length",
            "costPrice": "1100.00",
            "sellingPrice": "1400.00",
        }
        res = self.api_client.post("/api/v1/inventory/items/", payload, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        data = res.data

        # Angle Area = (40 + 40 - 5) * 5 = 375 mm²
        # Weight per meter = 375 * 7.85 / 1000 = 2.94375 kg/m
        # Weight per piece (6m) = 2.94375 * 6 = 17.6625 kg
        self.assertAlmostEqual(float(data["weightPerMeter"]), 2.9438, places=2)
        self.assertAlmostEqual(float(data["weightPerPiece"]), 17.6625, places=2)
        self.assertEqual(float(data["legA"]), 40.0)
        self.assertEqual(float(data["legB"]), 40.0)

    def test_create_flat_bar_with_weight_calculation(self):
        """Flat Bar — 50 mm x 6 mm x 6 m."""
        payload = {
            "sku": "FLT-MS-50X6-6M",
            "name": "MS Flat Bar 50x6mm 6m",
            "itemTypeId": str(self.type_flat.id),
            "categoryId": str(self.cat_flat_ms.id),
            "gradeId": str(self.grade_ms.id),
            "metalGrade": "MS",
            "sheetWidth": 50.0,
            "sheetThickness": 6.0,
            "tubeLength": 6000.0,
            "isWeightItem": True,
            "uom": "Pcs",
            "costPrice": "800.00",
            "sellingPrice": "1050.00",
        }
        res = self.api_client.post("/api/v1/inventory/items/", payload, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        data = res.data

        # Flat Bar Area = 50 * 6 = 300 mm²
        # Weight per meter = 300 * 7.85 / 1000 = 2.355 kg/m
        # Weight per piece = 2.355 * 6 = 14.13 kg
        self.assertAlmostEqual(float(data["weightPerMeter"]), 2.355, places=2)
        self.assertAlmostEqual(float(data["weightPerPiece"]), 14.13, places=2)

    def test_create_channel_beam_with_weight_calculation(self):
        """Channel — 100 x 50 x 5 mm web x 7.5 mm flange x 6m."""
        payload = {
            "sku": "CHN-MS-100X50-6M",
            "name": "MS Channel 100x50mm 6m",
            "itemTypeId": str(self.type_channel.id),
            "categoryId": str(self.cat_channel_ms.id),
            "gradeId": str(self.grade_ms.id),
            "metalGrade": "MS",
            "legA": 50.0,  # flange width
            "legB": 100.0,  # web height
            "webThickness": 5.0,
            "flangeThickness": 7.5,
            "tubeLength": 6000.0,
            "isWeightItem": True,
            "uom": "Pcs",
            "costPrice": "2400.00",
            "sellingPrice": "3100.00",
        }
        res = self.api_client.post("/api/v1/inventory/items/", payload, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        data = res.data

        # Area = 2 * (50 * 7.5) + (100 - 2 * 7.5) * 5 = 750 + 425 = 1175 mm²
        # Weight per meter = 1175 * 7.85 / 1000 = 9.2238 kg/m
        # Weight per piece (6m) = 9.2238 * 6 = 55.3425 kg
        self.assertAlmostEqual(float(data["weightPerMeter"]), 9.2238, places=2)
        self.assertAlmostEqual(float(data["weightPerPiece"]), 55.3425, places=2)

    def test_sku_uniqueness_enforcement(self):
        """Rejects duplicate SKU within same client."""
        Item.objects.create(
            client=self.client_obj,
            sku="UNIQUE-SKU-001",
            name="Original Item",
            category=self.cat_sheet_ms,
            item_type=self.type_sheet,
            cost_price=Decimal("100"),
            selling_price=Decimal("120"),
        )

        res = self.api_client.post(
            "/api/v1/inventory/items/",
            {
                "sku": "UNIQUE-SKU-001",
                "name": "Duplicate SKU Item",
                "categoryId": str(self.cat_sheet_ms.id),
                "costPrice": "150",
                "sellingPrice": "180",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 400)
        errors = res.data.get("field_errors", res.data)
        self.assertIn("sku", errors)

    def test_weight_tracked_item_requires_theoretical_weight(self):
        """If isWeightItem is true, theoretical weight must be computed or provided."""
        res = self.api_client.post(
            "/api/v1/inventory/items/",
            {
                "sku": "WT-ITEM-MISSING",
                "name": "Weight Item Without Spec",
                "categoryId": str(self.cat_sheet_ms.id),
                "isWeightItem": True,
                "costPrice": "100",
                "sellingPrice": "150",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 400)
        errors = res.data.get("field_errors", res.data)
        self.assertIn("theoreticalWeight", errors)

