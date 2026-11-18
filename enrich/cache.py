"""Caching HTTP client backed by `raw_payloads` with per-source rate limiting."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol, Self

import httpx
import psycopg


@dataclass(frozen=True)
class RawPayloadRecord:
    """Stored raw response row matching the `raw_payloads` table."""

    id: int | None
    source: str
    lookup_key: str
    business_id: int | None
    payload: dict[str, Any] | list[Any]
    http_status: int
    cost_inr: Decimal
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class CachedHttpResponse:
    """Result returned by CachedHttpClient."""

    source: str
    lookup_key: str
    payload: dict[str, Any] | list[Any]
    http_status: int
    cost_inr: Decimal
    from_cache: bool
    fetched_at: datetime


class RawPayloadStore(Protocol):
    """Storage interface for reading and persisting `raw_payloads`."""

    def get_latest(self, source: str, lookup_key: str) -> RawPayloadRecord | None: ...

    def save(self, record: RawPayloadRecord) -> RawPayloadRecord: ...

    def sum_cost_inr(
        self, source: str | None = None, business_id: int | None = None
    ) -> Decimal: ...


class InMemoryRawPayloadStore:
    """In-memory RawPayloadStore for offline tests and dry runs."""

    def __init__(self) -> None:
        self._rows: list[RawPayloadRecord] = []
        self._next_id = 1

    @property
    def records(self) -> list[RawPayloadRecord]:
        return list(self._rows)

    def get_latest(self, source: str, lookup_key: str) -> RawPayloadRecord | None:
        for row in reversed(self._rows):
            if row.source == source and row.lookup_key == lookup_key:
                return row
        return None

    def save(self, record: RawPayloadRecord) -> RawPayloadRecord:
        stored = RawPayloadRecord(
            id=self._next_id,
            source=record.source,
            lookup_key=record.lookup_key,
            business_id=record.business_id,
            payload=record.payload,
            http_status=record.http_status,
            cost_inr=Decimal(record.cost_inr),
            fetched_at=record.fetched_at,
        )
        self._next_id += 1
        self._rows.append(stored)
        return stored

    def sum_cost_inr(
        self, source: str | None = None, business_id: int | None = None
    ) -> Decimal:
        total = Decimal(0)
        for row in self._rows:
            if source is not None and row.source != source:
                continue
            if business_id is not None and row.business_id != business_id:
                continue
            total += row.cost_inr
        return total


class PostgresRawPayloadStore:
    """PostgreSQL-backed RawPayloadStore operating on `raw_payloads`."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def get_latest(self, source: str, lookup_key: str) -> RawPayloadRecord | None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, source, lookup_key, business_id, payload, http_status, cost_inr, fetched_at
                FROM raw_payloads
                WHERE source = %s AND lookup_key = %s
                ORDER BY fetched_at DESC, id DESC
                LIMIT 1
                """,
                (source, lookup_key),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return RawPayloadRecord(
            id=row[0],
            source=row[1],
            lookup_key=row[2],
            business_id=row[3],
            payload=row[4],
            http_status=row[5],
            cost_inr=Decimal(str(row[6])),
            fetched_at=row[7],
        )

    def save(self, record: RawPayloadRecord) -> RawPayloadRecord:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO raw_payloads (
                    source, lookup_key, business_id, payload, http_status, cost_inr, fetched_at
                )
                VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s)
                RETURNING id, fetched_at
                """,
                (
                    record.source,
                    record.lookup_key,
                    record.business_id,
                    json.dumps(record.payload),
                    record.http_status,
                    record.cost_inr,
                    record.fetched_at,
                ),
            )
            inserted = cur.fetchone()
        assert inserted is not None
        return RawPayloadRecord(
            id=inserted[0],
            source=record.source,
            lookup_key=record.lookup_key,
            business_id=record.business_id,
            payload=record.payload,
            http_status=record.http_status,
            cost_inr=Decimal(record.cost_inr),
            fetched_at=inserted[1],
        )

    def sum_cost_inr(
        self, source: str | None = None, business_id: int | None = None
    ) -> Decimal:
        clauses: list[str] = []
        params: list[Any] = []
        if source is not None:
            clauses.append("source = %s")
            params.append(source)
        if business_id is not None:
            clauses.append("business_id = %s")
            params.append(business_id)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT COALESCE(SUM(cost_inr), 0) FROM raw_payloads {where_sql}", params)
            row = cur.fetchone()
        assert row is not None
        return Decimal(str(row[0]))


class RateLimiter:
    """Simple per-source minimum-interval rate limiter."""

    def __init__(
        self,
        rates_per_sec: dict[str, float] | None = None,
        default_rate_per_sec: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self._rates = dict(rates_per_sec or {})
        self._default_rate = default_rate_per_sec
        self._clock = clock
        self._sleep = sleep_fn
        self._last_call: dict[str, float] = {}

    def acquire(self, source: str) -> float:
        """Sleep if needed to satisfy the source's requests/sec cap. Returns slept seconds."""
        rate = self._rates.get(source, self._default_rate)
        if rate <= 0:
            return 0.0
        min_interval = 1.0 / rate
        now = self._clock()
        last = self._last_call.get(source)
        slept = 0.0
        if last is not None:
            elapsed = now - last
            if elapsed < min_interval:
                wait = min_interval - elapsed
                self._sleep(wait)
                slept = wait
                now = self._clock()
        self._last_call[source] = now
        return slept


class CachedHttpClient:
    """Shared httpx client that caches every response in `raw_payloads` and rate-limits misses."""

    def __init__(
        self,
        payload_store: RawPayloadStore,
        *,
        http_client: httpx.Client | None = None,
        rate_limiter: RateLimiter | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.payload_store = payload_store
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=timeout_seconds)
        self.rate_limiter = rate_limiter or RateLimiter()
        self.network_calls = 0
        self.cache_hits = 0

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def put_manual_payload(
        self,
        source: str,
        lookup_key: str,
        payload: dict[str, Any] | list[Any],
        *,
        http_status: int = 200,
        business_id: int | None = None,
        cost_inr: Decimal = Decimal(0),
    ) -> RawPayloadRecord:
        """Store a manually imported or pre-fetched payload in `raw_payloads`."""
        return self.payload_store.save(
            RawPayloadRecord(
                id=None,
                source=source,
                lookup_key=lookup_key,
                business_id=business_id,
                payload=payload,
                http_status=http_status,
                cost_inr=cost_inr,
            )
        )

    def get_or_fetch(
        self,
        source: str,
        lookup_key: str,
        method: str,
        url: str,
        *,
        business_id: int | None = None,
        cost_inr: Decimal = Decimal(0),
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        force_refresh: bool = False,
    ) -> CachedHttpResponse:
        """Return cached JSON on hit (0 network calls, 0 cost); otherwise fetch, rate-limit, and persist."""
        if not force_refresh:
            cached = self.payload_store.get_latest(source, lookup_key)
            if cached is not None:
                self.cache_hits += 1
                return CachedHttpResponse(
                    source=cached.source,
                    lookup_key=cached.lookup_key,
                    payload=cached.payload,
                    http_status=cached.http_status,
                    cost_inr=Decimal(0),
                    from_cache=True,
                    fetched_at=cached.fetched_at,
                )

        self.rate_limiter.acquire(source)
        self.network_calls += 1

        try:
            resp = self._client.request(
                method=method.upper(),
                url=url,
                params=params,
                json=json_body,
                headers=headers,
            )
            http_status = resp.status_code
            payload = self._decode_response_payload(resp)
        except httpx.HTTPError as exc:
            http_status = 599
            payload = {"error": type(exc).__name__, "detail": str(exc)}

        stored = self.payload_store.save(
            RawPayloadRecord(
                id=None,
                source=source,
                lookup_key=lookup_key,
                business_id=business_id,
                payload=payload,
                http_status=http_status,
                cost_inr=Decimal(cost_inr),
            )
        )
        return CachedHttpResponse(
            source=stored.source,
            lookup_key=stored.lookup_key,
            payload=stored.payload,
            http_status=stored.http_status,
            cost_inr=stored.cost_inr,
            from_cache=False,
            fetched_at=stored.fetched_at,
        )

    @staticmethod
    def _decode_response_payload(resp: httpx.Response) -> dict[str, Any] | list[Any]:
        try:
            decoded = resp.json()
            if isinstance(decoded, (dict, list)):
                return decoded
            return {"value": decoded}
        except ValueError:
            return {"raw_text": resp.text, "status_code": resp.status_code}
