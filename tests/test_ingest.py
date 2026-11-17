"""Phase 1 tests: GSTIN validator, name normalization, deduplication, and seed importer."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from ingest.dedupe import (
    BusinessRecord,
    Deduplicator,
    InMemoryBusinessStore,
    normalize_business_name,
    normalize_pincode,
)
from ingest.gstin import compute_gstin_checksum, validate_gstin
from ingest.importer import (
    InMemorySeedStore,
    import_seed_dataframe,
    import_seed_file,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
MESSY_200_CSV = FIXTURES_DIR / "messy_seed_200.csv"
SAMPLE_MAPPING_YAML = FIXTURES_DIR / "sample_mapping.yaml"


def _make_gstin(prefix14: str) -> str:
    return prefix14 + compute_gstin_checksum(prefix14)


# ---------------------------------------------------------------------------
# Step 1.1: GSTIN Validator Tests
# ---------------------------------------------------------------------------
def test_validate_gstin_valid_with_whitespace_and_lowercase() -> None:
    valid_gstin = _make_gstin("27AABCP1234A1Z")
    res = validate_gstin(f"  {valid_gstin.lower()}  ")
    assert res.is_valid is True
    assert res.normalized == valid_gstin
    assert res.error_code is None
    assert res.state_code == "27"
    assert res.pan == "AABCP1234A"


@pytest.mark.parametrize(
    ("bad_gstin", "expected_code"),
    [
        (None, "EMPTY_GSTIN"),
        ("   ", "EMPTY_GSTIN"),
        ("27AABCP1234", "INVALID_LENGTH"),
        ("27AABCP1234A1Z59", "INVALID_LENGTH"),
        ("27123451234A1Z5", "INVALID_FORMAT"),
        ("27AABCP1234A0Z5", "INVALID_FORMAT"),  # 0 not allowed in entity position
        ("27AABCP1234A1X5", "INVALID_FORMAT"),  # 14th char must be Z
        ("00AABCP1234A1Z" + compute_gstin_checksum("00AABCP1234A1Z"), "INVALID_STATE_CODE"),
        ("39AABCP1234A1Z" + compute_gstin_checksum("39AABCP1234A1Z"), "INVALID_STATE_CODE"),
    ],
)
def test_validate_gstin_error_codes(bad_gstin: str | None, expected_code: str) -> None:
    res = validate_gstin(bad_gstin)
    assert res.is_valid is False
    assert res.error_code == expected_code


def test_validate_gstin_invalid_checksum() -> None:
    valid = _make_gstin("27AABCP1234A1Z")
    wrong_check = "0" if valid[-1] != "0" else "1"
    tampered = valid[:-1] + wrong_check
    res = validate_gstin(tampered)
    assert res.is_valid is False
    assert res.error_code == "INVALID_CHECKSUM"


# ---------------------------------------------------------------------------
# Step 1.2: Name Normalization & Two-Tier Deduplication Tests
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw_name", "expected"),
    [
        ("Apex Tools Pvt. Ltd.", "apex tools"),
        ("APEX TOOLS PRIVATE LIMITED", "apex tools"),
        ("Apex Tools Ltd", "apex tools"),
        ("Apex & Sons LLP", "apex and sons"),
        ("  Bharat   Precision   Components (P) Ltd. ", "bharat precision components"),
        ("Nova Engineering One Person Company", "nova engineering"),
    ],
)
def test_normalize_business_name(raw_name: str, expected: str) -> None:
    assert normalize_business_name(raw_name) == expected


def test_normalize_pincode_handles_float_and_invalid() -> None:
    assert normalize_pincode("411019") == "411019"
    assert normalize_pincode("411019.0") == "411019"
    assert normalize_pincode(411019.0) == "411019"
    assert normalize_pincode("4110") is None
    assert normalize_pincode("") is None


def test_deduplicator_tier1_gstin_and_tier2_name_pincode_merge_rules() -> None:
    early_ts = datetime(2024, 1, 10, tzinfo=UTC)
    later_ts = datetime(2025, 3, 1, tzinfo=UTC)
    g1 = _make_gstin("27AABCP5555B1Z")

    store = InMemoryBusinessStore(
        [
            BusinessRecord(
                gstin=g1,
                legal_name="Alpha Gears Pvt Ltd",
                trade_name=None,
                city="Pune",
                state=None,
                pincode="411019",
                source="gst",
                first_seen_at=later_ts,
            )
        ]
    )
    deduper = Deduplicator(store)

    # Tier 1 match by GSTIN: preserves existing legal_name/city, fills null state & trade_name,
    # and takes earlier first_seen_at
    res1 = deduper.upsert(
        BusinessRecord(
            gstin=g1,
            legal_name="ALPHA GEARS PRIVATE LIMITED",
            trade_name="Alpha Gears Brand",
            city="Mumbai",  # must NOT overwrite existing non-null city
            state="Maharashtra",
            pincode="411019",
            source="csv",
            first_seen_at=early_ts,
        )
    )
    assert res1.created is False
    assert res1.merge_event is not None
    assert res1.merge_event.tier == "tier1_gstin"

    merged_rec = store.find_by_gstin(g1)
    assert merged_rec is not None
    assert merged_rec.legal_name == "Alpha Gears Pvt Ltd"
    assert merged_rec.city == "Pune"
    assert merged_rec.state == "Maharashtra"
    assert merged_rec.trade_name == "Alpha Gears Brand"
    assert merged_rec.first_seen_at == early_ts

    # Tier 2 match: normalized name + pincode without GSTIN, then a second record fills GSTIN
    res2 = deduper.upsert(
        BusinessRecord(
            gstin=None,
            legal_name="alpha gears private limited",
            city="Pune",
            pincode="411019.0",
            source="csv",
        )
    )
    assert res2.created is False
    assert res2.business_id == merged_rec.id
    assert res2.merge_event is not None
    assert res2.merge_event.tier == "tier2_name_pincode"
    assert len(deduper.merge_log) == 2


# ---------------------------------------------------------------------------
# Step 1.3 & Gate 1: Messy 200-row CSV + Excel + Column Mapping Tests
# ---------------------------------------------------------------------------
def test_gate1_messy_seed_200_csv_import() -> None:
    assert MESSY_200_CSV.exists(), "Gate 1 fixture tests/fixtures/messy_seed_200.csv is missing"
    biz_store = InMemoryBusinessStore()
    seed_store = InMemorySeedStore()

    report = import_seed_file(
        MESSY_200_CSV,
        client_id=101,
        column_mapping=SAMPLE_MAPPING_YAML,
        business_store=biz_store,
        seed_store=seed_store,
    )

    # Exact fixture counts:
    # 120 valid GSTIN + 25 valid no-GSTIN + 15 Tier-1 dupes + 10 Tier-2 dupes = 170 valid rows
    # 12 invalid GSTIN + 10 other invalid rows = 22 error rows
    # 8 blank rows
    assert report.total_rows == 200
    assert report.skipped_blank_rows == 8
    assert report.error_rows == 22
    assert report.invalid_gstin_count == 12
    assert report.valid_rows == 170
    assert report.inserted_businesses == 145
    assert report.merged_duplicates == 25
    assert report.inserted_seed_customers == 170
    assert report.low_confidence_rows == 35  # 25 unique no-GSTIN + 10 Tier-2 no-GSTIN dupes
    assert len(report.merge_log) == 25
    assert len(biz_store.records) == 145
    assert len(seed_store.records) == 170

    # Verify every invalid GSTIN error code is flagged cleanly
    gstin_error_codes = {
        err.error_code for err in report.errors if err.error_code != "VALIDATION_ERROR"
    }
    assert gstin_error_codes == {
        "INVALID_LENGTH",
        "INVALID_FORMAT",
        "INVALID_STATE_CODE",
        "INVALID_CHECKSUM",
    }

    # Verify Tier-1 and Tier-2 merges are both logged
    tiers = {ev.tier for ev in report.merge_log}
    assert tiers == {"tier1_gstin", "tier2_name_pincode"}

    # Ensure no invalid GSTIN leaked into stored businesses
    for rec in biz_store.records:
        if rec.gstin is not None:
            assert validate_gstin(rec.gstin).is_valid is True


def test_import_xlsx_with_dict_column_mapping(tmp_path: Path) -> None:
    g_valid = _make_gstin("27AAACP7777C1Z")
    g_bad = "27AAACP7777C1Z0"  # wrong checksum unless by coincidence 0; ensure invalid:
    if validate_gstin(g_bad).is_valid:
        g_bad = "27AAACP7777C1Z1"

    df = pd.DataFrame(
        [
            {
                "Party": "Pune Packaging Pvt Ltd",
                "GST No": g_valid,
                "Town": "Pune",
                "Pin": 411026,
                "Status": "Won",
                "Invoice Amt": "1,50,000",
                "Date": "2025-04-12",
            },
            {
                "Party": "Broken GSTIN Co",
                "GST No": g_bad,
                "Town": "Pune",
                "Pin": 411001,
                "Status": "lost",
                "Invoice Amt": "",
                "Date": "",
            },
            {
                "Party": "Plain Name City Co",
                "GST No": "",
                "Town": "Nashik",
                "Pin": "",
                "Status": "churned",
                "Invoice Amt": None,
                "Date": None,
            },
        ]
    )
    xlsx_path = tmp_path / "tally_export.xlsx"
    df.to_excel(xlsx_path, index=False, engine="openpyxl")

    mapping = {
        "name": "Party",
        "gstin": "GST No",
        "city": "Town",
        "pincode": "Pin",
        "outcome": "Status",
        "deal_value_inr": "Invoice Amt",
        "closed_at": "Date",
    }
    biz_store = InMemoryBusinessStore()
    seed_store = InMemorySeedStore()

    report = import_seed_file(
        xlsx_path,
        client_id=7,
        column_mapping=mapping,
        business_store=biz_store,
        seed_store=seed_store,
    )

    assert report.total_rows == 3
    assert report.valid_rows == 2
    assert report.error_rows == 1
    assert report.invalid_gstin_count == 1
    assert report.low_confidence_rows == 1
    assert seed_store.records[0].deal_value_inr == Decimal("150000.00")
    assert seed_store.records[1].match_confidence == "low"


def test_import_dataframe_dedupes_against_existing_businesses() -> None:
    g_existing = _make_gstin("27AAACP8888D1Z")
    biz_store = InMemoryBusinessStore(
        [
            BusinessRecord(
                gstin=g_existing,
                legal_name="Existing Hydraulics Pvt Ltd",
                city="Pune",
                pincode="411018",
                source="seed_import",
            )
        ]
    )
    seed_store = InMemorySeedStore()

    df = pd.DataFrame(
        [
            {
                "name": "existing hydraulics private limited",
                "gstin": g_existing,
                "city": "Pune",
                "state": "Maharashtra",
                "pincode": "411018",
                "outcome": "won",
                "deal_value_inr": "250000",
                "closed_at": "2025-02-20",
            }
        ]
    )

    report = import_seed_dataframe(
        df,
        client_id=9,
        business_store=biz_store,
        seed_store=seed_store,
    )

    assert report.inserted_businesses == 0
    assert report.merged_duplicates == 1
    assert report.inserted_seed_customers == 1
    assert len(biz_store.records) == 1
    assert biz_store.records[0].state == "Maharashtra"
