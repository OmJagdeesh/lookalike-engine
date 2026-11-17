"""Business name normalizer and deterministic two-tier de-duplication."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

import psycopg

# Legal entity suffixes stripped from the end of normalized names (longest first)
_LEGAL_SUFFIXES = (
    "limited liability partnership",
    "private limited",
    "one person company",
    "public limited",
    "pvt limited",
    "incorporated",
    "corporation",
    "pvt ltd",
    "p ltd",
    "limited",
    "ltd",
    "llp",
    "opc",
    "inc",
    "llc",
    "corp",
)

_PUNCT_RE = re.compile(r"[^a-z0-9\s]")
_SPACE_RE = re.compile(r"\s+")
_PINCODE_RE = re.compile(r"^\d{6}$")


def normalize_business_name(name: str | None) -> str:
    """Normalize a business name for deterministic fallback matching.

    Lowercases, converts '&' to 'and', removes punctuation/extra whitespace,
    and strips trailing legal entity suffixes (e.g., Pvt Ltd, Private Limited, LLP, Ltd).
    """
    if not name:
        return ""

    text = str(name).strip().lower()
    text = text.replace("&", " and ")
    text = _PUNCT_RE.sub(" ", text)
    text = _SPACE_RE.sub(" ", text).strip()

    # Iteratively strip trailing legal suffixes (handles cases like "X Pvt. Ltd.")
    changed = True
    while changed and text:
        changed = False
        for suffix in _LEGAL_SUFFIXES:
            if text == suffix:
                break
            tail = f" {suffix}"
            if text.endswith(tail):
                text = text[: -len(tail)].strip()
                changed = True
                break

    return text


def normalize_pincode(pincode: str | float | None) -> str | None:
    """Normalize a 6-digit Indian pincode, handling float artifacts like '411001.0'."""
    if pincode is None:
        return None
    raw = str(pincode).strip()
    if not raw or raw.lower() in {"nan", "none", "null"}:
        return None
    raw = raw.removesuffix(".0")
    digits = re.sub(r"\s+", "", raw)
    if _PINCODE_RE.match(digits):
        return digits
    return None


@dataclass
class BusinessRecord:
    """Canonical public business record matching the `businesses` table."""

    id: int | None = None
    gstin: str | None = None
    legal_name: str = ""
    trade_name: str | None = None
    city: str | None = None
    state: str | None = None
    pincode: str | None = None
    source: str = "csv_import"
    first_seen_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class MergeEvent:
    """Structured audit record for a de-duplication match and merge."""

    tier: str  # "tier1_gstin" or "tier2_name_pincode"
    matched_business_id: int
    incoming_legal_name: str
    incoming_gstin: str | None
    match_key: str
    fields_filled: tuple[str, ...] = ()


@dataclass(frozen=True)
class UpsertOutcome:
    """Result of upserting one business through the deduplicator."""

    business_id: int
    created: bool
    merge_event: MergeEvent | None = None


class BusinessStore(Protocol):
    """Storage interface for two-tier business lookup, insert, and merge."""

    def find_by_gstin(self, gstin: str) -> BusinessRecord | None: ...

    def find_by_name_and_pincode(
        self, normalized_name: str, pincode: str
    ) -> BusinessRecord | None: ...

    def insert_business(self, record: BusinessRecord) -> BusinessRecord: ...

    def update_business_fields(
        self, business_id: int, updates: dict[str, object]
    ) -> BusinessRecord: ...


class InMemoryBusinessStore:
    """In-memory BusinessStore for offline testing and dry-run deduplication."""

    def __init__(self, initial_records: list[BusinessRecord] | None = None) -> None:
        self._records: dict[int, BusinessRecord] = {}
        self._gstin_index: dict[str, int] = {}
        self._name_pin_index: dict[tuple[str, str], int] = {}
        self._next_id = 1

        for rec in initial_records or []:
            self.insert_business(rec)

    @property
    def records(self) -> list[BusinessRecord]:
        return list(self._records.values())

    def find_by_gstin(self, gstin: str) -> BusinessRecord | None:
        biz_id = self._gstin_index.get(gstin.strip().upper())
        return self._records.get(biz_id) if biz_id is not None else None

    def find_by_name_and_pincode(
        self, normalized_name: str, pincode: str
    ) -> BusinessRecord | None:
        if not normalized_name or not pincode:
            return None
        biz_id = self._name_pin_index.get((normalized_name, pincode))
        return self._records.get(biz_id) if biz_id is not None else None

    def insert_business(self, record: BusinessRecord) -> BusinessRecord:
        biz_id = record.id if record.id is not None else self._next_id
        self._next_id = max(self._next_id, biz_id + 1)

        stored = BusinessRecord(
            id=biz_id,
            gstin=record.gstin.strip().upper() if record.gstin else None,
            legal_name=record.legal_name.strip(),
            trade_name=record.trade_name.strip() if record.trade_name else None,
            city=record.city.strip() if record.city else None,
            state=record.state.strip() if record.state else None,
            pincode=normalize_pincode(record.pincode),
            source=record.source,
            first_seen_at=record.first_seen_at,
        )
        self._records[biz_id] = stored
        self._index_record(stored)
        return stored

    def update_business_fields(
        self, business_id: int, updates: dict[str, object]
    ) -> BusinessRecord:
        existing = self._records[business_id]
        for key, value in updates.items():
            setattr(existing, key, value)
        self._index_record(existing)
        return existing

    def _index_record(self, record: BusinessRecord) -> None:
        assert record.id is not None
        if record.gstin:
            self._gstin_index[record.gstin] = record.id
        norm_name = normalize_business_name(record.legal_name)
        if norm_name and record.pincode:
            self._name_pin_index.setdefault((norm_name, record.pincode), record.id)


class PostgresBusinessStore:
    """PostgreSQL-backed BusinessStore operating on the `businesses` table."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def find_by_gstin(self, gstin: str) -> BusinessRecord | None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, gstin, legal_name, trade_name, city, state, pincode, source, first_seen_at
                FROM businesses
                WHERE gstin = %s
                """,
                (gstin.strip().upper(),),
            )
            row = cur.fetchone()
        return self._row_to_record(row) if row else None

    def find_by_name_and_pincode(
        self, normalized_name: str, pincode: str
    ) -> BusinessRecord | None:
        if not normalized_name or not pincode:
            return None
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, gstin, legal_name, trade_name, city, state, pincode, source, first_seen_at
                FROM businesses
                WHERE pincode = %s
                ORDER BY first_seen_at ASC, id ASC
                """,
                (pincode,),
            )
            rows = cur.fetchall()
        for row in rows:
            rec = self._row_to_record(row)
            if normalize_business_name(rec.legal_name) == normalized_name:
                return rec
        return None

    def insert_business(self, record: BusinessRecord) -> BusinessRecord:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO businesses (
                    gstin, legal_name, trade_name, city, state, pincode, source, first_seen_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, gstin, legal_name, trade_name, city, state, pincode, source, first_seen_at
                """,
                (
                    record.gstin.strip().upper() if record.gstin else None,
                    record.legal_name.strip(),
                    record.trade_name.strip() if record.trade_name else None,
                    record.city.strip() if record.city else None,
                    record.state.strip() if record.state else None,
                    normalize_pincode(record.pincode),
                    record.source,
                    record.first_seen_at,
                ),
            )
            row = cur.fetchone()
        assert row is not None
        return self._row_to_record(row)

    def update_business_fields(
        self, business_id: int, updates: dict[str, object]
    ) -> BusinessRecord:
        allowed_cols = {"gstin", "trade_name", "city", "state", "pincode", "first_seen_at"}
        filtered = {k: v for k, v in updates.items() if k in allowed_cols}
        if not filtered:
            existing = self._get_by_id(business_id)
            assert existing is not None
            return existing

        set_clauses = ", ".join(f"{col} = %s" for col in filtered)
        values = list(filtered.values()) + [business_id]
        with self._conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE businesses
                SET {set_clauses}
                WHERE id = %s
                RETURNING id, gstin, legal_name, trade_name, city, state, pincode, source, first_seen_at
                """,
                values,
            )
            row = cur.fetchone()
        assert row is not None
        return self._row_to_record(row)

    def _get_by_id(self, business_id: int) -> BusinessRecord | None:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, gstin, legal_name, trade_name, city, state, pincode, source, first_seen_at
                FROM businesses
                WHERE id = %s
                """,
                (business_id,),
            )
            row = cur.fetchone()
        return self._row_to_record(row) if row else None

    @staticmethod
    def _row_to_record(row: tuple) -> BusinessRecord:
        return BusinessRecord(
            id=row[0],
            gstin=row[1],
            legal_name=row[2],
            trade_name=row[3],
            city=row[4],
            state=row[5],
            pincode=row[6],
            source=row[7],
            first_seen_at=row[8],
        )


class Deduplicator:
    """Deterministic two-tier business deduplicator and merger."""

    def __init__(self, store: BusinessStore) -> None:
        self.store = store
        self.merge_log: list[MergeEvent] = []

    def upsert(self, incoming: BusinessRecord) -> UpsertOutcome:
        """Match incoming business by GSTIN (Tier 1) then name+pincode (Tier 2), or insert."""
        gstin = incoming.gstin.strip().upper() if incoming.gstin else None
        pincode = normalize_pincode(incoming.pincode)
        norm_name = normalize_business_name(incoming.legal_name)

        matched: BusinessRecord | None = None
        tier: str | None = None
        match_key: str = ""

        # Tier 1: exact GSTIN
        if gstin:
            matched = self.store.find_by_gstin(gstin)
            if matched is not None:
                tier = "tier1_gstin"
                match_key = gstin

        # Tier 2: normalized legal_name + 6-digit pincode
        if matched is None and norm_name and pincode:
            matched = self.store.find_by_name_and_pincode(norm_name, pincode)
            if matched is not None:
                tier = "tier2_name_pincode"
                match_key = f"{norm_name}|{pincode}"

        if matched is None:
            inserted = self.store.insert_business(
                BusinessRecord(
                    gstin=gstin,
                    legal_name=incoming.legal_name,
                    trade_name=incoming.trade_name,
                    city=incoming.city,
                    state=incoming.state,
                    pincode=pincode,
                    source=incoming.source,
                    first_seen_at=incoming.first_seen_at,
                )
            )
            assert inserted.id is not None
            return UpsertOutcome(business_id=inserted.id, created=True)

        assert matched.id is not None
        assert tier is not None

        updates: dict[str, object] = {}
        fields_filled: list[str] = []

        # Keep earliest first_seen_at
        if incoming.first_seen_at < matched.first_seen_at:
            updates["first_seen_at"] = incoming.first_seen_at
            fields_filled.append("first_seen_at")

        # Fill null attributes only (never overwrite existing/verified values)
        candidate_fills = {
            "gstin": gstin,
            "trade_name": incoming.trade_name.strip() if incoming.trade_name else None,
            "city": incoming.city.strip() if incoming.city else None,
            "state": incoming.state.strip() if incoming.state else None,
            "pincode": pincode,
        }
        for attr, new_val in candidate_fills.items():
            if getattr(matched, attr) in (None, "") and new_val not in (None, ""):
                updates[attr] = new_val
                fields_filled.append(attr)

        if updates:
            self.store.update_business_fields(matched.id, updates)

        event = MergeEvent(
            tier=tier,
            matched_business_id=matched.id,
            incoming_legal_name=incoming.legal_name.strip(),
            incoming_gstin=gstin,
            match_key=match_key,
            fields_filled=tuple(fields_filled),
        )
        self.merge_log.append(event)
        return UpsertOutcome(
            business_id=matched.id,
            created=False,
            merge_event=event,
        )
