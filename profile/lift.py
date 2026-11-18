"""Stage 3.1: Feature lift calculator (won vs. lost/churned or baseline)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

import pandas as pd
import psycopg

from enrich.normalize import normalize_features
from enrich.pipeline import BusinessFeaturesRecord
from ingest.dedupe import BusinessRecord
from ingest.importer import SeedCustomerRecord

CATEGORICAL_FEATURES: tuple[str, ...] = (
    "industry",
    "business_age_band",
    "review_count_band",
    "rating_band",
    "turnover_band",
    "employee_band",
    "city",
    "business_type",
    "filing_regular",
    "has_website",
)


@dataclass(frozen=True)
class LiftTrait:
    """Single feature=value lift statistic."""

    feature: str
    value: str
    won_count: int
    baseline_count: int
    support_count: int
    won_share: float
    baseline_share: float
    lift: float
    summary: str

    @property
    def key(self) -> str:
        return f"{self.feature}={self.value}"


@dataclass
class LiftResult:
    """Output of the lift calculator."""

    client_id: int
    won_total: int
    baseline_total: int
    baseline_mode: str  # "lost_churned" or "seed_baseline" or "external_baseline"
    traits: list[LiftTrait]
    table: pd.DataFrame


def build_seed_feature_dataframe(
    seed_customers: list[SeedCustomerRecord],
    businesses: dict[int, BusinessRecord],
    features_by_business: dict[int, BusinessFeaturesRecord],
    *,
    as_of_date: date | None = None,
) -> pd.DataFrame:
    """Join seed_customers + businesses + latest business_features into a normalized DataFrame."""
    rows: list[dict[str, object]] = []
    for sc in seed_customers:
        biz = businesses.get(sc.business_id)
        if biz is None:
            continue
        feat = features_by_business.get(sc.business_id)
        norm = normalize_features(
            industry=feat.industry if feat else None,
            business_name=biz.trade_name or biz.legal_name,
            registration_date=feat.registration_date if feat else None,
            review_count=feat.review_count if feat else None,
            google_rating=feat.google_rating if feat else None,
            turnover_band=feat.turnover_band if feat else None,
            employee_band=feat.employee_band if feat else None,
            city=biz.city,
            business_type=feat.business_type if feat else None,
            filing_regular=feat.filing_regular if feat else None,
            has_website=feat.has_website if feat else None,
            as_of_date=as_of_date,
        )
        row: dict[str, object] = {
            "client_id": sc.client_id,
            "business_id": sc.business_id,
            "outcome": sc.outcome,
            "deal_value_inr": float(sc.deal_value_inr) if sc.deal_value_inr is not None else None,
        }
        row.update(norm.to_feature_dict())
        rows.append(row)

    return pd.DataFrame(rows)


def load_client_seed_dataframe(
    conn: psycopg.Connection,
    client_id: int,
    *,
    as_of_date: date | None = None,
) -> pd.DataFrame:
    """Load seed_customers + businesses + latest business_features strictly for one client_id."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                sc.client_id,
                sc.business_id,
                sc.outcome,
                sc.deal_value_inr,
                b.legal_name,
                b.trade_name,
                b.city,
                bf.industry,
                bf.business_type,
                bf.registration_date,
                bf.filing_regular,
                bf.turnover_band,
                bf.employee_band,
                bf.google_rating,
                bf.review_count,
                bf.has_website
            FROM seed_customers sc
            JOIN businesses b ON b.id = sc.business_id
            LEFT JOIN LATERAL (
                SELECT *
                FROM business_features bf_inner
                WHERE bf_inner.business_id = sc.business_id
                ORDER BY bf_inner.enriched_at DESC, bf_inner.id DESC
                LIMIT 1
            ) bf ON TRUE
            WHERE sc.client_id = %s
            """,
            (client_id,),
        )
        db_rows = cur.fetchall()

    rows: list[dict[str, object]] = []
    for r in db_rows:
        norm = normalize_features(
            industry=r[7],
            business_name=r[5] or r[4],
            registration_date=r[9],
            review_count=r[14],
            google_rating=float(r[13]) if r[13] is not None else None,
            turnover_band=r[11],
            employee_band=r[12],
            city=r[6],
            business_type=r[8],
            filing_regular=r[10],
            has_website=r[15],
            as_of_date=as_of_date,
        )
        row: dict[str, object] = {
            "client_id": r[0],
            "business_id": r[1],
            "outcome": r[2],
            "deal_value_inr": float(r[3]) if r[3] is not None else None,
        }
        row.update(norm.to_feature_dict())
        rows.append(row)

    return pd.DataFrame(rows)


def calculate_lift(
    df: pd.DataFrame,
    client_id: int,
    *,
    baseline_df: pd.DataFrame | None = None,
    alpha: float = 1.0,
    min_support: int = 3,
    min_negatives: int = 5,
    ignore_unknown: bool = True,
    features: tuple[str, ...] = CATEGORICAL_FEATURES,
) -> LiftResult:
    """Compute Laplace-smoothed feature lift for won deals vs. lost/churned or baseline."""
    if df.empty:
        return LiftResult(
            client_id=client_id,
            won_total=0,
            baseline_total=0,
            baseline_mode="empty",
            traits=[],
            table=pd.DataFrame(
                columns=[
                    "feature",
                    "value",
                    "won_count",
                    "baseline_count",
                    "support_count",
                    "won_share",
                    "baseline_share",
                    "lift",
                    "summary",
                ]
            ),
        )

    if "client_id" in df.columns:
        scoped = df[df["client_id"] == client_id]
    else:
        scoped = df

    won_df = scoped[scoped["outcome"] == "won"]
    neg_df = scoped[scoped["outcome"].isin(["lost", "churned"])]

    if baseline_df is not None and not baseline_df.empty:
        base_df = baseline_df
        baseline_mode = "external_baseline"
    elif len(neg_df) >= min_negatives:
        base_df = neg_df
        baseline_mode = "lost_churned"
    else:
        # When lost deals are scarce, fall back to overall seed base rate
        base_df = scoped
        baseline_mode = "seed_baseline"

    won_total = len(won_df)
    baseline_total = len(base_df)
    if won_total == 0 or baseline_total == 0:
        return LiftResult(
            client_id=client_id,
            won_total=won_total,
            baseline_total=baseline_total,
            baseline_mode=baseline_mode,
            traits=[],
            table=pd.DataFrame(),
        )

    traits: list[LiftTrait] = []
    for feat in features:
        if feat not in won_df.columns and feat not in base_df.columns:
            continue

        won_series = (
            won_df[feat].dropna().astype(str) if feat in won_df.columns else pd.Series(dtype=str)
        )
        base_series = (
            base_df[feat].dropna().astype(str) if feat in base_df.columns else pd.Series(dtype=str)
        )
        won_counts = won_series.value_counts().to_dict()
        base_counts = base_series.value_counts().to_dict()
        all_values = sorted(set(won_counts) | set(base_counts))

        for val in all_values:
            if ignore_unknown and val.lower() in {"unknown", "unrated", "none", ""}:
                continue
            w_cnt = int(won_counts.get(val, 0))
            b_cnt = int(base_counts.get(val, 0))
            support = w_cnt + b_cnt
            if support < min_support:
                continue

            # Binary Laplace smoothing prevents zero-division on small 30-500 row datasets
            won_share = (w_cnt + alpha) / (won_total + 2.0 * alpha)
            base_share = (b_cnt + alpha) / (baseline_total + 2.0 * alpha)
            lift = won_share / base_share
            if not math.isfinite(lift) or lift <= 0:
                continue

            comparison_word = "more" if lift >= 1.0 else "less"
            summary = f"{val}: {lift:.2f}x {comparison_word} common in won deals"
            traits.append(
                LiftTrait(
                    feature=feat,
                    value=val,
                    won_count=w_cnt,
                    baseline_count=b_cnt,
                    support_count=support,
                    won_share=round(won_share, 4),
                    baseline_share=round(base_share, 4),
                    lift=round(lift, 4),
                    summary=summary,
                )
            )

    traits.sort(key=lambda t: (-t.lift, -t.won_count, t.feature, t.value))

    table = pd.DataFrame(
        [
            {
                "feature": t.feature,
                "value": t.value,
                "won_count": t.won_count,
                "baseline_count": t.baseline_count,
                "support_count": t.support_count,
                "won_share": t.won_share,
                "baseline_share": t.baseline_share,
                "lift": t.lift,
                "summary": t.summary,
            }
            for t in traits
        ]
    )
    return LiftResult(
        client_id=client_id,
        won_total=won_total,
        baseline_total=baseline_total,
        baseline_mode=baseline_mode,
        traits=traits,
        table=table,
    )
