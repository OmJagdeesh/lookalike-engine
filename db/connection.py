"""Database connection and schema initialization helpers."""

from __future__ import annotations

import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def get_database_url(database_url: str | None = None) -> str:
    """Resolve DATABASE_URL from argument or environment (.env loaded)."""
    load_dotenv()
    url = database_url or os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env and configure DATABASE_URL."
        )
    return url


def get_connection(database_url: str | None = None) -> psycopg.Connection:
    """Open a PostgreSQL connection using psycopg."""
    return psycopg.connect(get_database_url(database_url))


def apply_schema(
    database_url: str | None = None,
    schema_path: Path = SCHEMA_PATH,
) -> None:
    """Apply schema.sql idempotently to the target PostgreSQL database."""
    sql = schema_path.read_text(encoding="utf-8")
    with get_connection(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()


def require_signed_dpa(conn: psycopg.Connection, client_id: int) -> None:
    """Verify a client exists and has a signed DPA before importing proprietary data."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT dpa_signed, dpa_signed_at FROM clients WHERE id = %s",
            (client_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"Client id={client_id} does not exist.")
    dpa_signed, dpa_signed_at = row
    if not dpa_signed or dpa_signed_at is None:
        raise PermissionError(
            f"Client id={client_id} does not have a signed DPA. "
            "Complete the onboarding checklist and record DPA signature before importing data."
        )
