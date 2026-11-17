# Lookalike engine: intern build guide

Nov 14, 2026 · @Akash Patel

## What we're building

We are building a B2B lookalike engine: given a client's best existing customers, it finds other Indian businesses that look like them and ranks them for our sales team. Think of Meta's lookalike audiences, but for businesses instead of people, using India-specific data such as GST records and local directories.

The engine answers one question for a client: "Which businesses should we approach next, and why?" A good result is a ranked list of verified, active businesses, each with a score and a one-paragraph reason a salesperson can trust.

The first version must work with small data. A typical client gives us 30 to 500 past customers, not millions. Simple, explainable scoring beats a clever model nobody can debug.

## How the engine works

The engine runs in five stages. Stage 5 feeds back into stage 3, so the scoring improves with every won or lost deal.

1. **Seed.** Load the client's past customers: won deals, plus lost deals and churned customers as negative examples.
2. **Enrich.** Turn each seed business into a feature record: industry, size, location, business age, GST status, online presence.
3. **Profile.** Find what the best customers have in common, and what separates them from lost deals. Output: an ideal customer profile (ICP) in plain words plus feature weights.
4. **Expand and score.** Collect candidate businesses from directories and Maps, enrich them the same way, and score how closely each matches the profile. Remove anyone who is already a customer.
5. **Feedback.** Record what happened to every lead the sales team contacted. Re-run stage 3 with the new outcomes.

Every score must come with a reason a human can read, for example "Matches 4 of 5 traits of top customers: manufacturer, Pune, 5 to 10 years old, regular GST filer."

## Data sources

Start with what the client already has; public sources come second. B2B invoices are the best seed source because they carry the buyer's GSTIN, which links to verified public details.

| Source | What we get | Used in stage | Notes |
| --- | --- | --- | --- |
| Client CRM or spreadsheet | Customer names, contacts, deal value, won or lost | Seed, Feedback | Ask for lost deals too, not just wins |
| Client sales invoices (Tally, Zoho, Excel exports) | Buyer GSTIN, order value, order frequency | Seed | GSTIN is the join key for everything else |
| Public GST taxpayer search | Legal and trade name, state, business type, registration date, status, filing regularity, turnover range where shown | Enrich | Manual or low-volume lookup only; no bulk scraping |
| Google Maps and Business listings | Category, address, rating, review count, website | Enrich, Expand | Use the official Places API, which is paid per request |
| IndiaMART, JustDial, TradeIndia | Business category, products, location | Expand | Check each site's terms before automating |
| Trade associations, industrial estate lists (for example MIDC) | Member businesses by sector and area | Expand | Often PDFs or Excel; parse by hand at first |
| Company website and job posts | What they sell, team size signals, hiring | Enrich | Optional in v1 |

If a client cannot share invoices, a plain list of customer names and cities still works; matching will just be less accurate.

## Data model

One `businesses` table holds every business we know about, whether it came from a client's seed or from a directory. Everything else links to it, so a business is enriched once and reused.

| Table | Purpose | Key fields |
| --- | --- | --- |
| `clients` | Our customers who use the engine | id, name, product\_sold, created\_at |
| `businesses` | Every business, seed or candidate | id, gstin (unique, nullable), legal\_name, trade\_name, city, state, pincode, source, first\_seen\_at |
| `business_features` | Enriched traits, one row per business per enrichment run | business\_id, industry, business\_type, registration\_date, gst\_status, filing\_regular (bool), turnover\_band, employee\_band, google\_rating, review\_count, has\_website, enriched\_at |
| `seed_customers` | A client's past customers and how the deal went | client\_id, business\_id, outcome (won, lost, churned), deal\_value\_inr, closed\_at |
| `profiles` | The ICP learned for a client | id, client\_id, version, description\_text, feature\_weights (JSON), created\_at |
| `lead_scores` | Each candidate's score against a profile | profile\_id, business\_id, score (0 to 100), reasons (JSON), scored\_at |
| `lead_outcomes` | What happened after sales contacted the lead | lead\_score\_id, status (contacted, meeting, won, lost, no response), notes, updated\_at |

Rules: never delete rows in `lead_outcomes`; keep every `profiles` version so we can compare old and new scoring; store raw source responses in a separate `raw_payloads` table so enrichment can be re-run without fetching again.

## Build tasks

Build the stages in order and test each one on a single real or sample client before moving on. Each task lists what "done" means.

### Stage 1: Seed import

- [ ] CSV and Excel importer for customer lists with column mapping (name, GSTIN, city, outcome, deal value). Done when: a messy 200-row sample imports with clear errors for bad rows, not a crash.
- [ ] GSTIN validator (15-character format and checksum). Done when: invalid GSTINs are flagged before any lookup.
- [ ] De-duplication: match the same business across sources by GSTIN first, then normalized name plus pincode. Done when: duplicates in the sample are merged and logged.

### Stage 2: Enrichment

- [ ] GST lookup module that fills `business_features` from the public taxpayer search, with caching and a rate limit. Done when: 50 seed businesses enriched, raw responses saved, no repeat lookups.
- [ ] Google Places enrichment (category, rating, reviews, website) using the official API. Done when: cost per business is logged and under the budget we set.
- [ ] Feature normalizer: map raw categories into a fixed industry list of about 30 values. Done when: fewer than 10% of businesses land in "other".

### Stage 3: Profile

- [ ] Lift calculator: for each feature value, compare how often it appears in won customers versus lost or all candidates. Done when: it outputs a ranked table of traits, for example "manufacturer: 3.1x more common in won deals".
- [ ] Weights from lift (start with log of lift, capped), saved as a `profiles` version.
- [ ] Plain-language ICP: send the top traits to an LLM to write a 3-sentence description. The LLM writes the summary only; it never computes scores. Done when: a salesperson can read the ICP and agree or disagree with it.

### Stage 4: Expand and score

- [ ] Candidate collector for one city and up to 3 industries from the sources table, respecting each source's terms.
- [ ] Scorer: score = sum of weights for matching traits, scaled to 0 to 100, with the top 3 reasons stored.
- [ ] Exclusion filter: remove existing customers, inactive GSTINs and duplicates.
- [ ] Export: ranked CSV plus a simple web page showing score, reasons and contact details. Done when: the client's sales lead reviews the top 50 and says at least half are worth calling.

### Stage 5: Feedback

- [ ] Outcome capture: a form or sheet where sales updates each lead's status.
- [ ] Re-profile job that adds new outcomes and creates a new profile version.
- [ ] Backtest: hide 20% of a client's past won customers, run the engine, and check how many appear in our top results. Done when: we report hit rate in top 50 and top 200 for every profile version.

## Tech stack and repo

Keep it boring: Python, PostgreSQL and plain scripts. No agents framework, queues or microservices until the scoring is proven.

| Layer | Choice | Why |
| --- | --- | --- |
| Language | Python 3.11+ | Team knows it; strong data libraries |
| Database | PostgreSQL | Joins across businesses, features and scores |
| Data work | pandas | Import, clean and compute lift |
| HTTP and caching | httpx with a local cache table | Avoid repeat paid lookups |
| LLM | One model API call for ICP text and research briefs | Summaries only, never scoring |
| UI | Streamlit or a simple FastAPI page | Enough for sales to review leads |
| Tests | pytest with fixed sample data | Every stage testable offline |
| Version control | Git, one branch per task, pull request reviewed by a second intern | Catch mistakes early |

Suggested repo layout:

```
lookalike-engine/
  ingest/        importers, GSTIN validator, dedupe
  enrich/        gst.py, places.py, normalize.py
  profile/       lift.py, weights.py, icp_text.py
  score/         collect.py, scorer.py, exclude.py, export.py
  feedback/      outcomes.py, backtest.py
  db/            schema.sql, migrations
  tests/         fixtures with fake businesses
  README.md
```

Never commit API keys or client data to Git. Use a `.env` file that is listed in `.gitignore`, and test only with fake or anonymized fixtures.

## Milestones

Version 1 takes six weeks, and each phase starts only after the previous gate is met.

&#91;embedded content: six-week roadmap · 4 phases, 4 gates\]

The client review gate matters most: if the sales lead does not find at least half of the top 50 worth calling, fix the profile and scoring before building the feedback loop.

## Data protection and scraping rules

Client customer lists contain personal data protected under India's Digital Personal Data Protection Act, so we handle them as the client's processor and follow these rules without exception.

- Use a client's data only for that client. Never reuse one client's customers to score leads for another client.
- Store client data in our database only, encrypted at rest, with access limited to the people working on that client.
- Collect only the contact fields sales actually needs. Do not store personal phone numbers or emails that are not business contacts.
- Delete a client's data when the contract ends or when they ask.
- Respect each website's terms of service and robots.txt. Prefer official APIs. If a source forbids automated access, collect from it by hand or not at all.
- Rate-limit every lookup and cache results so we never hammer a public portal.
- No WhatsApp or email outreach from this system. The engine produces leads; contacting people is a separate, opt-in process.

If you are unsure whether something is allowed, stop and ask before you build it. A signed data processing agreement with each client comes before any import.

## Definition of done and reporting

Version 1 is done when one real client's sales lead reviews our top 50 leads, agrees at least half are worth calling, and the backtest shows hidden past customers appearing in our top results.

Weekly reporting, every Friday, one short note per intern:

- What shipped this week (link the pull request).
- One number that moved: businesses enriched, enrichment cost per business, backtest hit rate, or sales acceptance rate.
- What is blocked and what you need.

Do not build yet:

- Automated outreach on WhatsApp or email.
- Machine learning models. Wait until we have at least a few hundred won and lost outcomes.
- Multi-agent research pipelines. A single LLM call per brief is enough.
- Dashboards beyond the lead review page.
- Support for many cities or industries at once. One city, up to 3 industries, until the scores are trusted.
