"""Stage 3.5b: Deterministic 0-100 lookalike scorer and `lead_scores` persistence."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from profile.weights import ProfileRecord
from typing import Any, Protocol

import psycopg

from enrich.normalize import normalize_features
from enrich.pipeline import BusinessFeaturesRecord
from ingest.dedupe import BusinessRecord

_AGE_LABELS = {
    "<2y": "under 2 years old",
    "2-5y": "2 to 5 years old",
    "5-10y": "5 to 10 years old",
    "10y+": "10+ years old",
}


def _humanize_trait(feature: str, value: str) -> str:
    if feature == "business_age_band":
        return _AGE_LABELS.get(value, value)
    if feature == "filing_regular":
        return "regular GST filer" if value == "true" else "irregular GST filer"
    if feature == "has_website":
        return "has website" if value == "true" else "no website"
    if feature == "review_count_band":
        return f"{value} Google reviews"
    if feature == "rating_band":
        return f"{value} Google rating"
    if feature == "industry":
        return value.replace("_", " ")
    return value


@dataclass(frozen=True)
class LeadScoreRecord:
    """Persisted lead score row matching `lead_scores`."""

    id: int | None
    profile_id: int
    business_id: int
    score: int
    reasons: dict[str, Any]
    scored_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class ScoredLead:
    """Enriched scored candidate ready for review UI and CSV export."""

    lead_score_id: int | None
    profile_id: int
    business_id: int
    score: int
    business_name: str
    gstin: str | None
    city: str | None
    industry: str | None
    gst_status: str | None
    website: str | None
    business_contact: str | None
    reasons_summary: str
    top_reasons: tuple[str, ...]
    raw_score: float


class LeadScoreStore(Protocol):
    """Storage interface for persisting and reading `lead_scores`."""

    def save_scores(self, records: list[LeadScoreRecord]) -> list[LeadScoreRecord]: ...

    def list_by_profile(self, profile_id: int) -> list[LeadScoreRecord]: ...


class InMemoryLeadScoreStore:
    """In-memory LeadScoreStore for offline tests and backtesting."""

    def __init__(self) -> None:
        self._rows: dict[tuple[int, int], LeadScoreRecord] = {}
        self._next_id = 1

    def save_scores(self, records: list[LeadScoreRecord]) -> list[LeadScoreRecord]:
        saved: list[LeadScoreRecord] = []
        for rec in records:
            key = (rec.profile_id, rec.business_id)
            existing = self._rows.get(key)
            rec_id = existing.id if existing and existing.id is not None else self._next_id
            if existing is None:
                self._next_id += 1
            stored = LeadScoreRecord(
                id=rec_id,
                profile_id=rec.profile_id,
                business_id=rec.business_id,
                score=rec.score,
                reasons=dict(rec.reasons),
                scored_at=rec.scored_at,
            )
            self._rows[key] = stored
            saved.append(stored)
        return saved

    def list_by_profile(self, profile_id: int) -> list[LeadScoreRecord]:
        return sorted(
            (r for r in self._rows.values() if r.profile_id == profile_id),
            key=lambda r: (-r.score, r.business_id),
        )


class PostgresLeadScoreStore:
    """PostgreSQL-backed LeadScoreStore operating on `lead_scores`."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def save_scores(self, records: list[LeadScoreRecord]) -> list[LeadScoreRecord]:
        saved: list[LeadScoreRecord] = []
        with self._conn.cursor() as cur:
            for rec in records:
                cur.execute(
                    """
                    INSERT INTO lead_scores (profile_id, business_id, score, reasons, scored_at)
                    VALUES (%s, %s, %s, %s::jsonb, %s)
                    ON CONFLICT (profile_id, business_id)
                    DO UPDATE SET
                        score = EXCLUDED.score,
                        reasons = EXCLUDED.reasons,
                        scored_at = EXCLUDED.scored_at
                    RETURNING id, scored_at
                    """,
                    (
                        rec.profile_id,
                        rec.business_id,
                        rec.score,
                        json.dumps(rec.reasons),
                        rec.scored_at,
                    ),
                )
                row = cur.fetchone()
                assert row is not None
                saved.append(
                    LeadScoreRecord(
                        id=row[0],
                        profile_id=rec.profile_id,
                        business_id=rec.business_id,
                        score=rec.score,
                        reasons=dict(rec.reasons),
                        scored_at=row[1],
                    )
                )
        return saved

    def list_by_profile(self, profile_id: int) -> list[LeadScoreRecord]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, profile_id, business_id, score, reasons, scored_at
                FROM lead_scores
                WHERE profile_id = %s
                ORDER BY score DESC, business_id ASC
                """,
                (profile_id,),
            )
            rows = cur.fetchall()
        return [
            LeadScoreRecord(
                id=r[0],
                profile_id=r[1],
                business_id=r[2],
                score=int(r[3]),
                reasons=dict(r[4]),
                scored_at=r[5],
            )
            for r in rows
        ]


def _theoretical_bounds(feature_weights: dict[str, float]) -> tuple[float, float]:
    """Compute per-feature-group min/max theoretical raw score bounds."""
    groups: dict[str, list[float]] = defaultdict(list)
    for key, weight in feature_weights.items():
        if "=" in key:
            feat, _ = key.split("=", 1)
        else:
            feat = key
        groups[feat].append(float(weight))

    if not groups:
        return 0.0, 0.0

    min_bound = sum(min(0.0, min(vals)) for vals in groups.values())
    max_bound = sum(max(0.0, max(vals)) for vals in groups.values())
    return min_bound, max_bound


def _scale_score(raw_score: float, min_bound: float, max_bound: float) -> int:
    if max_bound <= min_bound:
        return 50
    normalized = (raw_score - min_bound) / (max_bound - min_bound)
    clamped = max(0.0, min(1.0, normalized))
    return round(clamped * 100)


def score_candidates(
    candidates: list[BusinessRecord],
    features_by_business: dict[int, BusinessFeaturesRecord],
    profile: ProfileRecord,
    *,
    score_store: LeadScoreStore | None = None,
    as_of_date: date | None = None,
) -> list[ScoredLead]:
    """Deterministically score eligible candidates 0-100 against an ICP profile."""
    if profile.id is None:
        raise ValueError("ProfileRecord must have a persisted `id` before scoring.")

    min_bound, max_bound = _theoretical_bounds(profile.feature_weights)
    positive_feature_count = sum(
        1
        for feat in {
            k.split("=", 1)[0]
            for k, w in profile.feature_weights.items()
            if w > 0 and "=" in k
        }
    )

    pending_records: list[LeadScoreRecord] = []
    intermediate: list[tuple[BusinessRecord, BusinessFeaturesRecord | None, int, dict[str, Any], float, tuple[str, ...]]] = []

    for biz in candidates:
        if biz.id is None:
            continue
        feat = features_by_business.get(biz.id)
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
        pair_dict = norm.to_feature_dict()

        raw_score = 0.0
        matched_positive: list[tuple[float, str, str, str]] = []
        matched_negative: list[tuple[float, str, str, str]] = []

        for f_name, f_val in pair_dict.items():
            key = f"{f_name}={f_val}"
            weight = profile.feature_weights.get(key)
            if weight is None:
                continue
            raw_score += weight
            label = _humanize_trait(f_name, f_val)
            if weight > 0:
                matched_positive.append((weight, f_name, f_val, label))
            elif weight < 0:
                matched_negative.append((weight, f_name, f_val, label))

        matched_positive.sort(key=lambda item: (-item[0], item[1], item[2]))
        matched_negative.sort(key=lambda item: (item[0], item[1], item[2]))

        top_3 = matched_positive[:3]
        top_labels = tuple(item[3] for item in top_3)
        scaled = _scale_score(raw_score, min_bound, max_bound)

        denom = max(positive_feature_count, len(matched_positive), 1)
        if top_labels:
            summary = (
                f"Matches {len(matched_positive)} of {denom} traits of top customers: "
                f"{', '.join(top_labels)}."
            )
        elif matched_negative:
            summary = (
                f"Low fit — matches negative signals: "
                f"{', '.join(item[3] for item in matched_negative[:2])}."
            )
        else:
            summary = "Insufficient matching traits against top customer profile."

        reasons_payload: dict[str, Any] = {
            "summary": summary,
            "top_traits": [
                {
                    "feature": item[1],
                    "value": item[2],
                    "weight": round(item[0], 4),
                    "label": item[3],
                }
                for item in top_3
            ],
            "matched_positive_count": len(matched_positive),
            "total_positive_traits": denom,
            "raw_score": round(raw_score, 4),
        }

        pending_records.append(
            LeadScoreRecord(
                id=None,
                profile_id=profile.id,
                business_id=biz.id,
                score=scaled,
                reasons=reasons_payload,
            )
        )
        intermediate.append((biz, feat, scaled, reasons_payload, raw_score, top_labels))

    persisted_by_biz_id: dict[int, LeadScoreRecord] = {}
    if score_store is not None and pending_records:
        for saved in score_store.save_scores(pending_records):
            persisted_by_biz_id[saved.business_id] = saved

    scored_leads: list[ScoredLead] = []
    for biz, feat, scaled, reasons_payload, raw_score, top_labels in intermediate:
        assert biz.id is not None
        persisted = persisted_by_biz_id.get(biz.id)
        scored_leads.append(
            ScoredLead(
                lead_score_id=persisted.id if persisted else None,
                profile_id=profile.id,
                business_id=biz.id,
                score=scaled,
                business_name=biz.trade_name or biz.legal_name,
                gstin=biz.gstin,
                city=biz.city,
                industry=feat.industry if feat else None,
                gst_status=feat.gst_status if feat else None,
                website=feat.website if feat else None,
                business_contact=feat.business_contact if feat else None,
                reasons_summary=reasons_payload["summary"],
                top_reasons=top_labels,
                raw_score=round(raw_score, 4),
            )
        )

    scored_leads.sort(key=lambda lead: (-lead.score, -lead.raw_score, lead.business_name.lower()))
    return scored_leads
