"""Stage 2 enrichment orchestrator and `business_features` persistence."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Protocol

import psycopg

from enrich.cache import CachedHttpClient
from enrich.gst import (
    GstExtractedFeatures,
    lookup_gst_taxpayer,
    reextract_gst_features,
)
from enrich.normalize import normalize_industry
from enrich.places import (
    BudgetExceededError,
    PlacesEnricher,
    PlacesExtractedFeatures,
    reextract_places_features,
)
from ingest.dedupe import BusinessRecord, BusinessStore


@dataclass(frozen=True)
class BusinessFeaturesRecord:
    """Enriched feature snapshot matching the `business_features` table."""

    id: int | None
    business_id: int
    industry: str | None = None
    business_type: str | None = None
    registration_date: date | None = None
    gst_status: str | None = None
    filing_regular: bool | None = None
    turnover_band: str | None = None
    employee_band: str | None = None
    google_rating: float | None = None
    review_count: int | None = None
    has_website: bool | None = None
    website: str | None = None
    business_contact: str | None = None
    enriched_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class BusinessFeaturesStore(Protocol):
    """Storage interface for `business_features` snapshots."""

    def save(self, record: BusinessFeaturesRecord) -> BusinessFeaturesRecord: ...

    def get_latest(self, business_id: int) -> BusinessFeaturesRecord | None: ...


class InMemoryBusinessFeaturesStore:
    """In-memory BusinessFeaturesStore for offline testing."""

    def __init__(self) -> None:
        self._rows: list[BusinessFeaturesRecord] = []
        self._next_id = 1

    @property
    def records(self) -> list[BusinessFeaturesRecord]:
        return list(self._rows)

    def save(self, record: BusinessFeaturesRecord) -> BusinessFeaturesRecord:
        stored = BusinessFeaturesRecord(
            id=self._next_id,
            business_id=record.business_id,
            industry=record.industry,
            business_type=record.business_type,
            registration_date=record.registration_date,
            gst_status=record.gst_status,
            filing_regular=record.filing_regular,
            turnover_band=record.turnover_band,
            employee_band=record.employee_band,
            google_rating=record.google_rating,
            review_count=record.review_count,
            has_website=record.has_website,
            website=record.website,
            business_contact=record.business_contact,
            enriched_at=record.enriched_at,
        )
        self._next_id += 1
        self._rows.append(stored)
        return stored

    def get_latest(self, business_id: int) -> BusinessFeaturesRecord | None:
        for row in reversed(self._rows):
            if row.business_id == business_id:
                return row
        return None


class PostgresBusinessFeaturesStore:
    """PostgreSQL-backed BusinessFeaturesStore operating on `business_features`."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def save(self, record: BusinessFeaturesRecord) -> BusinessFeaturesRecord:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO business_features (
                    business_id, industry, business_type, registration_date,
                    gst_status, filing_regular, turnover_band, employee_band,
                    google_rating, review_count, has_website, website,
                    business_contact, enriched_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, enriched_at
                """,
                (
                    record.business_id,
                    record.industry,
                    record.business_type,
                    record.registration_date,
                    record.gst_status,
                    record.filing_regular,
                    record.turnover_band,
                    record.employee_band,
                    record.google_rating,
                    record.review_count,
                    record.has_website,
                    record.website,
                    record.business_contact,
                    record.enriched_at,
                ),
            )
            inserted = cur.fetchone()
        assert inserted is not None
        return BusinessFeaturesRecord(
            id=inserted[0],
            business_id=record.business_id,
            industry=record.industry,
            business_type=record.business_type,
            registration_date=record.registration_date,
            gst_status=record.gst_status,
            filing_regular=record.filing_regular,
            turnover_band=record.turnover_band,
            employee_band=record.employee_band,
            google_rating=record.google_rating,
            review_count=record.review_count,
            has_website=record.has_website,
            website=record.website,
            business_contact=record.business_contact,
            enriched_at=inserted[1],
        )

    def get_latest(self, business_id: int) -> BusinessFeaturesRecord | None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, business_id, industry, business_type, registration_date,
                       gst_status, filing_regular, turnover_band, employee_band,
                       google_rating, review_count, has_website, website,
                       business_contact, enriched_at
                FROM business_features
                WHERE business_id = %s
                ORDER BY enriched_at DESC, id DESC
                LIMIT 1
                """,
                (business_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return BusinessFeaturesRecord(
            id=row[0],
            business_id=row[1],
            industry=row[2],
            business_type=row[3],
            registration_date=row[4],
            gst_status=row[5],
            filing_regular=row[6],
            turnover_band=row[7],
            employee_band=row[8],
            google_rating=float(row[9]) if row[9] is not None else None,
            review_count=row[10],
            has_website=row[11],
            website=row[12],
            business_contact=row[13],
            enriched_at=row[14],
        )


@dataclass
class EnrichmentBatchReport:
    """Summary diagnostics for a Stage 2 enrichment batch."""

    total_businesses: int = 0
    enriched_count: int = 0
    gst_enriched_count: int = 0
    places_enriched_count: int = 0
    total_cost_inr: Decimal = Decimal(0)
    network_calls: int = 0
    cache_hits: int = 0
    aborted_due_to_budget: bool = False
    other_industry_count: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    features: list[BusinessFeaturesRecord] = field(default_factory=list)


def enrich_business(
    business: BusinessRecord,
    *,
    http_client: CachedHttpClient,
    features_store: BusinessFeaturesStore,
    business_store: BusinessStore | None = None,
    places_enricher: PlacesEnricher | None = None,
    gst_endpoint_url: str | None = None,
    enable_gst: bool = True,
    enable_places: bool = True,
    reextract_only: bool = False,
) -> tuple[BusinessFeaturesRecord, bool, bool, Decimal]:
    """Enrich a single business using GST + Google Places and normalize its features.

    Returns (saved_features, has_gst, has_places, cost_inr_spent).
    """
    if business.id is None:
        raise ValueError("BusinessRecord must have a persisted `id` before enrichment.")

    gst_features: GstExtractedFeatures | None = None
    places_features: PlacesExtractedFeatures | None = None
    cost_spent = Decimal(0)

    if enable_gst and business.gstin:
        if reextract_only:
            gst_features = reextract_gst_features(http_client.payload_store, business.gstin)
        else:
            gst_features = lookup_gst_taxpayer(
                business.gstin,
                http_client=http_client,
                business_id=business.id,
                endpoint_url=gst_endpoint_url,
                business_store=business_store,
            )

    if enable_places:
        if reextract_only:
            places_features = reextract_places_features(http_client.payload_store, business)
        elif places_enricher is not None:
            places_features, places_cost = places_enricher.enrich_business(business)
            cost_spent += places_cost

    industry = normalize_industry(
        places_primary_type=places_features.primary_type if places_features else None,
        places_types=places_features.types if places_features else None,
        gst_nature_of_business=gst_features.nature_of_business if gst_features else None,
        business_name=(
            (places_features.display_name if places_features else None)
            or (gst_features.trade_name if gst_features else None)
            or business.trade_name
            or business.legal_name
        ),
    )

    record = BusinessFeaturesRecord(
        id=None,
        business_id=business.id,
        industry=industry,
        business_type=gst_features.business_type if gst_features else None,
        registration_date=gst_features.registration_date if gst_features else None,
        gst_status=gst_features.gst_status if gst_features else None,
        filing_regular=gst_features.filing_regular if gst_features else None,
        turnover_band=gst_features.turnover_band if gst_features else None,
        employee_band=None,
        google_rating=places_features.google_rating if places_features else None,
        review_count=places_features.review_count if places_features else None,
        has_website=places_features.has_website if places_features else None,
        website=places_features.website if places_features else None,
        business_contact=places_features.business_contact if places_features else None,
    )
    saved = features_store.save(record)
    return saved, gst_features is not None, places_features is not None, cost_spent


def enrich_batch(
    businesses: list[BusinessRecord],
    *,
    http_client: CachedHttpClient,
    features_store: BusinessFeaturesStore,
    business_store: BusinessStore | None = None,
    places_enricher: PlacesEnricher | None = None,
    gst_endpoint_url: str | None = None,
    enable_gst: bool = True,
    enable_places: bool = True,
    reextract_only: bool = False,
) -> EnrichmentBatchReport:
    """Enrich a batch of businesses, aborting cleanly if Google Places budget is exceeded."""
    start_network_calls = http_client.network_calls
    start_cache_hits = http_client.cache_hits
    report = EnrichmentBatchReport(total_businesses=len(businesses))

    for biz in businesses:
        try:
            saved, has_gst, has_places, spent = enrich_business(
                biz,
                http_client=http_client,
                features_store=features_store,
                business_store=business_store,
                places_enricher=places_enricher,
                gst_endpoint_url=gst_endpoint_url,
                enable_gst=enable_gst,
                enable_places=enable_places,
                reextract_only=reextract_only,
            )
            report.enriched_count += 1
            if has_gst:
                report.gst_enriched_count += 1
            if has_places:
                report.places_enriched_count += 1
            if saved.industry == "other":
                report.other_industry_count += 1
            report.total_cost_inr += spent
            report.features.append(saved)
        except BudgetExceededError as exc:
            report.aborted_due_to_budget = True
            report.errors.append(
                {
                    "business_id": biz.id,
                    "legal_name": biz.legal_name,
                    "error": str(exc),
                }
            )
            break
        except (RuntimeError, ValueError, TypeError, KeyError, psycopg.Error) as exc:
            report.errors.append(
                {
                    "business_id": biz.id,
                    "legal_name": biz.legal_name,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    report.network_calls = http_client.network_calls - start_network_calls
    report.cache_hits = http_client.cache_hits - start_cache_hits
    return report
