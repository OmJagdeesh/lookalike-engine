"""Phase 3 (Stage 3) tests: lift calculation, capped weights, versioning, and ICP text."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from profile.icp_text import (
    build_icp_prompt,
    generate_icp_fallback_text,
    generate_icp_text,
)
from profile.lift import (
    build_seed_feature_dataframe,
    calculate_lift,
)
from profile.weights import (
    InMemoryProfileStore,
    compute_feature_weights,
    create_profile_version,
)

import pytest

from enrich.pipeline import BusinessFeaturesRecord
from ingest.dedupe import BusinessRecord
from ingest.importer import SeedCustomerRecord


def _sample_seed_dataset() -> tuple[
    list[SeedCustomerRecord], dict[int, BusinessRecord], dict[int, BusinessFeaturesRecord]
]:
    seeds: list[SeedCustomerRecord] = []
    businesses: dict[int, BusinessRecord] = {}
    features: dict[int, BusinessFeaturesRecord] = {}

    # 15 won deals: predominantly Pune, machinery_equipment, 5-10y, regular GST filer
    for i in range(1, 16):
        biz_id = i
        businesses[biz_id] = BusinessRecord(
            id=biz_id,
            legal_name=f"Won Precision {i} Pvt Ltd",
            city="Pune" if i <= 13 else "Nashik",
            pincode="411026",
            source="seed_import",
        )
        features[biz_id] = BusinessFeaturesRecord(
            id=i,
            business_id=biz_id,
            industry="machinery_equipment" if i <= 14 else "packaging_plastics",
            business_type="Pvt Ltd" if i <= 10 else "LLP",
            registration_date=date(2019, 1, 1) if i <= 10 else date(2022, 1, 1),
            gst_status="Active",
            filing_regular=(i <= 14),
            turnover_band="5Cr-25Cr" if i <= 9 else "1.5Cr-5Cr",
            google_rating=4.4 if i <= 9 else 3.9,
            review_count=35 if i <= 8 else 6,
            has_website=(i <= 10),
        )
        seeds.append(
            SeedCustomerRecord(
                id=i,
                client_id=1,
                business_id=biz_id,
                outcome="won",
                deal_value_inr=Decimal(200000),
                closed_at=date(2025, 4, 1),
            )
        )

    # 15 lost/churned deals: predominantly Mumbai, retail_b2c_commerce, <2y, irregular filer
    for i in range(16, 31):
        biz_id = i
        businesses[biz_id] = BusinessRecord(
            id=biz_id,
            legal_name=f"Lost Account {i} Co",
            city="Mumbai" if i <= 28 else "Pune",
            pincode="400001",
            source="seed_import",
        )
        features[biz_id] = BusinessFeaturesRecord(
            id=i,
            business_id=biz_id,
            industry="retail_b2c_commerce" if i <= 29 else "machinery_equipment",
            business_type="Proprietorship" if i <= 24 else "Pvt Ltd",
            registration_date=date(2025, 6, 1) if i <= 24 else date(2022, 1, 1),
            gst_status="Active",
            filing_regular=(i > 28),
            turnover_band="<40L" if i <= 24 else "1.5Cr-5Cr",
            google_rating=3.2 if i <= 24 else 3.9,
            review_count=2 if i <= 24 else 6,
            has_website=(i > 25),
        )
        seeds.append(
            SeedCustomerRecord(
                id=i,
                client_id=1,
                business_id=biz_id,
                outcome="lost" if i % 2 == 0 else "churned",
                deal_value_inr=Decimal(40000),
                closed_at=date(2025, 5, 1),
            )
        )

    return seeds, businesses, features


def test_lift_calculator_ranks_positive_and_filters_rare_traits() -> None:
    seeds, businesses, features = _sample_seed_dataset()
    df = build_seed_feature_dataframe(
        seeds, businesses, features, as_of_date=date(2026, 11, 18)
    )
    res = calculate_lift(df, client_id=1, alpha=1.0, min_support=3)

    assert res.won_total == 15
    assert res.baseline_total == 15
    assert res.baseline_mode == "lost_churned"
    assert not res.table.empty

    top_keys = [t.key for t in res.traits[:10]]
    assert "industry=machinery_equipment" in top_keys
    assert "city=Pune" in top_keys
    assert "filing_regular=true" in top_keys

    # Rare noise (e.g. Nashik appears only twice < min_support=3) is filtered out
    assert all(t.support_count >= 3 for t in res.traits)
    assert all(t.value != "Nashik" for t in res.traits)

    # Verify descending lift ranking
    lifts = [t.lift for t in res.traits]
    assert lifts == sorted(lifts, reverse=True)


def test_lift_falls_back_to_seed_baseline_when_lost_deals_scarce() -> None:
    seeds, businesses, features = _sample_seed_dataset()
    # Keep only 2 lost deals (< min_negatives=5)
    scarce_seeds = [s for s in seeds if s.outcome == "won"][:12] + [
        s for s in seeds if s.outcome != "won"
    ][:2]
    df = build_seed_feature_dataframe(
        scarce_seeds, businesses, features, as_of_date=date(2026, 11, 18)
    )
    res = calculate_lift(df, client_id=1, min_negatives=5, min_support=2)
    assert res.baseline_mode == "seed_baseline"
    assert res.won_total == 12
    assert res.baseline_total == 14


def test_weights_capped_and_profile_versions_never_overwrite() -> None:
    seeds, businesses, features = _sample_seed_dataset()
    df = build_seed_feature_dataframe(
        seeds, businesses, features, as_of_date=date(2026, 11, 18)
    )
    lift_res = calculate_lift(df, client_id=1, min_support=3)

    weights = compute_feature_weights(lift_res.traits, w_max=1.25)
    assert weights
    assert all(-1.25 <= w <= 1.25 for w in weights.values())
    assert weights["industry=machinery_equipment"] > 0
    assert weights["industry=retail_b2c_commerce"] < 0

    store = InMemoryProfileStore()
    p1 = create_profile_version(store, 1, lift_res.traits, "Sentence 1. Sentence 2. Sentence 3.")
    p2 = create_profile_version(store, 1, lift_res.traits, "Updated 1. Updated 2. Updated 3.")

    assert p1.version == 1
    assert p2.version == 2
    assert len(store.list_versions(1)) == 2
    assert store.get_version(1, 1) == p1
    assert store.get_latest(1) == p2


def test_icp_text_single_llm_call_has_no_pii_and_returns_three_sentences() -> None:
    seeds, businesses, features = _sample_seed_dataset()
    df = build_seed_feature_dataframe(
        seeds, businesses, features, as_of_date=date(2026, 11, 18)
    )
    lift_res = calculate_lift(df, client_id=1, min_support=3)

    prompt = build_icp_prompt("Industrial ERP", lift_res.traits)
    # Prompt must NEVER contain raw company names or PII
    assert "Won Precision 1" not in prompt
    assert "Lost Account" not in prompt
    assert "industry=machinery_equipment" in prompt

    call_count = 0

    def fake_llm(prompt_text: str) -> str:
        nonlocal call_count
        call_count += 1
        return (
            "Our best customers are established Pune machinery and equipment manufacturers with active websites. "
            "They consistently maintain regular GST filings and 5 to 10 years of operating history. "
            "Unlike churned small retail accounts, these B2B firms show strong Google ratings and mid-market turnover."
        )

    summary = generate_icp_text("Industrial ERP", lift_res.traits, llm_callable=fake_llm)
    assert call_count == 1
    assert len(re.split(r"(?<=[.!?])\s+", summary)) == 3

    fallback = generate_icp_fallback_text("Industrial ERP", lift_res.traits)
    assert len(re.split(r"(?<=[.!?])\s+", fallback)) == 3

    with pytest.raises(RuntimeError, match="LLM_API_KEY"):
        generate_icp_text("Industrial ERP", lift_res.traits, api_key="", use_fallback_if_no_key=False)
