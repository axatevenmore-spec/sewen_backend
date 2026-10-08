"""
Seed comprehensive project data for Sweven Fabricators according to the Dev Spec PDF.

Populates:
- Roles & RBAC: Administrator, Sales Manager, Project Manager, HR Manager,
  Accountant, Purchase Officer, Store Keeper, Sales Executive, Employee, Customer
- Users: Employees, Customers, Managers, Executives, Administrators (Password: Sweven@123)
- HRMS: Departments, Designations, Employee profiles, linked to users
- Masters: Units, Warehouses/Locations, Item Categories, Items (Machines, Spares, MS Weight items, Fabrications), Parties (Customers & Vendors with weight variation tolerances)
- Inventory: Stock movements and opening balances
- CRM: Leads across pipeline stages (New Lead, Details Collected, Quotation Shared, Negotiation, Won)
- Sales: Quotations and confirmed Sales Orders
- Purchase: Purchase Orders with steel weight parameters
- PMS: Projects with stages, completion progress, PM assignments, and Customer Tracking access
- Accounting: Bank accounts and Chart of Accounts
"""

from decimal import Decimal
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from apps.core.tenancy import tenant_context
from apps.core.tenant_setup import bootstrap_configuration
from apps.accounts.models import Client, Role, Permission, RolePermission, User
from apps.accounts.permission_catalogue import sync_permissions, seed_roles
from apps.core.models import CompanyProfile, Setting
from apps.masters.models import Unit, Location, ItemCategory, Item, Party, PartyContact
from apps.inventory.models import StockMovement
from apps.hrms.models import Department, Designation, Employee
from apps.crm.models import Stage, Lead, LeadProduct
from apps.sales.models import Quotation, QuotationLine, SalesOrder, SalesOrderLine
from apps.purchase.models import PurchaseOrder, PurchaseOrderLine
from apps.pms.models import Project, ProjectStage, StageConfig
from apps.pms import models as pms_models
from apps.accounting.models import Account, BankAccount


class Command(BaseCommand):
    help = "Seed realistic data according to Sweven Functional Spec PDF."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", default="sweven", help="Tenant slug (default: sweven).")
        parser.add_argument(
            "--email-domain", default="sweven.com",
            help="Domain for the seeded logins. Another tenant needs its own: login "
                 "refuses an address that exists in two tenants.",
        )
        parser.add_argument("--password", default="Sweven@123", help="Password for every seeded login.")

    def handle(self, *args, **options):
        slug = options["tenant"]
        self.email_domain = options.get("email_domain") or "sweven.com"
        self.password = options.get("password") or "Sweven@123"
        client = Client.objects.filter(slug=slug).first()
        if not client:
            client = Client.objects.create(
                slug=slug,
                name="Sweven Fabricators",
                plan="Enterprise",
                currency="INR",
                fy_start_month=4,
                contact_name="Management Sweven",
                contact_email="ceo@evenmore.in",
                contact_phone="+91 99988 67024",
                industry="Metal Fabrication & Machinery",
                onboarded_on=timezone.localdate(),
            )
            self.stdout.write(self.style.SUCCESS(f"Created client '{client.name}'"))
        else:
            self.stdout.write(f"Using client '{client.name}'")

        with tenant_context(client.id, push_to_db=False):
            with transaction.atomic():
                self.stdout.write("1. Bootstrapping configuration & permissions...")
                sync_permissions()
                seed_roles(client)
                bootstrap_configuration(client)

                self.stdout.write("2. Configuring Company Profile...")
                self._seed_company_profile(client)

                self.stdout.write("3. Configuring Roles & RBAC (including Sales Executive)...")
                roles = self._seed_roles_and_permissions(client)

                self.stdout.write("4. Creating HRMS Departments & Designations...")
                depts, desigs = self._seed_hrms_structure(client)

                self.stdout.write("5. Creating Locations (Warehouses)...")
                locations = self._seed_locations(client)

                self.stdout.write("6. Creating Item Categories & Items (Machines, Spares, Weight-based Steel)...")
                items = self._seed_items(client, locations)

                self.stdout.write("7. Creating Parties (Customers & Vendors with Weight Tolerances)...")
                parties = self._seed_parties(client)

                self.stdout.write("8. Creating Users & linking HRMS Employees...")
                users = self._seed_users_and_employees(client, roles, depts, desigs, parties)

                self.stdout.write("9. Recording Opening Stock Movements...")
                self._seed_stock(client, items, locations)

                self.stdout.write("10. Seeding CRM Pipeline & Leads...")
                self._seed_crm(client, users, parties, items)

                self.stdout.write("11. Seeding Sales & Purchase Documents...")
                sales_orders = self._seed_sales_and_purchase(client, parties, items, users, locations)

                self.stdout.write("12. Seeding PMS Projects & Progress Stages...")
                self._seed_pms_projects(client, parties, sales_orders, users)

                self.stdout.write("13. Seeding Bank Accounts...")
                self._seed_bank_accounts(client)

        self.stdout.write(self.style.SUCCESS("[SUCCESS] Successfully entered all project data according to PDF spec and configured RBAC!"))

    def _seed_company_profile(self, client):
        profile, _ = CompanyProfile.objects.get_or_create(
            client=client,
            defaults={
                "legal_name": "Sweven Fabricators Pvt. Ltd.",
                "trade_name": "Sweven Fabricators",
                "gstin": "24AAACS1234F1Z1",
                "pan": "AAACS1234F",
                "state": "Gujarat",
                "state_code": "24",
                "currency": "INR",
                "phone": "+91 99988 67024",
                "email": "info@sweven.com",
                "website": "https://sweven.in",
                "address": {
                    "line1": "Plot No. 42-45, GIDC Industrial Estate",
                    "line2": "Road No. 6, Sachin",
                    "city": "Surat",
                    "state": "Gujarat",
                    "pincode": "394230",
                    "country": "India",
                },
            },
        )
        profile.legal_name = "Sweven Fabricators Pvt. Ltd."
        profile.trade_name = "Sweven Fabricators"
        profile.gstin = "24AAACS1234F1Z1"
        profile.state = "Gujarat"
        profile.state_code = "24"
        profile.save()

    def _seed_roles_and_permissions(self, client):
        roles = {r.code: r for r in Role.objects.filter(client=client)}

        # Ensure Sales Executive (SE) role exists
        if "SE" not in roles:
            se_role, _ = Role.objects.get_or_create(
                client=client,
                code="SE",
                defaults={
                    "name": "Sales Executive",
                    "description": "Field sales, enquiry handling, lead management and quotations.",
                    "is_system": False,
                },
            )
            se_perms = [
                "menu_sales", "menu_crm", "menu_inventory",
                "view_sales", "create_quotation", "create_sales_order",
                "view_lead", "create_lead", "edit_lead", "move_lead",
                "view_task", "create_task", "edit_task",
                "view_inventory", "show_crm_dashboard", "export_excel",
            ]
            all_p = set(Permission.objects.values_list("id", flat=True))
            for pid in se_perms:
                if pid in all_p:
                    RolePermission.objects.get_or_create(role=se_role, permission_id=pid)
            roles["SE"] = se_role

        # Ensure Customer (CU) has project viewing permissions
        if "CU" in roles:
            cu_role = roles["CU"]
            for pid in ["view_projects", "view_pms"]:
                if Permission.objects.filter(id=pid).exists():
                    RolePermission.objects.get_or_create(role=cu_role, permission_id=pid)

        return roles

    def _seed_hrms_structure(self, client):
        departments_data = [
            ("Management & Administration", "ADM"),
            ("Sales & Marketing", "SAL"),
            ("Project Management", "PMS"),
            ("Production & Fabrication", "PROD"),
            ("Quality Assurance", "QA"),
            ("Accounts & Finance", "ACC"),
            ("Stores & Logistics", "STR"),
            ("Human Resources", "HR"),
        ]
        depts = {}
        for name, code in departments_data:
            dept, _ = Department.objects.get_or_create(
                client=client,
                name=name,
                defaults={"code": code, "status": "Active"},
            )
            depts[code] = dept

        designations_data = [
            ("General Manager", 1, "ADM"),
            ("Sales Manager", 2, "SAL"),
            ("Senior Sales Executive", 3, "SAL"),
            ("Project Manager", 2, "PMS"),
            ("Production Supervisor", 3, "PROD"),
            ("Fabrication Technician", 4, "PROD"),
            ("Assembly Technician", 4, "PROD"),
            ("Welder & Fitter", 4, "PROD"),
            ("Quality Assurance Inspector", 3, "QA"),
            ("Accounts Manager", 2, "ACC"),
            ("Accounts Executive", 3, "ACC"),
            ("Purchase Officer", 3, "STR"),
            ("Store Keeper", 3, "STR"),
            ("HR Manager", 2, "HR"),
        ]
        desigs = {}
        for name, level, dcode in designations_data:
            desig, _ = Designation.objects.get_or_create(
                client=client,
                name=name,
                defaults={"level": level, "department": depts[dcode]},
            )
            desigs[name] = desig

        return depts, desigs

    def _seed_locations(self, client):
        locations_data = [
            ("WH-STEEL", "Main Raw Material Yard (Steel Yard)", "Warehouse"),
            ("WH-FG", "Finished Goods Warehouse", "Warehouse"),
            ("WH-SPARES", "Machine & Spare Parts Store", "Warehouse"),
            ("WH-SCRAP", "Scrap & Trimmings Yard", "Scrap"),
        ]
        locations = {}
        for code, name, ltype in locations_data:
            loc, _ = Location.objects.get_or_create(
                client=client,
                code=code,
                defaults={"name": name, "type": ltype, "is_active": True},
            )
            locations[code] = loc
        return locations

    def _seed_items(self, client, locations):
        categories_data = [
            ("CAT-MACH", "Machines & Capital Equipment", "machine", "8462"),
            ("CAT-SPARE", "Spare Parts & Accessories", "stock", "8482"),
            ("CAT-RAW-MS", "Raw Materials - Mild Steel by Weight", "stock", "7216"),
            ("CAT-FAB", "Fabricated MS Products", "stock", "7308"),
        ]
        cats = {}
        for code, name, kind, hsn in categories_data:
            cat, _ = ItemCategory.objects.get_or_create(
                client=client,
                code=code,
                defaults={"name": name, "kind": kind, "default_hsn_code": hsn},
            )
            cats[code] = cat

        # Sweven Product Lines per Dev Spec:
        items_spec = [
            # Product line 1: Machines
            {
                "sku": "MCH-CNC-3KW",
                "name": "CNC Fiber Laser Cutting Machine 3kW",
                "description": "High-precision 3000W fiber laser cutting machine with dual exchange shuttle table and CypCut control system.",
                "category": cats["CAT-MACH"],
                "item_kind": "Machine",
                "hsn_code": "84561100",
                "uom": "Nos",
                "selling_price": Decimal("1850000.00"),
                "cost_price": Decimal("1350000.00"),
                "default_location": locations["WH-FG"],
                "tax_pct": Decimal("18.00"),
            },
            {
                "sku": "MCH-HPB-100T",
                "name": "Hydraulic Press Brake 100T/3200",
                "description": "100-ton capacity, 3200mm bending length hydraulic sheet metal press brake with Delem digital controller.",
                "category": cats["CAT-MACH"],
                "item_kind": "Machine",
                "hsn_code": "84622100",
                "uom": "Nos",
                "selling_price": Decimal("1250000.00"),
                "cost_price": Decimal("920000.00"),
                "default_location": locations["WH-FG"],
                "tax_pct": Decimal("18.00"),
            },
            # Product line 1: Spares
            {
                "sku": "SPR-BRG-22212",
                "name": "Industrial Spherical Roller Bearing 22212-E1",
                "description": "Heavy-duty double-row spherical roller bearing for high radial and axial loads.",
                "category": cats["CAT-SPARE"],
                "item_kind": "Part",
                "hsn_code": "84821011",
                "uom": "Nos",
                "selling_price": Decimal("3200.00"),
                "cost_price": Decimal("2100.00"),
                "default_location": locations["WH-SPARES"],
                "tax_pct": Decimal("18.00"),
            },
            {
                "sku": "SPR-SEAL-HYD",
                "name": "High-Pressure Hydraulic Cylinder Seal Kit",
                "description": "Polyurethane piston and rod seal pack rated up to 350 bar pressure.",
                "category": cats["CAT-SPARE"],
                "item_kind": "Part",
                "hsn_code": "84841000",
                "uom": "Set",
                "selling_price": Decimal("1850.00"),
                "cost_price": Decimal("1150.00"),
                "default_location": locations["WH-SPARES"],
                "tax_pct": Decimal("18.00"),
            },
            {
                "sku": "SPR-MTR-5HP",
                "name": "5HP Heavy Duty 3-Phase Induction Motor 1440 RPM",
                "description": "TEFC 4-pole cast iron foot-mounted electric motor, Class F insulation.",
                "category": cats["CAT-SPARE"],
                "item_kind": "Part",
                "hsn_code": "85015210",
                "uom": "Nos",
                "selling_price": Decimal("14500.00"),
                "cost_price": Decimal("10200.00"),
                "default_location": locations["WH-SPARES"],
                "tax_pct": Decimal("18.00"),
            },
            # Product line 2: Raw Materials (MS Steel bought by weight)
            {
                "sku": "RAW-MS-PIPE-50",
                "name": "Mild Steel Square Hollow Pipe 50x50x2.5mm",
                "description": "MS square pipe IS:4923 grade, purchased in kilograms with theoretical weight 3.65 kg/metre.",
                "category": cats["CAT-RAW-MS"],
                "item_kind": "Standalone",
                "hsn_code": "73066100",
                "uom": "Kg",
                "purchase_unit": "Kg",
                "sales_unit": "Kg",
                "is_weight_item": True,
                "theoretical_weight": Decimal("3.6500"),
                "weight_unit": "kg",
                "tolerance_pct": Decimal("3.0000"),
                "cost_price": Decimal("68.00"),
                "selling_price": Decimal("82.00"),
                "default_location": locations["WH-STEEL"],
                "tax_pct": Decimal("18.00"),
            },
            {
                "sku": "RAW-MS-ANG-50",
                "name": "Mild Steel Equal Angle 50x50x5mm",
                "description": "MS equal structural angle IS:2062 E250A grade, theoretical weight 3.80 kg/metre.",
                "category": cats["CAT-RAW-MS"],
                "item_kind": "Standalone",
                "hsn_code": "72162100",
                "uom": "Kg",
                "purchase_unit": "Kg",
                "sales_unit": "Kg",
                "is_weight_item": True,
                "theoretical_weight": Decimal("3.8000"),
                "weight_unit": "kg",
                "tolerance_pct": Decimal("2.5000"),
                "cost_price": Decimal("66.00"),
                "selling_price": Decimal("79.00"),
                "default_location": locations["WH-STEEL"],
                "tax_pct": Decimal("18.00"),
            },
            {
                "sku": "RAW-MS-PLT-12",
                "name": "Mild Steel Plate 12mm IS:2062 Grade",
                "description": "Heavy industrial MS hot rolled steel plate, weight-based inventory receiving with 2% tolerance.",
                "category": cats["CAT-RAW-MS"],
                "item_kind": "Standalone",
                "hsn_code": "72085110",
                "uom": "Kg",
                "purchase_unit": "Kg",
                "sales_unit": "Kg",
                "is_weight_item": True,
                "tolerance_pct": Decimal("2.0000"),
                "cost_price": Decimal("64.00"),
                "selling_price": Decimal("76.00"),
                "default_location": locations["WH-STEEL"],
                "tax_pct": Decimal("18.00"),
            },
            {
                "sku": "RAW-MS-SHT-3MM",
                "name": "Mild Steel Sheet 3mm Hot Rolled (2500x1250mm)",
                "description": "HR sheet 3.0mm thickness, IS:2062 grade with sheet dimensional specifications.",
                "category": cats["CAT-RAW-MS"],
                "item_kind": "Standalone",
                "hsn_code": "72085310",
                "uom": "Kg",
                "is_weight_item": True,
                "has_sheet_spec": True,
                "sheet_height": Decimal("3.0000"),
                "sheet_height_unit": "mm",
                "sheet_width": Decimal("1250.0000"),
                "sheet_width_unit": "mm",
                "sheet_length": Decimal("2500.0000"),
                "sheet_length_unit": "mm",
                "sheet_weight_kg": Decimal("73.6000"),
                "tolerance_pct": Decimal("2.0000"),
                "cost_price": Decimal("65.00"),
                "selling_price": Decimal("78.00"),
                "default_location": locations["WH-STEEL"],
                "tax_pct": Decimal("18.00"),
            },
            # Product line 2: Fabricated Products
            {
                "sku": "FAB-TBL-8X4",
                "name": "Heavy Duty MS Fabrication Table 8x4 ft (12mm Top Plate)",
                "description": "Industrial heavy fabrication table with 12mm machined top plate, 50x50mm boxed SHS frame, leveling pads, and 1500 kg load rating.",
                "category": cats["CAT-FAB"],
                "item_kind": "Standalone",
                "hsn_code": "73089090",
                "uom": "Nos",
                "selling_price": Decimal("48000.00"),
                "cost_price": Decimal("31500.00"),
                "default_location": locations["WH-FG"],
                "tax_pct": Decimal("18.00"),
            },
            {
                "sku": "FAB-WS-6X3",
                "name": "Modular MS Workstation 6x3 ft with Lockable Drawer Unit",
                "description": "Ergonomic industrial assembly workstation with tool overhead rack, power strip, and powder-coated finish.",
                "category": cats["CAT-FAB"],
                "item_kind": "Standalone",
                "hsn_code": "73089090",
                "uom": "Nos",
                "selling_price": Decimal("26500.00"),
                "cost_price": Decimal("17200.00"),
                "default_location": locations["WH-FG"],
                "tax_pct": Decimal("18.00"),
            },
        ]

        items = {}
        for idata in items_spec:
            sku = idata.pop("sku")
            item, _ = Item.objects.get_or_create(
                client=client,
                sku=sku,
                defaults=idata,
            )
            items[sku] = item
        return items

    def _seed_parties(self, client):
        parties_data = [
            # Customers
            {
                "code": "CUST-001",
                "type": "Customer",
                "name": "Apex Heavy Engineering Works",
                "phone": "+91 98251 12345",
                "email": "procurement@apexheavyeng.com",
                "gst_treatment": "Registered Business",
                "gstin": "24AAACA1234A1Z5",
                "place_of_supply": "Gujarat",
                "credit_limit": Decimal("5000000.00"),
                "payment_terms": "30 Days Net",
                "billing_address": {
                    "line1": "Phase 2, GIDC Vatva",
                    "city": "Ahmedabad",
                    "state": "Gujarat",
                    "pincode": "382445",
                    "country": "India",
                },
            },
            {
                "code": "CUST-002",
                "type": "Customer",
                "name": "Surya Precision Fabtech",
                "phone": "+91 97240 54321",
                "email": "purchase@suryasteel.com",
                "gst_treatment": "Registered Business",
                "gstin": "24AABCS5678B1Z2",
                "place_of_supply": "Gujarat",
                "credit_limit": Decimal("3000000.00"),
                "payment_terms": "15 Days Net",
                "billing_address": {
                    "line1": "Plot 88, Ichhapore Industrial Estate",
                    "city": "Surat",
                    "state": "Gujarat",
                    "pincode": "394510",
                    "country": "India",
                },
            },
            {
                "code": "CUST-003",
                "type": "Customer",
                "name": "Reliance Industrial Infrastructure Ltd",
                "phone": "+91 98250 99999",
                "email": "vendor.support@riil.in",
                "gst_treatment": "Registered Business",
                "gstin": "24AABCR9999C1Z9",
                "place_of_supply": "Gujarat",
                "credit_limit": Decimal("10000000.00"),
                "payment_terms": "45 Days Net",
                "billing_address": {
                    "line1": "Manufacturing Complex, Hazira",
                    "city": "Surat",
                    "state": "Gujarat",
                    "pincode": "394518",
                    "country": "India",
                },
            },
            {
                "code": "CUST-004",
                "type": "Customer",
                "name": "Marmo Stone & Machine Tools",
                "phone": "+91 99245 44444",
                "email": "accounts@marmomachine.com",
                "gst_treatment": "Registered Business",
                "gstin": "24AAACM4321D1Z1",
                "place_of_supply": "Gujarat",
                "credit_limit": Decimal("2500000.00"),
                "payment_terms": "Immediate",
                "billing_address": {
                    "line1": "Metoda GIDC, Lodhika",
                    "city": "Rajkot",
                    "state": "Gujarat",
                    "pincode": "360021",
                    "country": "India",
                },
            },
            # Vendors (with weight variance tolerance % per Dev Spec §2.3)
            {
                "code": "VEND-001",
                "type": "Vendor",
                "name": "Jindal Steel & Power Ltd",
                "phone": "+91 98110 33333",
                "email": "orders@jindalsteel.com",
                "gst_treatment": "Registered Business",
                "gstin": "24AAACJ1111E1Z3",
                "place_of_supply": "Gujarat",
                "weight_tolerance_pct": Decimal("3.00"),
                "billing_address": {
                    "line1": "Industrial Area, Hazira",
                    "city": "Surat",
                    "state": "Gujarat",
                    "pincode": "394518",
                    "country": "India",
                },
            },
            {
                "code": "VEND-002",
                "type": "Vendor",
                "name": "Tata Steel Distributor - Gujarat Steels",
                "phone": "+91 98254 77777",
                "email": "sales@gujaratsteels.com",
                "gst_treatment": "Registered Business",
                "gstin": "24AAACG2222F1Z4",
                "place_of_supply": "Gujarat",
                "weight_tolerance_pct": Decimal("2.50"),
                "billing_address": {
                    "line1": "Ring Road, Udhna",
                    "city": "Surat",
                    "state": "Gujarat",
                    "pincode": "394210",
                    "country": "India",
                },
            },
            {
                "code": "VEND-003",
                "type": "Vendor",
                "name": "National Machine Tools & Spares Co",
                "phone": "+91 98791 88888",
                "email": "supplies@nationalspares.co.in",
                "gst_treatment": "Registered Business",
                "gstin": "24AAACN3333G1Z6",
                "place_of_supply": "Gujarat",
                "weight_tolerance_pct": Decimal("0.00"),
                "billing_address": {
                    "line1": "Near Naroda GIDC",
                    "city": "Ahmedabad",
                    "state": "Gujarat",
                    "pincode": "382330",
                    "country": "India",
                },
            },
        ]

        parties = {}
        for pdata in parties_data:
            code = pdata.pop("code")
            party, _ = Party.objects.get_or_create(
                client=client,
                code=code,
                defaults=pdata,
            )
            parties[code] = party
        return parties

    def _seed_users_and_employees(self, client, roles, depts, desigs, parties):
        # Master User Roster covering all requested tiers:
        # Administrators, Managers, Executives, Employees, Customers
        users_specs = [
            # 1. Administrators
            {
                "email": "admin@sweven.com",
                "name": "Administrator",
                "role_code": "AD",
                "department": "Management & Administration",
                "desig": "General Manager",
                "dept_code": "ADM",
                "is_staff": True,
                "is_superuser": True,
                "phone": "+91 99988 67024",
                "emp_code": "EMP-001",
            },
            {
                "email": "admin1@gmail.com",
                "name": "Super Admin User",
                "role_code": "AD",
                "department": "Management & Administration",
                "desig": "General Manager",
                "dept_code": "ADM",
                "is_staff": True,
                "is_superuser": True,
                "phone": "+91 98000 00001",
                "emp_code": "EMP-000",
            },
            # 2. Managers
            {
                "email": "sales.manager@sweven.com",
                "name": "Rajesh Patel",
                "role_code": "SM",
                "department": "Sales & Marketing",
                "desig": "Sales Manager",
                "dept_code": "SAL",
                "is_staff": True,
                "phone": "+91 98250 11001",
                "emp_code": "EMP-002",
            },
            {
                "email": "project.manager@sweven.com",
                "name": "Kiran Sharma",
                "role_code": "PM",
                "department": "Project Management",
                "desig": "Project Manager",
                "dept_code": "PMS",
                "is_staff": True,
                "phone": "+91 98250 11002",
                "emp_code": "EMP-003",
            },
            {
                "email": "hr.manager@sweven.com",
                "name": "Ayesha Khan",
                "role_code": "HR",
                "department": "Human Resources",
                "desig": "HR Manager",
                "dept_code": "HR",
                "is_staff": True,
                "phone": "+91 98250 11003",
                "emp_code": "EMP-004",
            },
            {
                "email": "accounts.manager@sweven.com",
                "name": "Sanjay Mehta",
                "role_code": "AC",
                "department": "Accounts & Finance",
                "desig": "Accounts Manager",
                "dept_code": "ACC",
                "is_staff": True,
                "phone": "+91 98250 11004",
                "emp_code": "EMP-005",
            },
            # 3. Executives
            {
                "email": "sales.executive@sweven.com",
                "name": "Neha Desai",
                "role_code": "SE",
                "department": "Sales & Marketing",
                "desig": "Senior Sales Executive",
                "dept_code": "SAL",
                "is_staff": True,
                "phone": "+91 98250 22001",
                "emp_code": "EMP-006",
            },
            {
                "email": "purchase.executive@sweven.com",
                "name": "Arjun Mehta",
                "role_code": "PU",
                "department": "Stores & Procurement",
                "desig": "Purchase Officer",
                "dept_code": "STR",
                "is_staff": True,
                "phone": "+91 98250 22002",
                "emp_code": "EMP-007",
            },
            {
                "email": "accounts.executive@sweven.com",
                "name": "Priya Shah",
                "role_code": "AC",
                "department": "Accounts & Finance",
                "desig": "Accounts Executive",
                "dept_code": "ACC",
                "is_staff": True,
                "phone": "+91 98250 22003",
                "emp_code": "EMP-008",
            },
            {
                "email": "store.executive@sweven.com",
                "name": "Sameer Joshi",
                "role_code": "ST",
                "department": "Stores & Logistics",
                "desig": "Store Keeper",
                "dept_code": "STR",
                "is_staff": True,
                "phone": "+91 98250 22004",
                "emp_code": "EMP-009",
            },
            # 4. Employees (Shopfloor & Technicians)
            {
                "email": "employee.fabrication@sweven.com",
                "name": "Rahul Panchal",
                "role_code": "EM",
                "department": "Production & Fabrication",
                "desig": "Fabrication Technician",
                "dept_code": "PROD",
                "is_staff": False,
                "phone": "+91 98250 33001",
                "emp_code": "EMP-010",
            },
            {
                "email": "employee.assembly@sweven.com",
                "name": "Amit Kumar",
                "role_code": "EM",
                "department": "Production & Fabrication",
                "desig": "Assembly Technician",
                "dept_code": "PROD",
                "is_staff": False,
                "phone": "+91 98250 33002",
                "emp_code": "EMP-011",
            },
            {
                "email": "employee.welder@sweven.com",
                "name": "Vikram Rathod",
                "role_code": "EM",
                "department": "Production & Fabrication",
                "desig": "Welder & Fitter",
                "dept_code": "PROD",
                "is_staff": False,
                "phone": "+91 98250 33003",
                "emp_code": "EMP-012",
            },
            {
                "email": "employee1@gmail.com",
                "name": "Default Employee",
                "role_code": "EM",
                "department": "Production & Fabrication",
                "desig": "Fabrication Technician",
                "dept_code": "PROD",
                "is_staff": False,
                "phone": "+91 98250 33000",
                "emp_code": "EMP-013",
            },
            # 5. Customers (Portal Users)
            {
                "email": "customer.apex@sweven.com",
                "name": "Gaurav Dave (Apex Eng)",
                "role_code": "CU",
                "department": "Customer Portal",
                "party": parties["CUST-001"],
                "is_staff": False,
                "phone": "+91 98251 12345",
            },
            {
                "email": "customer.surya@sweven.com",
                "name": "Bhavesh Patel (Surya Fabtech)",
                "role_code": "CU",
                "department": "Customer Portal",
                "party": parties["CUST-002"],
                "is_staff": False,
                "phone": "+91 97240 54321",
            },
        ]

        users = {}
        for uspec in users_specs:
            # Keyed by the spec address (the rest of the seeder looks users up
            # by it); the login itself is on ``--email-domain``.
            spec_email = uspec["email"].lower()
            email = f"{spec_email.split('@')[0]}@{self.email_domain}".lower()
            role = roles.get(uspec.get("role_code"))
            party = uspec.get("party")
            emp_code = uspec.get("emp_code")

            user, created = User.objects.get_or_create(
                client=client,
                email=email,
                defaults={
                    "name": uspec["name"],
                    "role": role,
                    "party": party,
                    "department": uspec.get("department"),
                    "phone": uspec.get("phone"),
                    "status": "Active",
                    "is_staff": uspec.get("is_staff", False),
                    "is_superuser": uspec.get("is_superuser", False),
                    "joined_date": timezone.localdate() - timedelta(days=90),
                },
            )
            user.role = role
            user.party = party
            user.status = "Active"
            user.set_password(self.password)
            user.save()
            users[spec_email] = user

            # Create & Link HRMS Employee if this is a staff user
            if emp_code:
                emp, _ = Employee.objects.get_or_create(
                    client=client,
                    employee_code=emp_code,
                    defaults={
                        "name": uspec["name"],
                        "email": email,
                        "phone": uspec.get("phone"),
                        "department": depts.get(uspec.get("dept_code")),
                        "designation": desigs.get(uspec.get("desig")),
                        "status": "Active",
                        "joining_date": timezone.localdate() - timedelta(days=90),
                    },
                )
                user.employee = emp
                user.save(update_fields=["employee"])

        return users

    def _seed_stock(self, client, items, locations):
        today = timezone.localdate()
        movements_data = [
            # Raw Materials in WH-STEEL
            (items["RAW-MS-PIPE-50"], locations["WH-STEEL"], Decimal("8500.00"), Decimal("8500.00"), Decimal("68.00"), "Opening stock - MS Pipe batch 1"),
            (items["RAW-MS-ANG-50"], locations["WH-STEEL"], Decimal("6200.00"), Decimal("6200.00"), Decimal("66.00"), "Opening stock - MS Angle batch 1"),
            (items["RAW-MS-PLT-12"], locations["WH-STEEL"], Decimal("9400.00"), Decimal("9400.00"), Decimal("64.00"), "Opening stock - MS Plate batch 1"),
            (items["RAW-MS-SHT-3MM"], locations["WH-STEEL"], Decimal("5500.00"), Decimal("5500.00"), Decimal("65.00"), "Opening stock - MS Sheet 3mm HR"),
            # Spares in WH-SPARES
            (items["SPR-BRG-22212"], locations["WH-SPARES"], Decimal("45.00"), None, Decimal("2100.00"), "Opening stock - Spherical bearings"),
            (items["SPR-SEAL-HYD"], locations["WH-SPARES"], Decimal("30.00"), None, Decimal("1150.00"), "Opening stock - Cylinder seal kits"),
            (items["SPR-MTR-5HP"], locations["WH-SPARES"], Decimal("12.00"), None, Decimal("10200.00"), "Opening stock - 5HP electric motors"),
            # Machines in WH-FG
            (items["MCH-CNC-3KW"], locations["WH-FG"], Decimal("2.00"), None, Decimal("1350000.00"), "Floor stock - 3kW CNC Laser machine"),
            (items["MCH-HPB-100T"], locations["WH-FG"], Decimal("1.00"), None, Decimal("920000.00"), "Floor stock - 100T Press brake"),
            # Fabricated Tables in WH-FG
            (items["FAB-TBL-8X4"], locations["WH-FG"], Decimal("4.00"), None, Decimal("31500.00"), "Finished stock - 8x4 MS heavy tables"),
            (items["FAB-WS-6X3"], locations["WH-FG"], Decimal("8.00"), None, Decimal("17200.00"), "Finished stock - 6x3 MS workstations"),
        ]

        for item, loc, qty, w_qty, cost, note in movements_data:
            if not StockMovement.objects.filter(client=client, item=item, notes=note).exists():
                # Through the inventory service, so the stock balances (what
                # "available" is read from) move with the ledger.
                from apps.inventory.services import post_movement

                post_movement(
                    client_id=client.id,
                    item=item,
                    location=loc,
                    type="ADJUSTMENT",
                    quantity=qty,
                    weighed_qty=w_qty,
                    unit_cost=cost,
                    movement_date=today - timedelta(days=15),
                    notes=note,
                )

    def _seed_crm(self, client, users, parties, items):
        stages = {s.name: s for s in Stage.objects.filter(client=client)}
        pm_user = users.get("sales.manager@sweven.com")
        se_user = users.get("sales.executive@sweven.com")

        leads_data = [
            {
                "lead_number": "LEAD-2026-001",
                "name": "Bulk MS Industrial Work Tables (25 Units)",
                "party": parties["CUST-001"],
                "owner": se_user,
                "stage": stages.get("Negotiation") or stages.get("New Lead"),
                "amount": Decimal("662500.00"),
                "city": "Ahmedabad",
                "state": "Gujarat",
                "country": "India",
            },
            {
                "lead_number": "LEAD-2026-002",
                "name": "CNC Fiber Laser Cutting Machine 3kW Requirement",
                "party": parties["CUST-002"],
                "owner": pm_user,
                "stage": stages.get("Quotation Shared") or stages.get("New Lead"),
                "amount": Decimal("1850000.00"),
                "city": "Surat",
                "state": "Gujarat",
                "country": "India",
            },
            {
                "lead_number": "LEAD-2026-003",
                "name": "Heavy Duty Welding & Assembly Tables (15 Units)",
                "party": parties["CUST-003"],
                "owner": se_user,
                "stage": stages.get("Won") or stages.get("New Lead"),
                "amount": Decimal("720000.00"),
                "city": "Hazira",
                "state": "Gujarat",
                "country": "India",
            },
            {
                "lead_number": "LEAD-2026-004",
                "name": "Hydraulic Press Spares & Annual Maintenance Supply",
                "party": parties["CUST-004"],
                "owner": se_user,
                "stage": stages.get("Details Collected") or stages.get("New Lead"),
                "amount": Decimal("145000.00"),
                "city": "Rajkot",
                "state": "Gujarat",
                "country": "India",
            },
            {
                "lead_number": "LEAD-2026-005",
                "name": "Inbound Enquiry: Custom Fabrication Workstation Setup",
                "party": None,
                "owner": pm_user,
                "stage": stages.get("New Lead"),
                "amount": Decimal("85000.00"),
                "city": "Vadodara",
                "state": "Gujarat",
                "country": "India",
            },
        ]

        for ldata in leads_data:
            lnum = ldata["lead_number"]
            lead, _ = Lead.objects.get_or_create(
                client=client,
                lead_number=lnum,
                defaults=ldata,
            )

    def _seed_sales_and_purchase(self, client, parties, items, users, locations):
        today = timezone.localdate()
        admin = users.get("admin@sweven.com")

        # 1. Quotation QT-2026-001 for Apex Heavy Engineering
        quot, _ = Quotation.objects.get_or_create(
            client=client,
            quotation_number="QT-2026-001",
            defaults={
                "party": parties["CUST-001"],
                "party_name": parties["CUST-001"].name,
                "party_gstin": parties["CUST-001"].gstin,
                "doc_date": today - timedelta(days=10),
                "valid_until": today + timedelta(days=20),
                "status": "Accepted",
                "subject": "Quotation for Heavy Duty MS Fabrication Tables",
                "subtotal": Decimal("480000.00"),
                "taxable_value": Decimal("480000.00"),
                "cgst": Decimal("43200.00"),
                "sgst": Decimal("43200.00"),
                "total_tax": Decimal("86400.00"),
                "total": Decimal("566400.00"),
                "posted_by": admin,
                "posted_at": timezone.now() - timedelta(days=10),
            },
        )
        if not quot.line_items.exists():
            QuotationLine.objects.create(
                client=client,
                quotation=quot,
                line_no=1,
                item=items["FAB-TBL-8X4"],
                item_name=items["FAB-TBL-8X4"].name,
                sku=items["FAB-TBL-8X4"].sku,
                uom="Nos",
                qty=Decimal("10.00"),
                rate=Decimal("48000.00"),
                amount=Decimal("480000.00"),
                tax_pct=Decimal("18.00"),
                tax_amount=Decimal("86400.00"),
                line_total=Decimal("566400.00"),
                line_kind="Fabrication",
                specification="12mm machined plate, 50x50mm SHS frame",
            )

        # 2. Confirmed Sales Order SO-2026-001
        so1, _ = SalesOrder.objects.get_or_create(
            client=client,
            order_number="SO-2026-001",
            defaults={
                "party": parties["CUST-001"],
                "party_name": parties["CUST-001"].name,
                "party_gstin": parties["CUST-001"].gstin,
                "quotation": quot,
                "doc_date": today - timedelta(days=8),
                "delivery_date": today + timedelta(days=14),
                "stage": "Confirmed",
                "payment_status": "Partially Paid",
                "subtotal": Decimal("480000.00"),
                "taxable_value": Decimal("480000.00"),
                "cgst": Decimal("43200.00"),
                "sgst": Decimal("43200.00"),
                "total_tax": Decimal("86400.00"),
                "total": Decimal("566400.00"),
                "amount_paid": Decimal("200000.00"),
                "posted_by": admin,
                "posted_at": timezone.now() - timedelta(days=8),
            },
        )
        if not so1.line_items.exists():
            SalesOrderLine.objects.create(
                client=client,
                sales_order=so1,
                line_no=1,
                item=items["FAB-TBL-8X4"],
                item_name=items["FAB-TBL-8X4"].name,
                sku=items["FAB-TBL-8X4"].sku,
                uom="Nos",
                qty=Decimal("10.00"),
                rate=Decimal("48000.00"),
                amount=Decimal("480000.00"),
                tax_pct=Decimal("18.00"),
                tax_amount=Decimal("86400.00"),
                line_total=Decimal("566400.00"),
                line_kind="Fabrication",
                specification="12mm machined plate, 50x50mm SHS frame",
            )

        # 3. Confirmed Sales Order SO-2026-002 for CNC Laser Machine
        so2, _ = SalesOrder.objects.get_or_create(
            client=client,
            order_number="SO-2026-002",
            defaults={
                "party": parties["CUST-002"],
                "party_name": parties["CUST-002"].name,
                "party_gstin": parties["CUST-002"].gstin,
                "doc_date": today - timedelta(days=5),
                "delivery_date": today + timedelta(days=25),
                "stage": "Confirmed",
                "payment_status": "Partially Paid",
                "subtotal": Decimal("1850000.00"),
                "taxable_value": Decimal("1850000.00"),
                "cgst": Decimal("166500.00"),
                "sgst": Decimal("166500.00"),
                "total_tax": Decimal("333000.00"),
                "total": Decimal("2183000.00"),
                "amount_paid": Decimal("1000000.00"),
                "posted_by": admin,
                "posted_at": timezone.now() - timedelta(days=5),
            },
        )
        if not so2.line_items.exists():
            SalesOrderLine.objects.create(
                client=client,
                sales_order=so2,
                line_no=1,
                item=items["MCH-CNC-3KW"],
                item_name=items["MCH-CNC-3KW"].name,
                sku=items["MCH-CNC-3KW"].sku,
                uom="Nos",
                qty=Decimal("1.00"),
                rate=Decimal("1850000.00"),
                amount=Decimal("1850000.00"),
                tax_pct=Decimal("18.00"),
                tax_amount=Decimal("333000.00"),
                line_total=Decimal("2183000.00"),
                line_kind="Machine",
            )

        # 4. Weight-Based Purchase Order PO-2026-001 to Jindal Steel
        po1, _ = PurchaseOrder.objects.get_or_create(
            client=client,
            po_number="PO-2026-001",
            defaults={
                "party": parties["VEND-001"],
                "party_name": parties["VEND-001"].name,
                "party_gstin": parties["VEND-001"].gstin,
                "location": locations["WH-STEEL"],
                "doc_date": today - timedelta(days=12),
                "expected_date": today + timedelta(days=5),
                "status": "Issued",
                "subtotal": Decimal("532000.00"),
                "taxable_value": Decimal("532000.00"),
                "cgst": Decimal("47880.00"),
                "sgst": Decimal("47880.00"),
                "total_tax": Decimal("95760.00"),
                "total": Decimal("627760.00"),
                "posted_by": admin,
                "posted_at": timezone.now() - timedelta(days=12),
            },
        )
        if not po1.line_items.exists():
            PurchaseOrderLine.objects.create(
                client=client,
                purchase_order=po1,
                line_no=1,
                item=items["RAW-MS-PIPE-50"],
                item_name=items["RAW-MS-PIPE-50"].name,
                sku=items["RAW-MS-PIPE-50"].sku,
                uom="Kg",
                qty=Decimal("5000.00"),
                rate=Decimal("68.00"),
                amount=Decimal("340000.00"),
                tax_pct=Decimal("18.00"),
                tax_amount=Decimal("61200.00"),
                line_total=Decimal("401200.00"),
            )
            PurchaseOrderLine.objects.create(
                client=client,
                purchase_order=po1,
                line_no=2,
                item=items["RAW-MS-PLT-12"],
                item_name=items["RAW-MS-PLT-12"].name,
                sku=items["RAW-MS-PLT-12"].sku,
                uom="Kg",
                qty=Decimal("3000.00"),
                rate=Decimal("64.00"),
                amount=Decimal("192000.00"),
                tax_pct=Decimal("18.00"),
                tax_amount=Decimal("34560.00"),
                line_total=Decimal("226560.00"),
            )

        return {"SO-2026-001": so1, "SO-2026-002": so2}

    def _seed_pms_projects(self, client, parties, sales_orders, users):
        pm = users.get("project.manager@sweven.com")
        fab_worker = users.get("employee.fabrication@sweven.com")
        pms_depts = {d.name: d for d in pms_models.Department.objects.filter(client=client)}
        pms_configs = list(StageConfig.objects.filter(client=client).order_by("sequence"))
        now = timezone.now()

        # Project 1: Heavy Duty MS Fabrication Tables Batch #101
        so1 = sales_orders["SO-2026-001"]
        prj1, _ = Project.objects.get_or_create(
            client=client,
            code="PRJ-2026-001",
            defaults={
                "party": parties["CUST-001"],
                "customer_name": parties["CUST-001"].name,
                "sales_order": so1,
                "product_name": "Heavy Duty MS Fabrication Table 8x4 ft (10 Units Batch)",
                "quantity": Decimal("10.00"),
                "order_value": Decimal("566400.00"),
                "specifications": "12mm ground MS top plate, 50x50x2.5mm SHS boxed truss frame, laser cut corner gussets, M24 leveling pads.",
                "project_manager": pm,
                "priority": "High",
                "overall_completion_pct": 35,
                "status": "In Progress",
                "start_date": now - timedelta(days=6),
                "expected_completion_date": now + timedelta(days=12),
            },
        )

        if not prj1.stages.exists():
            stage_specs = [
                ("Design & Drawing", 1, "Design", pm, "Approved", 100, Decimal("20.00"), True, True),
                ("Fabrication", 2, "Production", fab_worker, "In Progress", 60, Decimal("40.00"), False, False),
                ("Quality Inspection", 3, "Quality", pm, "Not Started", 0, Decimal("15.00"), True, False),
                ("Packaging", 4, "Packaging", fab_worker, "Not Started", 0, Decimal("10.00"), False, False),
                ("Installation", 5, "Installation", pm, "Not Started", 0, Decimal("15.00"), False, True),
            ]
            first_in_prog = None
            for sname, seq, dname, usr, s_stat, pct, wt, req_doc, req_app in stage_specs:
                stage = ProjectStage.objects.create(
                    client=client,
                    project=prj1,
                    name=sname,
                    sequence=seq,
                    department=pms_depts.get(dname) or pms_models.Department.objects.filter(client=client).first(),
                    assigned_user=usr,
                    status=s_stat,
                    completion_pct=pct,
                    weight_pct=wt,
                    planned_duration=Decimal("3.00"),
                    duration_unit="Days",
                    required_document=req_doc,
                    required_approval=req_app,
                    start_datetime=now - timedelta(days=6) if seq <= 2 else None,
                    expected_completion_datetime=now + timedelta(days=4) if seq == 2 else None,
                )
                if s_stat == "In Progress" and not first_in_prog:
                    first_in_prog = stage
            if first_in_prog:
                prj1.current_stage = first_in_prog
                prj1.current_department = first_in_prog.department
                prj1.save(update_fields=["current_stage", "current_department"])

        # Project 2: CNC Laser Machine Installation
        so2 = sales_orders["SO-2026-002"]
        prj2, _ = Project.objects.get_or_create(
            client=client,
            code="PRJ-2026-002",
            defaults={
                "party": parties["CUST-002"],
                "customer_name": parties["CUST-002"].name,
                "sales_order": so2,
                "product_name": "CNC Fiber Laser Cutting Machine 3kW Setup & Integration",
                "quantity": Decimal("1.00"),
                "order_value": Decimal("2183000.00"),
                "specifications": "Dual bed 3000W fiber laser, chiller unit, voltage stabilizer, CypCut software training.",
                "project_manager": pm,
                "priority": "Urgent",
                "overall_completion_pct": 65,
                "status": "In Progress",
                "start_date": now - timedelta(days=10),
                "expected_completion_date": now + timedelta(days=15),
            },
        )

        if not prj2.stages.exists():
            stage_specs_2 = [
                ("Design & Drawing", 1, "Design", pm, "Approved", 100, Decimal("15.00"), True, True),
                ("Fabrication", 2, "Production", fab_worker, "Completed", 100, Decimal("45.00"), False, False),
                ("Quality Inspection", 3, "Quality", pm, "In Progress", 50, Decimal("20.00"), True, False),
                ("Packaging", 4, "Packaging", fab_worker, "Not Started", 0, Decimal("10.00"), False, False),
                ("Installation", 5, "Installation", pm, "Not Started", 0, Decimal("10.00"), False, True),
            ]
            current_s = None
            for sname, seq, dname, usr, s_stat, pct, wt, req_doc, req_app in stage_specs_2:
                stage = ProjectStage.objects.create(
                    client=client,
                    project=prj2,
                    name=sname,
                    sequence=seq,
                    department=pms_depts.get(dname) or pms_models.Department.objects.filter(client=client).first(),
                    assigned_user=usr,
                    status=s_stat,
                    completion_pct=pct,
                    weight_pct=wt,
                    planned_duration=Decimal("4.00"),
                    duration_unit="Days",
                    required_document=req_doc,
                    required_approval=req_app,
                    start_datetime=now - timedelta(days=10) if seq <= 3 else None,
                )
                if s_stat == "In Progress":
                    current_s = stage
            if current_s:
                prj2.current_stage = current_s
                prj2.current_department = current_s.department
                prj2.save(update_fields=["current_stage", "current_department"])

    def _seed_bank_accounts(self, client):
        bank_acc = Account.objects.filter(client=client, system_key="bank").first()
        if not bank_acc:
            bank_acc = Account.objects.filter(client=client, type="Asset").first()
        if bank_acc:
            BankAccount.objects.get_or_create(
                client=client,
                name="State Bank of India - Current A/c",
                defaults={
                    "account": bank_acc,
                    "type": "Bank",
                    "account_number": "394820194820",
                    "ifsc": "SBIN0001234",
                    "bank_name": "State Bank of India",
                    "branch": "Sachin GIDC, Surat",
                    "opening_balance": Decimal("1500000.00"),
                    "is_default": True,
                    "is_active": True,
                },
            )
            BankAccount.objects.get_or_create(
                client=client,
                name="HDFC Bank - Operations A/c",
                defaults={
                    "account": bank_acc,
                    "type": "Bank",
                    "account_number": "50200084920194",
                    "ifsc": "HDFC0000567",
                    "bank_name": "HDFC Bank",
                    "branch": "Ring Road, Surat",
                    "opening_balance": Decimal("850000.00"),
                    "is_default": False,
                    "is_active": True,
                },
            )
