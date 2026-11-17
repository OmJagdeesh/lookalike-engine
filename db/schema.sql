-- Lookalike Engine PostgreSQL Schema (Phase 0)
-- Enforces DPDP client data isolation, append-only lead_outcomes, and raw payload caching.

-- =============================================================================
-- 1. Clients (Tenant registry + DPA gate)
-- =============================================================================
CREATE TABLE IF NOT EXISTS clients (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    product_sold TEXT NOT NULL,
    dpa_signed BOOLEAN NOT NULL DEFAULT FALSE,
    dpa_signed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_clients_dpa_timestamp CHECK (NOT dpa_signed OR dpa_signed_at IS NOT NULL)
);

-- =============================================================================
-- 2. Shared Public Entity Tables (NO client-proprietary data)
-- =============================================================================
CREATE TABLE IF NOT EXISTS businesses (
    id BIGSERIAL PRIMARY KEY,
    gstin VARCHAR(15) UNIQUE,
    legal_name TEXT NOT NULL,
    trade_name TEXT,
    city TEXT,
    state TEXT,
    pincode VARCHAR(10),
    source TEXT NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_businesses_gstin_len CHECK (gstin IS NULL OR char_length(gstin) = 15)
);

-- Fallback dedupe index: normalized legal_name + pincode
CREATE INDEX IF NOT EXISTS idx_businesses_name_pincode
    ON businesses (LOWER(TRIM(legal_name)), pincode);

CREATE INDEX IF NOT EXISTS idx_businesses_city
    ON businesses (LOWER(TRIM(city)));

CREATE TABLE IF NOT EXISTS business_features (
    id BIGSERIAL PRIMARY KEY,
    business_id BIGINT NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    industry TEXT,
    business_type TEXT,
    registration_date DATE,
    gst_status TEXT,
    filing_regular BOOLEAN,
    turnover_band TEXT,
    employee_band TEXT,
    google_rating NUMERIC(3, 2),
    review_count INTEGER,
    has_website BOOLEAN,
    website TEXT,
    business_contact TEXT,
    enriched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_google_rating_range CHECK (
        google_rating IS NULL OR (google_rating >= 0 AND google_rating <= 5)
    ),
    CONSTRAINT chk_review_count_nonneg CHECK (
        review_count IS NULL OR review_count >= 0
    )
);

CREATE INDEX IF NOT EXISTS idx_business_features_business_enriched
    ON business_features (business_id, enriched_at DESC);

CREATE INDEX IF NOT EXISTS idx_business_features_industry
    ON business_features (industry);

CREATE TABLE IF NOT EXISTS raw_payloads (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    lookup_key TEXT NOT NULL,
    business_id BIGINT REFERENCES businesses(id) ON DELETE SET NULL,
    payload JSONB NOT NULL,
    http_status INTEGER NOT NULL,
    cost_inr NUMERIC(10, 4) NOT NULL DEFAULT 0,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_raw_payloads_cost_nonneg CHECK (cost_inr >= 0)
);

CREATE INDEX IF NOT EXISTS idx_raw_payloads_source_lookup
    ON raw_payloads (source, lookup_key, fetched_at DESC);

-- =============================================================================
-- 3. Client-Proprietary Tables (Strictly scoped by client_id; never cross-joined)
-- =============================================================================
CREATE TABLE IF NOT EXISTS seed_customers (
    id BIGSERIAL PRIMARY KEY,
    client_id BIGINT NOT NULL REFERENCES clients(id) ON DELETE RESTRICT,
    business_id BIGINT NOT NULL REFERENCES businesses(id) ON DELETE RESTRICT,
    outcome TEXT NOT NULL,
    deal_value_inr NUMERIC(14, 2),
    closed_at DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_seed_customers_outcome CHECK (outcome IN ('won', 'lost', 'churned')),
    CONSTRAINT chk_seed_customers_deal_nonneg CHECK (
        deal_value_inr IS NULL OR deal_value_inr >= 0
    )
);

CREATE INDEX IF NOT EXISTS idx_seed_customers_client_business
    ON seed_customers (client_id, business_id);

CREATE INDEX IF NOT EXISTS idx_seed_customers_client_outcome
    ON seed_customers (client_id, outcome);

CREATE TABLE IF NOT EXISTS profiles (
    id BIGSERIAL PRIMARY KEY,
    client_id BIGINT NOT NULL REFERENCES clients(id) ON DELETE RESTRICT,
    version INTEGER NOT NULL CHECK (version >= 1),
    description_text TEXT NOT NULL,
    feature_weights JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_profiles_client_version UNIQUE (client_id, version)
);

CREATE TABLE IF NOT EXISTS lead_scores (
    id BIGSERIAL PRIMARY KEY,
    profile_id BIGINT NOT NULL REFERENCES profiles(id) ON DELETE RESTRICT,
    business_id BIGINT NOT NULL REFERENCES businesses(id) ON DELETE RESTRICT,
    score INTEGER NOT NULL CHECK (score >= 0 AND score <= 100),
    reasons JSONB NOT NULL,
    scored_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_lead_scores_profile_business UNIQUE (profile_id, business_id)
);

CREATE INDEX IF NOT EXISTS idx_lead_scores_profile_score
    ON lead_scores (profile_id, score DESC);

CREATE TABLE IF NOT EXISTS lead_outcomes (
    id BIGSERIAL PRIMARY KEY,
    lead_score_id BIGINT NOT NULL REFERENCES lead_scores(id) ON DELETE RESTRICT,
    status TEXT NOT NULL,
    notes TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_lead_outcomes_status CHECK (
        status IN ('contacted', 'meeting', 'won', 'lost', 'no response')
    )
);

CREATE INDEX IF NOT EXISTS idx_lead_outcomes_lead_score
    ON lead_outcomes (lead_score_id, updated_at DESC);

-- =============================================================================
-- 4. Append-Only Enforcement on lead_outcomes
-- =============================================================================
CREATE OR REPLACE FUNCTION prevent_lead_outcomes_mutation()
RETURNS TRIGGER AS $$
BEGIN
    -- Allow deletion ONLY inside delete_client_data() DPDP erasure transaction
    IF TG_OP = 'DELETE' AND current_setting('lookalike.allow_erasure', TRUE) = 'true' THEN
        RETURN OLD;
    END IF;

    RAISE EXCEPTION
        'lead_outcomes is append-only: % is forbidden (insert a new outcome row instead)',
        TG_OP
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_lead_outcomes_append_only ON lead_outcomes;
CREATE TRIGGER trg_lead_outcomes_append_only
    BEFORE UPDATE OR DELETE ON lead_outcomes
    FOR EACH ROW
    EXECUTE FUNCTION prevent_lead_outcomes_mutation();

-- =============================================================================
-- 5. DPDP Client Data Erasure Procedure
-- =============================================================================
-- Removes all client-proprietary rows while preserving shared public entity data.
CREATE OR REPLACE FUNCTION delete_client_data(p_client_id BIGINT)
RETURNS JSONB AS $$
DECLARE
    v_outcomes_deleted BIGINT := 0;
    v_scores_deleted BIGINT := 0;
    v_profiles_deleted BIGINT := 0;
    v_seeds_deleted BIGINT := 0;
    v_clients_deleted BIGINT := 0;
BEGIN
    PERFORM set_config('lookalike.allow_erasure', 'true', TRUE);

    DELETE FROM lead_outcomes lo
    USING lead_scores ls
    JOIN profiles p ON p.id = ls.profile_id
    WHERE lo.lead_score_id = ls.id
      AND p.client_id = p_client_id;
    GET DIAGNOSTICS v_outcomes_deleted = ROW_COUNT;

    DELETE FROM lead_scores ls
    USING profiles p
    WHERE ls.profile_id = p.id
      AND p.client_id = p_client_id;
    GET DIAGNOSTICS v_scores_deleted = ROW_COUNT;

    DELETE FROM profiles
    WHERE client_id = p_client_id;
    GET DIAGNOSTICS v_profiles_deleted = ROW_COUNT;

    DELETE FROM seed_customers
    WHERE client_id = p_client_id;
    GET DIAGNOSTICS v_seeds_deleted = ROW_COUNT;

    DELETE FROM clients
    WHERE id = p_client_id;
    GET DIAGNOSTICS v_clients_deleted = ROW_COUNT;

    PERFORM set_config('lookalike.allow_erasure', 'false', TRUE);

    RETURN jsonb_build_object(
        'client_id', p_client_id,
        'lead_outcomes_deleted', v_outcomes_deleted,
        'lead_scores_deleted', v_scores_deleted,
        'profiles_deleted', v_profiles_deleted,
        'seed_customers_deleted', v_seeds_deleted,
        'clients_deleted', v_clients_deleted
    );
END;
$$ LANGUAGE plpgsql;
