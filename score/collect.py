"""Stage 3.4: Scoped candidate collector (1 city, <= 3 industries) with compliant sources."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.robotparser import RobotFileParser

import pandas as pd

from enrich.cache import CachedHttpClient
from enrich.normalize import INDUSTRY_TAXONOMY
from enrich.pipeline import BusinessFeaturesStore, enrich_batch
from enrich.places import (
    DEFAULT_SEARCH_FIELD_MASK,
    PLACES_TEXT_SEARCH_URL,
    BudgetExceededError,
    PlacesBudgetConfig,
    PlacesEnricher,
    extract_places_features_from_payload,
)
from ingest.dedupe import BusinessRecord, BusinessStore, Deduplicator, normalize_pincode
from ingest.gstin import validate_gstin

MAX_INDUSTRIES_PER_COLLECTION = 3
PLACES_CANDIDATE_SEARCH_SOURCE = "google_places_candidate_search"
_PINCODE_IN_ADDR_RE = re.compile(r"\b(\d{6})\b")


def validate_collection_scope(
    city: str,
    industries: list[str] | tuple[str, ...],
) -> tuple[str, tuple[str, ...]]:
    """Enforce v1 scope guardrail: 1 city and 1 to 3 industries."""
    clean_city = (city or "").strip()
    if not clean_city:
        raise ValueError("Collection scope requires exactly 1 non-empty target city.")
    if "," in clean_city or ";" in clean_city:
        raise ValueError(f"v1 allows 1 target city only, got {clean_city!r}.")

    clean_industries = tuple(ind.strip() for ind in industries if ind and ind.strip())
    if not clean_industries:
        raise ValueError("Collection scope requires at least 1 target industry.")
    if len(clean_industries) > MAX_INDUSTRIES_PER_COLLECTION:
        raise ValueError(
            f"v1 restricts expansion to at most {MAX_INDUSTRIES_PER_COLLECTION} industries, "
            f"got {len(clean_industries)}: {clean_industries}"
        )
    return clean_city, clean_industries


def check_robots_txt_allowed(
    robots_url: str,
    target_url: str,
    *,
    http_client: CachedHttpClient,
    user_agent: str = "*",
) -> bool:
    """Fetch and parse robots.txt via CachedHttpClient to verify path is permitted."""
    resp = http_client.get_or_fetch(
        "robots_txt",
        robots_url.lower(),
        "GET",
        robots_url,
    )
    if resp.http_status >= 400:
        return False
    raw_text = ""
    if isinstance(resp.payload, dict):
        raw_text = str(resp.payload.get("raw_text") or "")
    if not raw_text.strip():
        return False

    parser = RobotFileParser()
    parser.parse(raw_text.splitlines())
    return parser.can_fetch(user_agent, target_url)


@dataclass
class CollectionReport:
    """Summary of candidate collection, deduplication, and Stage 2 enrichment."""

    city: str
    industries: tuple[str, ...]
    candidates_seen: int = 0
    new_businesses: int = 0
    merged_duplicates: int = 0
    already_enriched_skipped: int = 0
    newly_enriched: int = 0
    cost_inr: Decimal = Decimal(0)
    aborted_due_to_budget: bool = False
    candidate_business_ids: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _extract_pincode_from_address(address: str | None) -> str | None:
    if not address:
        return None
    match = _PINCODE_IN_ADDR_RE.search(address)
    return match.group(1) if match else None


def _industry_to_query_term(industry: str) -> str:
    if industry in INDUSTRY_TAXONOMY:
        return industry.replace("_", " ")
    return industry


def _upsert_and_enrich_candidates(
    candidates: list[BusinessRecord],
    *,
    business_store: BusinessStore,
    features_store: BusinessFeaturesStore,
    http_client: CachedHttpClient,
    places_enricher: PlacesEnricher | None,
    gst_endpoint_url: str | None,
    enable_gst: bool,
    enable_places: bool,
    report: CollectionReport,
) -> CollectionReport:
    deduper = Deduplicator(business_store)
    to_enrich: list[BusinessRecord] = []
    seen_ids: set[int] = set(report.candidate_business_ids)

    for candidate in candidates:
        report.candidates_seen += 1
        outcome = deduper.upsert(candidate)
        if outcome.created:
            report.new_businesses += 1
        else:
            report.merged_duplicates += 1

        if outcome.business_id in seen_ids:
            continue
        seen_ids.add(outcome.business_id)
        report.candidate_business_ids.append(outcome.business_id)

        if features_store.get_latest(outcome.business_id) is not None:
            report.already_enriched_skipped += 1
            continue

        stored = (
            business_store.find_by_gstin(candidate.gstin)
            if candidate.gstin
            else None
        )
        if stored is None:
            stored = BusinessRecord(
                id=outcome.business_id,
                gstin=candidate.gstin,
                legal_name=candidate.legal_name,
                trade_name=candidate.trade_name,
                city=candidate.city,
                state=candidate.state,
                pincode=candidate.pincode,
                source=candidate.source,
                first_seen_at=candidate.first_seen_at,
            )
        to_enrich.append(stored)

    if to_enrich:
        enrich_res = enrich_batch(
            to_enrich,
            http_client=http_client,
            features_store=features_store,
            business_store=business_store,
            places_enricher=places_enricher,
            gst_endpoint_url=gst_endpoint_url,
            enable_gst=enable_gst,
            enable_places=enable_places,
        )
        report.newly_enriched += enrich_res.enriched_count
        report.cost_inr += enrich_res.total_cost_inr
        if enrich_res.aborted_due_to_budget:
            report.aborted_due_to_budget = True
        for err in enrich_res.errors:
            report.errors.append(str(err.get("error", err)))

    return report


def collect_from_places(
    city: str,
    industries: list[str] | tuple[str, ...],
    *,
    http_client: CachedHttpClient,
    business_store: BusinessStore,
    features_store: BusinessFeaturesStore,
    api_key: str | None = None,
    budget: PlacesBudgetConfig | None = None,
    max_results_per_industry: int = 20,
    enable_gst: bool = False,
    gst_endpoint_url: str | None = None,
) -> CollectionReport:
    """Collect candidates via official Google Places Text Search (1 city, <= 3 industries)."""
    clean_city, clean_industries = validate_collection_scope(city, industries)
    resolved_key = api_key if api_key is not None else os.getenv("GOOGLE_PLACES_API_KEY", "")
    resolved_budget = budget or PlacesBudgetConfig.from_env()
    report = CollectionReport(city=clean_city, industries=clean_industries)

    collected: list[BusinessRecord] = []
    for industry in clean_industries:
        query_term = _industry_to_query_term(industry)
        text_query = f"{query_term} in {clean_city}"
        lookup_key = text_query.lower()

        cached = http_client.payload_store.get_latest(PLACES_CANDIDATE_SEARCH_SOURCE, lookup_key)
        if cached is None:
            if report.cost_inr + resolved_budget.text_search_cost_inr > resolved_budget.max_run_budget_inr:
                report.aborted_due_to_budget = True
                report.errors.append(
                    f"Places collection budget exceeded before querying {text_query!r}"
                )
                break
            if not resolved_key:
                raise RuntimeError(
                    "GOOGLE_PLACES_API_KEY is not configured and no cached search exists."
                )

            resp = http_client.get_or_fetch(
                PLACES_CANDIDATE_SEARCH_SOURCE,
                lookup_key,
                "POST",
                PLACES_TEXT_SEARCH_URL,
                cost_inr=resolved_budget.text_search_cost_inr,
                json_body={
                    "textQuery": text_query,
                    "maxResultCount": max(1, min(20, max_results_per_industry)),
                    "regionCode": "IN",
                },
                headers={
                    "X-Goog-Api-Key": resolved_key,
                    "X-Goog-FieldMask": DEFAULT_SEARCH_FIELD_MASK,
                    "Content-Type": "application/json",
                },
            )
            report.cost_inr += resp.cost_inr
        else:
            resp = http_client.get_or_fetch(
                PLACES_CANDIDATE_SEARCH_SOURCE,
                lookup_key,
                "POST",
                PLACES_TEXT_SEARCH_URL,
            )

        if resp.http_status >= 400 or not isinstance(resp.payload, dict):
            continue

        places = resp.payload.get("places") or []
        for place in places:
            if not isinstance(place, dict):
                continue
            extracted = extract_places_features_from_payload(place)
            if extracted is None or not extracted.display_name:
                continue
            collected.append(
                BusinessRecord(
                    gstin=None,
                    legal_name=extracted.display_name,
                    trade_name=extracted.display_name,
                    city=clean_city,
                    pincode=_extract_pincode_from_address(extracted.formatted_address),
                    source="google_places_expand",
                )
            )

    if collected and not report.aborted_due_to_budget:
        # Enrich only newly discovered / un-enriched candidates; skip already-enriched ones
        remaining_budget = PlacesBudgetConfig(
            max_run_budget_inr=max(Decimal(0), resolved_budget.max_run_budget_inr - report.cost_inr),
            max_per_business_inr=resolved_budget.max_per_business_inr,
            text_search_cost_inr=resolved_budget.text_search_cost_inr,
            details_cost_inr=resolved_budget.details_cost_inr,
        )
        places_enricher = PlacesEnricher(
            http_client,
            api_key=resolved_key,
            budget=remaining_budget,
        )
        _upsert_and_enrich_candidates(
            collected,
            business_store=business_store,
            features_store=features_store,
            http_client=http_client,
            places_enricher=places_enricher,
            gst_endpoint_url=gst_endpoint_url,
            enable_gst=enable_gst,
            enable_places=True,
            report=report,
        )

    return report


def collect_from_csv(
    file_path: str | Path,
    city: str,
    industries: list[str] | tuple[str, ...],
    *,
    business_store: BusinessStore,
    features_store: BusinessFeaturesStore,
    http_client: CachedHttpClient,
    places_enricher: PlacesEnricher | None = None,
    gst_endpoint_url: str | None = None,
    enable_gst: bool = True,
    enable_places: bool = True,
    source: str = "manual_directory_csv",
) -> CollectionReport:
    """Import candidates from a manual industrial-estate/association CSV and enrich un-enriched ones."""
    clean_city, clean_industries = validate_collection_scope(city, industries)
    df = pd.read_csv(file_path, dtype=str, keep_default_na=False)
    report = CollectionReport(city=clean_city, industries=clean_industries)

    records: list[BusinessRecord] = []
    for _, row in df.iterrows():
        raw_name = str(row.get("name") or row.get("legal_name") or "").strip()
        if not raw_name:
            continue
        raw_gstin = str(row.get("gstin") or "").strip().upper()
        valid_gstin: str | None = None
        if raw_gstin:
            res = validate_gstin(raw_gstin)
            if res.is_valid:
                valid_gstin = res.normalized
            else:
                report.errors.append(f"Skipped invalid GSTIN {raw_gstin!r} for {raw_name!r}")

        row_city = str(row.get("city") or clean_city).strip() or clean_city
        row_state = str(row.get("state") or "").strip() or None
        row_pin = normalize_pincode(row.get("pincode"))
        records.append(
            BusinessRecord(
                gstin=valid_gstin,
                legal_name=raw_name,
                trade_name=str(row.get("trade_name") or "").strip() or None,
                city=row_city,
                state=row_state,
                pincode=row_pin,
                source=source,
            )
        )

    return _upsert_and_enrich_candidates(
        records,
        business_store=business_store,
        features_store=features_store,
        http_client=http_client,
        places_enricher=places_enricher,
        gst_endpoint_url=gst_endpoint_url,
        enable_gst=enable_gst,
        enable_places=enable_places,
        report=report,
    )


def collect_from_directory_if_permitted(
    directory_records: list[dict[str, Any]],
    city: str,
    industries: list[str] | tuple[str, ...],
    *,
    tos_permitted: bool,
    robots_url: str,
    target_url: str,
    http_client: CachedHttpClient,
    business_store: BusinessStore,
    features_store: BusinessFeaturesStore,
    places_enricher: PlacesEnricher | None = None,
    source: str = "directory_export",
) -> CollectionReport:
    """Collect from a B2B directory ONLY when both ToS permission and robots.txt allow it."""
    clean_city, clean_industries = validate_collection_scope(city, industries)
    if not tos_permitted:
        raise PermissionError(
            "Directory collection blocked: source ToS permission is not confirmed. "
            "Use manual collection instead."
        )
    if not check_robots_txt_allowed(robots_url, target_url, http_client=http_client):
        raise PermissionError(
            f"Directory collection blocked by robots.txt for {target_url!r}."
        )

    candidates = [
        BusinessRecord(
            gstin=(
                validate_gstin(item["gstin"]).normalized
                if item.get("gstin") and validate_gstin(item["gstin"]).is_valid
                else None
            ),
            legal_name=str(item["name"]).strip(),
            trade_name=str(item.get("trade_name") or "").strip() or None,
            city=str(item.get("city") or clean_city).strip(),
            state=str(item.get("state") or "").strip() or None,
            pincode=normalize_pincode(item.get("pincode")),
            source=source,
        )
        for item in directory_records
        if item.get("name")
    ]
    report = CollectionReport(city=clean_city, industries=clean_industries)
    try:
        return _upsert_and_enrich_candidates(
            candidates,
            business_store=business_store,
            features_store=features_store,
            http_client=http_client,
            places_enricher=places_enricher,
            gst_endpoint_url=None,
            enable_gst=False,
            enable_places=places_enricher is not None,
            report=report,
        )
    except BudgetExceededError as exc:
        report.aborted_due_to_budget = True
        report.errors.append(str(exc))
        return report
