"""Stage 5 (Steps 4.1 & 4.2): Append-only lead outcome capture and re-profile job."""

from __future__ import annotations

import csv
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from profile.icp_text import generate_icp_text
from profile.lift import LiftResult, build_seed_feature_dataframe, calculate_lift
from profile.weights import (
    DEFAULT_W_MAX,
    ProfileRecord,
    ProfileStore,
    create_profile_version,
)
from typing import Protocol

import psycopg

from enrich.pipeline import BusinessFeaturesRecord
from ingest.dedupe import BusinessRecord
from ingest.importer import SeedCustomerRecord
from score.exclude import filter_candidates
from score.scorer import LeadScoreStore, ScoredLead, score_candidates

VALID_OUTCOME_STATUSES: tuple[str, ...] = (
    "contacted",
    "meeting",
    "won",
    "lost",
    "no response",
)
RESOLVED_OUTCOME_STATUSES: frozenset[str] = frozenset({"won", "lost"})


def validate_outcome_status(status: str) -> str:
    """Normalize and validate status against the append-only `lead_outcomes` enum."""
    cleaned = (status or "").strip().lower()
    if cleaned not in VALID_OUTCOME_STATUSES:
        raise ValueError(
            f"Invalid outcome status {status!r}; expected one of: {VALID_OUTCOME_STATUSES}"
        )
    return cleaned


@dataclass(frozen=True)
class LeadOutcomeRecord:
    """Append-only sales outcome event matching `lead_outcomes`."""

    id: int | None
    lead_score_id: int
    status: str
    notes: str | None = None
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class ClientResolvedOutcome:
    """Latest resolved outcome joined with client_id and business_id for re-profiling."""

    client_id: int
    business_id: int
    lead_score_id: int
    status: str
    updated_at: datetime


class OutcomeStore(Protocol):
    """Append-only storage interface for `lead_outcomes`."""

    def record_outcome(
        self,
        lead_score_id: int,
        status: str,
        notes: str | None = None,
        *,
        updated_at: datetime | None = None,
    ) -> LeadOutcomeRecord: ...

    def list_for_lead_score(self, lead_score_id: int) -> list[LeadOutcomeRecord]: ...

    def list_latest_resolved_for_client(
        self, client_id: int
    ) -> list[ClientResolvedOutcome]: ...


class InMemoryOutcomeStore:
    """In-memory append-only OutcomeStore (never updates or deletes existing rows)."""

    def __init__(self) -> None:
        self._rows: list[LeadOutcomeRecord] = []
        self._next_id = 1
        # Maps lead_score_id -> (client_id, business_id) for client-scoped re-profiling
        self._score_links: dict[int, tuple[int, int]] = {}

    def register_lead_score(self, lead_score_id: int, client_id: int, business_id: int) -> None:
        """Register ownership of a lead_score_id so re-profiling stays strictly scoped by client_id."""
        self._score_links[lead_score_id] = (client_id, business_id)

    @property
    def records(self) -> list[LeadOutcomeRecord]:
        return list(self._rows)

    def record_outcome(
        self,
        lead_score_id: int,
        status: str,
        notes: str | None = None,
        *,
        updated_at: datetime | None = None,
    ) -> LeadOutcomeRecord:
        norm_status = validate_outcome_status(status)
        stored = LeadOutcomeRecord(
            id=self._next_id,
            lead_score_id=lead_score_id,
            status=norm_status,
            notes=notes.strip() if notes and notes.strip() else None,
            updated_at=updated_at or datetime.now(UTC),
        )
        self._next_id += 1
        self._rows.append(stored)
        return stored

    def list_for_lead_score(self, lead_score_id: int) -> list[LeadOutcomeRecord]:
        return [r for r in self._rows if r.lead_score_id == lead_score_id]

    def list_latest_resolved_for_client(self, client_id: int) -> list[ClientResolvedOutcome]:
        latest_by_biz: dict[int, ClientResolvedOutcome] = {}
        for row in self._rows:
            link = self._score_links.get(row.lead_score_id)
            if link is None:
                continue
            row_client_id, business_id = link
            if row_client_id != client_id:
                continue
            prev = latest_by_biz.get(business_id)
            if prev is None or (row.updated_at, row.id or 0) >= (prev.updated_at, 0):
                latest_by_biz[business_id] = ClientResolvedOutcome(
                    client_id=client_id,
                    business_id=business_id,
                    lead_score_id=row.lead_score_id,
                    status=row.status,
                    updated_at=row.updated_at,
                )
        return [
            item
            for item in latest_by_biz.values()
            if item.status in RESOLVED_OUTCOME_STATUSES
        ]


class PostgresOutcomeStore:
    """PostgreSQL-backed append-only OutcomeStore operating on `lead_outcomes`."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def record_outcome(
        self,
        lead_score_id: int,
        status: str,
        notes: str | None = None,
        *,
        updated_at: datetime | None = None,
    ) -> LeadOutcomeRecord:
        norm_status = validate_outcome_status(status)
        clean_notes = notes.strip() if notes and notes.strip() else None
        ts = updated_at or datetime.now(UTC)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO lead_outcomes (lead_score_id, status, notes, updated_at)
                VALUES (%s, %s, %s, %s)
                RETURNING id, updated_at
                """,
                (lead_score_id, norm_status, clean_notes, ts),
            )
            row = cur.fetchone()
        assert row is not None
        return LeadOutcomeRecord(
            id=int(row[0]),
            lead_score_id=lead_score_id,
            status=norm_status,
            notes=clean_notes,
            updated_at=row[1],
        )

    def list_for_lead_score(self, lead_score_id: int) -> list[LeadOutcomeRecord]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, lead_score_id, status, notes, updated_at
                FROM lead_outcomes
                WHERE lead_score_id = %s
                ORDER BY updated_at ASC, id ASC
                """,
                (lead_score_id,),
            )
            rows = cur.fetchall()
        return [
            LeadOutcomeRecord(
                id=int(r[0]),
                lead_score_id=int(r[1]),
                status=str(r[2]),
                notes=r[3],
                updated_at=r[4],
            )
            for r in rows
        ]

    def list_latest_resolved_for_client(self, client_id: int) -> list[ClientResolvedOutcome]:
        """Return latest outcome per business for client_id where latest status is won/lost."""
        with self._conn.cursor() as cur:
            cur.execute(
                """
                WITH ranked AS (
                    SELECT
                        p.client_id,
                        ls.business_id,
                        lo.lead_score_id,
                        lo.status,
                        lo.updated_at,
                        ROW_NUMBER() OVER (
                            PARTITION BY ls.business_id
                            ORDER BY lo.updated_at DESC, lo.id DESC
                        ) AS rn
                    FROM lead_outcomes lo
                    JOIN lead_scores ls ON ls.id = lo.lead_score_id
                    JOIN profiles p ON p.id = ls.profile_id
                    WHERE p.client_id = %s
                )
                SELECT client_id, business_id, lead_score_id, status, updated_at
                FROM ranked
                WHERE rn = 1 AND status IN ('won', 'lost')
                ORDER BY updated_at ASC
                """,
                (client_id,),
            )
            rows = cur.fetchall()
        return [
            ClientResolvedOutcome(
                client_id=int(r[0]),
                business_id=int(r[1]),
                lead_score_id=int(r[2]),
                status=str(r[3]),
                updated_at=r[4],
            )
            for r in rows
        ]


def import_outcomes_csv(
    csv_path: str | Path,
    outcome_store: OutcomeStore,
) -> list[LeadOutcomeRecord]:
    """Append outcome rows from a sales sync CSV (`lead_score_id,status,notes`)."""
    inserted: list[LeadOutcomeRecord] = []
    with Path(csv_path).open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            raw_id = (row.get("lead_score_id") or "").strip()
            raw_status = (row.get("status") or "").strip()
            if not raw_id or not raw_status:
                continue
            inserted.append(
                outcome_store.record_outcome(
                    lead_score_id=int(raw_id),
                    status=raw_status,
                    notes=row.get("notes"),
                )
            )
    return inserted


@dataclass
class ReprofileResult:
    """Outputs of running Stage 5 Step 4.2 re-profiling + open-candidate re-scoring."""

    client_id: int
    new_profile: ProfileRecord
    lift_result: LiftResult
    seed_count: int
    resolved_outcomes_added: int
    rescored_leads: list[ScoredLead]


def combine_seeds_and_resolved_outcomes(
    seed_customers: list[SeedCustomerRecord],
    resolved_outcomes: list[ClientResolvedOutcome],
    client_id: int,
) -> tuple[list[SeedCustomerRecord], int]:
    """Combine original seeds with resolved ('won'/'lost') lead_outcomes for one client."""
    scoped_seeds = [s for s in seed_customers if s.client_id == client_id]
    existing_seed_biz_ids = {s.business_id for s in scoped_seeds}

    added_count = 0
    combined = list(scoped_seeds)
    for outcome in resolved_outcomes:
        if outcome.client_id != client_id:
            continue
        if outcome.status not in RESOLVED_OUTCOME_STATUSES:
            continue
        if outcome.business_id in existing_seed_biz_ids:
            continue
        combined.append(
            SeedCustomerRecord(
                id=None,
                client_id=client_id,
                business_id=outcome.business_id,
                outcome=outcome.status,
                deal_value_inr=None,
                closed_at=outcome.updated_at.date(),
            )
        )
        existing_seed_biz_ids.add(outcome.business_id)
        added_count += 1

    return combined, added_count


def reprofile_client(
    *,
    client_id: int,
    product_sold: str,
    seed_customers: list[SeedCustomerRecord],
    businesses: dict[int, BusinessRecord],
    features_by_business: dict[int, BusinessFeaturesRecord],
    candidate_pool: list[BusinessRecord],
    outcome_store: OutcomeStore,
    profile_store: ProfileStore,
    score_store: LeadScoreStore | None = None,
    alpha: float = 1.0,
    min_support: int = 3,
    min_negatives: int = 5,
    w_max: float = DEFAULT_W_MAX,
    llm_callable: Callable[[str], str] | None = None,
    use_fallback_if_no_key: bool = True,
    as_of_date: date | None = None,
) -> ReprofileResult:
    """Re-run lift, weights, and ICP text with resolved outcomes, publish v+1, and re-score open leads."""
    resolved = outcome_store.list_latest_resolved_for_client(client_id)
    combined_training, added_count = combine_seeds_and_resolved_outcomes(
        seed_customers, resolved, client_id
    )

    training_df = build_seed_feature_dataframe(
        combined_training,
        businesses,
        features_by_business,
        as_of_date=as_of_date,
    )
    lift_res = calculate_lift(
        training_df,
        client_id=client_id,
        alpha=alpha,
        min_support=min_support,
        min_negatives=min_negatives,
    )
    icp_summary = generate_icp_text(
        product_sold,
        lift_res.traits,
        llm_callable=llm_callable,
        use_fallback_if_no_key=use_fallback_if_no_key,
    )
    new_profile = create_profile_version(
        profile_store,
        client_id,
        lift_res.traits,
        icp_summary,
        w_max=w_max,
    )

    # Exclude both historical seed_customers and newly resolved won/lost accounts;
    # open leads ('contacted', 'meeting', 'no response', or untouched) remain eligible for re-scoring.
    excluded_ids = {s.business_id for s in combined_training}
    filtered = filter_candidates(
        candidate_pool,
        seed_business_ids=excluded_ids,
        features_by_business=features_by_business,
    )
    rescored = score_candidates(
        filtered.eligible,
        features_by_business,
        new_profile,
        score_store=score_store,
        as_of_date=as_of_date,
    )

    return ReprofileResult(
        client_id=client_id,
        new_profile=new_profile,
        lift_result=lift_res,
        seed_count=len([s for s in seed_customers if s.client_id == client_id]),
        resolved_outcomes_added=added_count,
        rescored_leads=rescored,
    )
