# Generated data migration for SEWEN ERP Phase 2
from decimal import Decimal
from django.db import migrations


def seed_item_types_and_categories(apps, schema_editor):
    Client = apps.get_model("accounts", "Client")
    ItemType = apps.get_model("masters", "ItemType")
    ItemCategory = apps.get_model("masters", "ItemCategory")
    MaterialGrade = apps.get_model("masters", "MaterialGrade")
    Item = apps.get_model("masters", "Item")

    ITEM_TYPES_DATA = [
        ("SHEET", "Metal Sheet", "Flat metal sheets and plates (length x width x thickness)", "SHEET"),
        ("ROD", "Rod", "Solid round bars and rods (diameter x length)", "ROUND_SOLID"),
        ("ANGLE", "Angle", "L-section structural angles (leg A x leg B x thickness x length)", "EQUAL_ANGLE"),
        ("TUBE", "Tube", "Square and rectangular hollow section tubes", "HOLLOW_SECTION"),
        ("PIPE", "Pipe", "Round hollow pipes (outer diameter / nominal bore x wall thickness x length)", "ROUND_HOLLOW"),
        ("CHANNEL", "Channel", "C / U structural channels (depth x flange width x thickness x length)", "CHANNEL"),
        ("BEAM", "Beam", "I / H structural beams (depth x flange width x thickness x length)", "BEAM"),
        ("FLAT", "Flat", "Solid flat bars and strips (width x thickness x length)", "FLAT_BAR"),
        ("BAR", "Bar", "Square and hexagonal solid bars", "SQUARE_SOLID"),
        ("MACHINE", "Machine / Equipment", "Assembled machines, capital equipment, and multi-component systems", "CUSTOM"),
        ("GENERAL", "General / Other", "General parts, hardware, consumables, and miscellaneous items", "CUSTOM"),
    ]

    CATEGORIES_BY_TYPE = {
        "SHEET": [
            ("Mild Steel", "SHT-MS", "Mild Steel Sheet / Plate"),
            ("Stainless Steel", "SHT-SS", "Stainless Steel Sheet / Plate"),
            ("Aluminium", "SHT-AL", "Aluminium Sheet / Plate"),
            ("Galvanized Iron", "SHT-GI", "GI / Galvanized Sheet"),
        ],
        "ROD": [
            ("Mild Steel", "ROD-MS", "Mild Steel Round Rod"),
            ("Stainless Steel", "ROD-SS", "Stainless Steel Round Rod"),
            ("Aluminium", "ROD-AL", "Aluminium Round Rod"),
        ],
        "ANGLE": [
            ("Mild Steel", "ANG-MS", "Mild Steel Angle Section"),
            ("Stainless Steel", "ANG-SS", "Stainless Steel Angle Section"),
            ("Aluminium", "ANG-AL", "Aluminium Angle Section"),
        ],
        "TUBE": [
            ("Mild Steel", "TUB-MS", "Mild Steel Hollow Tube (SHS/RHS)"),
            ("Stainless Steel", "TUB-SS", "Stainless Steel Hollow Tube (SHS/RHS)"),
            ("Galvanized Steel", "TUB-GS", "Galvanized Steel Hollow Tube"),
        ],
        "PIPE": [
            ("Mild Steel", "PIP-MS", "Mild Steel Round Pipe"),
            ("Stainless Steel", "PIP-SS", "Stainless Steel Round Pipe"),
            ("Galvanized Iron", "PIP-GI", "Galvanized Iron Round Pipe"),
        ],
        "CHANNEL": [
            ("Mild Steel", "CHN-MS", "Mild Steel C/U Channel"),
            ("Stainless Steel", "CHN-SS", "Stainless Steel Channel"),
        ],
        "BEAM": [
            ("Mild Steel", "BM-MS", "Mild Steel I/H Structural Beam"),
        ],
        "FLAT": [
            ("Mild Steel", "FLT-MS", "Mild Steel Flat Bar"),
            ("Stainless Steel", "FLT-SS", "Stainless Steel Flat Bar"),
            ("Aluminium", "FLT-AL", "Aluminium Flat Bar"),
        ],
        "BAR": [
            ("Mild Steel", "BAR-MS", "Mild Steel Square/Hex Bar"),
            ("Stainless Steel", "BAR-SS", "Stainless Steel Square/Hex Bar"),
            ("Brass", "BAR-BR", "Brass Bar"),
            ("Copper", "BAR-CU", "Copper Bar"),
        ],
    }

    MATERIAL_GRADES = [
        ("MS", "Mild Steel IS 2062", "Mild Steel", Decimal("7.8500")),
        ("IS 2062", "IS 2062 Structural Steel", "Mild Steel", Decimal("7.8500")),
        ("SS 304", "Stainless Steel 304", "Stainless Steel", Decimal("7.9300")),
        ("SS 316", "Stainless Steel 316", "Stainless Steel", Decimal("7.9800")),
        ("SS 202", "Stainless Steel 202", "Stainless Steel", Decimal("7.8000")),
        ("SS 430", "Stainless Steel 430", "Stainless Steel", Decimal("7.7000")),
        ("Aluminium 6061", "Aluminium Grade 6061", "Aluminium", Decimal("2.7000")),
        ("Copper", "Copper Commercial", "Copper", Decimal("8.9600")),
        ("Brass", "Brass Commercial", "Brass", Decimal("8.5000")),
        ("GI", "Galvanized Iron / GP", "Mild Steel", Decimal("7.8500")),
    ]

    for client in Client.objects.all():
        type_map = {}
        for code, name, desc, shape in ITEM_TYPES_DATA:
            it, _ = ItemType.objects.get_or_create(
                client=client,
                code=code,
                defaults={
                    "name": name,
                    "description": desc,
                    "shape_profile": shape,
                    "is_active": True,
                },
            )
            type_map[code] = it

        for type_code, cat_list in CATEGORIES_BY_TYPE.items():
            it = type_map.get(type_code)
            if not it:
                continue
            for cat_name, cat_code, cat_desc in cat_list:
                cat_qs = ItemCategory.objects.filter(client=client, item_type=it, name=cat_name, deleted_at__isnull=True)
                if not cat_qs.exists():
                    # check code collision
                    final_code = cat_code
                    if ItemCategory.objects.filter(client=client, code=final_code, deleted_at__isnull=True).exists():
                        final_code = f"{cat_code}-{str(client.id)[:4]}"
                    ItemCategory.objects.create(
                        client=client,
                        item_type=it,
                        name=cat_name,
                        code=final_code,
                        description=cat_desc,
                        kind="stock",
                        is_active=True,
                    )

        for grade_code, grade_name, family, density in MATERIAL_GRADES:
            MaterialGrade.objects.get_or_create(
                client=client,
                code=grade_code,
                defaults={
                    "name": grade_name,
                    "family": family,
                    "density": density,
                    "is_active": True,
                },
            )

        # Backfill existing categories
        for cat in ItemCategory.objects.filter(client=client, item_type__isnull=True):
            if cat.kind == "machine" or cat.has_sub_parts or "Machine" in cat.name:
                cat.item_type = type_map.get("MACHINE")
                cat.save(update_fields=["item_type"])
            elif "Raw" in cat.name or "Steel" in cat.name:
                cat.item_type = type_map.get("SHEET")
                cat.save(update_fields=["item_type"])
            elif "Fabricated" in cat.name or "Spare" in cat.name:
                cat.item_type = type_map.get("GENERAL")
                cat.save(update_fields=["item_type"])

        # Backfill existing items
        for item in Item.objects.filter(client=client, item_type__isnull=True):
            if getattr(item, "has_sheet_spec", False):
                item.item_type = type_map.get("SHEET")
            elif getattr(item, "has_tube_spec", False):
                item.item_type = type_map.get("TUBE")
            elif item.item_kind == "Machine":
                item.item_type = type_map.get("MACHINE")
            elif item.category and item.category.item_type:
                item.item_type = item.category.item_type
            else:
                item.item_type = type_map.get("GENERAL")
            item.save(update_fields=["item_type"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("masters", "0005_materialgrade_itemcategory_default_unit_and_more"),
    ]

    operations = [
        migrations.RunPython(seed_item_types_and_categories, noop_reverse),
    ]
