from datetime import date
from decimal import Decimal
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.accounting.services import seed_chart_of_accounts
from apps.masters.models import Item, ItemCategory, Location, Party, Unit
from apps.sales.models import Estimate, Quotation, SalesOrder, SalesInvoice
from apps.inventory.models import StockMovement


class AnyPartyAndMetalTests(TestCase):
    def setUp(self):
        self.client_obj, _ = Client.objects.get_or_create(
            slug="test-anyparty-tenant", defaults={"name": "Test AnyParty Tenant"}
        )
        seed_chart_of_accounts(self.client_obj)
        self.user = User.objects.filter(email="anyparty_user@example.com").first()
        if not self.user:
            self.user = User.objects.create_superuser(
                email="anyparty_user@example.com",
                password="password123",
                client=self.client_obj,
            )
        tokens = build_tokens(self.user)
        self.api_client = APIClient()
        self.api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

        self.location = Location.objects.create(
            client=self.client_obj,
            code="WH-MAIN",
            name="Main Warehouse",
            type="Warehouse",
        )

        self.cat_sheet = ItemCategory.objects.create(
            client=self.client_obj,
            code="CAT-SHEET",
            name="Metal Sheets",
        )
        self.cat_tube = ItemCategory.objects.create(
            client=self.client_obj,
            code="CAT-TUBE",
            name="Metal Tubes",
        )

    def test_one_time_party_estimate_creation(self):
        initial_party_count = Party.objects.filter(client=self.client_obj).count()

        payload = {
            "isOneTimeParty": True,
            "partyName": "Walk-in Contractor Rajesh",
            "partyType": "Contractor",
            "partyPhone": "9876543210",
            "partyEmail": "rajesh@example.com",
            "date": str(date.today()),
            "lineItems": [
                {
                    "description": "Custom MS Laser Cut Plate 10mm",
                    "qty": 5,
                    "rate": 1200,
                    "tax": 18,
                }
            ],
        }

        res = self.api_client.post("/api/v1/sales/estimates/", payload, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        doc_id = res.data["id"]

        est = Estimate.objects.get(id=doc_id)
        self.assertIsNone(est.party)
        self.assertTrue(est.is_one_time_party)
        self.assertEqual(est.party_name, "Walk-in Contractor Rajesh")
        self.assertEqual(est.party_phone, "9876543210")
        self.assertEqual(est.party_email, "rajesh@example.com")
        self.assertEqual(est.party_type, "Contractor")

        # Verify NO dummy Party was created in the database
        new_party_count = Party.objects.filter(client=self.client_obj).count()
        self.assertEqual(initial_party_count, new_party_count)

        # Verify stock movement: Estimates must never create stock movements
        movements = StockMovement.objects.filter(client=self.client_obj)
        self.assertEqual(movements.count(), 0)

    def test_one_time_party_quotation_and_conversion(self):
        initial_party_count = Party.objects.filter(client=self.client_obj).count()

        payload = {
            "isOneTimeParty": True,
            "partyName": "Sharma Fabricators",
            "partyType": "Business",
            "partyPhone": "9123456789",
            "partyEmail": "sharma@fab.com",
            "partyGstin": "27AAAAA0000A1Z5",
            "date": str(date.today()),
            "lineItems": [
                {
                    "description": "Fabricated MS Table Frame 40x40 Pipe",
                    "qty": 2,
                    "rate": 4500,
                    "tax": 18,
                }
            ],
        }

        res = self.api_client.post("/api/v1/sales/quotations/", payload, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        quote_id = res.data["id"]

        quote = Quotation.objects.get(id=quote_id)
        self.assertIsNone(quote.party)
        self.assertTrue(quote.is_one_time_party)
        self.assertEqual(quote.party_name, "Sharma Fabricators")
        self.assertEqual(quote.party_gstin, "27AAAAA0000A1Z5")

        # Convert to Sales Order
        convert_res = self.api_client.post(f"/api/v1/sales/quotations/{quote_id}/convert-to-order/")
        self.assertEqual(convert_res.status_code, 201, convert_res.data)
        order_id = convert_res.data["id"]

        order = SalesOrder.objects.get(id=order_id)
        self.assertIsNone(order.party)
        self.assertTrue(order.is_one_time_party)
        self.assertEqual(order.party_name, "Sharma Fabricators")
        self.assertEqual(order.party_phone, "9123456789")
        self.assertEqual(order.party_email, "sharma@fab.com")

        # Verify NO dummy Party was created in the database during conversion
        new_party_count = Party.objects.filter(client=self.client_obj).count()
        self.assertEqual(initial_party_count, new_party_count)

    def test_metal_sheet_weight_calculation_endpoint(self):
        # Test sheet theoretical weight calculation endpoint
        payload = {
            "type": "sheet",
            "lengthMm": 2500,
            "widthMm": 1250,
            "thicknessMm": 5,
            "materialOrGrade": "MS",
            "pieces": 2,
        }
        res = self.api_client.post("/api/v1/inventory/items/calculate-weight/", payload, format="json")
        self.assertEqual(res.status_code, 200)
        data = res.json() if hasattr(res, "json") else res.data
        self.assertTrue(data["is_valid"])
        # Expected: 2500 * 1250 * 5 * 7.85 / 1,000,000 = 122.65625 kg per piece
        self.assertAlmostEqual(float(data["weight_per_piece"]), 122.6563, places=3)
        self.assertAlmostEqual(float(data["total_weight"]), 245.3125, places=3)

    def test_metal_tube_weight_calculation_endpoint(self):
        # Test round tube: OD=48.3, wall=3.2, length=6000 (6m standard pipe)
        payload = {
            "type": "tube",
            "profile": "Round",
            "outerDiameterMm": 48.3,
            "wallThicknessMm": 3.2,
            "lengthMm": 6000,
            "materialOrGrade": "MS",
            "pieces": 1,
        }
        res = self.api_client.post("/api/v1/inventory/items/calculate-weight/", payload, format="json")
        self.assertEqual(res.status_code, 200)
        data = res.json() if hasattr(res, "json") else res.data
        self.assertTrue(data["is_valid"])
        # Cross section area = pi * (48.3 - 3.2) * 3.2 = ~453.39 mm2
        # Weight per meter = 453.39 * 7.85 / 1000 = ~3.559 kg/m
        # 6m length = ~21.35 kg
        self.assertGreater(float(data["weight_per_meter"]), 3.5)
        self.assertLess(float(data["weight_per_meter"]), 3.7)
        self.assertGreater(float(data["weight_per_piece"]), 21.0)
        self.assertLess(float(data["weight_per_piece"]), 22.0)

    def test_item_creation_with_metal_spec_auto_calculates_weight(self):
        # Create sheet item
        sheet_payload = {
            "sku": "MS-SHT-5MM-TEST",
            "name": "MS Sheet 5.0mm (2500x1250)",
            "uom": "Nos",
            "metalGrade": "MS",
            "hasSheetSpec": True,
            "sheetLength": "2500",
            "sheetWidth": "1250",
            "sheetThickness": "5",
            "isWeightItem": True,
        }
        res = self.api_client.post("/api/v1/inventory/items/", sheet_payload, format="json")
        self.assertEqual(res.status_code, 201)
        data = res.json() if hasattr(res, "json") else res.data
        item = Item.objects.get(id=data["id"])
        self.assertIsNotNone(item.theoretical_weight)
        self.assertAlmostEqual(float(item.theoretical_weight), 122.6563, places=3)
        self.assertAlmostEqual(float(item.sheet_weight_kg), 122.6563, places=3)

    def test_one_time_party_sales_invoice_and_stock_safety(self):
        # Create an inventory item with opening stock
        item = Item.objects.create(
            client=self.client_obj,
            sku="MS-TEST-STOCK-ITEM",
            name="MS Sheet 5.0mm Stocked",
            uom="Nos",
            cost_price=Decimal("1000.00"),
            selling_price=Decimal("1500.00"),
        )
        # Add stock via movement
        from apps.inventory.services import post_movement
        post_movement(
            client_id=self.client_obj.id,
            item=item.id,
            location=self.location.id,
            type="ADJUSTMENT",
            quantity=Decimal("20.0000"),
            unit_cost=Decimal("1000.00"),
            notes="Initial opening stock",
            user=self.user,
        )

        initial_party_count = Party.objects.filter(client=self.client_obj).count()

        # Create one-time party invoice directly
        inv_payload = {
            "isOneTimeParty": True,
            "partyName": "Walk-in Individual Suresh",
            "partyType": "Individual",
            "partyPhone": "9811122233",
            "partyEmail": "suresh@example.com",
            "date": str(date.today()),
            "lineItems": [
                {
                    "itemId": str(item.id),
                    "qty": 3,
                    "rate": 1500,
                    "tax": 18,
                },
                {
                    "description": "Fabrication labor & cutting service",
                    "qty": 1,
                    "rate": 500,
                    "tax": 18,
                }
            ],
        }

        res = self.api_client.post("/api/v1/sales/invoices/", inv_payload, format="json")
        self.assertEqual(res.status_code, 201)
        inv_id = res.data["id"]

        inv = SalesInvoice.objects.get(id=inv_id)
        self.assertIsNone(inv.party)
        self.assertTrue(inv.is_one_time_party)
        self.assertEqual(inv.party_name, "Walk-in Individual Suresh")
        self.assertEqual(inv.party_type, "Individual")

        # Prior to finalization: NO stock movement for this invoice
        inv_movements = StockMovement.objects.filter(
            client=self.client_obj, reference_id=str(inv.id)
        )
        self.assertEqual(inv_movements.count(), 0)

        # Finalize invoice (authoritative stock event)
        finalize_res = self.api_client.post(f"/api/v1/sales/invoices/{inv_id}/finalize/")
        self.assertEqual(finalize_res.status_code, 200, finalize_res.data)

        # After finalization: Exactly ONE stock movement created for the inventory item (qty = 3)
        inv_movements = StockMovement.objects.filter(
            client=self.client_obj, reference_id=str(inv.id)
        )
        self.assertEqual(inv_movements.count(), 1)
        movement = inv_movements.first()
        self.assertEqual(movement.type, "SALE")
        self.assertEqual(movement.quantity, Decimal("-3.0000"))
        self.assertEqual(movement.item, item)

        # Confirm Party count remained unchanged (no dummy Party created)
        new_party_count = Party.objects.filter(client=self.client_obj).count()
        self.assertEqual(initial_party_count, new_party_count)
