"""DPDP client data erasure helper and CLI."""

from __future__ import annotations

import argparse
import json
from typing import Any

import psycopg

from db.connection import get_connection


def delete_client_data(
    client_id: int,
    conn: psycopg.Connection | None = None,
    database_url: str | None = None,
) -> dict[str, Any]:
    """Delete all client-proprietary rows for client_id via DB procedure.

    Removes rows from lead_outcomes, lead_scores, profiles, seed_customers,
    and clients while leaving shared public entity tables untouched.
    """
    if conn is not None:
        return _run_delete(conn, client_id)

    with get_connection(database_url) as owned_conn:
        result = _run_delete(owned_conn, client_id)
        owned_conn.commit()
        return result


def _run_delete(conn: psycopg.Connection, client_id: int) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT delete_client_data(%s)", (client_id,))
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"delete_client_data({client_id}) returned no result.")
    result = row[0]
    if isinstance(result, str):
        return json.loads(result)
    return dict(result)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Erase all proprietary data for a client (DPDP erasure / contract end)."
    )
    parser.add_argument("--client-id", type=int, required=True, help="Client ID to erase")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm permanent deletion of client-proprietary rows",
    )
    args = parser.parse_args()

    if not args.yes:
        parser.error("Refusing to erase client data without --yes confirmation.")

    summary = delete_client_data(args.client_id)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
