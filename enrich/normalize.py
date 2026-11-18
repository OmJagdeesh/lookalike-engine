"""Canonical B2B Indian industry taxonomy and deterministic feature band normalizer."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

# 30 canonical B2B Indian industry categories + "other" fallback
INDUSTRY_TAXONOMY: tuple[str, ...] = (
    "automotive_components",
    "pharma_chemicals",
    "textiles_apparel",
    "machinery_equipment",
    "packaging_plastics",
    "food_beverage_processing",
    "electronics_electrical",
    "construction_materials",
    "logistics_warehousing",
    "it_software_services",
    "metals_fabrication",
    "rubber_polymers",
    "paper_printing",
    "furniture_wood_products",
    "agriculture_agro_inputs",
    "medical_devices_healthcare",
    "energy_solar_power",
    "water_waste_management",
    "industrial_automation_robotics",
    "precision_tools_dies",
    "wholesale_industrial_distribution",
    "retail_b2c_commerce",
    "hospitality_food_service",
    "real_estate_infrastructure",
    "engineering_consulting",
    "financial_accounting_services",
    "media_marketing_agencies",
    "education_training",
    "gems_jewelry_precision_craft",
    "leather_footwear",
    "other",
)

# Ordered deterministic keyword rules: (canonical_industry, tuple_of_keywords)
# Checked against combined normalized tokens from Places types, directory categories,
# business name hints, and GST nature-of-business strings.
_SPECIFIC_INDUSTRY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "industrial_automation_robotics",
        (
            "automation",
            "robotics",
            "plc",
            "scada",
            "mechatronics",
            "pneumatic",
            "hydraulic system",
            "control panel",
        ),
    ),
    (
        "precision_tools_dies",
        (
            "tool room",
            "tool and die",
            "tools & dies",
            "dies",
            "mould",
            "mold",
            "cnc",
            "precision tool",
            "cutting tool",
            "jig",
            "fixture",
            "fastener",
            "machine shop",
        ),
    ),
    (
        "automotive_components",
        (
            "auto parts",
            "auto_parts",
            "automotive",
            "automobile",
            "car_dealer",
            "car_repair",
            "auto component",
            "autocomp",
            "forgings",
            "brake",
            "clutch",
            "axle",
            "ev component",
        ),
    ),
    (
        "pharma_chemicals",
        (
            "pharma",
            "pharmaceutical",
            "chemical",
            "api ",
            "bulk drug",
            "formulation",
            "solvent",
            "reagent",
            "paints",
            "coatings",
            "dye",
            "pigment",
            "adhesive",
        ),
    ),
    (
        "medical_devices_healthcare",
        (
            "medical device",
            "medical_equipment",
            "surgical",
            "diagnostic",
            "hospital",
            "clinic",
            "healthcare",
            "dental",
            "lab equipment",
            "laboratory",
        ),
    ),
    (
        "packaging_plastics",
        (
            "packaging",
            "corrugated",
            "carton",
            "plastic",
            "hdpe",
            "ldpe",
            "pet bottle",
            "blow mold",
            "injection mold",
            "polybag",
            "stretch film",
            "bopp",
            "shrink wrap",
            "thermoform",
        ),
    ),
    (
        "rubber_polymers",
        (
            "rubber",
            "polymer",
            "elastomer",
            "tyre",
            "tire",
            "gasket",
            "o-ring",
            "silicone",
            "extrusion",
        ),
    ),
    (
        "metals_fabrication",
        (
            "steel",
            "metal",
            "fabrication",
            "foundry",
            "casting",
            "sheet metal",
            "aluminium",
            "aluminum",
            "brass",
            "copper",
            "iron",
            "welding",
            "galvaniz",
            "pipe",
            "tube",
        ),
    ),
    (
        "machinery_equipment",
        (
            "machinery",
            "industrial_equipment",
            "machine",
            "compressor",
            "boiler",
            "pump",
            "valve",
            "gearbox",
            "bearing",
            "conveyor",
            "crane",
            "lathe",
            "generator",
            "hvac",
        ),
    ),
    (
        "electronics_electrical",
        (
            "electronic",
            "electrical",
            "pcb",
            "switchgear",
            "transformer",
            "cable",
            "wire",
            "led ",
            "lighting",
            "semiconductor",
            "battery",
            "inverter",
            "ups ",
        ),
    ),
    (
        "energy_solar_power",
        (
            "solar",
            "renewable",
            "power plant",
            "wind energy",
            "energy",
            "photovoltaic",
            "biomass",
        ),
    ),
    (
        "water_waste_management",
        (
            "water treatment",
            "effluent",
            "stp ",
            "etp ",
            "ro plant",
            "waste management",
            "recycling",
            "sewage",
            "filtration",
        ),
    ),
    (
        "textiles_apparel",
        (
            "textile",
            "garment",
            "apparel",
            "fabric",
            "yarn",
            "spinning",
            "weaving",
            "knitting",
            "cotton",
            "clothing",
            "hosiery",
        ),
    ),
    (
        "food_beverage_processing",
        (
            "food processing",
            "food_manufacturer",
            "beverage",
            "dairy",
            "bakery",
            "spice",
            "flour",
            "edible oil",
            "confectionery",
            "cold storage",
            "agro food",
            "snack",
        ),
    ),
    (
        "agriculture_agro_inputs",
        (
            "agriculture",
            "agro",
            "fertilizer",
            "pesticide",
            "seed",
            "irrigation",
            "tractor",
            "farm equipment",
            "nursery",
        ),
    ),
    (
        "construction_materials",
        (
            "cement",
            "concrete",
            "brick",
            "tile",
            "ceramic",
            "glass",
            "marble",
            "granite",
            "building material",
            "hardware_store",
            "plywood",
            "roofing",
            "sanitaryware",
        ),
    ),
    (
        "real_estate_infrastructure",
        (
            "real_estate",
            "real estate",
            "developer",
            "builder",
            "infrastructure",
            "civil contractor",
            "construction",
            "works contract",
        ),
    ),
    (
        "logistics_warehousing",
        (
            "logistics",
            "warehousing",
            "warehouse",
            "freight",
            "transport",
            "shipping",
            "courier",
            "supply chain",
            "cargo",
            "customs",
            "moving_company",
        ),
    ),
    (
        "it_software_services",
        (
            "software",
            "it service",
            "information technology",
            "saas",
            "cloud",
            "cybersecurity",
            "data analytics",
            "web development",
            "tech",
        ),
    ),
    (
        "paper_printing",
        (
            "paper",
            "printing",
            "print",
            "stationery",
            "label",
            "offset",
            "bookbinding",
            "pulp",
        ),
    ),
    (
        "furniture_wood_products",
        (
            "furniture",
            "timber",
            "wood",
            "carpentry",
            "modular kitchen",
            "office chair",
            "sawmill",
        ),
    ),
    (
        "engineering_consulting",
        (
            "engineering",
            "consultant",
            "consultancy",
            "architect",
            "design service",
            "inspection",
            "calibration",
            "testing lab",
        ),
    ),
    (
        "financial_accounting_services",
        (
            "accounting",
            "chartered accountant",
            "finance",
            "fintech",
            "insurance",
            "tax consultant",
            "audit",
            "banking",
        ),
    ),
    (
        "media_marketing_agencies",
        (
            "marketing",
            "advertising",
            "media",
            "branding",
            "digital agency",
            "pr agency",
            "event management",
        ),
    ),
    (
        "education_training",
        (
            "education",
            "training",
            "institute",
            "academy",
            "school",
            "college",
            "coaching",
            "skill development",
        ),
    ),
    (
        "hospitality_food_service",
        (
            "hotel",
            "restaurant",
            "catering",
            "hospitality",
            "resort",
            "cafe",
            "banquet",
        ),
    ),
    (
        "gems_jewelry_precision_craft",
        (
            "jewel",
            "gem",
            "diamond",
            "goldsmith",
            "bullion",
            "watch",
        ),
    ),
    (
        "leather_footwear",
        (
            "leather",
            "footwear",
            "shoe",
            "tannery",
            "handbag",
            "belt",
        ),
    ),
)

_GENERIC_FALLBACK_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "wholesale_industrial_distribution",
        ("wholesaler", "wholesale", "distributor", "trader", "trading", "dealer", "stockist"),
    ),
    (
        "retail_b2c_commerce",
        ("retail", "store", "shop", "supermarket", "ecommerce", "e-commerce"),
    ),
)

_CLEAN_TOKEN_RE = re.compile(r"[_\-/]+")


def normalize_industry(
    *,
    places_primary_type: str | None = None,
    places_types: list[str] | tuple[str, ...] | None = None,
    gst_nature_of_business: list[str] | tuple[str, ...] | str | None = None,
    directory_category: str | None = None,
    business_name: str | None = None,
) -> str:
    """Map raw Places types, GST nature-of-business, and directory categories to INDUSTRY_TAXONOMY."""
    chunks: list[str] = []
    if places_primary_type:
        chunks.append(places_primary_type)
    if places_types:
        chunks.extend(places_types)
    if directory_category:
        chunks.append(directory_category)
    if business_name:
        chunks.append(business_name)
    if gst_nature_of_business:
        if isinstance(gst_nature_of_business, str):
            chunks.append(gst_nature_of_business)
        else:
            chunks.extend(gst_nature_of_business)

    combined = " " + " ".join(_CLEAN_TOKEN_RE.sub(" ", c.lower()) for c in chunks if c) + " "
    if not combined.strip():
        return "other"

    for industry, keywords in _SPECIFIC_INDUSTRY_RULES:
        for kw in keywords:
            if kw in combined:
                return industry

    for industry, keywords in _GENERIC_FALLBACK_RULES:
        for kw in keywords:
            if kw in combined:
                return industry

    return "other"


def derive_business_age_band(
    registration_date: date | None,
    *,
    as_of_date: date | None = None,
) -> str:
    """Bucket registration_date into `<2y`, `2-5y`, `5-10y`, `10y+`, or `unknown`."""
    if registration_date is None:
        return "unknown"
    ref = as_of_date or date.today()
    years = (ref - registration_date).days / 365.25
    if years < 2:
        return "<2y"
    if years < 5:
        return "2-5y"
    if years < 10:
        return "5-10y"
    return "10y+"


def derive_review_count_band(review_count: int | None) -> str:
    """Bucket review_count into `0`, `1-10`, `11-50`, `50+`, or `unknown`."""
    if review_count is None:
        return "unknown"
    if review_count <= 0:
        return "0"
    if review_count <= 10:
        return "1-10"
    if review_count <= 50:
        return "11-50"
    return "50+"


def derive_rating_band(google_rating: float | None) -> str:
    """Bucket google_rating into `<3.5`, `3.5-4.2`, `4.2+`, or `unrated`."""
    if google_rating is None:
        return "unrated"
    if google_rating < 3.5:
        return "<3.5"
    if google_rating < 4.2:
        return "3.5-4.2"
    return "4.2+"


@dataclass(frozen=True)
class NormalizedFeatures:
    """Normalized categorical feature bundle ready for Stage 3 lift scoring."""

    industry: str
    business_age_band: str
    review_count_band: str
    rating_band: str
    turnover_band: str
    employee_band: str
    city: str
    business_type: str
    filing_regular: bool | None
    has_website: bool | None

    def to_feature_dict(self) -> dict[str, str]:
        """Return categorical feature-value pairs for lift/weight computation."""
        pairs: dict[str, str] = {
            "industry": self.industry,
            "business_age_band": self.business_age_band,
            "review_count_band": self.review_count_band,
            "rating_band": self.rating_band,
            "turnover_band": self.turnover_band,
            "employee_band": self.employee_band,
            "city": self.city,
            "business_type": self.business_type,
        }
        if self.filing_regular is not None:
            pairs["filing_regular"] = str(self.filing_regular).lower()
        if self.has_website is not None:
            pairs["has_website"] = str(self.has_website).lower()
        return pairs


def normalize_features(
    *,
    industry: str | None = None,
    places_primary_type: str | None = None,
    places_types: list[str] | tuple[str, ...] | None = None,
    gst_nature_of_business: list[str] | tuple[str, ...] | str | None = None,
    directory_category: str | None = None,
    business_name: str | None = None,
    registration_date: date | None = None,
    review_count: int | None = None,
    google_rating: float | None = None,
    turnover_band: str | None = None,
    employee_band: str | None = None,
    city: str | None = None,
    business_type: str | None = None,
    filing_regular: bool | None = None,
    has_website: bool | None = None,
    as_of_date: date | None = None,
) -> NormalizedFeatures:
    """Derive all normalized scoring buckets for a business."""
    resolved_industry = (
        industry
        if industry in INDUSTRY_TAXONOMY
        else normalize_industry(
            places_primary_type=places_primary_type,
            places_types=places_types,
            gst_nature_of_business=gst_nature_of_business,
            directory_category=directory_category,
            business_name=business_name,
        )
    )
    return NormalizedFeatures(
        industry=resolved_industry,
        business_age_band=derive_business_age_band(registration_date, as_of_date=as_of_date),
        review_count_band=derive_review_count_band(review_count),
        rating_band=derive_rating_band(google_rating),
        turnover_band=turnover_band.strip() if turnover_band else "unknown",
        employee_band=employee_band.strip() if employee_band else "unknown",
        city=city.strip().title() if city else "unknown",
        business_type=business_type.strip() if business_type else "unknown",
        filing_regular=filing_regular,
        has_website=has_website,
    )
