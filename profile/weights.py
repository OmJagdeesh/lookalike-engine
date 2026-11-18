"""Stage 3.2: Capped log(lift) feature weights and immutable versioned profiles."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from profile.lift import LiftTrait
from typing import Protocol

import psycopg

DEFAULT_W_MAX = 1.5


@dataclass(frozen=True)
class ProfileRecord:
    """Versioned ICP profile row matching the `profiles` table."""

    id: int | None
    client_id: int
    version: int
    description_text: str
    feature_weights: dict[str, float]
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class ProfileStore(Protocol):
    """Storage interface for versioned client ICP profiles."""

    def get_next_version(self, client_id: int) -> int: ...

    def save_profile(
        self,
        client_id: int,
        feature_weights: dict[str, float],
        description_text: str,
    ) -> ProfileRecord: ...

    def get_latest(self, client_id: int) -> ProfileRecord | None: ...

    def get_version(self, client_id: int, version: int) -> ProfileRecord | None: ...

    def list_versions(self, client_id: int) -> list[ProfileRecord]: ...


class InMemoryProfileStore:
    """In-memory ProfileStore for offline tests and backtesting."""

    def __init__(self) -> None:
        self._rows: list[ProfileRecord] = []
        self._next_id = 1

    def get_next_version(self, client_id: int) -> int:
        versions = [r.version for r in self._rows if r.client_id == client_id]
        return (max(versions) + 1) if versions else 1

    def save_profile(
        self,
        client_id: int,
        feature_weights: dict[str, float],
        description_text: str,
    ) -> ProfileRecord:
        version = self.get_next_version(client_id)
        stored = ProfileRecord(
            id=self._next_id,
            client_id=client_id,
            version=version,
            description_text=description_text,
            feature_weights=dict(feature_weights),
        )
        self._next_id += 1
        self._rows.append(stored)
        return stored

    def get_latest(self, client_id: int) -> ProfileRecord | None:
        client_rows = [r for r in self._rows if r.client_id == client_id]
        if not client_rows:
            return None
        return max(client_rows, key=lambda r: r.version)

    def get_version(self, client_id: int, version: int) -> ProfileRecord | None:
        for r in self._rows:
            if r.client_id == client_id and r.version == version:
                return r
        return None

    def list_versions(self, client_id: int) -> list[ProfileRecord]:
        return sorted(
            (r for r in self._rows if r.client_id == client_id),
            key=lambda r: r.version,
        )


class PostgresProfileStore:
    """PostgreSQL-backed ProfileStore operating on the `profiles` table."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def get_next_version(self, client_id: int) -> int:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM profiles WHERE client_id = %s",
                (client_id,),
            )
            row = cur.fetchone()
        assert row is not None
        return int(row[0])

    def save_profile(
        self,
        client_id: int,
        feature_weights: dict[str, float],
        description_text: str,
    ) -> ProfileRecord:
        version = self.get_next_version(client_id)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO profiles (client_id, version, description_text, feature_weights)
                VALUES (%s, %s, %s, %s::jsonb)
                RETURNING id, created_at
                """,
                (client_id, version, description_text, json.dumps(feature_weights)),
            )
            inserted = cur.fetchone()
        assert inserted is not None
        return ProfileRecord(
            id=inserted[0],
            client_id=client_id,
            version=version,
            description_text=description_text,
            feature_weights=dict(feature_weights),
            created_at=inserted[1],
        )

    def get_latest(self, client_id: int) -> ProfileRecord | None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, client_id, version, description_text, feature_weights, created_at
                FROM profiles
                WHERE client_id = %s
                ORDER BY version DESC
                LIMIT 1
                """,
                (client_id,),
            )
            row = cur.fetchone()
        return self._row_to_record(row) if row else None

    def get_version(self, client_id: int, version: int) -> ProfileRecord | None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, client_id, version, description_text, feature_weights, created_at
                FROM profiles
                WHERE client_id = %s AND version = %s
                """,
                (client_id, version),
            )
            row = cur.fetchone()
        return self._row_to_record(row) if row else None

    def list_versions(self, client_id: int) -> list[ProfileRecord]:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, client_id, version, description_text, feature_weights, created_at
                FROM profiles
                WHERE client_id = %s
                ORDER BY version ASC
                """,
                (client_id,),
            )
            rows = cur.fetchall()
        return [self._row_to_record(r) for r in rows]

    @staticmethod
    def _row_to_record(row: tuple) -> ProfileRecord:
        return ProfileRecord(
            id=row[0],
            client_id=row[1],
            version=row[2],
            description_text=row[3],
            feature_weights={str(k): float(v) for k, v in dict(row[4]).items()},
            created_at=row[5],
        )


def compute_feature_weights(
    traits: list[LiftTrait],
    *,
    w_max: float = DEFAULT_W_MAX,
) -> dict[str, float]:
    """Compute capped ln(lift) weights keyed by `feature=value`."""
    if w_max <= 0:
        raise ValueError(f"w_max must be positive, got {w_max}")

    weights: dict[str, float] = {}
    for trait in traits:
        if trait.lift <= 0:
            continue
        raw = math.log(trait.lift)
        capped = max(-w_max, min(w_max, raw))
        weights[trait.key] = round(capped, 4)
    return weights


def create_profile_version(
    store: ProfileStore,
    client_id: int,
    traits: list[LiftTrait],
    description_text: str,
    *,
    w_max: float = DEFAULT_W_MAX,
) -> ProfileRecord:
    """Compute weights from lift traits and persist a new immutable profile version."""
    weights = compute_feature_weights(traits, w_max=w_max)
    return store.save_profile(client_id, weights, description_text)
