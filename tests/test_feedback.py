"""Phase 4 (Stage 5) tests: append-only outcome capture, re-profiling, and holdout backtester."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from profile.icp_text import generate_icp_fallback_text
from profile.lift import build_seed_feature_dataframe, calculate_lift
from profile.weights import InMemoryProfileStore, create_profile_version

import pytest

from enrich.pipeline import BusinessFeaturesRecord
from feedback.backtest import (
    compare_profile_versions,
    run_holdout_backtest,
    split_holdout_won_customers,
    train_backtest_profile,
)
from feedback.outcomes import (
    ClientResolvedOutcome,
    InMemoryOutcomeStore,
    import_outcomes_csv,
    reprofile_client,
)
from ingest.dedupe import BusinessRecord
from ingest.importer import SeedCustomerRecord
from score.scorer import InMemoryLeadScoreStore, score_candidates
from ui.app import format_leads_dataframe


def _build_backtest_universe() -> tuple[
    list[SeedCustomerRecord],
    dict[int, BusinessRecord],
    dict[int, BusinessFeaturesRecord],
    list[BusinessRecord],
]:
    """Create 25 won + 15 lost seeds plus 80 non-seed candidates (120 businesses total)."""
    businesses: dict[int, BusinessRecord] = {}
    features: dict[int, BusinessFeaturesRecord] = {}
    seeds: list[SeedCustomerRecord] = []

    # 25 historical won customers: Pune, machinery_equipment, 5-10y, regular GST, high rating
    for i in range(1, 26):
        businesses[i] = BusinessRecord(
            id=i,
            legal_name=f"Won Hydraulics {i} Pvt Ltd",
            city="Pune",
            pincode="411026",
            source="seed_import",
        )
        features[i] = BusinessFeaturesRecord(
            id=i,
            business_id=i,
            industry="machinery_equipment" if i <= 22 else "packaging_plastics",
            business_type="Pvt Ltd",
            registration_date=date(2018, 4, 1),
            gst_status="Active",
            filing_regular=True,
            turnover_band="5Cr-25Cr",
            google_rating=4.5,
            review_count=40,
            has_website=True,
        )
        seeds.append(
            SeedCustomerRecord(
                id=i,
                client_id=1,
                business_id=i,
                outcome="won",
                deal_value_inr=Decimal(250000),
                closed_at=date(2025, 3, 1),
            )
        )

    # 15 historical lost/churned customers: Mumbai, retail_b2c_commerce, <2y, irregular filer
    for i in range(26, 41):
        businesses[i] = BusinessRecord(
            id=i,
            legal_name=f"Lost Retail {i} Store",
            city="Mumbai",
            pincode="400001",
            source="seed_import",
        )
        features[i] = BusinessFeaturesRecord(
            id=i,
            business_id=i,
            industry="retail_b2c_commerce",
            business_type="Proprietorship",
            registration_date=date(2025, 7, 1),
            gst_status="Active",
            filing_regular=False,
            turnover_band="<40L",
            google_rating=3.0,
            review_count=2,
            has_website=False,
        )
        seeds.append(
            SeedCustomerRecord(
                id=i,
                client_id=1,
                business_id=i,
                outcome="lost" if i % 2 == 0 else "churned",
                deal_value_inr=Decimal(30000),
                closed_at=date(2025, 4, 1),
            )
        )

    # 80 non-seed candidates: mostly lower-fit or mixed profiles so true won lookalikes rank top
    candidate_pool: list[BusinessRecord] = []
    for i in range(101, 181):
        is_mid_fit = i <= 115
        biz = BusinessRecord(
            id=i,
            legal_name=f"Candidate Co {i} {'Pvt Ltd' if is_mid_fit else 'Traders'}",
            city="Nashik" if is_mid_fit else "Mumbai",
            pincode="422007" if is_mid_fit else "400002",
            source="places",
        )
        businesses[i] = biz
        candidate_pool.append(biz)
        features[i] = BusinessFeaturesRecord(
            id=i,
            business_id=i,
            industry="packaging_plastics" if is_mid_fit else "retail_b2c_commerce",
            business_type="LLP" if is_mid_fit else "Proprietorship",
            registration_date=date(2023, 1, 1) if is_mid_fit else date(2025, 8, 1),
            gst_status="Active",
            filing_regular=is_mid_fit,
            turnover_band="1.5Cr-5Cr" if is_mid_fit else "<40L",
            google_rating=3.9 if is_mid_fit else 3.1,
            review_count=8 if is_mid_fit else 1,
            has_website=is_mid_fit,
        )

    return seeds, businesses, features, candidate_pool


# ---------------------------------------------------------------------------
# Step 4.1: Append-Only Outcome Capture & CSV/UI Sync
# ---------------------------------------------------------------------------
def test_outcome_capture_append_only_and_csv_sync(tmp_path: Path) -> None:
    store = InMemoryOutcomeStore()
    store.register_lead_score(lead_score_id=10, client_id=1, business_id=101)
    store.register_lead_score(lead_score_id=11, client_id=1, business_id=102)
    store.register_lead_score(lead_score_id=12, client_id=2, business_id=103)  # other client

    t0 = datetime(2026, 11, 1, tzinfo=UTC)
    e1 = store.record_outcome(10, "contacted", "Called reception", updated_at=t0)
    e2 = store.record_outcome(
        10, "meeting", "Demo scheduled", updated_at=t0 + timedelta(days=1)
    )
    e3 = store.record_outcome(10, "won", "Signed annual deal", updated_at=t0 + timedelta(days=3))
    store.record_outcome(11, "no response", "Voicemail", updated_at=t0)
    store.record_outcome(12, "won", "Client 2 deal", updated_at=t0)

    # All 3 historical events for lead_score_id=10 are preserved (append-only)
    history = store.list_for_lead_score(10)
    assert [h.id for h in history] == [e1.id, e2.id, e3.id]
    assert [h.status for h in history] == ["contacted", "meeting", "won"]

    # Client-scoped resolved lookup returns ONLY client 1's resolved ('won'/'lost') leads
    resolved_c1 = store.list_latest_resolved_for_client(client_id=1)
    assert len(resolved_c1) == 1
    assert resolved_c1[0].business_id == 101
    assert resolved_c1[0].status == "won"

    with pytest.raises(ValueError, match="Invalid outcome status"):
        store.record_outcome(10, "invalid_stage")

    # CSV sync appends rows cleanly
    sync_csv = tmp_path / "outcomes.csv"
    sync_csv.write_text(
        "lead_score_id,status,notes\n11,lost,Went with competitor\n",
        encoding="utf-8",
    )
    imported = import_outcomes_csv(sync_csv, store)
    assert len(imported) == 1
    assert len(store.list_latest_resolved_for_client(client_id=1)) == 2


# ---------------------------------------------------------------------------
# Step 4.2: Re-Profile Job & UI Formatting
# ---------------------------------------------------------------------------
def test_reprofile_combines_resolved_outcomes_publishes_v2_and_rescores_open() -> None:
    seeds, businesses, features, candidate_pool = _build_backtest_universe()
    as_of = date(2026, 11, 18)

    # Create v1 from seeds
    seed_df = build_seed_feature_dataframe(seeds, businesses, features, as_of_date=as_of)
    lift_v1 = calculate_lift(seed_df, client_id=1, min_support=3)
    profile_store = InMemoryProfileStore()
    p1 = create_profile_version(
        profile_store,
        1,
        lift_v1.traits,
        generate_icp_fallback_text("CNC Monitoring", lift_v1.traits),
    )
    score_store = InMemoryLeadScoreStore()
    scored_v1 = score_candidates(
        candidate_pool, features, p1, score_store=score_store, as_of_date=as_of
    )

    outcome_store = InMemoryOutcomeStore()
    for lead in scored_v1:
        assert lead.lead_score_id is not None
        outcome_store.register_lead_score(lead.lead_score_id, client_id=1, business_id=lead.business_id)

    # Log resolved outcomes for 2 candidates + 1 unresolved 'contacted' outcome
    lead_101 = next(x for x in scored_v1 if x.business_id == 101)
    lead_102 = next(x for x in scored_v1 if x.business_id == 102)
    lead_120 = next(x for x in scored_v1 if x.business_id == 120)

    outcome_store.record_outcome(lead_101.lead_score_id or 0, "won", "Closed deal")
    outcome_store.record_outcome(lead_102.lead_score_id or 0, "contacted", "Waiting on quote")
    outcome_store.record_outcome(lead_120.lead_score_id or 0, "lost", "Too small")

    # Verify UI helper shows Latest Outcome cleanly
    latest_map = {
        lead_101.lead_score_id or 0: outcome_store.list_for_lead_score(lead_101.lead_score_id or 0)[-1]
    }
    ui_df = format_leads_dataframe([lead_101, lead_102], latest_outcomes=latest_map)
    assert list(ui_df["Latest Outcome"]) == ["won", ""]

    # Re-profile -> publishes v2, keeps v1 intact, excludes resolved 101 & 120 from open re-scoring
    reprof = reprofile_client(
        client_id=1,
        product_sold="CNC Monitoring",
        seed_customers=seeds,
        businesses=businesses,
        features_by_business=features,
        candidate_pool=candidate_pool,
        outcome_store=outcome_store,
        profile_store=profile_store,
        score_store=score_store,
        as_of_date=as_of,
    )

    assert reprof.new_profile.version == 2
    assert profile_store.get_version(1, 1) == p1  # v1 never overwritten
    assert reprof.resolved_outcomes_added == 2  # 'won' (101) + 'lost' (120); excludes 'contacted' (102)
    rescored_ids = {lead.business_id for lead in reprof.rescored_leads}
    assert 101 not in rescored_ids
    assert 120 not in rescored_ids
    assert 102 in rescored_ids


# ---------------------------------------------------------------------------
# Step 4.3 & Gate 4: Deterministic 20% Holdout Backtester & Version Comparison
# ---------------------------------------------------------------------------
def test_holdout_backtester_and_version_comparison() -> None:
    seeds, businesses, features, candidate_pool = _build_backtest_universe()
    as_of = date(2026, 11, 18)

    # Reproducible 20% holdout of 25 won customers -> 5 held-out won customers
    split1 = split_holdout_won_customers(seeds, client_id=1, holdout_ratio=0.20, random_seed=42)
    split2 = split_holdout_won_customers(seeds, client_id=1, holdout_ratio=0.20, random_seed=42)
    assert split1.holdout_business_ids == split2.holdout_business_ids
    assert len(split1.holdout_business_ids) == 5

    # Run Stage 3->4 backtest on 80% won + lost vs candidate pool + 5 held-out won
    metrics = run_holdout_backtest(
        client_id=1,
        seed_customers=seeds,
        businesses=businesses,
        features_by_business=features,
        candidate_pool=candidate_pool,
        holdout_ratio=0.20,
        random_seed=42,
        as_of_date=as_of,
    )
    assert metrics.holdout_count == 5
    assert metrics.candidate_pool_size == 85  # 80 pool + 5 held-out won
    assert metrics.top_50_hits == 5
    assert metrics.top_50_hit_rate == 100.0
    assert metrics.top_200_hit_rate == 100.0

    # Compare v1 (initial) vs v2 (after adding resolved sales feedback) on the exact same holdout
    p1 = train_backtest_profile(
        client_id=1,
        version=1,
        train_seeds=split1.train_seeds,
        businesses=businesses,
        features_by_business=features,
        holdout_business_ids=split1.holdout_business_ids,
        as_of_date=as_of,
    )
    # Simulate v2 trained with additional resolved lost feedback on non-fit Mumbai retailers
    extra_feedback = [
        ClientResolvedOutcome(
            client_id=1,
            business_id=bid,
            lead_score_id=bid,
            status="lost",
            updated_at=datetime(2026, 11, 10, tzinfo=UTC),
        )
        for bid in range(120, 130)
    ]
    p2 = train_backtest_profile(
        client_id=1,
        version=2,
        train_seeds=split1.train_seeds,
        businesses=businesses,
        features_by_business=features,
        resolved_outcomes=extra_feedback,
        holdout_business_ids=split1.holdout_business_ids,
        as_of_date=as_of,
    )

    comparison = compare_profile_versions(
        client_id=1,
        profiles=[p1, p2],
        seed_customers=seeds,
        businesses=businesses,
        features_by_business=features,
        candidate_pool=candidate_pool,
        holdout_ratio=0.20,
        random_seed=42,
        as_of_date=as_of,
    )
    assert list(comparison.comparison_table["version"]) == ["v1", "v2"]
    assert list(comparison.comparison_table["top_50_hit_rate_pct"]) == [100.0, 100.0]
    assert list(comparison.comparison_table["top_200_hit_rate_pct"]) == [100.0, 100.0]
