"""Official Google Places API (v1) enrichment with FieldMask, caching, and INR budget caps."""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from enrich.cache import CachedHttpClient, RawPayloadStore
from ingest.dedupe import BusinessRecord

PLACES_SEARCH_SOURCE = "google_places_search"
PLACES_DETAILS_SOURCE = "google_places_details"

PLACES_TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
PLACES_DETAILS_URL_TEMPLATE = "https://places.googleapis.com/v1/places/{place_id}"

# Minimal FieldMask to avoid unnecessary billing SKUs
DEFAULT_SEARCH_FIELD_MASK = (
    "places.id,places.displayName,places.primaryType,places.types,"
    "places.formattedAddress,places.rating,places.userRatingCount,"
    "places.websiteUri,places.nationalPhoneNumber"
)

DEFAULT_DETAILS_FIELD_MASK = (
    "id,displayName,primaryType,types,formattedAddress,"
    "rating,userRatingCount,websiteUri,nationalPhoneNumber"
)

# Conservative default INR cost estimates per Places API (New) call
DEFAULT_TEXT_SEARCH_COST_INR = Decimal("3.00")
DEFAULT_DETAILS_COST_INR = Decimal("1.50")


class BudgetExceededError(RuntimeError):
    """Raised when a Google Places enrichment call would exceed the configured INR budget."""


@dataclass(frozen=True)
class PlacesExtractedFeatures:
    """Structured Google Places attributes extracted from raw API JSON."""

    place_id: str | None
    display_name: str | None
    primary_type: str | None
    types: tuple[str, ...]
    formatted_address: str | None
    google_rating: float | None
    review_count: int | None
    has_website: bool
    website: str | None
    business_contact: str | None


@dataclass(frozen=True)
class PlacesBudgetConfig:
    """Budget guardrails for Google Places API calls."""

    max_run_budget_inr: Decimal = Decimal(2000)
    max_per_business_inr: Decimal = Decimal(5)
    text_search_cost_inr: Decimal = DEFAULT_TEXT_SEARCH_COST_INR
    details_cost_inr: Decimal = DEFAULT_DETAILS_COST_INR

    @classmethod
    def from_env(cls) -> PlacesBudgetConfig:
        run_cap = Decimal(os.getenv("PLACES_MAX_BUDGET_INR", "2000"))
        biz_cap = Decimal(os.getenv("PLACES_MAX_PER_BUSINESS_INR", "5"))
        return cls(
            max_run_budget_inr=run_cap,
            max_per_business_inr=biz_cap,
        )


def build_places_query(business: BusinessRecord) -> str:
    """Build deterministic Text Search query using trade_name/legal_name + city + pincode."""
    name = (business.trade_name or business.legal_name or "").strip()
    parts = [p for p in (name, business.city, business.pincode) if p and str(p).strip()]
    if not parts:
        raise ValueError("Business must have at least a name, city, or pincode for Places search.")
    return " ".join(str(p).strip() for p in parts)


def extract_places_features_from_payload(payload: dict[str, Any]) -> PlacesExtractedFeatures | None:
    """Extract normalized Google Places fields purely from stored JSON (0 network calls)."""
    place: dict[str, Any] | None = None
    if isinstance(payload.get("places"), list):
        places_list = payload["places"]
        if not places_list:
            return None
        first = places_list[0]
        if isinstance(first, dict):
            place = first
    elif "id" in payload or "displayName" in payload or "primaryType" in payload:
        place = payload

    if place is None:
        return None

    display_name_raw = place.get("displayName")
    if isinstance(display_name_raw, dict):
        display_name = display_name_raw.get("text")
    elif isinstance(display_name_raw, str):
        display_name = display_name_raw
    else:
        display_name = None

    raw_types = place.get("types") or []
    types_tuple = tuple(str(t).strip() for t in raw_types if str(t).strip())

    website = place.get("websiteUri")
    website_clean = str(website).strip() if website else None

    phone = place.get("nationalPhoneNumber")
    phone_clean = str(phone).strip() if phone else None

    rating_raw = place.get("rating")
    google_rating = float(rating_raw) if rating_raw is not None else None

    review_raw = place.get("userRatingCount")
    review_count = int(review_raw) if review_raw is not None else None

    return PlacesExtractedFeatures(
        place_id=str(place["id"]) if place.get("id") else None,
        display_name=str(display_name).strip() if display_name else None,
        primary_type=str(place["primaryType"]).strip() if place.get("primaryType") else None,
        types=types_tuple,
        formatted_address=(
            str(place["formattedAddress"]).strip() if place.get("formattedAddress") else None
        ),
        google_rating=google_rating,
        review_count=review_count,
        has_website=bool(website_clean),
        website=website_clean,
        business_contact=phone_clean,
    )


class PlacesEnricher:
    """Official Google Places API enricher with caching, FieldMask, and INR budget tracking."""

    def __init__(
        self,
        http_client: CachedHttpClient,
        *,
        api_key: str | None = None,
        budget: PlacesBudgetConfig | None = None,
        fetch_details: bool = False,
        search_field_mask: str = DEFAULT_SEARCH_FIELD_MASK,
        details_field_mask: str = DEFAULT_DETAILS_FIELD_MASK,
    ) -> None:
        self.http_client = http_client
        self.api_key = api_key if api_key is not None else os.getenv("GOOGLE_PLACES_API_KEY", "")
        self.budget = budget or PlacesBudgetConfig.from_env()
        self.fetch_details = fetch_details
        self.search_field_mask = search_field_mask
        self.details_field_mask = details_field_mask
        self.run_spend_inr = Decimal(0)

    def enrich_business(
        self,
        business: BusinessRecord,
    ) -> tuple[PlacesExtractedFeatures | None, Decimal]:
        """Lookup a business in Google Places (cache-first), returning (features, cost_inr_spent)."""
        query = build_places_query(business)
        lookup_key = query.lower()
        spent_this_call = Decimal(0)

        cached_search = self.http_client.payload_store.get_latest(PLACES_SEARCH_SOURCE, lookup_key)
        if cached_search is None:
            self._check_budget(
                proposed_cost=self.budget.text_search_cost_inr,
                business_id=business.id,
                business_spent=spent_this_call,
            )
            if not self.api_key:
                raise RuntimeError(
                    "GOOGLE_PLACES_API_KEY is not configured and no cached payload exists."
                )

            search_resp = self.http_client.get_or_fetch(
                PLACES_SEARCH_SOURCE,
                lookup_key,
                "POST",
                PLACES_TEXT_SEARCH_URL,
                business_id=business.id,
                cost_inr=self.budget.text_search_cost_inr,
                json_body={"textQuery": query, "maxResultCount": 1, "regionCode": "IN"},
                headers={
                    "X-Goog-Api-Key": self.api_key,
                    "X-Goog-FieldMask": self.search_field_mask,
                    "Content-Type": "application/json",
                },
            )
            spent_this_call += search_resp.cost_inr
            self.run_spend_inr += search_resp.cost_inr
        else:
            search_resp = self.http_client.get_or_fetch(
                PLACES_SEARCH_SOURCE,
                lookup_key,
                "POST",
                PLACES_TEXT_SEARCH_URL,
                business_id=business.id,
            )

        if search_resp.http_status >= 400 or not isinstance(search_resp.payload, dict):
            return None, spent_this_call

        features = extract_places_features_from_payload(search_resp.payload)
        if features is None:
            return None, spent_this_call

        if self.fetch_details and features.place_id:
            details_key = features.place_id
            cached_details = self.http_client.payload_store.get_latest(
                PLACES_DETAILS_SOURCE, details_key
            )
            if cached_details is None:
                self._check_budget(
                    proposed_cost=self.budget.details_cost_inr,
                    business_id=business.id,
                    business_spent=spent_this_call,
                )
                if not self.api_key:
                    raise RuntimeError(
                        "GOOGLE_PLACES_API_KEY is not configured for Place Details lookup."
                    )
                details_resp = self.http_client.get_or_fetch(
                    PLACES_DETAILS_SOURCE,
                    details_key,
                    "GET",
                    PLACES_DETAILS_URL_TEMPLATE.format(place_id=features.place_id),
                    business_id=business.id,
                    cost_inr=self.budget.details_cost_inr,
                    headers={
                        "X-Goog-Api-Key": self.api_key,
                        "X-Goog-FieldMask": self.details_field_mask,
                    },
                )
                spent_this_call += details_resp.cost_inr
                self.run_spend_inr += details_resp.cost_inr
            else:
                details_resp = self.http_client.get_or_fetch(
                    PLACES_DETAILS_SOURCE,
                    details_key,
                    "GET",
                    PLACES_DETAILS_URL_TEMPLATE.format(place_id=features.place_id),
                    business_id=business.id,
                )

            if details_resp.http_status < 400 and isinstance(details_resp.payload, dict):
                detailed = extract_places_features_from_payload(details_resp.payload)
                if detailed is not None:
                    features = detailed

        return features, spent_this_call

    def _check_budget(
        self,
        *,
        proposed_cost: Decimal,
        business_id: int | None,
        business_spent: Decimal,
    ) -> None:
        if self.run_spend_inr + proposed_cost > self.budget.max_run_budget_inr:
            raise BudgetExceededError(
                f"Run budget cap exceeded: spent ₹{self.run_spend_inr} + ₹{proposed_cost} "
                f"> max ₹{self.budget.max_run_budget_inr}"
            )
        prior_biz_cost = (
            self.http_client.payload_store.sum_cost_inr(
                source=PLACES_SEARCH_SOURCE, business_id=business_id
            )
            + self.http_client.payload_store.sum_cost_inr(
                source=PLACES_DETAILS_SOURCE, business_id=business_id
            )
            if business_id is not None
            else Decimal(0)
        )
        # Note: prior_biz_cost includes anything already persisted in this run if saved before details
        total_biz_cost = max(prior_biz_cost, business_spent) + proposed_cost
        if total_biz_cost > self.budget.max_per_business_inr:
            raise BudgetExceededError(
                f"Per-business budget cap exceeded for business_id={business_id}: "
                f"₹{total_biz_cost} > max ₹{self.budget.max_per_business_inr}"
            )


def reextract_places_features(
    payload_store: RawPayloadStore,
    business: BusinessRecord,
) -> PlacesExtractedFeatures | None:
    """Re-extract Google Places features purely from `raw_payloads` without network calls."""
    lookup_key = build_places_query(business).lower()
    cached = payload_store.get_latest(PLACES_SEARCH_SOURCE, lookup_key)
    if cached is None or cached.http_status >= 400 or not isinstance(cached.payload, dict):
        return None
    features = extract_places_features_from_payload(cached.payload)
    if features and features.place_id:
        cached_details = payload_store.get_latest(PLACES_DETAILS_SOURCE, features.place_id)
        if (
            cached_details is not None
            and cached_details.http_status < 400
            and isinstance(cached_details.payload, dict)
        ):
            detailed = extract_places_features_from_payload(cached_details.payload)
            if detailed is not None:
                return detailed
    return features
