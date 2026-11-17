"""Phase 0 smoke tests for repository layout, schema definitions, and DB lifecycle."""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import psycopg
import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
SCHEMA_FILE = ROOT_DIR / "db" / "schema.sql"
GITIGNORE_FILE = ROOT_DIR / ".gitignore"


def test_packages_importable() -> None:
    """All top-level stage packages must be importable."""
    for pkg in ("db", "ingest", "enrich", "profile", "score", "feedback", "ui"):
        mod = importlib.import_module(pkg)
        assert mod is not None


def test_schema_contains_core_tables_and_rules() -> None:
    """Verify db/schema.sql defines all 8 tables, key indexes, trigger, and erasure fn."""
    sql = SCHEMA_FILE.read_text(encoding="utf-8")

    expected_objects = [
        "CREATE TABLE IF NOT EXISTS clients",
        "CREATE TABLE IF NOT EXISTS businesses",
        "CREATE TABLE IF NOT EXISTS business_features",
        "CREATE TABLE IF NOT EXISTS raw_payloads",
        "CREATE TABLE IF NOT EXISTS seed_customers",
        "CREATE TABLE IF NOT EXISTS profiles",
        "CREATE TABLE IF NOT EXISTS lead_scores",
        "CREATE TABLE IF NOT EXISTS lead_outcomes",
        "idx_businesses_name_pincode",
        "idx_raw_payloads_source_lookup",
        "trg_lead_outcomes_append_only",
        "delete_client_data(p_client_id BIGINT)",
    ]
    for marker in expected_objects:
        assert marker in sql, f"Missing expected SQL object/marker: {marker}"


def test_gitignore_protects_secrets_and_client_data() -> None:
    """Verify .gitignore excludes .env and client spreadsheets outside tests/fixtures."""
    content = GITIGNORE_FILE.read_text(encoding="utf-8")
    for pattern in (".env", "*.csv", "*.xlsx", "!tests/fixtures/**"):
        assert pattern in content


def _get_test_db_url() -> str | None:
    url = os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not url:
        return None
    try:
        with psycopg.connect(url, connect_timeout=2):
            return url
    except psycopg.OperationalError:
        return None


@pytest.mark.skipif(
    _get_test_db_url() is None,
    reason="No reachable PostgreSQL instance (set TEST_DATABASE_URL or DATABASE_URL)",
)
def test_postgres_schema_append_only_and_erasure() -> None:
    """Verify schema applies cleanly, lead_outcomes is append-only, and erasure isolates public data."""
    from db.connection import apply_schema, get_connection, require_signed_dpa
    from db.erasure import delete_client_data

    db_url = _get_test_db_url()
    assert db_url is not None

    apply_schema(db_url)

    with get_connection(db_url) as conn:
        with conn.cursor() as cur:
            # 1. Unsigned DPA client is rejected by require_signed_dpa
            cur.execute(
                "INSERT INTO clients (name, product_sold, dpa_signed) "
                "VALUES ('Smoke Test Co', 'ERP', FALSE) RETURNING id"
            )
            unsigned_id = cur.fetchone()[0]
            with pytest.raises(PermissionError):
                require_signed_dpa(conn, unsigned_id)

            # 2. Signed DPA client passes gate
            cur.execute(
                "UPDATE clients SET dpa_signed = TRUE, dpa_signed_at = NOW() WHERE id = %s",
                (unsigned_id,),
            )
            require_signed_dpa(conn, unsigned_id)

            # 3. Insert shared public business + proprietary seed, profile, score, outcome
            cur.execute(
                "INSERT INTO businesses (gstin, legal_name, city, state, pincode, source) "
                "VALUES ('27AABCU9603R1ZM', 'Smoke Mfg Pvt Ltd', 'Pune', 'Maharashtra', '411001', 'fixture') "
                "ON CONFLICT (gstin) DO UPDATE SET legal_name = EXCLUDED.legal_name "
                "RETURNING id"
            )
            biz_id = cur.fetchone()[0]

            cur.execute(
                "INSERT INTO seed_customers (client_id, business_id, outcome, deal_value_inr) "
                "VALUES (%s, %s, 'won', 150000)",
                (unsigned_id, biz_id),
            )
            cur.execute(
                "INSERT INTO profiles (client_id, version, description_text, feature_weights) "
                "VALUES (%s, 1, 'Test ICP', '{\"city:Pune\": 0.5}'::jsonb) RETURNING id",
                (unsigned_id,),
            )
            profile_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO lead_scores (profile_id, business_id, score, reasons) "
                "VALUES (%s, %s, 85, '[\"Pune\"]'::jsonb) RETURNING id",
                (profile_id, biz_id),
            )
            score_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO lead_outcomes (lead_score_id, status, notes) "
                "VALUES (%s, 'contacted', 'Initial call') RETURNING id",
                (score_id,),
            )
            outcome_id = cur.fetchone()[0]

            # 4. Append-only rule forbids UPDATE and direct DELETE on lead_outcomes
            cur.execute("SAVEPOINT sp_update")
            with pytest.raises(psycopg.errors.RestrictViolation):
                cur.execute(
                    "UPDATE lead_outcomes SET status = 'won' WHERE id = %s",
                    (outcome_id,),
                )
            cur.execute("ROLLBACK TO SAVEPOINT sp_update")

            cur.execute("SAVEPOINT sp_delete")
            with pytest.raises(psycopg.errors.RestrictViolation):
                cur.execute("DELETE FROM lead_outcomes WHERE id = %s", (outcome_id,))
            cur.execute("ROLLBACK TO SAVEPOINT sp_delete")

        # 5. DPDP erasure deletes proprietary rows and leaves shared business intact
        summary = delete_client_data(unsigned_id, conn=conn)
        assert summary["clients_deleted"] == 1
        assert summary["seed_customers_deleted"] == 1
        assert summary["profiles_deleted"] == 1
        assert summary["lead_scores_deleted"] == 1
        assert summary["lead_outcomes_deleted"] == 1

        with conn.cursor() as cur:
            cur.execute("SELECT id FROM businesses WHERE id = %s", (biz_id,))
            assert cur.fetchone() is not None

        # Roll back test transaction so DB stays clean
        conn.rollback()
