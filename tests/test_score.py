"""Phase 3 (Stage 4) tests: scoped collector, exclusions, 0-100 scorer, CSV export, and UI."""

from __future__ import annotations

import csv
import io
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from profile.icp_text import generate_icp_fallback_text
from profile.lift import build_seed_feature_dataframe, calculate_lift
from profile.weights import InMemoryProfileStore, create_profile_version

import httpx
import pytest

from enrich.cache import CachedHttpClient, InMemoryRawPayloadStore, RateLimiter
from enrich.pipeline import BusinessFeaturesRecord, InMemoryBusinessFeaturesStore
from ingest.dedupe import BusinessRecord, InMemoryBusinessStore
from ingest.gstin import compute_gstin_checksum
from ingest.importer import SeedCustomerRecord
from score.collect import (
    collect_from_csv,
    collect_from_directory_if_permitted,
    collect_from_places,
    validate_collection_scope,
)
from score.exclude import filter_candidates
from score.export import EXPORT_COLUMNS, export_leads_csv, export_leads_csv_string
from score.scorer import InMemoryLeadScoreStore, score_candidates
from ui.app import format_leads_dataframe, format_top_traits_dataframe


def _make_gstin(prefix14: str) -> str:
    return prefix14 + compute_gstin_checksum(prefix14)


# ---------------------------------------------------------------------------
# Step 3.4: Scoped Candidate Collector
# ---------------------------------------------------------------------------
def test_collection_scope_guardrails() -> None:
    city, inds = validate_collection_scope(
        "Pune", ["machinery_equipment", "packaging_plastics", "automotive_components"]
    )
    assert city == "Pune"
    assert len(inds) == 3

    with pytest.raises(ValueError, match="1 target city"):
        validate_collection_scope("Pune, Mumbai", ["machinery_equipment"])

    with pytest.raises(ValueError, match="at most 3 industries"):
        validate_collection_scope(
            "Pune",
            [
                "machinery_equipment",
                "packaging_plastics",
                "automotive_components",
                "pharma_chemicals",
            ],
        )


def test_collect_from_places_and_skips_already_enriched(tmp_path: Path) -> None:
    biz_store = InMemoryBusinessStore()
    features_store = InMemoryBusinessFeaturesStore()
    payload_store = InMemoryRawPayloadStore()

    # Pre-seed an already-enriched business to verify collector skips re-enriching it
    existing = biz_store.insert_business(
        BusinessRecord(
            legal_name="Existing MIDC Pumps",
            trade_name="Existing MIDC Pumps",
            city="Pune",
            pincode="411026",
            source="seed_import",
        )
    )
    assert existing.id is not None
    features_store.save(
        BusinessFeaturesRecord(
            id=None,
            business_id=existing.id,
            industry="machinery_equipment",
            gst_status="Active",
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        q = body["textQuery"].lower()
        if "in pune" in q:
            return httpx.Response(
                200,
                json={
                    "places": [
                        {
                            "id": "ChIJExisting",
                            "displayName": {"text": "Existing MIDC Pumps"},
                            "primaryType": "industrial_equipment_supplier",
                            "types": ["machinery"],
                            "formattedAddress": "MIDC Bhosari, Pune 411026",
                        },
                        {
                            "id": "ChIJNew1",
                            "displayName": {"text": "Sahyadri Hydraulic Valves Pvt Ltd"},
                            "primaryType": "industrial_equipment_supplier",
                            "types": ["machinery", "manufacturer"],
                            "formattedAddress": "Chakan MIDC, Pune 410501",
                            "rating": 4.5,
                            "userRatingCount": 32,
                            "websiteUri": "https://sahyadrivalves.example.in",
                            "nationalPhoneNumber": "020-27129999",
                        },
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "places": [
                    {
                        "id": "ChIJNew1",
                        "displayName": {"text": "Sahyadri Hydraulic Valves Pvt Ltd"},
                        "primaryType": "industrial_equipment_supplier",
                        "types": ["machinery", "manufacturer"],
                        "formattedAddress": "Chakan MIDC, Pune 410501",
                        "rating": 4.5,
                        "userRatingCount": 32,
                        "websiteUri": "https://sahyadrivalves.example.in",
                        "nationalPhoneNumber": "020-27129999",
                    }
                ]
            },
        )

    client = CachedHttpClient(
        payload_store,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limiter=RateLimiter(default_rate_per_sec=0),
    )

    report = collect_from_places(
        "Pune",
        ["machinery_equipment"],
        http_client=client,
        business_store=biz_store,
        features_store=features_store,
        api_key="mock-key",
    )
    assert report.candidates_seen == 2
    assert report.merged_duplicates == 1
    assert report.new_businesses == 1
    assert report.already_enriched_skipped == 1
    assert report.newly_enriched == 1

    # Also test manual MIDC CSV import skips already-enriched businesses
    midc_csv = tmp_path / "midc.csv"
    midc_csv.write_text(
        "name,city,pincode\n"
        "Existing MIDC Pumps,Pune,411026\n"
        "Chakan Corrugated Boxes LLP,Pune,410501\n",
        encoding="utf-8",
    )
    csv_report = collect_from_csv(
        midc_csv,
        "Pune",
        ["packaging_plastics"],
        business_store=biz_store,
        features_store=features_store,
        http_client=client,
        enable_gst=False,
        enable_places=False,
    )
    assert csv_report.already_enriched_skipped == 1
    assert csv_report.newly_enriched == 1


def test_directory_collection_requires_tos_and_robots_txt() -> None:
    biz_store = InMemoryBusinessStore()
    features_store = InMemoryBusinessFeaturesStore()
    payload_store = InMemoryRawPayloadStore()

    def robots_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="User-agent: *\nDisallow: /search/\n")

    client = CachedHttpClient(
        payload_store,
        http_client=httpx.Client(transport=httpx.MockTransport(robots_handler)),
        rate_limiter=RateLimiter(default_rate_per_sec=0),
    )

    with pytest.raises(PermissionError, match="ToS permission"):
        collect_from_directory_if_permitted(
            [{"name": "Blocked Co"}],
            "Pune",
            ["machinery_equipment"],
            tos_permitted=False,
            robots_url="https://dir.test/robots.txt",
            target_url="https://dir.test/search/pune",
            http_client=client,
            business_store=biz_store,
            features_store=features_store,
        )

    with pytest.raises(PermissionError, match="robots.txt"):
        collect_from_directory_if_permitted(
            [{"name": "Blocked Co"}],
            "Pune",
            ["machinery_equipment"],
            tos_permitted=True,
            robots_url="https://dir.test/robots.txt",
            target_url="https://dir.test/search/pune",
            http_client=client,
            business_store=biz_store,
            features_store=features_store,
        )


# ---------------------------------------------------------------------------
# Step 3.5 & 3.6 + Gate 3 End-to-End Pilot Flow
# ---------------------------------------------------------------------------
def test_exclusion_scoring_export_and_gate3_end_to_end(tmp_path: Path) -> None:
    # 1. Build pilot client seed data (Pune, machinery_equipment + packaging_plastics)
    biz_store = InMemoryBusinessStore()
    features_store = InMemoryBusinessFeaturesStore()
    seeds: list[SeedCustomerRecord] = []

    for i in range(1, 21):
        g = _make_gstin(f"27AABCP{1000 + i:04d}A1Z")
        biz = biz_store.insert_business(
            BusinessRecord(
                gstin=g,
                legal_name=f"Seed Winner {i} Pvt Ltd",
                city="Pune",
                state="Maharashtra",
                pincode="411026",
                source="seed_import",
            )
        )
        assert biz.id is not None
        features_store.save(
            BusinessFeaturesRecord(
                id=None,
                business_id=biz.id,
                industry="machinery_equipment" if i <= 14 else "packaging_plastics",
                business_type="Pvt Ltd",
                registration_date=date(2018, 5, 1),
                gst_status="Active",
                filing_regular=True,
                turnover_band="5Cr-25Cr",
                google_rating=4.5,
                review_count=40,
                has_website=True,
            )
        )
        seeds.append(
            SeedCustomerRecord(
                id=i,
                client_id=1,
                business_id=biz.id,
                outcome="won",
                deal_value_inr=Decimal(250000),
                closed_at=date(2025, 4, 1),
            )
        )

    for i in range(21, 31):
        g = _make_gstin(f"27AABCP{1000 + i:04d}B1Z")
        biz = biz_store.insert_business(
            BusinessRecord(
                gstin=g,
                legal_name=f"Seed Lost {i} Co",
                city="Mumbai",
                state="Maharashtra",
                pincode="400001",
                source="seed_import",
            )
        )
        assert biz.id is not None
        features_store.save(
            BusinessFeaturesRecord(
                id=None,
                business_id=biz.id,
                industry="retail_b2c_commerce",
                business_type="Proprietorship",
                registration_date=date(2025, 8, 1),
                gst_status="Active",
                filing_regular=False,
                turnover_band="<40L",
                google_rating=3.1,
                review_count=2,
                has_website=False,
            )
        )
        seeds.append(
            SeedCustomerRecord(
                id=i,
                client_id=1,
                business_id=biz.id,
                outcome="lost",
                deal_value_inr=Decimal(30000),
                closed_at=date(2025, 5, 1),
            )
        )

    # 2. Profile (lift -> weights -> 3-sentence ICP -> profile v1)
    biz_by_id = {b.id: b for b in biz_store.records if b.id is not None}
    feat_by_id = {b_id: features_store.get_latest(b_id) for b_id in biz_by_id}
    clean_feat_by_id = {k: v for k, v in feat_by_id.items() if v is not None}

    seed_df = build_seed_feature_dataframe(
        seeds, biz_by_id, clean_feat_by_id, as_of_date=date(2026, 11, 18)
    )
    lift_res = calculate_lift(seed_df, client_id=1, min_support=3)
    icp_text = generate_icp_fallback_text("Industrial SaaS", lift_res.traits)
    profile_store = InMemoryProfileStore()
    profile = create_profile_version(profile_store, 1, lift_res.traits, icp_text)

    # 3. Candidate pool including:
    #    - 1 existing seed customer (excluded)
    #    - 1 cancelled GSTIN (excluded)
    #    - 1 duplicate by name+pincode (excluded)
    #    - 1 high-fit Pune machinery manufacturer
    #    - 1 low-fit Mumbai small retailer
    seed_biz = biz_store.records[0]

    g_cancelled = _make_gstin("27AABCP9001C1Z")
    cand_cancelled = biz_store.insert_business(
        BusinessRecord(
            gstin=g_cancelled,
            legal_name="Cancelled Corp Pvt Ltd",
            city="Pune",
            pincode="411026",
            source="places",
        )
    )
    assert cand_cancelled.id is not None
    features_store.save(
        BusinessFeaturesRecord(
            id=None,
            business_id=cand_cancelled.id,
            industry="machinery_equipment",
            gst_status="Cancelled",
            filing_regular=False,
        )
    )

    g_high = _make_gstin("27AABCP9002D1Z")
    cand_high = biz_store.insert_business(
        BusinessRecord(
            gstin=g_high,
            legal_name="Bhosari Precision Machinery Pvt Ltd",
            city="Pune",
            pincode="411026",
            source="places",
        )
    )
    assert cand_high.id is not None
    features_store.save(
        BusinessFeaturesRecord(
            id=None,
            business_id=cand_high.id,
            industry="machinery_equipment",
            business_type="Pvt Ltd",
            registration_date=date(2018, 1, 1),
            gst_status="Active",
            filing_regular=True,
            turnover_band="5Cr-25Cr",
            google_rating=4.6,
            review_count=45,
            has_website=True,
            website="https://bhosariprecision.example.in",
            business_contact="020-27128888",
        )
    )

    # Remaining duplicate with same normalized name + pincode as cand_high
    cand_dup = BusinessRecord(
        id=9999,
        gstin=None,
        legal_name="bhosari precision machinery private limited",
        city="Pune",
        pincode="411026",
        source="csv",
    )

    g_low = _make_gstin("27AABCP9003E1Z")
    cand_low = biz_store.insert_business(
        BusinessRecord(
            gstin=g_low,
            legal_name="Mumbai Corner Retail Store",
            city="Mumbai",
            pincode="400001",
            source="places",
        )
    )
    assert cand_low.id is not None
    features_store.save(
        BusinessFeaturesRecord(
            id=None,
            business_id=cand_low.id,
            industry="retail_b2c_commerce",
            business_type="Proprietorship",
            registration_date=date(2025, 9, 1),
            gst_status="Active",
            filing_regular=False,
            turnover_band="<40L",
            google_rating=3.0,
            review_count=1,
            has_website=False,
        )
    )

    all_feat_by_id = {
        b.id: f
        for b in [cand_cancelled, cand_high, cand_low, seed_biz]
        if b.id is not None and (f := features_store.get_latest(b.id)) is not None
    }
    all_feat_by_id[9999] = all_feat_by_id[cand_high.id]

    seed_ids = {s.business_id for s in seeds}
    excl = filter_candidates(
        [seed_biz, cand_cancelled, cand_high, cand_dup, cand_low],
        seed_business_ids=seed_ids,
        features_by_business=all_feat_by_id,
    )
    assert [b.id for b in excl.eligible] == [cand_high.id, cand_low.id]
    assert {e.reason for e in excl.excluded} == {
        "existing_seed_customer",
        "inactive_gst_status:Cancelled",
        "duplicate_name_pincode",
    }

    # 4. Score eligible candidates
    score_store = InMemoryLeadScoreStore()
    scored = score_candidates(
        excl.eligible,
        all_feat_by_id,
        profile,
        score_store=score_store,
        as_of_date=date(2026, 11, 18),
    )
    assert len(scored) == 2
    assert scored[0].business_id == cand_high.id
    assert scored[1].business_id == cand_low.id
    assert 0 <= scored[1].score < scored[0].score <= 100
    assert len(scored[0].top_reasons) == 3
    assert "Matches " in scored[0].reasons_summary
    assert len(score_store.list_by_profile(profile.id or 1)) == 2

    # 5. Export ranked CSV and verify columns + UI helper formatting
    csv_text = export_leads_csv_string(scored, top_n=10)
    parsed = list(csv.DictReader(io.StringIO(csv_text)))
    assert tuple(parsed[0].keys()) == EXPORT_COLUMNS
    assert parsed[0]["rank"] == "1"
    assert parsed[0]["business_name"] == "Bhosari Precision Machinery Pvt Ltd"
    assert parsed[0]["website"] == "https://bhosariprecision.example.in"

    out_csv = export_leads_csv(scored, tmp_path / "ranked.csv", top_n=50)
    assert out_csv.exists()

    ui_leads_df = format_leads_dataframe(scored)
    assert list(ui_leads_df["Score"]) == [scored[0].score, scored[1].score]
    ui_traits_df = format_top_traits_dataframe(profile.feature_weights)
    assert not ui_traits_df.empty
