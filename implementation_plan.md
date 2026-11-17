# Lookalike Engine — Stepwise Implementation Plan

Based on [Lookalike engine guide.md](file:///d:/Delpat/Lookalike%20engine%20guide.md), this plan breaks the build into sequential phases with explicit **acceptance gates** ("Done when") and **DPDP / scraping guardrails**.

> [!IMPORTANT]
> **Build Philosophy & Non-Goals (v1)**
>
> - Keep it boring: **Python 3.11+**, **PostgreSQL**, **pandas**, **httpx**, **pytest**, and a single **Streamlit** or **FastAPI** review UI.
> - **Do NOT build yet:** Automated WhatsApp/email outreach, ML models (wait for hundreds of won/lost outcomes), multi-agent pipelines, extra analytics dashboards, or multi-city / >3-industry expansion.
> - **Sequential Execution Rule:** Build stages in order and verify each stage on a single sample/real client before advancing. Never commit API keys or real client data to Git.

---

## Architecture & Repository Layout

Create the project at `d:\Delpat\lookalike-engine` matching the guide's layout:

```text
lookalike-engine/
  db/
    schema.sql          # Core tables, indexes, constraints, raw_payloads cache
    migrations/         # Incremental SQL migrations
  ingest/
    __init__.py
    importer.py         # CSV/Excel loader + column mapping + row error report
    gstin.py            # 15-char format + mod-36 checksum validator
    dedupe.py           # Match by GSTIN first, then normalized name + pincode
  enrich/
    __init__.py
    cache.py            # httpx wrapper + raw_payloads cache + rate limiter
    gst.py              # Public GST taxpayer lookup (low-volume/manual-assisted)
    places.py           # Official Google Places API lookup + cost logger
    normalize.py        # Fixed ~30-industry taxonomy + feature band derivation
  profile/
    __init__.py
    lift.py             # Feature lift calculator (won vs. lost/baseline)
    weights.py          # Capped log(lift) weights + versioned profiles writer
    icp_text.py         # Single LLM call to generate 3-sentence ICP summary
  score/
    __init__.py
    collect.py          # Candidate collector (1 city, <= 3 industries)
    scorer.py           # Deterministic 0-100 scorer + top-3 reasons builder
    exclude.py          # Filter existing customers, inactive GSTINs, duplicates
    export.py           # Ranked CSV exporter
  feedback/
    __init__.py
    outcomes.py         # Append-only lead_outcomes recorder + re-profile job
    backtest.py         # 20% holdout backtest reporting top-50 & top-200 hit rates
  ui/
    app.py              # Streamlit or FastAPI lead review & outcome capture page
  tests/
    fixtures/           # Anonymized/fake CSVs, GST/Places JSON mocks
    test_ingest.py
    test_enrich.py
    test_profile.py
    test_score.py
    test_feedback.py
  .env.example
  .gitignore            # Ignores .env, local data/exports, __pycache__
  pyproject.toml
  README.md
```

---

## Phase 0: Compliance, Environment & Database Foundation (Week 1, Days 1–2)

### Step 0.1 — Data Protection & Governance Setup

1. Draft a client onboarding checklist requiring a **signed Data Processing Agreement (DPA)** before any real client list is imported.
2. Enforce **client data isolation**:
   - Shared public entity tables (`businesses`, `business_features`, `raw_payloads`) store public/directory attributes only.
   - Client-proprietary tables (`seed_customers`, `profiles`, `lead_scores`, `lead_outcomes`) are strictly scoped by `client_id` and must never be joined across clients.
   - Do not store personal phone numbers or personal emails—only business contact details sales needs.
3. Implement a client-deletion script/procedure (`delete_client_data(client_id)`) to remove a client's proprietary rows (`lead_outcomes`, `lead_scores`, `profiles`, `seed_customers`, `clients`) upon contract end or erasure request.

### Step 0.2 — Project & Git Workflow Setup

1. Initialize `lookalike-engine/` with Python 3.11+ virtual environment and dependencies (`pandas`, `openpyxl`, `httpx`, `psycopg[binary]` / `sqlalchemy`, `pydantic`, `pytest`, `python-dotenv`, `streamlit` or `fastapi` + `uvicorn`, and an LLM client SDK).
2. Add `.gitignore` excluding `.env`, `*.csv`, `*.xlsx` outside `tests/fixtures/`, and local artifacts. Create `.env.example` with placeholders (`DATABASE_URL`, `GOOGLE_PLACES_API_KEY`, `LLM_API_KEY`, `PLACES_MAX_BUDGET_INR`).
3. Configure Git branching convention: **one branch per build task, PR reviewed by a second intern** before merging.

### Step 0.3 — PostgreSQL Schema (`db/schema.sql`)

Implement all 7 core tables from the guide plus `raw_payloads`:

1. `clients` (`id`, `name`, `product_sold`, `created_at`)
2. `businesses` (`id`, `gstin` UNIQUE NULLABLE, `legal_name`, `trade_name`, `city`, `state`, `pincode`, `source`, `first_seen_at`) + index on `(LOWER(TRIM(legal_name)), pincode)` for fallback dedupe.
3. `business_features` (`id`, `business_id` FK, `industry`, `business_type`, `registration_date`, `gst_status`, `filing_regular` BOOLEAN, `turnover_band`, `employee_band`, `google_rating`, `review_count`, `has_website` BOOLEAN, `enriched_at`).
4. `seed_customers` (`id`, `client_id` FK, `business_id` FK, `outcome` CHECK IN `('won', 'lost', 'churned')`, `deal_value_inr`, `closed_at`).
5. `profiles` (`id`, `client_id` FK, `version` INT, `description_text`, `feature_weights` JSONB, `created_at`) with UNIQUE `(client_id, version)`.
6. `lead_scores` (`id`, `profile_id` FK, `business_id` FK, `score` NUMERIC/INT 0–100, `reasons` JSONB, `scored_at`).
7. `lead_outcomes` (`id`, `lead_score_id` FK, `status` CHECK IN `('contacted', 'meeting', 'won', 'lost', 'no response')`, `notes`, `updated_at`) — **append-only rule enforced** (DB trigger or app layer forbids `DELETE`, and updates insert/log history).
8. `raw_payloads` (`id`, `source`, `lookup_key`, `business_id` NULLABLE, `payload` JSONB, `http_status`, `cost_inr`, `fetched_at`) with index on `(source, lookup_key)` so enrichment can be re-run offline without re-fetching.

**Phase 0 Check:** Schema applies cleanly to a local PostgreSQL instance (encrypted at rest in production/staging) and `pytest` runs an empty smoke test.

---

## Phase 1: Stage 1 — Seed Import, Validation & De-duplication (Weeks 1–2)

### Step 1.1 — GSTIN Validator (`ingest/gstin.py`)

1. Implement `validate_gstin(gstin: str) -> ValidationResult`:
   - Normalize uppercase and strip whitespace.
   - Check 15-character regex: `^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$` (validates state code `01–38`, PAN format, entity count, `'Z'`, checksum char).
   - Compute and verify the official GSTIN **mod-36 weighted checksum** on characters 1–14 against character 15.
2. Return structured error reason (e.g., `INVALID_LENGTH`, `INVALID_FORMAT`, `INVALID_CHECKSUM`) so bad GSTINs are flagged before any lookup.

### Step 1.2 — Business Name Normalizer & De-duplication (`ingest/dedupe.py`)

1. Implement name normalization (lowercase, strip legal suffixes like `Pvt Ltd`, `Private Limited`, `LLP`, `Ltd`, punctuation, extra spaces).
2. Implement deterministic two-tier match:
   - **Tier 1:** Exact `gstin` match against `businesses`.
   - **Tier 2:** Normalized name + 6-digit `pincode` match (used when GSTIN is missing or across sources).
3. Merge rules: keep earliest `first_seen_at`, fill null attributes (`trade_name`, `city`, `state`, `pincode`) without overwriting verified GST values, and write a structured merge log (count + matched IDs).

### Step 1.3 — CSV & Excel Seed Importer (`ingest/importer.py`)

1. Support `.csv` and `.xlsx` files (CRM exports, Tally/Zoho invoice exports, or plain name+city sheets) via configurable column-mapping dict/YAML (`name`, `gstin`, `city`, `state`, `pincode`, `outcome`, `deal_value_inr`, `closed_at`).
2. Row-by-row validation using `pydantic`:
   - Never crash on malformed rows; collect row index, raw values, and clear error messages into an `ImportReport`.
   - Run `validate_gstin()` when GSTIN is present; allow rows without GSTIN if `name` + `city` are present (flagging lower match confidence).
   - Deduplicate within the batch and against existing `businesses`, then insert `businesses` + `seed_customers` for `client_id`.

### Gate 1 Verification

- Create a messy 200-row fake fixture (`tests/fixtures/messy_seed_200.csv`) containing duplicate businesses (with/without GSTIN, varied casing/suffixes), invalid GSTINs, missing deal values, and blank rows.
- **Done when:** `pytest tests/test_ingest.py` passes; importer finishes without crashing, flags every invalid GSTIN before lookup, reports clear row-level errors, and merges & logs all duplicates.

---

## Phase 2: Stage 2 — Enrichment & Feature Normalization (Weeks 2–3)

### Step 2.1 — Caching HTTP Client & Rate Limiter (`enrich/cache.py`)

1. Build a shared `httpx` wrapper backed by `raw_payloads`:
   - Before any network call, query `raw_payloads` by `(source, lookup_key)`. On cache hit, return stored JSON immediately (`0` network calls, `0` cost).
   - Enforce per-source token-bucket / sleep rate limiting (e.g., configurable requests/sec).
   - Persist raw response, HTTP status, timestamp, and request cost (`cost_inr`) to `raw_payloads` even on 404/error responses to prevent hammering portals.

### Step 2.2 — GST Taxpayer Lookup (`enrich/gst.py`)

1. Implement public GST lookup respecting portal terms (**manual-assisted import or low-volume rate-limited lookup only; no bulk scraping**).
2. Extract and map to `business_features`:
   - `legal_name`, `trade_name`, `state`, `pincode` (backfilling `businesses` if missing)
   - `business_type` (Proprietorship, Partnership, Pvt Ltd, LLP, Public Ltd, etc.)
   - `registration_date`, `gst_status` (Active, Cancelled, Suspended)
   - `filing_regular` (boolean derived from recent GSTR-1/3B filing consistency where shown)
   - `turnover_band` (where publicly shown)
3. Support re-running feature extraction purely from `raw_payloads` without external calls.

### Step 2.3 — Google Places Enrichment (`enrich/places.py`)

1. Integrate the official **Google Places API** (Text Search + Place Details requesting only needed fields via `FieldMask` to minimize billing tier: primary/secondary categories, formatted address, rating, user rating count, website URI).
2. Query using `trade_name`/`legal_name` + `city` + `pincode`.
3. Track cumulative and per-business API cost (`cost_inr`) in `raw_payloads`; abort cleanly if a run exceeds the configured budget threshold (`PLACES_MAX_BUDGET_INR`).
4. Populate `google_rating`, `review_count`, `has_website` in `business_features` (plus business contact/website fields needed by sales—no personal contacts).

### Step 2.4 — Feature Normalizer & Industry Taxonomy (`enrich/normalize.py`)

1. Define a fixed canonical `INDUSTRY_TAXONOMY` of **~30 B2B Indian industry categories** (e.g., `automotive_components`, `pharma_chemicals`, `textiles_apparel`, `machinery_equipment`, `packaging_plastics`, `food_beverage_processing`, `electronics_electrical`, `construction_materials`, `logistics_warehousing`, `it_software_services`, etc., plus `"other"`).
2. Build a deterministic keyword/mapping table from raw Google Places types, GST nature-of-business strings, and directory categories to the 30 canonical industries.
3. Derive normalized buckets for scoring:
   - `business_age_band` from `registration_date` (`<2y`, `2-5y`, `5-10y`, `10y+`)
   - `review_count_band` (`0`, `1-10`, `11-50`, `50+`)
   - `rating_band` (`<3.5`, `3.5-4.2`, `4.2+`, `unrated`)
   - `turnover_band`, `employee_band`, `city`, `business_type`, `filing_regular`, `has_website`.

### Gate 2 Verification

- Run Stage 2 on 50 sample seed businesses (offline mocked in `pytest` + controlled test batch).
- **Done when:**
  1. 50 seed businesses enriched with raw payloads stored in `raw_payloads` and **zero repeat lookups** on a second run.
  2. Google Places cost per business is logged and verified under budget.
  3. `< 10%` of businesses fall into `"other"` industry after running `normalize.py`.

---

## Phase 3: Stage 3 & Stage 4 — Profiling, Expansion, Scoring & Client Review Gate (Weeks 4–5)

### Step 3.1 — Lift Calculator (`profile/lift.py`)

1. Using `pandas`, load `seed_customers` + latest `business_features` + `businesses` (`city`/`state`) for a single `client_id`.
2. Split into positives (`outcome = 'won'`) and negatives/baseline (`outcome IN ('lost', 'churned')`, or overall candidate/seed base rate when lost deals are scarce, with Laplace smoothing `alpha` to handle small-sample `30–500` row counts safely).
3. For each categorical feature-value pair `(feature, value)` (e.g., `industry=machinery_equipment`, `city=Pune`, `business_age_band=5-10y`, `filing_regular=True`), compute:
   - `won_share`, `baseline_share`, `support_count`, and `lift = won_share / baseline_share`.
4. Filter out ultra-rare noise (e.g., `support_count < min_support`) and output a ranked table (e.g., `"manufacturer: 3.1x more common in won deals"`).

### Step 3.2 — Weight Calculator & Profile Versioning (`profile/weights.py`)

1. Compute `raw_weight = ln(lift)` for qualifying traits, **capped** to `[-W_MAX, +W_MAX]` (e.g., `[-1.5, +1.5]`) so one noisy feature cannot dominate.
2. Determine next `version` (`max(version) + 1`) for `client_id` and store `feature_weights` JSON in `profiles` (never overwriting prior versions).

### Step 3.3 — Plain-Language ICP Generator (`profile/icp_text.py`)

1. Pass the top positive and negative traits from the lift table (never raw PII) into **one single LLM API call** with a strict prompt: write a **3-sentence plain-language ICP** describing who the client's best customers are and what traits distinguish them.
2. Save `description_text` to the `profiles` row. Verify the LLM never alters or computes numeric weights/scores.

### Step 3.4 — Scoped Candidate Collector (`score/collect.py`)

1. Restrict v1 expansion to **1 city and up to 3 industries** for the pilot client.
2. Collect candidates via compliant sources:
   - Official Google Places API text/nearby search for `(city, industry_query)`.
   - Manual/CSV/PDF imports from industrial estate lists (e.g., MIDC) or trade associations.
   - Directory exports (IndiaMART / JustDial / TradeIndia) **only** if explicitly permitted by ToS and `robots.txt`; otherwise manual collection only.
3. Deduplicate into `businesses` (`ingest/dedupe.py`) and enrich via Stage 2 (`enrich/`), skipping any business already enriched.

### Step 3.5 — Exclusion Filter & Deterministic Scorer (`score/exclude.py`, `score/scorer.py`)

1. `exclude.py`: For `client_id`, exclude:
   - Any `business_id` present in `seed_customers` for that `client_id`.
   - Any business with inactive/cancelled `gst_status`.
   - Any remaining duplicates (by GSTIN or normalized name + pincode).
2. `scorer.py`:
   - For each eligible candidate, sum matching `feature_weights` from the active `profiles` version.
   - Min-max / theoretical-bound scale the raw sum to `0–100`.
   - Pick the **top 3 contributing positive traits** (plus mismatch/status summary if helpful) and format human-readable `reasons` JSON + sentence, e.g.:
     `"Matches 4 of 5 traits of top customers: manufacturer, Pune, 5 to 10 years old, regular GST filer."`
   - Persist into `lead_scores`.

### Step 3.6 — Export & Lead Review UI (`score/export.py`, `ui/app.py`)

1. `export.py`: Export top N ranked leads to CSV (`rank`, `score`, `business_name`, `gstin`, `city`, `industry`, `reasons`, `website`, `business_contact`).
2. `ui/app.py` (Streamlit or simple FastAPI HTML page):
   - Select `client_id` & `profile` version.
   - Display the 3-sentence ICP summary and top lift traits.
   - Show ranked lead table with score (`0–100`), top 3 reasons, GST status, and business contact/website details.
   - Keep scope strictly to this review page (no extra analytics dashboards).

### Gate 3 Verification — Client Review Gate (CRITICAL)

- Run end-to-end on one real or pilot client (1 city, <= 3 industries).
- **Done when:**
  1. Salesperson reads the 3-sentence ICP and validates/agrees with the profile traits.
  2. The client's sales lead reviews the **top 50 leads** and confirms **at least half (>= 25/50) are worth calling**.
- **Hard Stop Rule:** If `< 25/50` are worth calling, diagnose feature normalization, lift thresholds, exclusions, or data quality and fix Stage 2–4 **before** starting Stage 5.

---

## Phase 4: Stage 5 — Feedback Loop & Backtesting (Week 6)

### Step 4.1 — Outcome Capture (`feedback/outcomes.py`, `ui/app.py`)

1. Add inline status controls (or CSV/Google Sheet sync) in `ui/app.py` allowing sales to log `status` (`contacted`, `meeting`, `won`, `lost`, `no response`) and `notes` for any `lead_score_id`.
2. Write rows to `lead_outcomes` **append-only** (never delete or destructively overwrite previous audit rows).

### Step 4.2 — Re-Profile Job (`feedback/outcomes.py`)

1. Combine original `seed_customers` (`won`, `lost`, `churned`) with new `lead_outcomes` where `status IN ('won', 'lost')` for that `client_id` (excluding `contacted`, `meeting`, `no response` until resolved).
2. Re-run `lift.py`, `weights.py`, and `icp_text.py` to publish a new `profiles` version (`version + 1`) and re-score open candidates.

### Step 4.3 — Holdout Backtester (`feedback/backtest.py`)

1. Split a client's historical `won` `seed_customers`: randomly (seeded for reproducibility) hide **20% of won customers** from Stage 3 training, keeping them in the candidate pool for Stage 4 (temporarily bypassing `seed_customers` exclusion for the held-out 20% only).
2. Run `lift -> weights -> scorer` and measure:
   - **Top-50 Hit Rate:** `%` of held-out won customers appearing in the top 50 scored candidates.
   - **Top-200 Hit Rate:** `%` of held-out won customers appearing in the top 200 scored candidates.
3. Log and compare hit rates across every `profiles` version (`v1`, `v2`, ...) to verify scoring improves over time.

### Gate 4 Verification — Version 1 Definition of Done

- **Done when:**
  1. Full `pytest` suite passes offline with fake fixtures across all 5 stages.
  2. Real client sales lead confirms `>= 50%` of top 50 leads are worth calling.
  3. `feedback/backtest.py` reports top-50 and top-200 hit rates for every profile version, showing held-out past winners ranking near the top.

---

## Weekly Reporting Cadence (Every Friday)

Each intern posts a short note covering:

1. **Shipped this week:** PR links merged.
2. **One number that moved:** businesses enriched, enrichment cost per business (INR), backtest hit rate (top-50 / top-200), or sales acceptance rate (`x/50`).
3. **Blocked / Needs:** Clear blockers or decisions required.

---

## Open Decisions to Confirm Before Coding

1. **UI Framework:** Streamlit (faster to build for tabular review + status forms) vs. FastAPI + Jinja templates.
   Answer: Use Streamlit.
2. **Public GST Lookup Method:** Manual/batch-export assist vs. an authorized GST API provider (to strictly honor the "no bulk scraping" rule).
   Answer: public GST you need to smartly scrape things
3. **LLM Provider:** Which single model API key to configure in `.env` for `icp_text.py`.
   Answer: LLM will be your antigravity, manual connection with your llm api for now

4. **Pilot Client & Scope:** Which pilot client, target **1 city**, **up to 3 industries**, and **max Google Places INR budget per business** to use for Gates 2–4.
   Answer: Use anonymized/sample client data for Stages 1–3 first (then plug in 1 real B2B pilot client with signed DPA before Gate 3). Target city: Pune. Target industries (up to 3): Manufacturing, Packaging & Plastics, Industrial Machinery (or top 3 from the pilot client's won deals). Google Places budget cap: ₹5/business max, ₹2,000 total pilot cap.
