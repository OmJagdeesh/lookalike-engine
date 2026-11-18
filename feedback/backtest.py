"""Stage 5 (Step 4.3): Deterministic 20% holdout backtester and version comparison."""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import date
from profile.lift import build_seed_feature_dataframe, calculate_lift
from profile.weights import DEFAULT_W_MAX, ProfileRecord, compute_feature_weights

import pandas as pd

from enrich.pipeline import BusinessFeaturesRecord
from feedback.outcomes import (
    ClientResolvedOutcome,
    combine_seeds_and_resolved_outcomes,
)
from ingest.dedupe import BusinessRecord
from ingest.importer import SeedCustomerRecord
from score.exclude import filter_candidates
from score.scorer import score_candidates

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HoldoutSplit:
    """Result of splitting a client's historical won seed customers."""

    train_seeds: list[SeedCustomerRecord]
    holdout_business_ids: set[int]
    all_seed_business_ids: set[int]


@dataclass(frozen=True)
class BacktestMetrics:
    """Top-50 and Top-200 hit-rate metrics for a single profile version."""

    profile_version: int
    holdout_count: int
    candidate_pool_size: int
    top_50_hits: int
    top_50_hit_rate: float  # percentage 0.0 - 100.0
    top_200_hits: int
    top_200_hit_rate: float  # percentage 0.0 - 100.0
    held_out_ranks: dict[int, int]


@dataclass
class BacktestComparisonReport:
    """Side-by-side backtest comparison across profile versions."""

    client_id: int
    random_seed: int
    holdout_ratio: float
    holdout_business_ids: set[int]
    metrics_by_version: list[BacktestMetrics]
    comparison_table: pd.DataFrame


def split_holdout_won_customers(
    seed_customers: list[SeedCustomerRecord],
    client_id: int,
    *,
    holdout_ratio: float = 0.20,
    random_seed: int = 42,
) -> HoldoutSplit:
    """Deterministically hide `holdout_ratio` (default 20%) of won seed customers."""
    if not (0.0 < holdout_ratio < 1.0):
        raise ValueError(f"holdout_ratio must be in (0, 1), got {holdout_ratio}")

    scoped = [s for s in seed_customers if s.client_id == client_id]
    won_biz_ids = sorted({s.business_id for s in scoped if s.outcome == "won"})
    if len(won_biz_ids) < 2:
        raise ValueError(
            f"Need at least 2 won seed customers to hold out 20%, found {len(won_biz_ids)}."
        )

    rng = random.Random(random_seed)
    shuffled = list(won_biz_ids)
    rng.shuffle(shuffled)

    holdout_size = max(1, round(len(shuffled) * holdout_ratio))
    if holdout_size >= len(shuffled):
        holdout_size = len(shuffled) - 1

    holdout_ids = set(shuffled[:holdout_size])
    train_seeds = [s for s in scoped if s.business_id not in holdout_ids]
    all_seed_ids = {s.business_id for s in scoped}

    return HoldoutSplit(
        train_seeds=train_seeds,
        holdout_business_ids=holdout_ids,
        all_seed_business_ids=all_seed_ids,
    )


def evaluate_profile_holdout(
    profile: ProfileRecord,
    *,
    split: HoldoutSplit,
    businesses: dict[int, BusinessRecord],
    features_by_business: dict[int, BusinessFeaturesRecord],
    candidate_pool: list[BusinessRecord],
    as_of_date: date | None = None,
) -> BacktestMetrics:
    """Evaluate a profile by placing held-out won customers into the Stage 4 candidate pool."""
    # Merge candidate pool + held-out won businesses (avoiding duplicates)
    pool_by_id: dict[int, BusinessRecord] = {}
    for biz in candidate_pool:
        if biz.id is not None:
            pool_by_id[biz.id] = biz
    for bid in sorted(split.holdout_business_ids):
        if bid in businesses:
            pool_by_id[bid] = businesses[bid]

    filtered = filter_candidates(
        list(pool_by_id.values()),
        seed_business_ids=split.all_seed_business_ids,
        features_by_business=features_by_business,
        bypass_business_ids=split.holdout_business_ids,
    )

    eval_profile = (
        profile
        if profile.id is not None
        else ProfileRecord(
            id=profile.version,
            client_id=profile.client_id,
            version=profile.version,
            description_text=profile.description_text,
            feature_weights=profile.feature_weights,
            created_at=profile.created_at,
        )
    )
    ranked_leads = score_candidates(
        filtered.eligible,
        features_by_business,
        eval_profile,
        score_store=None,
        as_of_date=as_of_date,
    )

    ranks: dict[int, int] = {}
    for rank_idx, lead in enumerate(ranked_leads, start=1):
        if lead.business_id in split.holdout_business_ids:
            ranks[lead.business_id] = rank_idx

    holdout_count = len(split.holdout_business_ids)
    top_50_hits = sum(1 for r in ranks.values() if r <= 50)
    top_200_hits = sum(1 for r in ranks.values() if r <= 200)
    top_50_rate = round((top_50_hits / holdout_count) * 100.0, 2) if holdout_count else 0.0
    top_200_rate = round((top_200_hits / holdout_count) * 100.0, 2) if holdout_count else 0.0

    metrics = BacktestMetrics(
        profile_version=profile.version,
        holdout_count=holdout_count,
        candidate_pool_size=len(ranked_leads),
        top_50_hits=top_50_hits,
        top_50_hit_rate=top_50_rate,
        top_200_hits=top_200_hits,
        top_200_hit_rate=top_200_rate,
        held_out_ranks=ranks,
    )
    logger.info(
        "Backtest profile v%s: holdout=%s pool=%s top50=%.2f%% (%s/%s) top200=%.2f%% (%s/%s)",
        metrics.profile_version,
        metrics.holdout_count,
        metrics.candidate_pool_size,
        metrics.top_50_hit_rate,
        metrics.top_50_hits,
        metrics.holdout_count,
        metrics.top_200_hit_rate,
        metrics.top_200_hits,
        metrics.holdout_count,
    )
    return metrics


def train_backtest_profile(
    *,
    client_id: int,
    version: int,
    train_seeds: list[SeedCustomerRecord],
    businesses: dict[int, BusinessRecord],
    features_by_business: dict[int, BusinessFeaturesRecord],
    resolved_outcomes: list[ClientResolvedOutcome] | None = None,
    holdout_business_ids: set[int] | None = None,
    alpha: float = 1.0,
    min_support: int = 3,
    min_negatives: int = 5,
    w_max: float = DEFAULT_W_MAX,
    as_of_date: date | None = None,
) -> ProfileRecord:
    """Train a temporary backtest ProfileRecord via lift -> capped weights (excluding holdout)."""
    holdout_guard = holdout_business_ids or set()
    safe_outcomes = [
        o for o in (resolved_outcomes or []) if o.business_id not in holdout_guard
    ]
    combined, _ = combine_seeds_and_resolved_outcomes(train_seeds, safe_outcomes, client_id)

    df = build_seed_feature_dataframe(
        combined, businesses, features_by_business, as_of_date=as_of_date
    )
    lift_res = calculate_lift(
        df,
        client_id=client_id,
        alpha=alpha,
        min_support=min_support,
        min_negatives=min_negatives,
    )
    weights = compute_feature_weights(lift_res.traits, w_max=w_max)
    return ProfileRecord(
        id=version,
        client_id=client_id,
        version=version,
        description_text=f"Backtest profile v{version}",
        feature_weights=weights,
    )


def run_holdout_backtest(
    *,
    client_id: int,
    seed_customers: list[SeedCustomerRecord],
    businesses: dict[int, BusinessRecord],
    features_by_business: dict[int, BusinessFeaturesRecord],
    candidate_pool: list[BusinessRecord],
    holdout_ratio: float = 0.20,
    random_seed: int = 42,
    alpha: float = 1.0,
    min_support: int = 3,
    min_negatives: int = 5,
    w_max: float = DEFAULT_W_MAX,
    as_of_date: date | None = None,
) -> BacktestMetrics:
    """Run Stage 3 training on 80% won seeds and evaluate holdout hit rate in Stage 4."""
    split = split_holdout_won_customers(
        seed_customers,
        client_id,
        holdout_ratio=holdout_ratio,
        random_seed=random_seed,
    )
    profile = train_backtest_profile(
        client_id=client_id,
        version=1,
        train_seeds=split.train_seeds,
        businesses=businesses,
        features_by_business=features_by_business,
        holdout_business_ids=split.holdout_business_ids,
        alpha=alpha,
        min_support=min_support,
        min_negatives=min_negatives,
        w_max=w_max,
        as_of_date=as_of_date,
    )
    return evaluate_profile_holdout(
        profile,
        split=split,
        businesses=businesses,
        features_by_business=features_by_business,
        candidate_pool=candidate_pool,
        as_of_date=as_of_date,
    )


def compare_profile_versions(
    *,
    client_id: int,
    profiles: list[ProfileRecord],
    seed_customers: list[SeedCustomerRecord],
    businesses: dict[int, BusinessRecord],
    features_by_business: dict[int, BusinessFeaturesRecord],
    candidate_pool: list[BusinessRecord],
    holdout_ratio: float = 0.20,
    random_seed: int = 42,
    as_of_date: date | None = None,
) -> BacktestComparisonReport:
    """Evaluate and compare Top-50 and Top-200 hit rates across profile versions."""
    split = split_holdout_won_customers(
        seed_customers,
        client_id,
        holdout_ratio=holdout_ratio,
        random_seed=random_seed,
    )
    ordered_profiles = sorted(profiles, key=lambda p: p.version)

    metrics_list: list[BacktestMetrics] = []
    rows: list[dict[str, object]] = []
    for profile in ordered_profiles:
        m = evaluate_profile_holdout(
            profile,
            split=split,
            businesses=businesses,
            features_by_business=features_by_business,
            candidate_pool=candidate_pool,
            as_of_date=as_of_date,
        )
        metrics_list.append(m)
        rows.append(
            {
                "version": f"v{m.profile_version}",
                "holdout_count": m.holdout_count,
                "candidate_pool_size": m.candidate_pool_size,
                "top_50_hits": m.top_50_hits,
                "top_50_hit_rate_pct": m.top_50_hit_rate,
                "top_200_hits": m.top_200_hits,
                "top_200_hit_rate_pct": m.top_200_hit_rate,
            }
        )

    return BacktestComparisonReport(
        client_id=client_id,
        random_seed=random_seed,
        holdout_ratio=holdout_ratio,
        holdout_business_ids=split.holdout_business_ids,
        metrics_by_version=metrics_list,
        comparison_table=pd.DataFrame(rows),
    )
