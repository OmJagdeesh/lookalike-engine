"""Phase 2 tests: HTTP caching, rate limiting, GST lookup, Google Places, and feature normalization."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from enrich.cache import (
    CachedHttpClient,
    InMemoryRawPayloadStore,
    RateLimiter,
)
from enrich.gst import (
    GST_SOURCE,
    extract_gst_features_from_payload,
    import_manual_gst_payload,
    lookup_gst_taxpayer,
    reextract_gst_features,
)
from enrich.normalize import (
    INDUSTRY_TAXONOMY,
    derive_business_age_band,
    derive_rating_band,
    derive_review_count_band,
    normalize_features,
    normalize_industry,
)
from enrich.pipeline import (
    InMemoryBusinessFeaturesStore,
    enrich_batch,
)
from enrich.places import (
    PLACES_DETAILS_SOURCE,
    PLACES_SEARCH_SOURCE,
    BudgetExceededError,
    PlacesBudgetConfig,
    PlacesEnricher,
    build_places_query,
)
from ingest.dedupe import BusinessRecord, InMemoryBusinessStore
from ingest.gstin import compute_gstin_checksum

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
SAMPLE_50_MOCKS = FIXTURES_DIR / "sample_50_enrich_mocks.json"


def _make_gstin(prefix14: str) -> str:
    return prefix14 + compute_gstin_checksum(prefix14)


# ---------------------------------------------------------------------------
# Step 2.1: Caching HTTP Client & Rate Limiter
# ---------------------------------------------------------------------------
def test_cached_http_client_caches_hits_and_errors() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "missing" in str(request.url):
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json={"ok": True, "url": str(request.url)})

    store = InMemoryRawPayloadStore()
    transport = httpx.MockTransport(handler)
    with CachedHttpClient(
        store,
        http_client=httpx.Client(transport=transport),
        rate_limiter=RateLimiter(default_rate_per_sec=0),
    ) as client:
        # 1. Cache miss -> 1 network call, costs ₹2.50
        r1 = client.get_or_fetch(
            "test_src", "key-1", "GET", "https://example.test/ok", cost_inr=Decimal("2.50")
        )
        assert r1.from_cache is False
        assert r1.http_status == 200
        assert r1.cost_inr == Decimal("2.50")

        # 2. Cache hit -> 0 network calls, costs ₹0
        r2 = client.get_or_fetch(
            "test_src", "key-1", "GET", "https://example.test/ok", cost_inr=Decimal("2.50")
        )
        assert r2.from_cache is True
        assert r2.cost_inr == Decimal(0)
        assert r2.payload == r1.payload

        # 3. 404 error is also cached so portal is not hammered on repeat lookup
        e1 = client.get_or_fetch("test_src", "key-404", "GET", "https://example.test/missing")
        e2 = client.get_or_fetch("test_src", "key-404", "GET", "https://example.test/missing")
        assert e1.http_status == 404
        assert e1.from_cache is False
        assert e2.http_status == 404
        assert e2.from_cache is True

    assert len(calls) == 2
    assert len(store.records) == 2
    assert store.sum_cost_inr("test_src") == Decimal("2.50")


def test_rate_limiter_enforces_minimum_interval() -> None:
    now = [100.0]
    sleeps: list[float] = []

    def fake_clock() -> float:
        return now[0]

    def fake_sleep(dt: float) -> None:
        sleeps.append(dt)
        now[0] += dt

    limiter = RateLimiter(
        rates_per_sec={"gst_public": 0.5},  # 1 request every 2.0 seconds
        clock=fake_clock,
        sleep_fn=fake_sleep,
    )
    assert limiter.acquire("gst_public") == 0.0
    now[0] += 0.5
    slept = limiter.acquire("gst_public")
    assert slept == pytest.approx(1.5)
    assert sleeps == [pytest.approx(1.5)]


# ---------------------------------------------------------------------------
# Step 2.2: GST Taxpayer Lookup & Re-extraction
# ---------------------------------------------------------------------------
def test_gst_rejects_invalid_gstin_before_network() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    store = InMemoryRawPayloadStore()
    client = CachedHttpClient(
        store,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limiter=RateLimiter(default_rate_per_sec=0),
    )

    with pytest.raises(ValueError, match="INVALID_CHECKSUM"):
        lookup_gst_taxpayer(
            "27AABCP1234A1Z0",
            http_client=client,
            endpoint_url="https://gst.test/taxpayer/{gstin}",
        )
    assert called is False


def test_gst_manual_import_backfill_and_offline_reextraction() -> None:
    g1 = _make_gstin("27AABCP2222B1Z")
    biz_store = InMemoryBusinessStore(
        [
            BusinessRecord(
                gstin=g1,
                legal_name="Alpha Packaging Pvt Ltd",
                trade_name=None,
                city=None,
                state=None,
                pincode=None,
                source="seed_import",
            )
        ]
    )
    biz = biz_store.find_by_gstin(g1)
    assert biz is not None and biz.id is not None

    payload = {
        "gstin": g1,
        "lgnm": "ALPHA PACKAGING PRIVATE LIMITED",
        "tradeNam": "Alpha Pack",
        "ctb": "Private Limited Company",
        "rgdt": "15/08/2018",
        "sts": "Active",
        "nba": ["Factory / Manufacturing", "Wholesale Business"],
        "aggreTurnOver": "Slab: Rs. 5 Cr. to 25 Cr.",
        "pradr": {"addr": {"dst": "Pune", "stcd": "Maharashtra", "pncd": "411019"}},
        "filingStatus": [
            {"retType": "GSTR3B", "status": "Filed"},
            {"retType": "GSTR1", "status": "Filed"},
        ],
    }

    payload_store = InMemoryRawPayloadStore()
    import_manual_gst_payload(payload_store, g1, payload, business_id=biz.id)

    client = CachedHttpClient(payload_store, rate_limiter=RateLimiter(default_rate_per_sec=0))
    extracted = lookup_gst_taxpayer(
        g1, http_client=client, business_id=biz.id, business_store=biz_store
    )
    assert extracted is not None
    assert extracted.business_type == "Pvt Ltd"
    assert extracted.gst_status == "Active"
    assert extracted.registration_date == date(2018, 8, 15)
    assert extracted.filing_regular is True
    assert extracted.turnover_band == "5Cr-25Cr"
    assert client.network_calls == 0

    # Verify missing attributes on `businesses` were backfilled without overwriting legal_name
    updated_biz = biz_store.find_by_gstin(g1)
    assert updated_biz is not None
    assert updated_biz.legal_name == "Alpha Packaging Pvt Ltd"
    assert updated_biz.trade_name == "Alpha Pack"
    assert updated_biz.city == "Pune"
    assert updated_biz.state == "Maharashtra"
    assert updated_biz.pincode == "411019"

    # Re-extract purely from raw_payloads without any HTTP client
    reextracted = reextract_gst_features(payload_store, g1)
    assert reextracted == extracted


def test_gst_irregular_filing_detection() -> None:
    payload = {
        "sts": "Suspended",
        "ctb": "Proprietorship",
        "aggreTurnOver": "Slab: Up to Rs. 40 Lakhs",
        "filingStatus": [
            {"retType": "GSTR3B", "status": "Filed"},
            {"retType": "GSTR3B", "status": "Not Filed"},
        ],
    }
    feat = extract_gst_features_from_payload(payload)
    assert feat.gst_status == "Suspended"
    assert feat.business_type == "Proprietorship"
    assert feat.turnover_band == "<40L"
    assert feat.filing_regular is False


# ---------------------------------------------------------------------------
# Step 2.3: Google Places Enrichment & Budget Caps
# ---------------------------------------------------------------------------
def test_places_field_mask_details_and_budget_guardrail() -> None:
    captured_headers: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.append(dict(request.headers))
        if request.url.path.endswith(":searchText"):
            return httpx.Response(
                200,
                json={
                    "places": [
                        {
                            "id": "ChIJ123",
                            "displayName": {"text": "Pune Gears"},
                            "primaryType": "manufacturer",
                            "types": ["machinery", "manufacturer"],
                            "formattedAddress": "MIDC Bhosari, Pune 411026",
                            "rating": 4.4,
                            "userRatingCount": 38,
                            "websiteUri": "https://punegears.example.in",
                            "nationalPhoneNumber": "020-27120001",
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "ChIJ123",
                "displayName": {"text": "Pune Gears"},
                "primaryType": "industrial_equipment_supplier",
                "types": ["machinery", "manufacturer"],
                "formattedAddress": "MIDC Bhosari, Pune 411026",
                "rating": 4.5,
                "userRatingCount": 40,
                "websiteUri": "https://punegears.example.in",
                "nationalPhoneNumber": "020-27120001",
            },
        )

    store = InMemoryRawPayloadStore()
    client = CachedHttpClient(
        store,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        rate_limiter=RateLimiter(default_rate_per_sec=0),
    )
    budget = PlacesBudgetConfig(
        max_run_budget_inr=Decimal("5.00"),
        max_per_business_inr=Decimal("5.00"),
        text_search_cost_inr=Decimal("3.00"),
        details_cost_inr=Decimal("1.50"),
    )
    enricher = PlacesEnricher(client, api_key="test-key", budget=budget, fetch_details=True)

    b1 = BusinessRecord(id=1, legal_name="Pune Gears Pvt Ltd", city="Pune", pincode="411026")
    assert build_places_query(b1) == "Pune Gears Pvt Ltd Pune 411026"

    feat, cost = enricher.enrich_business(b1)
    assert feat is not None
    assert cost == Decimal("4.50")
    assert feat.google_rating == 4.5
    assert feat.review_count == 40
    assert feat.has_website is True
    assert feat.website == "https://punegears.example.in"
    assert feat.business_contact == "020-27120001"
    assert any("x-goog-fieldmask" in h for h in captured_headers)
    assert store.get_latest(PLACES_SEARCH_SOURCE, "pune gears pvt ltd pune 411026") is not None
    assert store.get_latest(PLACES_DETAILS_SOURCE, "ChIJ123") is not None

    # Second call for b1 hits cache (0 cost, succeeds even with 0.50 INR remaining)
    feat_cached, cost_cached = enricher.enrich_business(b1)
    assert feat_cached == feat
    assert cost_cached == Decimal(0)

    # Uncached second business exceeds remaining run budget (4.50 + 3.00 > 5.00) -> aborts cleanly
    b2 = BusinessRecord(id=2, legal_name="Other Co Pvt Ltd", city="Pune", pincode="411019")
    with pytest.raises(BudgetExceededError, match="Run budget cap exceeded"):
        enricher.enrich_business(b2)


# ---------------------------------------------------------------------------
# Step 2.4: Industry Taxonomy & Feature Band Normalizer
# ---------------------------------------------------------------------------
def test_industry_taxonomy_and_feature_bands() -> None:
    # 30 B2B categories + "other" = 31 entries
    assert len(INDUSTRY_TAXONOMY) == 31
    assert "other" in INDUSTRY_TAXONOMY

    assert (
        normalize_industry(
            places_primary_type="pharmaceutical_company",
            gst_nature_of_business=["Factory / Manufacturing"],
        )
        == "pharma_chemicals"
    )
    assert (
        normalize_industry(
            places_primary_type="establishment",
            business_name="Deccan Corrugated Packaging LLP",
        )
        == "packaging_plastics"
    )
    assert normalize_industry(places_primary_type="unknown_xyz") == "other"

    ref_date = date(2026, 11, 18)
    assert derive_business_age_band(date(2025, 6, 1), as_of_date=ref_date) == "<2y"
    assert derive_business_age_band(date(2023, 1, 1), as_of_date=ref_date) == "2-5y"
    assert derive_business_age_band(date(2019, 1, 1), as_of_date=ref_date) == "5-10y"
    assert derive_business_age_band(date(2012, 1, 1), as_of_date=ref_date) == "10y+"

    assert derive_review_count_band(0) == "0"
    assert derive_review_count_band(7) == "1-10"
    assert derive_review_count_band(34) == "11-50"
    assert derive_review_count_band(80) == "50+"

    assert derive_rating_band(None) == "unrated"
    assert derive_rating_band(3.2) == "<3.5"
    assert derive_rating_band(4.0) == "3.5-4.2"
    assert derive_rating_band(4.6) == "4.2+"

    bundle = normalize_features(
        places_primary_type="auto_parts_store",
        registration_date=date(2018, 5, 1),
        review_count=45,
        google_rating=4.3,
        turnover_band="5Cr-25Cr",
        city="pune",
        business_type="Pvt Ltd",
        filing_regular=True,
        has_website=True,
        as_of_date=ref_date,
    )
    pairs = bundle.to_feature_dict()
    assert pairs["industry"] == "automotive_components"
    assert pairs["business_age_band"] == "5-10y"
    assert pairs["review_count_band"] == "11-50"
    assert pairs["rating_band"] == "4.2+"
    assert pairs["city"] == "Pune"
    assert pairs["filing_regular"] == "true"


# ---------------------------------------------------------------------------
# Gate 2 Verification: 50 Sample Seed Businesses
# ---------------------------------------------------------------------------
def test_gate2_50_businesses_cache_budget_and_taxonomy() -> None:
    assert SAMPLE_50_MOCKS.exists()
    samples = json.loads(SAMPLE_50_MOCKS.read_text(encoding="utf-8"))
    assert len(samples) == 50

    gst_by_gstin = {item["gstin"]: item["gst_payload"] for item in samples}
    places_by_query = {
        f"{item['trade_name']} {item['city']} {item['pincode']}".lower(): item["places_payload"]
        for item in samples
    }

    def mock_handler(request: httpx.Request) -> httpx.Response:
        if "gst.test" in str(request.url):
            gstin = request.url.path.rsplit("/", 1)[-1]
            payload = gst_by_gstin.get(gstin)
            if payload is None:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json=payload)

        if request.url.host == "places.googleapis.com":
            body = json.loads(request.content.decode("utf-8"))
            query_key = body["textQuery"].lower()
            payload = places_by_query.get(query_key, {"places": []})
            return httpx.Response(200, json=payload)

        return httpx.Response(404, json={"error": "unexpected url"})

    biz_store = InMemoryBusinessStore()
    businesses: list[BusinessRecord] = []
    for item in samples:
        businesses.append(
            biz_store.insert_business(
                BusinessRecord(
                    gstin=item["gstin"],
                    legal_name=item["legal_name"],
                    trade_name=item["trade_name"],
                    city=item["city"],
                    state=item["state"],
                    pincode=item["pincode"],
                    source="seed_import",
                )
            )
        )

    payload_store = InMemoryRawPayloadStore()
    features_store = InMemoryBusinessFeaturesStore()
    http_client = CachedHttpClient(
        payload_store,
        http_client=httpx.Client(transport=httpx.MockTransport(mock_handler)),
        rate_limiter=RateLimiter(default_rate_per_sec=0),
    )
    places_enricher = PlacesEnricher(
        http_client,
        api_key="mock-places-key",
        budget=PlacesBudgetConfig(
            max_run_budget_inr=Decimal(2000),
            max_per_business_inr=Decimal(5),
            text_search_cost_inr=Decimal("3.00"),
        ),
    )

    # First run: enriches all 50 via mocked GST + Places APIs
    report1 = enrich_batch(
        businesses,
        http_client=http_client,
        features_store=features_store,
        business_store=biz_store,
        places_enricher=places_enricher,
        gst_endpoint_url="https://gst.test/taxpayer/{gstin}",
    )

    assert report1.total_businesses == 50
    assert report1.enriched_count == 50
    assert report1.gst_enriched_count == 50
    assert report1.places_enriched_count == 50
    assert report1.aborted_due_to_budget is False
    assert report1.errors == []
    assert report1.network_calls == 100  # 50 GST + 50 Places
    assert len(payload_store.records) == 100
    assert report1.total_cost_inr == Decimal("150.00")  # ₹3.00 * 50 <= ₹2000 budget
    assert payload_store.sum_cost_inr(PLACES_SEARCH_SOURCE) == Decimal("150.00")
    assert payload_store.sum_cost_inr(GST_SOURCE) == Decimal(0)

    # Gate 2 criterion: < 10% fall into "other" industry (< 5 out of 50)
    other_pct = report1.other_industry_count / report1.enriched_count
    assert other_pct < 0.10, f"Too many businesses fell into 'other': {other_pct:.1%}"

    # Second run: ZERO repeat network lookups and ZERO additional cost
    report2 = enrich_batch(
        businesses,
        http_client=http_client,
        features_store=features_store,
        business_store=biz_store,
        places_enricher=places_enricher,
        gst_endpoint_url="https://gst.test/taxpayer/{gstin}",
    )
    assert report2.enriched_count == 50
    assert report2.network_calls == 0
    assert report2.total_cost_inr == Decimal(0)
    assert len(payload_store.records) == 100

    # Offline re-extraction purely from raw_payloads also succeeds with 0 network calls
    report3 = enrich_batch(
        businesses,
        http_client=http_client,
        features_store=features_store,
        reextract_only=True,
    )
    assert report3.enriched_count == 50
    assert report3.network_calls == 0
    assert report3.features[0].industry == report1.features[0].industry
