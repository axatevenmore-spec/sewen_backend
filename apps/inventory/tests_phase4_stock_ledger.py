"""
Phase 4: Stock Ledger and Balance Upgrade Tests.

Verifies:
1. Opening stock posting (ensuring no double-counting).
2. Inward and outward movements updating balances consistently with UOM.
3. Two-legged transfers (TRANSFER_OUT + TRANSFER_IN) and transfer reversals.
4. Concurrency protection and insufficient/negative stock rejection.
5. Duplicate source document posting prevention.
6. Stock ledger reconciliation engine (discrepancy detection and auto-fix).
7. Stock position API endpoint metadata and valuation fields.
"""
import uuid
from decimal import Decimal
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.accounting.services import seed_chart_of_accounts
from apps.core.exceptions import BusinessRuleViolation, Codes, Conflict
from apps.inventory.models import StockBalance, StockMovement, StockTransfer
from apps.inventory import services as stock_services
from apps.masters.models import Item, ItemCategory, ItemType, Location, MaterialGrade, Unit


class Phase4StockLedgerTests(TestCase):
    def setUp(self):
        self.client_obj, _ = Client.objects.get_or_create(
            slug="test-phase4-ledger-tenant",
            defaults={"name": "Test Phase 4 Tenant"},
        )
        seed_chart_of_accounts(self.client_obj)
        self.user = User.objects.filter(email="phase4_tester@example.com").first()
        if not self.user:
            self.user = User.objects.create_superuser(
                email="phase4_tester@example.com",
                password="password123",
                client=self.client_obj,
            )
        tokens = build_tokens(self.user)
        self.api_client = APIClient()
        self.api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

        self.uom_pcs, _ = Unit.objects.get_or_create(
            client=self.client_obj,
            code="PCS",
            defaults={"label": "Pieces"},
        )
        self.uom_kg, _ = Unit.objects.get_or_create(
            client=self.client_obj,
            code="KG",
            defaults={"label": "Kilograms"},
        )

        self.item_type_sheet, _ = ItemType.objects.get_or_create(
            client=self.client_obj,
            name="Metal Sheet",
            defaults={"code": "SHEET", "shape_profile": "SHEET"},
        )

        self.category_ms, _ = ItemCategory.objects.get_or_create(
            client=self.client_obj,
            name="Mild Steel Sheet",
            defaults={"item_type": self.item_type_sheet, "code": "CAT-MS-SHT"},
        )

        self.grade_ms, _ = MaterialGrade.objects.get_or_create(
            client=self.client_obj,
            code="MS",
            defaults={"name": "Mild Steel IS 2062", "density": Decimal("7.8500")},
        )

        self.loc_main, _ = Location.objects.get_or_create(
            client=self.client_obj,
            name="Main Steel Yard",
            defaults={"code": "LOC-MAIN"},
        )
        self.loc_sec, _ = Location.objects.get_or_create(
            client=self.client_obj,
            name="Secondary Fabrication Yard",
            defaults={"code": "LOC-SEC"},
        )

        self.sheet_item = Item.objects.create(
            client=self.client_obj,
            sku="MS-SHT-2MM-8X4",
            name="MS Sheet 2mm 8x4 ft",
            item_type=self.item_type_sheet,
            category=self.category_ms,
            grade=self.grade_ms,
            has_sheet_spec=True,
            sheet_thickness=Decimal("2.00"),
            sheet_length=Decimal("2438.40"),
            sheet_width=Decimal("1219.20"),
            uom="PCS",
            cost_price=Decimal("1500.00"),
            reorder_level=Decimal("10.00"),
            default_location=self.loc_main,
            item_kind="Product",
        )

    def tearDown(self):
        StockMovement.objects.filter(client=self.client_obj).delete()
        StockBalance.objects.filter(client=self.client_obj).delete()

    def test_01_opening_stock_posting_and_no_double_count(self):
        """Verify OPENING_STOCK movement posts cleanly and matches the approved formula."""
        movement = stock_services.post_movement(
            client_id=self.client_obj.id,
            item=self.sheet_item,
            location=self.loc_main,
            type="OPENING_STOCK",
            quantity=Decimal("50.0000"),
            unit_cost=Decimal("1500.00"),
            notes="Opening stock balance for MS sheets",
            user=self.user,
        )

        self.assertEqual(movement.type, "OPENING_STOCK")
        self.assertEqual(movement.quantity, Decimal("50.0000"))
        self.assertEqual(movement.uom, "PCS")

        # Verify balance table
        balance = StockBalance.objects.get(
            client=self.client_obj, item=self.sheet_item, location=self.loc_main
        )
        self.assertEqual(balance.on_hand, Decimal("50.0000"))

        # Verify calculate_item_stock
        stock_data = stock_services.calculate_item_stock(
            self.client_obj.id, self.sheet_item, self.loc_main.id
        )
        self.assertEqual(stock_data["openingStock"], Decimal("50.0000"))
        self.assertEqual(stock_data["totalInward"], Decimal("0.0000"))
        self.assertEqual(stock_data["totalOutward"], Decimal("0.0000"))
        self.assertEqual(stock_data["onHand"], Decimal("50.0000"))
        self.assertEqual(stock_data["available"], Decimal("50.0000"))

        # Formula check: Available = Opening (50) + Inward (0) - Outward (0) - Reserved (0) = 50
        self.assertEqual(
            stock_data["available"],
            stock_data["openingStock"] + stock_data["totalInward"] - stock_data["totalOutward"] - stock_data["reserved"]
        )

    def test_02_inward_and_outward_movements_lifecycle(self):
        """Verify purchase inward and sales outward movements update balances and UOM."""
        # Initial Opening Stock: 20
        stock_services.post_movement(
            client_id=self.client_obj.id,
            item=self.sheet_item,
            location=self.loc_main,
            type="OPENING_STOCK",
            quantity=Decimal("20.0000"),
            unit_cost=Decimal("1500.00"),
            notes="Opening stock",
        )

        # Inward Purchase: 30
        ref_bill_id = uuid.uuid4()
        stock_services.post_movement(
            client_id=self.client_obj.id,
            item=self.sheet_item,
            location=self.loc_main,
            type="PURCHASE",
            quantity=Decimal("30.0000"),
            unit_cost=Decimal("1480.00"),
            reference_type="PurchaseBill",
            reference_id=ref_bill_id,
            reference_number="BILL-2026-001",
        )

        # Outward Sale: 15
        ref_inv_id = uuid.uuid4()
        stock_services.post_movement(
            client_id=self.client_obj.id,
            item=self.sheet_item,
            location=self.loc_main,
            type="SALE",
            quantity=Decimal("-15.0000"),
            unit_cost=Decimal("1500.00"),
            reference_type="SalesInvoice",
            reference_id=ref_inv_id,
            reference_number="INV-2026-001",
        )

        stock_data = stock_services.calculate_item_stock(
            self.client_obj.id, self.sheet_item, self.loc_main.id
        )
        self.assertEqual(stock_data["openingStock"], Decimal("20.0000"))
        self.assertEqual(stock_data["totalInward"], Decimal("30.0000"))
        self.assertEqual(stock_data["totalOutward"], Decimal("15.0000"))
        # onHand = 20 + 30 - 15 = 35
        self.assertEqual(stock_data["onHand"], Decimal("35.0000"))
        self.assertEqual(stock_data["available"], Decimal("35.0000"))

    def test_03_two_legged_transfer_and_reversal(self):
        """Verify two-legged transfers (TRANSFER_OUT + TRANSFER_IN) and reversal."""
        # Put 40 at Main Yard
        stock_services.post_movement(
            client_id=self.client_obj.id,
            item=self.sheet_item,
            location=self.loc_main,
            type="OPENING_STOCK",
            quantity=Decimal("40.0000"),
        )

        transfer_id = uuid.uuid4()
        # Leg 1: TRANSFER_OUT from Main Yard
        out_mv = stock_services.post_movement(
            client_id=self.client_obj.id,
            item=self.sheet_item,
            location=self.loc_main,
            type="TRANSFER_OUT",
            quantity=Decimal("-10.0000"),
            reference_type="StockTransfer",
            reference_id=transfer_id,
            reference_number="TRF-001",
        )
        # Leg 2: TRANSFER_IN to Secondary Yard
        in_mv = stock_services.post_movement(
            client_id=self.client_obj.id,
            item=self.sheet_item,
            location=self.loc_sec,
            type="TRANSFER_IN",
            quantity=Decimal("10.0000"),
            reference_type="StockTransfer",
            reference_id=transfer_id,
            reference_number="TRF-001",
        )

        main_stock = stock_services.calculate_item_stock(
            self.client_obj.id, self.sheet_item, self.loc_main.id
        )
        sec_stock = stock_services.calculate_item_stock(
            self.client_obj.id, self.sheet_item, self.loc_sec.id
        )
        self.assertEqual(main_stock["onHand"], Decimal("30.0000"))
        self.assertEqual(sec_stock["onHand"], Decimal("10.0000"))

        # Now test reversal of transfer
        reversals = stock_services.reverse_movements(
            reference_type="StockTransfer",
            reference_id=transfer_id,
            client_id=self.client_obj.id,
            notes="Transfer cancelled",
        )
        self.assertEqual(len(reversals), 2)

        main_stock_after = stock_services.calculate_item_stock(
            self.client_obj.id, self.sheet_item, self.loc_main.id
        )
        sec_stock_after = stock_services.calculate_item_stock(
            self.client_obj.id, self.sheet_item, self.loc_sec.id
        )
        self.assertEqual(main_stock_after["onHand"], Decimal("40.0000"))
        self.assertEqual(sec_stock_after["onHand"], Decimal("0.0000"))

    def test_04_insufficient_stock_and_negative_rejection(self):
        """Verify assert_sufficient_stock and negative balance rejection."""
        # Put 5 items on hand
        stock_services.post_movement(
            client_id=self.client_obj.id,
            item=self.sheet_item,
            location=self.loc_main,
            type="OPENING_STOCK",
            quantity=Decimal("5.0000"),
        )

        # 1. assert_sufficient_stock should reject dispatch of 6 items
        with self.assertRaises(BusinessRuleViolation) as ctx:
            stock_services.assert_sufficient_stock(
                self.client_obj.id, self.sheet_item, Decimal("6.0000"), self.loc_main.id
            )
        self.assertEqual(ctx.exception.code, Codes.INSUFFICIENT_STOCK)

        # 2. post_movement with allow_negative=False (default) should reject -6
        with self.assertRaises(BusinessRuleViolation) as ctx2:
            stock_services.post_movement(
                client_id=self.client_obj.id,
                item=self.sheet_item,
                location=self.loc_main,
                type="SALE",
                quantity=Decimal("-6.0000"),
                reference_type="SalesInvoice",
                reference_id=uuid.uuid4(),
            )
        self.assertEqual(ctx2.exception.code, Codes.INSUFFICIENT_STOCK)

    def test_05_duplicate_source_document_prevention(self):
        """Verify duplicate posting guard rejects re-posting the same document."""
        doc_id = uuid.uuid4()
        stock_services.post_movement(
            client_id=self.client_obj.id,
            item=self.sheet_item,
            location=self.loc_main,
            type="PURCHASE",
            quantity=Decimal("15.0000"),
            reference_type="PurchaseBill",
            reference_id=doc_id,
            prevent_duplicate=True,
        )

        # Attempt to post duplicate movement with same reference and type
        with self.assertRaises(Conflict) as ctx:
            stock_services.post_movement(
                client_id=self.client_obj.id,
                item=self.sheet_item,
                location=self.loc_main,
                type="PURCHASE",
                quantity=Decimal("15.0000"),
                reference_type="PurchaseBill",
                reference_id=doc_id,
                prevent_duplicate=True,
            )
        self.assertEqual(ctx.exception.code, Codes.ALREADY_POSTED)

        # Verify helper assert_not_already_posted
        with self.assertRaises(Conflict):
            stock_services.assert_not_already_posted(
                self.client_obj.id, "PurchaseBill", doc_id
            )

    def test_06_stock_reconciliation_engine(self):
        """Verify discrepancy detection and automatic reconciliation to append-only ledger."""
        # 1. Post movements: Net = 25
        stock_services.post_movement(
            client_id=self.client_obj.id,
            item=self.sheet_item,
            location=self.loc_main,
            type="OPENING_STOCK",
            quantity=Decimal("20.0000"),
        )
        stock_services.post_movement(
            client_id=self.client_obj.id,
            item=self.sheet_item,
            location=self.loc_main,
            type="PURCHASE",
            quantity=Decimal("10.0000"),
            reference_type="PurchaseBill",
            reference_id=uuid.uuid4(),
        )
        stock_services.post_movement(
            client_id=self.client_obj.id,
            item=self.sheet_item,
            location=self.loc_main,
            type="SALE",
            quantity=Decimal("-5.0000"),
            reference_type="SalesInvoice",
            reference_id=uuid.uuid4(),
        )

        # Intentionally tamper with StockBalance.on_hand to simulate drift
        StockBalance.objects.filter(
            client=self.client_obj, item=self.sheet_item, location=self.loc_main
        ).update(on_hand=Decimal("99.0000"))

        # Reconcile without auto-fix -> should detect 1 discrepancy
        report = stock_services.reconcile_stock(
            client_id=self.client_obj.id, auto_fix=False
        )
        self.assertEqual(report["discrepanciesCount"], 1)
        self.assertEqual(report["discrepancies"][0]["balanceOnHand"], Decimal("99.0000"))
        self.assertEqual(report["discrepancies"][0]["ledgerOnHand"], Decimal("25.0000"))
        self.assertEqual(report["discrepancies"][0]["difference"], Decimal("74.0000"))
        self.assertEqual(report["discrepancies"][0]["status"], "DISCREPANCY")

        # Reconcile with auto-fix -> should repair StockBalance to 25
        fix_report = stock_services.reconcile_stock(
            client_id=self.client_obj.id, auto_fix=True
        )
        self.assertEqual(fix_report["discrepanciesCount"], 1)
        self.assertEqual(fix_report["discrepancies"][0]["status"], "FIXED")

        # Verify database is now synchronized
        repaired_balance = StockBalance.objects.get(
            client=self.client_obj, item=self.sheet_item, location=self.loc_main
        )
        self.assertEqual(repaired_balance.on_hand, Decimal("25.0000"))

        # Third run -> 0 discrepancies
        clean_report = stock_services.reconcile_stock(
            client_id=self.client_obj.id, auto_fix=False
        )
        self.assertEqual(clean_report["discrepanciesCount"], 0)

    def test_07_stock_position_api_endpoint(self):
        """Verify GET /api/v1/inventory/stock/ and reconcile endpoint."""
        stock_services.post_movement(
            client_id=self.client_obj.id,
            item=self.sheet_item,
            location=self.loc_main,
            type="OPENING_STOCK",
            quantity=Decimal("12.0000"),
            unit_cost=Decimal("1500.00"),
        )

        # Test GET /api/v1/inventory/stock/
        res = self.api_client.get("/api/v1/inventory/stock/")
        self.assertEqual(res.status_code, 200)
        json_body = res.json()
        data = json_body.get("results") or json_body.get("data")
        item_row = next((r for r in data if r["itemId"] == str(self.sheet_item.id)), None)
        self.assertIsNotNone(item_row)
        self.assertEqual(item_row["sku"], "MS-SHT-2MM-8X4")
        self.assertEqual(item_row["itemType"], "Metal Sheet")
        self.assertEqual(item_row["category"], "Mild Steel Sheet")
        self.assertEqual(item_row["grade"], "MS")
        self.assertIn("THK", item_row["dimensions"])
        self.assertIn("2", item_row["dimensions"])
        self.assertEqual(Decimal(str(item_row["openingBalance"])), Decimal("12.0000"))
        self.assertEqual(Decimal(str(item_row["onHand"])), Decimal("12.0000"))
        self.assertEqual(Decimal(str(item_row["available"])), Decimal("12.0000"))
        self.assertEqual(Decimal(str(item_row["unitCost"])), Decimal("1500.0000"))
        self.assertEqual(Decimal(str(item_row["value"])), Decimal("18000.00"))

        # Test POST /api/v1/inventory/stock/reconcile/
        rec_res = self.api_client.post("/api/v1/inventory/stock/reconcile/", {"autoFix": False}, format="json")
        self.assertEqual(rec_res.status_code, 200)
        self.assertIn("totalBalancesChecked", rec_res.json())
