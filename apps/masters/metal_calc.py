"""
Metal weight calculation and validation utility.

Supports:
- Metal Sheets (Length x Width x Thickness)
- Metal Tubes:
  - Round Tube / Pipe (Outer Diameter, Wall Thickness, Length)
  - Square Tube / Hollow Section (Side, Wall Thickness, Length)
  - Rectangular Tube / Hollow Section (Outer Width, Outer Height, Wall Thickness, Length)

Densities (g/cm³):
- Mild Steel (MS IS 2062, etc.): 7.85
- SS 304: 7.93
- SS 316: 7.98
- SS 202: 7.80
- SS 430: 7.70
- Aluminium: 2.70
- Copper: 8.96
- Brass: 8.50
"""
from decimal import Decimal, ROUND_HALF_UP
import math

DENSITIES = {
    "MS": Decimal("7.85"),
    "MILD STEEL": Decimal("7.85"),
    "CARBON STEEL": Decimal("7.85"),
    "IS 2062": Decimal("7.85"),
    "HR": Decimal("7.85"),
    "CR": Decimal("7.85"),
    "GI": Decimal("7.85"),
    "GP": Decimal("7.85"),
    "SS 304": Decimal("7.93"),
    "SS304": Decimal("7.93"),
    "SS 316": Decimal("7.98"),
    "SS316": Decimal("7.98"),
    "SS 202": Decimal("7.80"),
    "SS202": Decimal("7.80"),
    "SS 430": Decimal("7.70"),
    "SS430": Decimal("7.70"),
    "ALUMINIUM": Decimal("2.70"),
    "AL": Decimal("2.70"),
    "COPPER": Decimal("8.96"),
    "CU": Decimal("8.96"),
    "BRASS": Decimal("8.50"),
}
DEFAULT_DENSITY = Decimal("7.85")


def resolve_density(material_or_grade=None, explicit_density=None):
    if explicit_density is not None:
        try:
            d = Decimal(str(explicit_density))
            if d > Decimal("0"):
                return d
        except Exception:
            pass

    if not material_or_grade:
        return DEFAULT_DENSITY

    lookup = str(material_or_grade).strip().upper()
    for key, val in DENSITIES.items():
        if key in lookup:
            return val
    return DEFAULT_DENSITY


def round4(val):
    if val is None:
        return None
    return Decimal(str(val)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def calculate_sheet_weight(
    length_mm,
    width_mm,
    thickness_mm,
    material_or_grade="MS",
    density=None,
    pieces=1,
):
    """
    Calculates sheet theoretical weight:
    Weight per piece (kg) = (Length_mm * Width_mm * Thickness_mm * Density) / 1,000,000
    Total weight (kg) = Weight per piece * pieces
    """
    errors = []
    try:
        length = Decimal(str(length_mm)) if length_mm is not None else None
        if length is None or length <= Decimal("0"):
            errors.append("Length must be greater than 0 mm.")
    except Exception:
        errors.append("Invalid length value.")

    try:
        width = Decimal(str(width_mm)) if width_mm is not None else None
        if width is None or width <= Decimal("0"):
            errors.append("Width must be greater than 0 mm.")
    except Exception:
        errors.append("Invalid width value.")

    try:
        thickness = Decimal(str(thickness_mm)) if thickness_mm is not None else None
        if thickness is None or thickness <= Decimal("0"):
            errors.append("Thickness must be greater than 0 mm.")
    except Exception:
        errors.append("Invalid thickness value.")

    try:
        pcs = int(pieces) if pieces is not None else 1
        if pcs <= 0:
            errors.append("Pieces must be at least 1.")
    except Exception:
        errors.append("Invalid pieces value.")

    if errors:
        return {"is_valid": False, "errors": errors, "weight_per_piece": None, "total_weight": None}

    rho = resolve_density(material_or_grade, density)
    # Volume in mm3 = length * width * thickness
    # Volume in cm3 = (length * width * thickness) / 1,000
    # Mass in grams = Volume in cm3 * rho
    # Mass in kg = (length * width * thickness * rho) / 1,000,000
    weight_per_piece = (length * width * thickness * rho) / Decimal("1000000")
    total_weight = weight_per_piece * Decimal(str(pcs))

    return {
        "is_valid": True,
        "errors": [],
        "density": rho,
        "weight_per_piece": round4(weight_per_piece),
        "total_weight": round4(total_weight),
    }


def calculate_tube_weight(
    profile,  # 'Round', 'Square', 'Rectangular'
    wall_thickness_mm,
    length_mm=None,
    outer_diameter_mm=None,
    outer_width_mm=None,
    outer_height_mm=None,
    material_or_grade="MS",
    density=None,
    pieces=1,
):
    """
    Calculates tube / hollow section theoretical weight:
    - Round: Area (mm²) = π * (OD - t) * t
    - Square: Area (mm²) = 4 * t * (Side - t) = Side² - (Side - 2t)²
    - Rectangular: Area (mm²) = W*H - (W - 2t)*(H - 2t) = 2 * t * (W + H - 2t)

    Weight per meter (kg/m) = Area (mm²) * Density / 1000
    Weight per piece (kg) = Weight per meter * (Length_mm / 1000)
    Total weight (kg) = Weight per piece * pieces
    """
    errors = []
    norm_profile = str(profile or "").strip().capitalize()
    if norm_profile not in ("Round", "Square", "Rectangular"):
        errors.append("Profile must be 'Round', 'Square', or 'Rectangular'.")

    try:
        t = Decimal(str(wall_thickness_mm)) if wall_thickness_mm is not None else None
        if t is None or t <= Decimal("0"):
            errors.append("Wall thickness must be greater than 0 mm.")
    except Exception:
        errors.append("Invalid wall thickness value.")
        t = None

    od = None
    ow = None
    oh = None

    if norm_profile == "Round":
        try:
            od = Decimal(str(outer_diameter_mm)) if outer_diameter_mm is not None else None
            if od is None or od <= Decimal("0"):
                errors.append("Outer diameter must be greater than 0 mm.")
            elif t is not None and t >= od / Decimal("2"):
                errors.append(f"Wall thickness ({t} mm) cannot be >= half of outer diameter ({od/2} mm).")
        except Exception:
            errors.append("Invalid outer diameter value.")

    elif norm_profile == "Square":
        try:
            ow = Decimal(str(outer_width_mm)) if outer_width_mm is not None else None
            if ow is None or ow <= Decimal("0"):
                errors.append("Outer side width must be greater than 0 mm.")
            elif t is not None and t >= ow / Decimal("2"):
                errors.append(f"Wall thickness ({t} mm) cannot be >= half of side width ({ow/2} mm).")
        except Exception:
            errors.append("Invalid outer width value.")

    elif norm_profile == "Rectangular":
        try:
            ow = Decimal(str(outer_width_mm)) if outer_width_mm is not None else None
            if ow is None or ow <= Decimal("0"):
                errors.append("Outer width must be greater than 0 mm.")
        except Exception:
            errors.append("Invalid outer width value.")

        try:
            oh = Decimal(str(outer_height_mm)) if outer_height_mm is not None else None
            if oh is None or oh <= Decimal("0"):
                errors.append("Outer height must be greater than 0 mm.")
        except Exception:
            errors.append("Invalid outer height value.")

        if ow is not None and oh is not None and t is not None:
            min_dim = min(ow, oh)
            if t >= min_dim / Decimal("2"):
                errors.append(f"Wall thickness ({t} mm) cannot be >= half of outer dimension ({min_dim/2} mm).")

    try:
        length = Decimal(str(length_mm)) if length_mm is not None else None
        if length is not None and length <= Decimal("0"):
            errors.append("Length must be greater than 0 mm.")
    except Exception:
        errors.append("Invalid length value.")

    try:
        pcs = int(pieces) if pieces is not None else 1
        if pcs <= 0:
            errors.append("Pieces must be at least 1.")
    except Exception:
        errors.append("Invalid pieces value.")

    if errors:
        return {
            "is_valid": False,
            "errors": errors,
            "weight_per_meter": None,
            "weight_per_piece": None,
            "total_weight": None,
        }

    rho = resolve_density(material_or_grade, density)
    pi = Decimal(str(math.pi))

    if norm_profile == "Round":
        # Cross sectional area = pi * (od - t) * t
        area_mm2 = pi * (od - t) * t
    elif norm_profile == "Square":
        # Cross sectional area = ow^2 - (ow - 2t)^2 = 4 * t * (ow - t)
        area_mm2 = Decimal("4") * t * (ow - t)
    else:  # Rectangular
        # Cross sectional area = ow * oh - (ow - 2t) * (oh - 2t) = 2 * t * (ow + oh - 2t)
        area_mm2 = Decimal("2") * t * (ow + oh - Decimal("2") * t)

    # Area (mm²) * 1000 mm (1 m) = Volume in mm³ for 1 meter
    # Volume in cm³ = Area (mm²) * 1000 / 1000 = Area (mm²)
    # Mass in grams = Area * rho
    # Weight per meter in kg = (Area * rho) / 1000
    weight_per_meter = (area_mm2 * rho) / Decimal("1000")

    weight_per_piece = None
    total_weight = None
    if length is not None:
        # Length in meters = length / 1000
        weight_per_piece = weight_per_meter * (length / Decimal("1000"))
        total_weight = weight_per_piece * Decimal(str(pcs))

    return {
        "is_valid": True,
        "errors": [],
        "density": rho,
        "area_mm2": round4(area_mm2),
        "weight_per_meter": round4(weight_per_meter),
        "weight_per_piece": round4(weight_per_piece) if weight_per_piece is not None else None,
        "total_weight": round4(total_weight) if total_weight is not None else None,
    }


def calculate_flat_weight(
    width_mm,
    thickness_mm,
    length_mm=None,
    material_or_grade="MS",
    density=None,
    pieces=1,
):
    """
    Calculates flat bar theoretical weight:
    Area (mm²) = Width * Thickness
    Weight per meter (kg/m) = Area * Density / 1000
    Weight per piece (kg) = Weight per meter * (Length_mm / 1000)
    """
    errors = []
    try:
        w = Decimal(str(width_mm)) if width_mm is not None else None
        if w is None or w <= Decimal("0"):
            errors.append("Width must be greater than 0 mm.")
    except Exception:
        errors.append("Invalid width value.")
        w = None

    try:
        t = Decimal(str(thickness_mm)) if thickness_mm is not None else None
        if t is None or t <= Decimal("0"):
            errors.append("Thickness must be greater than 0 mm.")
    except Exception:
        errors.append("Invalid thickness value.")
        t = None

    try:
        length = Decimal(str(length_mm)) if length_mm is not None else None
        if length is not None and length <= Decimal("0"):
            errors.append("Length must be greater than 0 mm.")
    except Exception:
        errors.append("Invalid length value.")
        length = None

    if errors:
        return {"is_valid": False, "errors": errors, "weight_per_meter": None, "weight_per_piece": None}

    rho = resolve_density(material_or_grade, density)
    area_mm2 = w * t
    weight_per_meter = (area_mm2 * rho) / Decimal("1000")

    weight_per_piece = None
    if length is not None:
        weight_per_piece = weight_per_meter * (length / Decimal("1000"))

    return {
        "is_valid": True,
        "errors": [],
        "density": rho,
        "area_mm2": round4(area_mm2),
        "weight_per_meter": round4(weight_per_meter),
        "weight_per_piece": round4(weight_per_piece) if weight_per_piece is not None else None,
    }


def calculate_channel_beam_weight(
    flange_width_mm,
    web_height_mm,
    web_thickness_mm,
    flange_thickness_mm,
    length_mm=None,
    material_or_grade="MS",
    density=None,
    pieces=1,
):
    """
    Calculates Channel / Beam theoretical weight:
    Area (mm²) = 2 * (Flange Width * Flange Thickness) + (Web Height - 2 * Flange Thickness) * Web Thickness
    Weight per meter (kg/m) = Area * Density / 1000
    Weight per piece (kg) = Weight per meter * (Length_mm / 1000)
    """
    errors = []
    try:
        bf = Decimal(str(flange_width_mm)) if flange_width_mm is not None else None
        if bf is None or bf <= Decimal("0"):
            errors.append("Flange width must be greater than 0 mm.")
    except Exception:
        errors.append("Invalid flange width.")
        bf = None

    try:
        hw = Decimal(str(web_height_mm)) if web_height_mm is not None else None
        if hw is None or hw <= Decimal("0"):
            errors.append("Web height must be greater than 0 mm.")
    except Exception:
        errors.append("Invalid web height.")
        hw = None

    try:
        tw = Decimal(str(web_thickness_mm)) if web_thickness_mm is not None else None
        if tw is None or tw <= Decimal("0"):
            errors.append("Web thickness must be greater than 0 mm.")
    except Exception:
        errors.append("Invalid web thickness.")
        tw = None

    try:
        tf = Decimal(str(flange_thickness_mm)) if flange_thickness_mm is not None else None
        if tf is None or tf <= Decimal("0"):
            errors.append("Flange thickness must be greater than 0 mm.")
        elif hw is not None and (Decimal("2") * tf >= hw):
            errors.append("2x flange thickness cannot exceed or equal web height.")
    except Exception:
        errors.append("Invalid flange thickness.")
        tf = None

    try:
        length = Decimal(str(length_mm)) if length_mm is not None else None
        if length is not None and length <= Decimal("0"):
            errors.append("Length must be greater than 0 mm.")
    except Exception:
        errors.append("Invalid length value.")
        length = None

    if errors:
        return {"is_valid": False, "errors": errors, "weight_per_meter": None, "weight_per_piece": None}

    rho = resolve_density(material_or_grade, density)
    area_mm2 = (Decimal("2") * bf * tf) + ((hw - (Decimal("2") * tf)) * tw)
    weight_per_meter = (area_mm2 * rho) / Decimal("1000")

    weight_per_piece = None
    if length is not None:
        weight_per_piece = weight_per_meter * (length / Decimal("1000"))

    return {
        "is_valid": True,
        "errors": [],
        "density": rho,
        "area_mm2": round4(area_mm2),
        "weight_per_meter": round4(weight_per_meter),
        "weight_per_piece": round4(weight_per_piece) if weight_per_piece is not None else None,
    }



def calculate_rod_weight(
    diameter_mm,
    length_mm=None,
    material_or_grade="MS",
    density=None,
    pieces=1,
):
    """
    Calculates round rod / bar theoretical weight:
    Area (mm²) = π * (Diameter / 2)²
    Weight per meter (kg/m) = Area * Density / 1000
    Weight per piece (kg) = Weight per meter * (Length_mm / 1000)
    """
    errors = []
    try:
        dia = Decimal(str(diameter_mm)) if diameter_mm is not None else None
        if dia is None or dia <= Decimal("0"):
            errors.append("Diameter must be greater than 0 mm.")
    except Exception:
        errors.append("Invalid diameter value.")
        dia = None

    try:
        length = Decimal(str(length_mm)) if length_mm is not None else None
        if length is not None and length <= Decimal("0"):
            errors.append("Length must be greater than 0 mm.")
    except Exception:
        errors.append("Invalid length value.")
        length = None

    if errors:
        return {"is_valid": False, "errors": errors, "weight_per_meter": None, "weight_per_piece": None}

    rho = resolve_density(material_or_grade, density)
    pi = Decimal(str(math.pi))
    radius = dia / Decimal("2")
    area_mm2 = pi * (radius ** Decimal("2"))
    weight_per_meter = (area_mm2 * rho) / Decimal("1000")

    weight_per_piece = None
    if length is not None:
        weight_per_piece = weight_per_meter * (length / Decimal("1000"))

    return {
        "is_valid": True,
        "errors": [],
        "density": rho,
        "area_mm2": round4(area_mm2),
        "weight_per_meter": round4(weight_per_meter),
        "weight_per_piece": round4(weight_per_piece) if weight_per_piece is not None else None,
    }


def calculate_angle_weight(
    leg_a_mm,
    leg_b_mm,
    thickness_mm,
    length_mm=None,
    material_or_grade="MS",
    density=None,
    pieces=1,
):
    """
    Calculates L-angle theoretical weight:
    Area (mm²) = (Leg_A + Leg_B - Thickness) * Thickness
    Weight per meter (kg/m) = Area * Density / 1000
    Weight per piece (kg) = Weight per meter * (Length_mm / 1000)
    """
    errors = []
    try:
        la = Decimal(str(leg_a_mm)) if leg_a_mm is not None else None
        if la is None or la <= Decimal("0"):
            errors.append("Leg A must be greater than 0 mm.")
    except Exception:
        errors.append("Invalid Leg A value.")
        la = None

    try:
        lb = Decimal(str(leg_b_mm)) if leg_b_mm is not None else la
        if lb is None or lb <= Decimal("0"):
            errors.append("Leg B must be greater than 0 mm.")
    except Exception:
        errors.append("Invalid Leg B value.")
        lb = None

    try:
        t = Decimal(str(thickness_mm)) if thickness_mm is not None else None
        if t is None or t <= Decimal("0"):
            errors.append("Thickness must be greater than 0 mm.")
        elif la is not None and lb is not None and (t >= la or t >= lb):
            errors.append("Thickness cannot be >= leg dimensions.")
    except Exception:
        errors.append("Invalid thickness value.")
        t = None

    try:
        length = Decimal(str(length_mm)) if length_mm is not None else None
        if length is not None and length <= Decimal("0"):
            errors.append("Length must be greater than 0 mm.")
    except Exception:
        errors.append("Invalid length value.")
        length = None

    if errors:
        return {"is_valid": False, "errors": errors, "weight_per_meter": None, "weight_per_piece": None}

    rho = resolve_density(material_or_grade, density)
    area_mm2 = (la + lb - t) * t
    weight_per_meter = (area_mm2 * rho) / Decimal("1000")

    weight_per_piece = None
    if length is not None:
        weight_per_piece = weight_per_meter * (length / Decimal("1000"))

    return {
        "is_valid": True,
        "errors": [],
        "density": rho,
        "area_mm2": round4(area_mm2),
        "weight_per_meter": round4(weight_per_meter),
        "weight_per_piece": round4(weight_per_piece) if weight_per_piece is not None else None,
    }
