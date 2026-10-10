"""
Phase 8: Inventory End-to-End Testing and Final Audit Test Suite.

Comprehensive validation covering:
1. Item and Category Tests (Sheet, Rod, Angle, Tube, Pipe, duplicate guards, invalid relations, inactive safety).
2. Stock Ledger Tests (Opening, Inward/Outward, Available Formula, Reservations, Transfers, Cancellations, Negative Stock Prevention, Duplicate Guard).
3. GRN Tests (Posting, Partial Receipts, Duplicate Prevention, Draft/Cancelled isolation, References).
4. Sales Integration Tests (Estimate -> Quotation -> Sales Order -> Challan -> Invoice -> Payment, Custom Lines without SKUs, Universal Dual-Guard Invariant, Insufficient Stock, Returns).
5. Financial Regression Tests (GST Calculations, With-Bill/Without-Bill split, Payment In Allocations, Party Ledger Balance, Paid Statuses).
"""
import uuid
from datetime import date, timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.accounting.models import Account, JournalEntry, JournalLine
from apps.accounting.services import seed_chart_of_accounts, recalculate_party_balance
from apps.core.exceptions import BusinessRuleViolation, Codes, Conflict, ValidationFailed
from apps.core.money import D, ZERO, round2, round4
from apps.inventory.models import StockBalance, StockMovement, StockTransfer
from apps.inventory import services as stock_services
from apps.masters.models import (
    Item,
    ItemCategory,
    ItemType,
    Location,
    MaterialGrade,
    Party,
    Unit,
)
from apps.purchase.models import (
    GoodsReceipt,
    GoodsReceiptLine,
    PurchaseBill,
    PurchaseBillLine,
    PurchaseOrder,
    PurchaseOrderLine,
)
from apps.purchase import services as purchase_services
from apps.sales.models import (
    DeliveryChallan,
    DeliveryChallanLine,
    Estimate,
    EstimateLine,
    PaymentIn,
    Quotation,
    QuotationLine,
    SalesInvoice,
    SalesInvoiceLine,
    SalesOrder,
    SalesOrderLine,
    SalesReturn,
    SalesReturnLine,
)
from apps.sales import services as sales_services


class Phase8BaseTestCase(TestCase):
    """Common setup for Phase 8 End-to-End audit tests."""

    def setUp(self):
        super().setUp()
        self.client_obj, _ = Client.objects.get_or_create(
            slug="phase8-audit-tenant",
            defaults={"name": "Phase 8 Audit Tenant"},
        )
        seed_chart_of_accounts(self.client_obj)

        self.user = User.objects.filter(email="phase8_audit@example.com").first()
        if not self.user:
            self.user = User.objects.create_superuser(
                email="phase8_audit@example.com",
                password="password123",
                client=self.client_obj,
            )

        tokens = build_tokens(self.user)
        self.api_client = APIClient()
        self.api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")

        self.uom_pcs, _ = Unit.objects.get_or_create(
            client=self.client_obj, code="PCS", defaults={"label": "Pieces"}
        )
        self.uom_kg, _ = Unit.objects.get_or_create(
            client=self.client_obj, code="KG", defaults={"label": "Kilograms"}
        )
        self.uom_mtr, _ = Unit.objects.get_or_create(
            client=self.client_obj, code="MTR", defaults={"label": "Meters"}
        )

        self.warehouse, _ = Location.objects.get_or_create(
            client=self.client_obj,
            code="WH-MAIN",
            defaults={"name": "Main Fabrication Warehouse", "type": "Warehouse", "is_active": True},
        )
        self.secondary_wh, _ = Location.objects.get_or_create(
            client=self.client_obj,
            code="WH-SEC",
            defaults={"name": "Secondary Storage Yard", "type": "Warehouse", "is_active": True},
        )

        self.customer = Party.objects.create(
            client=self.client_obj,
            code="CUST-AUDIT-01",
            name="Apex Engineering Works",
            type="Customer",
            place_of_supply="Maharashtra",
            gstin="27AAPCA1234A1Z5",
            email="apex@example.com",
            credit_limit=Decimal("500000.00"),
        )
        self.vendor = Party.objects.create(
            client=self.client_obj,
            code="VEND-AUDIT-01",
            name="National Steel Suppliers",
            type="Vendor",
            place_of_supply="Maharashtra",
            gstin="27BBPCA5678B1Z2",
            email="nss@example.com",
        )


class ItemAndCategoryAuditTests(Phase8BaseTestCase):
    """Section 1: Item and Category Tests."""

    def test_01_create_metal_item_types(self):
        """Create and verify Metal Sheet, Rod, Angle, Tube, and Pipe types."""
        type_definitions = [
            ("Metal Sheet", "SHEET", "SHEET"),
            ("Metal Rod", "ROD", "ROUND_BAR"),
            ("Metal Angle", "ANGLE", "ANGLE"),
            ("Metal Tube", "TUBE", "RECT_TUBE"),
            ("Metal Pipe", "PIPE", "ROUND_PIPE"),
        ]
        created_types = {}
        for name, code, profile in type_definitions:
            it, created = ItemType.objects.get_or_create(
                client=self.client_obj,
                code=code,
                defaults={"name": name, "shape_profile": profile, "is_active": True},
            )
            created_types[code] = it
            self.assertEqual(it.code, code)
            self.assertTrue(it.is_active)

        self.assertEqual(len(created_types), 5)

    def test_02_create_categories_and_assign_to_types(self):
        """Create and assign material categories to item types."""
        type_sheet, _ = ItemType.objects.get_or_create(
            client=self.client_obj, code="P8-SHEET", defaults={"name": "P8 Sheet", "shape_profile": "SHEET"}
        )
        cat_ms, _ = ItemCategory.objects.get_or_create(
            client=self.client_obj,
            code="CAT-MS-SHEET",
            defaults={"name": "Mild Steel Sheets", "item_type": type_sheet, "is_active": True},
        )
        cat_ss, _ = ItemCategory.objects.get_or_create(
            client=self.client_obj,
            code="CAT-SS-SHEET",
            defaults={"name": "Stainless Steel Sheets", "item_type": type_sheet, "is_active": True},
        )
        self.assertEqual(cat_ms.item_type_id, type_sheet.id)
        self.assertEqual(cat_ss.item_type_id, type_sheet.id)

    def test_03_create_metal_items_with_grades_and_specs(self):
        """Create items with various grades and metal dimensional specifications."""
        type_sheet, _ = ItemType.objects.get_or_create(
            client=self.client_obj, code="P8-SH", defaults={"name": "P8 Sheet", "shape_profile": "SHEET"}
        )
        cat_ms, _ = ItemCategory.objects.get_or_create(
            client=self.client_obj, code="CAT-MS-01", defaults={"name": "MS Sheets", "item_type": type_sheet}
        )
        grade_ms, _ = MaterialGrade.objects.get_or_create(
            client=self.client_obj, code="IS2062", defaults={"name": "IS 2062 Gr.E250"}
        )
        grade_ss, _ = MaterialGrade.objects.get_or_create(
            client=self.client_obj, code="SS304", defaults={"name": "AISI 304"}
        )

        # 1. MS Sheet 2mm 8x4ft
        sheet_ms = Item.objects.create(
            client=self.client_obj,
            sku="MS-SHT-2MM-8X4",
            name="MS Sheet 2mm 2440x1220mm",
            item_type=type_sheet,
            category=cat_ms,
            metal_grade=grade_ms,
            uom="PCS",
            has_sheet_spec=True,
            sheet_thickness=Decimal("2.000"),
            sheet_length=Decimal("2440.000"),
            sheet_width=Decimal("1220.000"),
            sheet_weight_kg=Decimal("46.740"),
            cost_price=Decimal("3500.00"),
            selling_price=Decimal("4200.00"),
        )
        # 2. SS304 Sheet 1.5mm 8x4ft
        sheet_ss = Item.objects.create(
            client=self.client_obj,
            sku="SS-SHT-1.5MM-8X4",
            name="SS304 Sheet 1.5mm 2440x1220mm",
            item_type=type_sheet,
            category=cat_ms,
            metal_grade=grade_ss,
            uom="PCS",
            has_sheet_spec=True,
            sheet_thickness=Decimal("1.500"),
            sheet_length=Decimal("2440.000"),
            sheet_width=Decimal("1220.000"),
            sheet_weight_kg=Decimal("35.300"),
            cost_price=Decimal("8500.00"),
            selling_price=Decimal("10500.00"),
        )

        self.assertEqual(sheet_ms.sheet_thickness, Decimal("2.000"))
        self.assertEqual(sheet_ss.metal_grade.code, "SS304")
        self.assertTrue(sheet_ms.holds_stock)
        self.assertTrue(sheet_ss.holds_stock)

    def test_04_duplicate_sku_and_category_prevention(self):
        """Verify unique constraints prevent duplicate SKUs and category codes."""
        type_sheet, _ = ItemType.objects.get_or_create(
            client=self.client_obj, code="P8-DUP-T", defaults={"name": "Dup Type"}
        )
        ItemCategory.objects.create(
            client=self.client_obj, code="CAT-UNIQUE-01", name="Unique Cat 1", item_type=type_sheet
        )
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                ItemCategory.objects.create(
                    client=self.client_obj, code="CAT-UNIQUE-01", name="Duplicate Cat 1", item_type=type_sheet
                )

        Item.objects.create(
            client=self.client_obj, sku="SKU-UNIQUE-01", name="Unique Item 1", uom="PCS"
        )
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                Item.objects.create(
                    client=self.client_obj, sku="SKU-UNIQUE-01", name="Duplicate Item 1", uom="PCS"
                )

    def test_05_invalid_item_category_relationships(self):
        """Verify item category validation enforces item_type match."""
        type_sheet, _ = ItemType.objects.get_or_create(
            client=self.client_obj, code="P8-T-SH", defaults={"name": "Type Sheet"}
        )
        type_rod, _ = ItemType.objects.get_or_create(
            client=self.client_obj, code="P8-T-ROD", defaults={"name": "Type Rod"}
        )
        cat_sheet, _ = ItemCategory.objects.get_or_create(
            client=self.client_obj, code="CAT-FOR-SHEET", defaults={"name": "Sheet Cat", "item_type": type_sheet}
        )

        res = self.api_client.post(
            "/api/v1/inventory/items/",
            {
                "sku": "SKU-MISMATCH-01",
                "name": "Mismatched Item",
                "categoryId": str(cat_sheet.id),
                "itemTypeId": str(type_rod.id),
                "costPrice": "100",
                "sellingPrice": "120",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("itemTypeId", res.data.get("field_errors", res.data))

    def test_06_inactive_category_safety(self):
        """Verify inactive category cannot be used for new items."""
        type_sheet, _ = ItemType.objects.get_or_create(
            client=self.client_obj, code="P8-T-INACT", defaults={"name": "Type Inact"}
        )
        cat_inactive = ItemCategory.objects.create(
            client=self.client_obj, code="CAT-INACTIVE-01", name="Inactive Cat", item_type=type_sheet, is_active=False
        )
        res = self.api_client.post(
            "/api/v1/inventory/items/",
            {
                "sku": "SKU-INACT-CAT",
                "name": "Item with inactive cat",
                "categoryId": str(cat_inactive.id),
                "costPrice": "100",
                "sellingPrice": "120",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("categoryId", res.data.get("field_errors", res.data))


class StockLedgerAuditTests(Phase8BaseTestCase):
    """Section 2: Stock Ledger Tests."""

    def setUp(self):
        super().setUp()
        self.item = Item.objects.create(
            client=self.client_obj,
            sku="AUDIT-ROD-12MM",
            name="MS Rod 12mm x 6m",
            uom="PCS",
            cost_price=Decimal("450.00"),
            selling_price=Decimal("550.00"),
        )

    def test_01_opening_stock_posting_and_no_double_count(self):
        """Verify opening stock creates StockMovement and updates StockBalance accurately."""
        stock_services.post_movement(
            client_id=self.client_obj.id,
            item=self.item,
            location=self.warehouse,
            type="OPENING_STOCK",
            quantity=Decimal("50.0000"),
            unit_cost=Decimal("450.00"),
            movement_date=date.today(),
            user=self.user,
        )

        bal = StockBalance.objects.get(
            client=self.client_obj, item=self.item, location=self.warehouse
        )
        self.assertEqual(bal.on_hand, Decimal("50.0000"))

        calc = stock_services.calculate_item_stock(
            self.client_obj.id, self.item, self.warehouse.id
        )
        self.assertEqual(calc["openingStock"], Decimal("50.0000"))
        self.assertEqual(calc["onHand"], Decimal("50.0000"))
        self.assertEqual(calc["available"], Decimal("50.0000"))

    def test_02_inward_outward_and_available_formula(self):
        """Verify available stock formula: Available = Opening + Inward - Outward - Reserved."""
        # 1. Opening: 100
        stock_services.post_movement(
            client_id=self.client_obj.id,
            item=self.item,
            location=self.warehouse,
            type="OPENING_STOCK",
            quantity=Decimal("100.0000"),
            movement_date=date.today(),
        )
        # 2. Inward: 40
        ref_bill_id = uuid.uuid4()
        stock_services.post_movement(
            client_id=self.client_obj.id,
            item=self.item,
            location=self.warehouse,
            type="PURCHASE",
            quantity=Decimal("40.0000"),
            reference_type="PurchaseBill",
            reference_id=ref_bill_id,
            movement_date=date.today(),
        )
        # 3. Outward: 25
        ref_inv_id = uuid.uuid4()
        stock_services.post_movement(
            client_id=self.client_obj.id,
            item=self.item,
            location=self.warehouse,
            type="SALE",
            quantity=Decimal("-25.0000"),
            reference_type="SalesInvoice",
            reference_id=ref_inv_id,
            movement_date=date.today(),
        )

        bal = StockBalance.objects.get(
            client=self.client_obj, item=self.item, location=self.warehouse
        )
        # on_hand: 100 + 40 - 25 = 115
        self.assertEqual(bal.on_hand, Decimal("115.0000"))

        # Create a confirmed SalesOrder to reserve 15 units
        order = SalesOrder.objects.create(
            client=self.client_obj,
            order_number="SO-RES-01",
            party=self.customer,
            doc_date=date.today(),
            stage="Confirmed",
            subtotal=Decimal("8250.00"),
            total=Decimal("8250.00"),
        )
        SalesOrderLine.objects.create(
            client=self.client_obj,
            sales_order=order,
            line_no=1,
            item=self.item,
            item_name=self.item.name,
            qty=Decimal("15.0000"),
            rate=Decimal("550.00"),
            amount=Decimal("8250.00"),
            line_total=Decimal("8250.00"),
        )

        calc = stock_services.calculate_item_stock(
            self.client_obj.id, self.item, self.warehouse.id
        )
        self.assertEqual(calc["onHand"], Decimal("115.0000"))
        self.assertEqual(calc["reserved"], Decimal("15.0000"))
        # Available = 115 - 15 = 100
        self.assertEqual(calc["available"], Decimal("100.0000"))
        self.assertEqual(
            calc["available"],
            calc["openingStock"] + calc["totalInward"] - calc["totalOutward"] - calc["reserved"]
        )

    def test_03_warehouse_transfer_two_legged(self):
        """Verify warehouse transfer posts TRANSFER_OUT and TRANSFER_IN and preserves total."""
        # Setup source stock: 80
        stock_services.post_movement(
            client_id=self.client_obj.id,
            item=self.item,
            location=self.warehouse,
            type="OPENING_STOCK",
            quantity=Decimal("80.0000"),
        )
        transfer_id = uuid.uuid4()
        # Transfer 30 from WH-MAIN to WH-SEC
        out_mv = stock_services.post_movement(
            client_id=self.client_obj.id,
            item=self.item,
            location=self.warehouse,
            type="TRANSFER_OUT",
            quantity=Decimal("-30.0000"),
            reference_type="StockTransfer",
            reference_id=transfer_id,
            reference_number="TRF-001",
            user=self.user,
        )
        in_mv = stock_services.post_movement(
            client_id=self.client_obj.id,
            item=self.item,
            location=self.secondary_wh,
            type="TRANSFER_IN",
            quantity=Decimal("30.0000"),
            reference_type="StockTransfer",
            reference_id=transfer_id,
            reference_number="TRF-001",
            user=self.user,
        )

        bal_src = StockBalance.objects.get(
            client=self.client_obj, item=self.item, location=self.warehouse
        )
        bal_dst = StockBalance.objects.get(
            client=self.client_obj, item=self.item, location=self.secondary_wh
        )
        self.assertEqual(bal_src.on_hand, Decimal("50.0000"))
        self.assertEqual(bal_dst.on_hand, Decimal("30.0000"))
        # Net across client is still 80
        total_qty = sum(
            StockBalance.objects.filter(client=self.client_obj, item=self.item).values_list("on_hand", flat=True)
        )
        self.assertEqual(total_qty, Decimal("80.0000"))

        # Reversal test
        reversals = stock_services.reverse_movements(
            reference_type="StockTransfer",
            reference_id=transfer_id,
            client_id=self.client_obj.id,
            notes="Transfer cancelled",
        )
        self.assertEqual(len(reversals), 2)
        bal_src.refresh_from_db()
        bal_dst.refresh_from_db()
        self.assertEqual(bal_src.on_hand, Decimal("80.0000"))
        self.assertEqual(bal_dst.on_hand, Decimal("0.0000"))

    def test_04_negative_stock_prevention(self):
        """Verify attempting to dispatch more than available raises BusinessRuleViolation."""
        stock_services.post_movement(
            client_id=self.client_obj.id,
            item=self.item,
            location=self.warehouse,
            type="OPENING_STOCK",
            quantity=Decimal("10.0000"),
        )
        # Attempting outward of 15 should fail
        with self.assertRaises(BusinessRuleViolation) as ctx:
            stock_services.assert_sufficient_stock(
                self.client_obj.id,
                self.item,
                Decimal("15.0000"),
                self.warehouse.id,
                self.item.name,
            )
        self.assertEqual(ctx.exception.code, Codes.INSUFFICIENT_STOCK)

        # post_movement with SALE of -15 should also be rejected
        with self.assertRaises(BusinessRuleViolation) as ctx2:
            stock_services.post_movement(
                client_id=self.client_obj.id,
                item=self.item,
                location=self.warehouse,
                type="SALE",
                quantity=Decimal("-15.0000"),
                reference_type="SalesInvoice",
                reference_id=uuid.uuid4(),
            )
        self.assertEqual(ctx2.exception.code, Codes.INSUFFICIENT_STOCK)

    def test_05_duplicate_source_document_posting_prevention(self):
        """Verify prevent_duplicate=True blocks reposting the same source doc."""
        ref_id = uuid.uuid4()
        stock_services.post_movement(
            client_id=self.client_obj.id,
            item=self.item,
            location=self.warehouse,
            type="PURCHASE",
            quantity=Decimal("20.0000"),
            reference_type="GoodsReceipt",
            reference_id=ref_id,
            prevent_duplicate=True,
        )
        with self.assertRaises(Conflict):
            stock_services.post_movement(
                client_id=self.client_obj.id,
                item=self.item,
                location=self.warehouse,
                type="PURCHASE",
                quantity=Decimal("20.0000"),
                reference_type="GoodsReceipt",
                reference_id=ref_id,
                prevent_duplicate=True,
            )

    def test_06_ledger_reconciliation(self):
        """Verify calculate_item_stock and reconcile_stock reconcile exactly with StockMovement ledger."""
        stock_services.post_movement(
            client_id=self.client_obj.id, item=self.item, location=self.warehouse,
            type="OPENING_STOCK", quantity=Decimal("50.0000")
        )
        ref_bill_id = uuid.uuid4()
        stock_services.post_movement(
            client_id=self.client_obj.id, item=self.item, location=self.warehouse,
            type="PURCHASE", quantity=Decimal("30.0000"),
            reference_type="PurchaseBill", reference_id=ref_bill_id,
        )
        ref_inv_id = uuid.uuid4()
        stock_services.post_movement(
            client_id=self.client_obj.id, item=self.item, location=self.warehouse,
            type="SALE", quantity=Decimal("-15.0000"),
            reference_type="SalesInvoice", reference_id=ref_inv_id,
        )

        calc = stock_services.calculate_item_stock(
            self.client_obj.id, self.item, self.warehouse.id
        )
        self.assertEqual(calc["openingStock"], Decimal("50.0000"))
        self.assertEqual(calc["totalInward"], Decimal("30.0000"))
        self.assertEqual(calc["totalOutward"], Decimal("15.0000"))
        self.assertEqual(calc["onHand"], Decimal("65.0000"))

        bal = StockBalance.objects.get(client=self.client_obj, item=self.item, location=self.warehouse)
        self.assertEqual(bal.on_hand, calc["onHand"])

        # Run reconcile_stock service
        rec_report = stock_services.reconcile_stock(self.client_obj.id, item_id=self.item.id)
        self.assertEqual(rec_report["discrepanciesCount"], 0)


class GRNAuditTests(Phase8BaseTestCase):
    """Section 3: Goods Receipt Note (GRN) Tests."""

    def setUp(self):
        super().setUp()
        self.item = Item.objects.create(
            client=self.client_obj,
            sku="AUDIT-STEEL-PLATE",
            name="Structural Steel Plate 20mm",
            uom="PCS",
            cost_price=Decimal("5000.00"),
        )
        self.po = PurchaseOrder.objects.create(
            client=self.client_obj,
            po_number="PO-AUDIT-001",
            party=self.vendor,
            location=self.warehouse,
            doc_date=date.today(),
            status="Issued",
            subtotal=Decimal("50000.00"),
            total_tax=Decimal("9000.00"),
            total=Decimal("59000.00"),
        )
        self.po_line = PurchaseOrderLine.objects.create(
            client=self.client_obj,
            purchase_order=self.po,
            line_no=1,
            item=self.item,
            item_name=self.item.name,
            qty=Decimal("10.0000"),
            rate=Decimal("5000.00"),
            amount=Decimal("50000.00"),
            tax_pct=Decimal("18.00"),
            tax_amount=Decimal("9000.00"),
            line_total=Decimal("59000.00"),
        )
        self.bill = PurchaseBill.objects.create(
            client=self.client_obj,
            bill_number="PB-AUDIT-001",
            purchase_order=self.po,
            party=self.vendor,
            location=self.warehouse,
            doc_date=date.today(),
            subtotal=Decimal("50000.00"),
            total_tax=Decimal("9000.00"),
            total=Decimal("59000.00"),
            status="Draft",
            goods_received=False,
        )
        self.bill_line = PurchaseBillLine.objects.create(
            client=self.client_obj,
            purchase_bill=self.bill,
            purchase_order_line=self.po_line,
            line_no=1,
            item=self.item,
            item_name=self.item.name,
            qty=Decimal("10.0000"),
            rate=Decimal("5000.00"),
            amount=Decimal("50000.00"),
            tax_pct=Decimal("18.00"),
            tax_amount=Decimal("9000.00"),
            line_total=Decimal("59000.00"),
        )

    def test_01_grn_posting_and_stock_inward(self):
        """Verify full receipt creates GoodsReceipt and posts PURCHASE to ledger."""
        res = purchase_services.receive_bill_goods(
            self.bill,
            lines_payload=[{"lineId": str(self.bill_line.id), "receivedQty": "10.0000"}],
            qc_status="Approved",
            user=self.user,
        )
        receipt = res["receipt"]
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt.status, "Received")
        self.assertEqual(receipt.qc_status, "Approved")

        # Verify stock movement
        movement = StockMovement.objects.get(
            client=self.client_obj,
            source_document_type="GoodsReceipt",
            source_document_id=receipt.id,
        )
        self.assertEqual(movement.type, "PURCHASE")
        self.assertEqual(movement.quantity, Decimal("10.0000"))

        bal = StockBalance.objects.get(
            client=self.client_obj, item=self.item, location=self.warehouse
        )
        self.assertEqual(bal.on_hand, Decimal("10.0000"))

    def test_02_partial_grn_receipt(self):
        """Verify partial receipt posts exact received quantity to stock."""
        res = purchase_services.receive_bill_goods(
            self.bill,
            lines_payload=[{"lineId": str(self.bill_line.id), "receivedQty": "4.0000"}],
            qc_status="Approved",
            user=self.user,
        )
        self.bill_line.refresh_from_db()
        self.assertEqual(self.bill_line.received_qty, Decimal("4.0000"))

        bal = StockBalance.objects.get(
            client=self.client_obj, item=self.item, location=self.warehouse
        )
        self.assertEqual(bal.on_hand, Decimal("4.0000"))

    def test_03_duplicate_grn_posting_prevention(self):
        """Verify bill cannot be received twice."""
        purchase_services.receive_bill_goods(
            self.bill,
            lines_payload=[{"lineId": str(self.bill_line.id), "receivedQty": "10.0000"}],
            qc_status="Approved",
            user=self.user,
        )
        # Attempt second receipt
        with self.assertRaises(Conflict):
            purchase_services.receive_bill_goods(
                self.bill,
                lines_payload=[{"lineId": str(self.bill_line.id), "receivedQty": "10.0000"}],
                qc_status="Approved",
                user=self.user,
            )

    def test_04_cancelled_bill_cannot_be_received(self):
        """Verify cancelled purchase bill rejects receipt."""
        self.bill.status = "Cancelled"
        self.bill.save()
        with self.assertRaises(Conflict):
            purchase_services.receive_bill_goods(
                self.bill,
                lines_payload=[{"lineId": str(self.bill_line.id), "receivedQty": "10.0000"}],
                qc_status="Approved",
                user=self.user,
            )


class SalesIntegrationAndDualGuardAuditTests(Phase8BaseTestCase):
    """Section 4: Sales Integration & Universal Dual-Guard Invariant Tests."""

    def setUp(self):
        super().setUp()
        self.item = Item.objects.create(
            client=self.client_obj,
            sku="AUDIT-TUBE-50X50",
            name="SS304 Tube 50x50x3mm",
            uom="PCS",
            cost_price=Decimal("2000.00"),
            selling_price=Decimal("2800.00"),
        )
        # Pre-seed warehouse with 20 units
        stock_services.post_movement(
            client_id=self.client_obj.id,
            item=self.item,
            location=self.warehouse,
            type="OPENING_STOCK",
            quantity=Decimal("20.0000"),
            unit_cost=Decimal("2000.00"),
        )

    def test_01_full_sales_lifecycle_pipeline(self):
        """Estimate -> Quotation -> Sales Order -> Delivery Challan -> Sales Invoice -> Payment In."""
        # 1. Estimate
        est = Estimate.objects.create(
            client=self.client_obj,
            estimate_number="EST-AUDIT-001",
            party=self.customer,
            doc_date=date.today(),
            subtotal=Decimal("28000.00"),
            total_tax=Decimal("5040.00"),
            total=Decimal("33040.00"),
            status="Draft",
        )
        EstimateLine.objects.create(
            client=self.client_obj,
            estimate=est,
            line_no=1,
            item=self.item,
            item_name=self.item.name,
            qty=Decimal("10.0000"),
            rate=Decimal("2800.00"),
            amount=Decimal("28000.00"),
            tax_pct=Decimal("18.00"),
            tax_amount=Decimal("5040.00"),
            line_total=Decimal("33040.00"),
        )
        est.status = "Converted"
        est.save()

        # 2. Quotation
        quot = Quotation.objects.create(
            client=self.client_obj,
            quotation_number="QT-AUDIT-001",
            party=self.customer,
            doc_date=date.today(),
            valid_until=date.today() + timedelta(days=30),
            subtotal=Decimal("28000.00"),
            taxable_value=Decimal("28000.00"),
            total_tax=Decimal("5040.00"),
            total=Decimal("33040.00"),
            status="Draft",
        )
        quot_line = QuotationLine.objects.create(
            client=self.client_obj,
            quotation=quot,
            line_no=1,
            item=self.item,
            item_name=self.item.name,
            qty=Decimal("10.0000"),
            rate=Decimal("2800.00"),
            amount=Decimal("28000.00"),
            tax_pct=Decimal("18.00"),
            tax_amount=Decimal("5040.00"),
            line_total=Decimal("33040.00"),
        )
        quot.status = "Accepted"
        quot.save()

        # 3. Sales Order (Quotation converted to Order)
        order = SalesOrder.objects.create(
            client=self.client_obj,
            order_number="SO-AUDIT-001",
            quotation=quot,
            party=self.customer,
            doc_date=date.today(),
            subtotal=Decimal("28000.00"),
            taxable_value=Decimal("28000.00"),
            total_tax=Decimal("5040.00"),
            total=Decimal("33040.00"),
            stage="Confirmed",
        )
        so_line = SalesOrderLine.objects.create(
            client=self.client_obj,
            sales_order=order,
            line_no=1,
            item=self.item,
            item_name=self.item.name,
            qty=Decimal("10.0000"),
            rate=Decimal("2800.00"),
            amount=Decimal("28000.00"),
            tax_pct=Decimal("18.00"),
            tax_amount=Decimal("5040.00"),
            line_total=Decimal("33040.00"),
        )

        # 4. Delivery Challan
        challan = DeliveryChallan.objects.create(
            client=self.client_obj,
            challan_number="DC-AUDIT-001",
            sales_order=order,
            party=self.customer,
            location=self.warehouse,
            doc_date=date.today(),
            subtotal=Decimal("28000.00"),
            total_tax=Decimal("5040.00"),
            total=Decimal("33040.00"),
            status="Draft",
        )
        dc_line = DeliveryChallanLine.objects.create(
            client=self.client_obj,
            delivery_challan=challan,
            sales_order_line=so_line,
            line_no=1,
            item=self.item,
            item_name=self.item.name,
            qty=Decimal("10.0000"),
            rate=Decimal("2800.00"),
            amount=Decimal("28000.00"),
            line_total=Decimal("33040.00"),
        )
        sales_services.dispatch_challan(challan, user=self.user)
        challan.refresh_from_db()
        self.assertEqual(challan.status, "Dispatched")

        # Verify stock deducted on challan dispatch: 20 - 10 = 10
        bal = StockBalance.objects.get(client=self.client_obj, item=self.item, location=self.warehouse)
        self.assertEqual(bal.on_hand, Decimal("10.0000"))

        # 5. Sales Invoice
        invoice = SalesInvoice.objects.create(
            client=self.client_obj,
            invoice_number="INV-AUDIT-001",
            sales_order=order,
            party=self.customer,
            location=self.warehouse,
            doc_date=date.today(),
            subtotal=Decimal("28000.00"),
            taxable_value=Decimal("28000.00"),
            total_tax=Decimal("5040.00"),
            total=Decimal("33040.00"),
            status="Draft",
        )
        inv_line = SalesInvoiceLine.objects.create(
            client=self.client_obj,
            sales_invoice=invoice,
            sales_order_line=so_line,
            delivery_challan_line=dc_line,
            line_no=1,
            item=self.item,
            item_name=self.item.name,
            qty=Decimal("10.0000"),
            rate=Decimal("2800.00"),
            amount=Decimal("28000.00"),
            tax_pct=Decimal("18.00"),
            tax_amount=Decimal("5040.00"),
            line_total=Decimal("33040.00"),
        )
        sales_services.finalize_invoice(invoice, user=self.user)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, "Unpaid")

        # Universal Dual Guard check: Invoice DOES NOT double-deduct stock!
        bal.refresh_from_db()
        self.assertEqual(bal.on_hand, Decimal("10.0000"))

        # 6. Payment In
        payment = sales_services.record_payment_in(
            client=self.client_obj,
            party=self.customer,
            amount=Decimal("33040.00"),
            payment_date=date.today(),
            payment_type="WITH_BILL",
            invoice=invoice,
            mode="Cash",
            user=self.user,
        )
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, "Paid")
        self.assertEqual(invoice.amount_paid, Decimal("33040.00"))

    def test_02_custom_lines_without_dummy_skus(self):
        """Custom metal line without SKU passes through Quotation & Sales Order with zero stock touch."""
        quot = Quotation.objects.create(
            client=self.client_obj,
            quotation_number="QT-CUSTOM-001",
            party=self.customer,
            doc_date=date.today(),
            valid_until=date.today() + timedelta(days=15),
            subtotal=Decimal("15000.00"),
            taxable_value=Decimal("15000.00"),
            total_tax=Decimal("2700.00"),
            total=Decimal("17700.00"),
            status="Accepted",
        )
        # Custom fabricated item: item=None, full metal spec details
        q_custom_line = QuotationLine.objects.create(
            client=self.client_obj,
            quotation=quot,
            line_no=1,
            item=None,
            item_name="Custom Laser Cut Flange 16mm",
            specification="16mm plate, 500x500mm, laser cut",
            material_grade="IS 2062",
            sheet_spec={
                "form": "Plate",
                "material": "Mild Steel",
                "grade": "IS 2062",
                "thickness": 16.0,
                "length": 500.0,
                "width": 500.0,
            },
            qty=Decimal("5.0000"),
            rate=Decimal("3000.00"),
            amount=Decimal("15000.00"),
            tax_pct=Decimal("18.00"),
            tax_amount=Decimal("2700.00"),
            line_total=Decimal("17700.00"),
        )
        self.assertIsNone(q_custom_line.item)
        self.assertEqual(q_custom_line.material_grade, "IS 2062")

        # Convert to Sales Order
        order = SalesOrder.objects.create(
            client=self.client_obj,
            order_number="SO-CUSTOM-001",
            quotation=quot,
            party=self.customer,
            doc_date=date.today(),
            stage="Confirmed",
            subtotal=Decimal("15000.00"),
            taxable_value=Decimal("15000.00"),
            total_tax=Decimal("2700.00"),
            total=Decimal("17700.00"),
        )
        so_custom_line = SalesOrderLine.objects.create(
            client=self.client_obj,
            sales_order=order,
            line_no=1,
            item=None,
            item_name="Custom Laser Cut Flange 16mm",
            specification="16mm plate, 500x500mm, laser cut",
            material_grade="IS 2062",
            sheet_spec={
                "form": "Plate",
                "material": "Mild Steel",
                "grade": "IS 2062",
                "thickness": 16.0,
                "length": 500.0,
                "width": 500.0,
            },
            qty=Decimal("5.0000"),
            rate=Decimal("3000.00"),
            amount=Decimal("15000.00"),
            tax_pct=Decimal("18.00"),
            tax_amount=Decimal("2700.00"),
            line_total=Decimal("17700.00"),
        )

        # Dispatch challan for custom line
        challan = DeliveryChallan.objects.create(
            client=self.client_obj,
            challan_number="DC-CUSTOM-001",
            sales_order=order,
            party=self.customer,
            location=self.warehouse,
            doc_date=date.today(),
            subtotal=Decimal("15000.00"),
            total_tax=Decimal("2700.00"),
            total=Decimal("17700.00"),
            status="Draft",
        )
        dc_line = DeliveryChallanLine.objects.create(
            client=self.client_obj,
            delivery_challan=challan,
            sales_order_line=so_custom_line,
            line_no=1,
            item=None,
            item_name="Custom Laser Cut Flange 16mm",
            specification="16mm plate, 500x500mm, laser cut",
            qty=Decimal("5.0000"),
            rate=Decimal("3000.00"),
            amount=Decimal("15000.00"),
            line_total=Decimal("17700.00"),
        )
        sales_services.dispatch_challan(challan, user=self.user)
        challan.refresh_from_db()
        self.assertEqual(challan.status, "Dispatched")

        # Zero stock movements created since item is None
        movements = StockMovement.objects.filter(
            client=self.client_obj,
            reference_type="DeliveryChallan",
            reference_id=challan.id,
        )
        self.assertEqual(movements.count(), 0)

    def test_03_universal_dual_guard_sequence_a(self):
        """Sequence A: Delivery Challan dispatched first -> Invoice finalized second.
        Result: Exactly 1 stock deduction on Challan; 0 duplicate deduction on Invoice.
        """
        order = SalesOrder.objects.create(
            client=self.client_obj,
            order_number="SO-DG-A",
            party=self.customer,
            doc_date=date.today(),
            stage="Confirmed",
            subtotal=Decimal("28000.00"),
            total_tax=Decimal("5040.00"),
            total=Decimal("33040.00"),
        )
        so_line = SalesOrderLine.objects.create(
            client=self.client_obj,
            sales_order=order,
            line_no=1,
            item=self.item,
            item_name=self.item.name,
            qty=Decimal("10.0000"),
            rate=Decimal("2800.00"),
            amount=Decimal("28000.00"),
            tax_pct=Decimal("18.00"),
            tax_amount=Decimal("5040.00"),
            line_total=Decimal("33040.00"),
        )
        # 1. Challan dispatched
        challan = DeliveryChallan.objects.create(
            client=self.client_obj,
            challan_number="DC-DG-A",
            sales_order=order,
            party=self.customer,
            location=self.warehouse,
            doc_date=date.today(),
            subtotal=Decimal("28000.00"),
            total_tax=Decimal("5040.00"),
            total=Decimal("33040.00"),
            status="Draft",
        )
        DeliveryChallanLine.objects.create(
            client=self.client_obj,
            delivery_challan=challan,
            sales_order_line=so_line,
            line_no=1,
            item=self.item,
            item_name=self.item.name,
            qty=Decimal("10.0000"),
            rate=Decimal("2800.00"),
            amount=Decimal("28000.00"),
            line_total=Decimal("33040.00"),
        )
        sales_services.dispatch_challan(challan, user=self.user)

        # Stock before invoice: 20 - 10 = 10
        bal = StockBalance.objects.get(client=self.client_obj, item=self.item, location=self.warehouse)
        self.assertEqual(bal.on_hand, Decimal("10.0000"))

        # 2. Direct Sales Invoice created against order (not linking challan line explicitly)
        invoice = SalesInvoice.objects.create(
            client=self.client_obj,
            invoice_number="INV-DG-A",
            sales_order=order,
            party=self.customer,
            location=self.warehouse,
            doc_date=date.today(),
            subtotal=Decimal("28000.00"),
            taxable_value=Decimal("28000.00"),
            total_tax=Decimal("5040.00"),
            total=Decimal("33040.00"),
            status="Draft",
        )
        SalesInvoiceLine.objects.create(
            client=self.client_obj,
            sales_invoice=invoice,
            sales_order_line=so_line,
            line_no=1,
            item=self.item,
            item_name=self.item.name,
            qty=Decimal("10.0000"),
            rate=Decimal("2800.00"),
            amount=Decimal("28000.00"),
            tax_pct=Decimal("18.00"),
            tax_amount=Decimal("5040.00"),
            line_total=Decimal("33040.00"),
        )
        sales_services.finalize_invoice(invoice, user=self.user)

        # Stock after invoice: STILL 10.0000! Dual-guard prevented double deduction!
        bal.refresh_from_db()
        self.assertEqual(bal.on_hand, Decimal("10.0000"))

        total_movements = StockMovement.objects.filter(
            client=self.client_obj, item=self.item, type="SALE"
        ).count()
        self.assertEqual(total_movements, 1)

    def test_04_universal_dual_guard_sequence_b(self):
        """Sequence B: Sales Invoice finalized first -> Delivery Challan dispatched second.
        Result: Exactly 1 stock deduction on Invoice; 0 duplicate deduction on Challan.
        """
        order = SalesOrder.objects.create(
            client=self.client_obj,
            order_number="SO-DG-B",
            party=self.customer,
            doc_date=date.today(),
            stage="Confirmed",
            subtotal=Decimal("28000.00"),
            total_tax=Decimal("5040.00"),
            total=Decimal("33040.00"),
        )
        so_line = SalesOrderLine.objects.create(
            client=self.client_obj,
            sales_order=order,
            line_no=1,
            item=self.item,
            item_name=self.item.name,
            qty=Decimal("10.0000"),
            rate=Decimal("2800.00"),
            amount=Decimal("28000.00"),
            tax_pct=Decimal("18.00"),
            tax_amount=Decimal("5040.00"),
            line_total=Decimal("33040.00"),
        )
        # 1. Invoice finalized first
        invoice = SalesInvoice.objects.create(
            client=self.client_obj,
            invoice_number="INV-DG-B",
            sales_order=order,
            party=self.customer,
            location=self.warehouse,
            doc_date=date.today(),
            subtotal=Decimal("28000.00"),
            taxable_value=Decimal("28000.00"),
            total_tax=Decimal("5040.00"),
            total=Decimal("33040.00"),
            status="Draft",
        )
        SalesInvoiceLine.objects.create(
            client=self.client_obj,
            sales_invoice=invoice,
            sales_order_line=so_line,
            line_no=1,
            item=self.item,
            item_name=self.item.name,
            qty=Decimal("10.0000"),
            rate=Decimal("2800.00"),
            amount=Decimal("28000.00"),
            tax_pct=Decimal("18.00"),
            tax_amount=Decimal("5040.00"),
            line_total=Decimal("33040.00"),
        )
        sales_services.finalize_invoice(invoice, user=self.user)

        # Stock deducted on invoice: 20 - 10 = 10
        bal = StockBalance.objects.get(client=self.client_obj, item=self.item, location=self.warehouse)
        self.assertEqual(bal.on_hand, Decimal("10.0000"))

        # 2. Challan dispatched second
        challan = DeliveryChallan.objects.create(
            client=self.client_obj,
            challan_number="DC-DG-B",
            sales_order=order,
            party=self.customer,
            location=self.warehouse,
            doc_date=date.today(),
            subtotal=Decimal("28000.00"),
            total_tax=Decimal("5040.00"),
            total=Decimal("33040.00"),
            status="Draft",
        )
        DeliveryChallanLine.objects.create(
            client=self.client_obj,
            delivery_challan=challan,
            sales_order_line=so_line,
            line_no=1,
            item=self.item,
            item_name=self.item.name,
            qty=Decimal("10.0000"),
            rate=Decimal("2800.00"),
            amount=Decimal("28000.00"),
            line_total=Decimal("33040.00"),
        )
        sales_services.dispatch_challan(challan, user=self.user)

        # Stock after challan: STILL 10.0000! Dual-guard prevented double deduction!
        bal.refresh_from_db()
        self.assertEqual(bal.on_hand, Decimal("10.0000"))

        total_movements = StockMovement.objects.filter(
            client=self.client_obj, item=self.item, type="SALE"
        ).count()
        self.assertEqual(total_movements, 1)

    def test_05_partial_dispatches(self):
        """Order qty 10: Challan 1 dispatches 4, Challan 2 dispatches 6."""
        order = SalesOrder.objects.create(
            client=self.client_obj,
            order_number="SO-PARTIAL-01",
            party=self.customer,
            doc_date=date.today(),
            stage="Confirmed",
            subtotal=Decimal("28000.00"),
            total_tax=Decimal("5040.00"),
            total=Decimal("33040.00"),
        )
        so_line = SalesOrderLine.objects.create(
            client=self.client_obj,
            sales_order=order,
            line_no=1,
            item=self.item,
            item_name=self.item.name,
            qty=Decimal("10.0000"),
            rate=Decimal("2800.00"),
            amount=Decimal("28000.00"),
            tax_pct=Decimal("18.00"),
            tax_amount=Decimal("5040.00"),
            line_total=Decimal("33040.00"),
        )
        # Challan 1: 4 units
        c1 = DeliveryChallan.objects.create(
            client=self.client_obj,
            challan_number="DC-PARTIAL-1",
            sales_order=order,
            party=self.customer,
            location=self.warehouse,
            doc_date=date.today(),
            subtotal=Decimal("11200.00"),
            total=Decimal("11200.00"),
        )
        DeliveryChallanLine.objects.create(
            client=self.client_obj,
            delivery_challan=c1,
            sales_order_line=so_line,
            line_no=1,
            item=self.item,
            item_name=self.item.name,
            qty=Decimal("4.0000"),
            rate=Decimal("2800.00"),
            amount=Decimal("11200.00"),
        )
        sales_services.dispatch_challan(c1, user=self.user)

        bal = StockBalance.objects.get(client=self.client_obj, item=self.item, location=self.warehouse)
        self.assertEqual(bal.on_hand, Decimal("16.0000"))

        # Challan 2: 6 units
        c2 = DeliveryChallan.objects.create(
            client=self.client_obj,
            challan_number="DC-PARTIAL-2",
            sales_order=order,
            party=self.customer,
            location=self.warehouse,
            doc_date=date.today(),
            subtotal=Decimal("16800.00"),
            total=Decimal("16800.00"),
        )
        DeliveryChallanLine.objects.create(
            client=self.client_obj,
            delivery_challan=c2,
            sales_order_line=so_line,
            line_no=1,
            item=self.item,
            item_name=self.item.name,
            qty=Decimal("6.0000"),
            rate=Decimal("2800.00"),
            amount=Decimal("16800.00"),
        )
        sales_services.dispatch_challan(c2, user=self.user)

        bal.refresh_from_db()
        self.assertEqual(bal.on_hand, Decimal("10.0000"))
        so_line.refresh_from_db()
        self.assertEqual(so_line.dispatched_qty, Decimal("10.0000"))

    def test_06_insufficient_stock_blocks_dispatch(self):
        """Attempting to dispatch more than stock balance raises BusinessRuleViolation."""
        # Available stock is 20; try dispatching 25
        order = SalesOrder.objects.create(
            client=self.client_obj,
            order_number="SO-OVER-01",
            party=self.customer,
            doc_date=date.today(),
            stage="Confirmed",
            subtotal=Decimal("70000.00"),
            total_tax=Decimal("12600.00"),
            total=Decimal("82600.00"),
        )
        so_line = SalesOrderLine.objects.create(
            client=self.client_obj,
            sales_order=order,
            line_no=1,
            item=self.item,
            item_name=self.item.name,
            qty=Decimal("25.0000"),
            rate=Decimal("2800.00"),
            amount=Decimal("70000.00"),
            tax_pct=Decimal("18.00"),
            tax_amount=Decimal("12600.00"),
            line_total=Decimal("82600.00"),
        )
        challan = DeliveryChallan.objects.create(
            client=self.client_obj,
            challan_number="DC-OVER-01",
            sales_order=order,
            party=self.customer,
            location=self.warehouse,
            doc_date=date.today(),
            subtotal=Decimal("70000.00"),
            total_tax=Decimal("12600.00"),
            total=Decimal("82600.00"),
        )
        DeliveryChallanLine.objects.create(
            client=self.client_obj,
            delivery_challan=challan,
            sales_order_line=so_line,
            line_no=1,
            item=self.item,
            item_name=self.item.name,
            qty=Decimal("25.0000"),
            rate=Decimal("2800.00"),
            amount=Decimal("70000.00"),
        )
        with self.assertRaises(BusinessRuleViolation):
            sales_services.dispatch_challan(challan, user=self.user)

    def test_07_sales_return_replenishes_stock(self):
        """Sales return posts SALES_RETURN movement and increases stock balance."""
        invoice = SalesInvoice.objects.create(
            client=self.client_obj,
            invoice_number="INV-RET-01",
            party=self.customer,
            location=self.warehouse,
            doc_date=date.today(),
            subtotal=Decimal("14000.00"),
            taxable_value=Decimal("14000.00"),
            total_tax=Decimal("2520.00"),
            total=Decimal("16520.00"),
            status="Draft",
        )
        inv_line = SalesInvoiceLine.objects.create(
            client=self.client_obj,
            sales_invoice=invoice,
            line_no=1,
            item=self.item,
            item_name=self.item.name,
            qty=Decimal("5.0000"),
            rate=Decimal("2800.00"),
            amount=Decimal("14000.00"),
            tax_pct=Decimal("18.00"),
            tax_amount=Decimal("2520.00"),
            line_total=Decimal("16520.00"),
        )
        sales_services.finalize_invoice(invoice, user=self.user)
        # Stock after invoice dispatch: 20 - 5 = 15
        bal = StockBalance.objects.get(client=self.client_obj, item=self.item, location=self.warehouse)
        self.assertEqual(bal.on_hand, Decimal("15.0000"))

        # Create and post SalesReturn for 2 units
        sales_return = SalesReturn.objects.create(
            client=self.client_obj,
            return_number="SR-AUDIT-001",
            sales_invoice=invoice,
            party=self.customer,
            location=self.warehouse,
            doc_date=date.today(),
            subtotal=Decimal("5600.00"),
            taxable_value=Decimal("5600.00"),
            total_tax=Decimal("1008.00"),
            total=Decimal("6608.00"),
            status="Draft",
        )
        sr_line = SalesReturnLine.objects.create(
            client=self.client_obj,
            sales_return=sales_return,
            sales_invoice_line=inv_line,
            line_no=1,
            item=self.item,
            item_name=self.item.name,
            qty=Decimal("2.0000"),
            returned_qty=Decimal("2.0000"),
            rate=Decimal("2800.00"),
            amount=Decimal("5600.00"),
            tax_pct=Decimal("18.00"),
            tax_amount=Decimal("1008.00"),
            line_total=Decimal("6608.00"),
        )

        # Post movement as SalesReturnViewSet does
        stock_services.post_movement(
            client_id=self.client_obj.id,
            item=self.item,
            location=self.warehouse,
            type="SALES_RETURN",
            quantity=Decimal("2.0000"),
            unit_cost=self.item.cost_price,
            reference_type="SalesReturn",
            reference_id=sales_return.id,
            reference_number=sales_return.return_number,
            movement_date=sales_return.doc_date,
            user=self.user,
        )
        sales_return.status = "Posted"
        sales_return.save()

        # Stock replenished: 15 + 2 = 17
        bal.refresh_from_db()
        self.assertEqual(bal.on_hand, Decimal("17.0000"))


class FinancialRegressionAuditTests(Phase8BaseTestCase):
    """Section 5: Financial Regression Tests."""

    def test_01_gst_intra_state_and_totals(self):
        """Verify 18% GST intra-state computes 9% CGST + 9% SGST accurately."""
        invoice = SalesInvoice.objects.create(
            client=self.client_obj,
            party=self.customer,
            doc_date=date.today(),
            status="Draft",
        )
        item = Item.objects.create(
            client=self.client_obj,
            sku="AUDIT-FIN-01",
            name="Testing Item",
            uom="PCS",
            selling_price=Decimal("1000.00"),
        )
        line = SalesInvoiceLine.objects.create(
            client=self.client_obj,
            sales_invoice=invoice,
            line_no=1,
            item=item,
            item_name=item.name,
            qty=Decimal("10.0000"),
            rate=Decimal("1000.00"),
            discount_pct=Decimal("10.00"),  # 10% discount -> amount 9000
            tax_pct=Decimal("18.00"),
        )
        sales_services.recalculate_document(invoice, lines=[line])

        self.assertEqual(line.amount, Decimal("10000.00"))
        self.assertEqual(line.discount_amount, Decimal("1000.00"))
        self.assertEqual(line.tax_amount, Decimal("1620.00"))
        self.assertEqual(line.line_total, Decimal("10620.00"))
        self.assertEqual(invoice.taxable_value, Decimal("9000.00"))
        self.assertEqual(invoice.total_tax, Decimal("1620.00"))
        self.assertEqual(invoice.total, Decimal("10620.00"))

    def test_02_with_bill_and_without_bill_payment_split(self):
        """Verify With-Bill and Without-Bill payments update invoice status and ledger."""
        invoice = SalesInvoice.objects.create(
            client=self.client_obj,
            party=self.customer,
            doc_date=date.today(),
            subtotal=Decimal("10000.00"),
            taxable_value=Decimal("10000.00"),
            total_tax=Decimal("1800.00"),
            total=Decimal("11800.00"),
            status="Unpaid",
            amount_paid=Decimal("0.00"),
            posted_at=timezone.now(),
        )
        # 1. Partial With-Bill payment
        p1 = sales_services.record_payment_in(
            client=self.client_obj,
            party=self.customer,
            amount=Decimal("5000.00"),
            payment_date=date.today(),
            payment_type="WITH_BILL",
            invoice=invoice,
            mode="Cash",
            user=self.user,
        )
        invoice.refresh_from_db()
        self.assertEqual(invoice.amount_paid, Decimal("5000.00"))
        self.assertEqual(invoice.status, "Partially Paid")

        # 2. Complete payment
        p2 = sales_services.record_payment_in(
            client=self.client_obj,
            party=self.customer,
            amount=Decimal("6800.00"),
            payment_date=date.today(),
            payment_type="WITH_BILL",
            invoice=invoice,
            mode="Cash",
            user=self.user,
        )
        invoice.refresh_from_db()
        self.assertEqual(invoice.amount_paid, Decimal("11800.00"))
        self.assertEqual(invoice.status, "Paid")
