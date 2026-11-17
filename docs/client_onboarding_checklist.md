# Client Onboarding & DPDP Compliance Checklist

Complete **every item** below before importing any real client customer list into the Lookalike Engine.

## 1. Signed Data Processing Agreement (DPA)
- [ ] Execute a **signed Data Processing Agreement (DPA)** designating us as the client's data processor under India's Digital Personal Data Protection (DPDP) Act.
- [ ] Record the client in the `clients` table with `dpa_signed = TRUE` and the exact `dpa_signed_at` timestamp (`require_signed_dpa()` blocks imports otherwise).

## 2. Scope Confirmation (v1 Pilot Guardrails)
- [ ] Confirm target **1 city** (pilot default: Pune).
- [ ] Confirm target **up to 3 industries** from the client's won deals or canonical taxonomy.
- [ ] Confirm Google Places API budget cap (`PLACES_MAX_BUDGET_INR`, default ₹2,000 total / ₹5 per business).
- [ ] Collect both **won** deals and **lost / churned** deals so lift scoring has a baseline.

## 3. Data Minimization & Personal Data Rules
- [ ] Strip any personal phone numbers or personal email addresses from the client's export **before** upload. Store only public business contact details needed by sales.
- [ ] Never commit client CSV/XLSX files, exports, or `.env` files to Git (verify `.gitignore` enforcement).
- [ ] Store client data only in the encrypted-at-rest PostgreSQL database with access restricted to team members assigned to that client.

## 4. Client Data Isolation
- [ ] Shared public entity tables (`businesses`, `business_features`, `raw_payloads`) store public/directory attributes only—never client deal values, outcomes, or notes.
- [ ] Client-proprietary tables (`seed_customers`, `profiles`, `lead_scores`, `lead_outcomes`) are strictly scoped by `client_id` and must **never** be joined or reused across clients.

## 5. Scraping, API & Outreach Guardrails
- [ ] Verify every external data source honors its Terms of Service and `robots.txt`; prefer official APIs (Google Places API) and low-volume rate-limited cached lookups.
- [ ] Confirm no automated WhatsApp or email outreach is connected; the engine outputs ranked leads only.

## 6. Offboarding & Erasure Readiness
- [ ] On contract termination or client erasure request, run:
  ```bash
  python -m db.erasure --client-id <CLIENT_ID> --yes
  ```
  which invokes `delete_client_data(client_id)` to remove all proprietary rows (`lead_outcomes`, `lead_scores`, `profiles`, `seed_customers`, `clients`) while leaving shared public business records intact.
