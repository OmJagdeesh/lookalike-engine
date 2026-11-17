"""CSV and Excel seed customer importer with Pydantic validation and de-duplication."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, Protocol

import pandas as pd
import psycopg
import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from db.connection import require_signed_dpa
from ingest.dedupe import (
    BusinessRecord,
    Deduplicator,
    InMemoryBusinessStore,
    MergeEvent,
    PostgresBusinessStore,
    normalize_pincode,
)
from ingest.gstin import validate_gstin

CANONICAL_FIELDS = (
    "name",
    "trade_name",
    "gstin",
    "city",
    "state",
    "pincode",
    "outcome",
    "deal_value_inr",
    "closed_at",
)

# Default lowercase header aliases used when no explicit column_mapping is provided
DEFAULT_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "name": (
        "name",
        "legal_name",
        "business_name",
        "company_name",
        "customer_name",
        "party_name",
        "buyer_name",
    ),
    "trade_name": ("trade_name", "brand_name", "dba"),
    "gstin": ("gstin", "gstin_uin", "gstin/uin", "gst_no", "gst_number", "buyer_gstin"),
    "city": ("city", "town", "district"),
    "state": ("state", "state_name"),
    "pincode": ("pincode", "pin_code", "pin", "postal_code", "zip"),
    "outcome": ("outcome", "deal_status", "status", "stage"),
    "deal_value_inr": (
        "deal_value_inr",
        "deal_value",
        "order_value",
        "invoice_value",
        "amount",
        "value_inr",
    ),
    "closed_at": ("closed_at", "close_date", "invoice_date", "date"),
}

_CURRENCY_CLEAN_RE = re.compile(r"[₹,\s]|^inr\s*", re.IGNORECASE)
_DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d-%b-%Y", "%d %b %Y")


def _clean_str(val: Any) -> str | None:
    if val is None:
        return None
    text = str(val).strip()
    if not text or text.lower() in {"nan", "none", "null", "nat"}:
        return None
    return text


class SeedRowInput(BaseModel):
    """Pydantic row schema for validating a single imported seed customer row."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str
    trade_name: str | None = None
    gstin: str | None = None
    city: str | None = None
    state: str | None = None
    pincode: str | None = None
    outcome: Literal["won", "lost", "churned"]
    deal_value_inr: Decimal | None = None
    closed_at: date | None = None
    match_confidence: Literal["high", "low"] = "high"

    @field_validator("name", mode="before")
    @classmethod
    def _validate_name(cls, v: Any) -> str:
        cleaned = _clean_str(v)
        if not cleaned:
            raise ValueError("Business name is required.")
        return cleaned

    @field_validator("trade_name", "city", "state", mode="before")
    @classmethod
    def _validate_optional_text(cls, v: Any) -> str | None:
        return _clean_str(v)

    @field_validator("pincode", mode="before")
    @classmethod
    def _validate_pincode(cls, v: Any) -> str | None:
        cleaned = _clean_str(v)
        if cleaned is None:
            return None
        normalized = normalize_pincode(cleaned)
        if normalized is None:
            raise ValueError(f"Invalid 6-digit pincode: {cleaned!r}")
        return normalized

    @field_validator("outcome", mode="before")
    @classmethod
    def _validate_outcome(cls, v: Any) -> str:
        cleaned = _clean_str(v)
        if not cleaned:
            raise ValueError("Outcome is required (won, lost, or churned).")
        lowered = cleaned.lower()
        if lowered not in {"won", "lost", "churned"}:
            raise ValueError(
                f"Invalid outcome {cleaned!r}; expected one of: won, lost, churned."
            )
        return lowered

    @field_validator("deal_value_inr", mode="before")
    @classmethod
    def _validate_deal_value(cls, v: Any) -> Decimal | None:
        cleaned = _clean_str(v)
        if cleaned is None:
            return None
        numeric_str = _CURRENCY_CLEAN_RE.sub("", cleaned)
        try:
            amount = Decimal(numeric_str)
        except InvalidOperation as exc:
            raise ValueError(f"Invalid deal_value_inr: {cleaned!r}") from exc
        if amount < 0:
            raise ValueError(f"deal_value_inr cannot be negative: {amount}")
        return amount.quantize(Decimal("0.01"))

    @field_validator("closed_at", mode="before")
    @classmethod
    def _validate_closed_at(cls, v: Any) -> date | None:
        if isinstance(v, datetime):
            return v.date()
        if isinstance(v, date):
            return v
        cleaned = _clean_str(v)
        if cleaned is None:
            return None
        # Strip time suffix if present (e.g., "2025-04-12 00:00:00")
        date_part = cleaned.split(" ")[0].split("T")[0]
        for fmt in _DATE_FORMATS:
            try:
                return datetime.strptime(date_part, fmt).date()
            except ValueError:
                continue
        raise ValueError(f"Invalid closed_at date format: {cleaned!r}")

    @model_validator(mode="after")
    def _validate_gstin_and_minimum_identity(self) -> SeedRowInput:
        if self.gstin is not None:
            cleaned_gstin = _clean_str(self.gstin)
            if cleaned_gstin is None:
                self.gstin = None
            else:
                result = validate_gstin(cleaned_gstin)
                if not result.is_valid:
                    raise ValueError(f"[{result.error_code}] {result.error_message}")
                self.gstin = result.normalized

        if self.gstin is None:
            if not self.city:
                raise ValueError(
                    "Rows without GSTIN must provide both business name and city."
                )
            self.match_confidence = "low"
        else:
            self.match_confidence = "high"

        return self


@dataclass(frozen=True)
class RowError:
    """Detailed validation error for a single input row."""

    row_number: int
    error_code: str
    message: str
    raw_values: dict[str, Any]


@dataclass(frozen=True)
class SeedCustomerRecord:
    """Persisted client-proprietary seed customer entry."""

    id: int | None
    client_id: int
    business_id: int
    outcome: str
    deal_value_inr: Decimal | None
    closed_at: date | None
    match_confidence: str = "high"


@dataclass
class ImportReport:
    """Summary and row-level diagnostics for a seed import run."""

    client_id: int
    total_rows: int = 0
    valid_rows: int = 0
    inserted_businesses: int = 0
    merged_duplicates: int = 0
    inserted_seed_customers: int = 0
    skipped_blank_rows: int = 0
    error_rows: int = 0
    invalid_gstin_count: int = 0
    low_confidence_rows: int = 0
    errors: list[RowError] = field(default_factory=list)
    merge_log: list[MergeEvent] = field(default_factory=list)


class SeedCustomerStore(Protocol):
    """Storage interface for persisting `seed_customers` rows."""

    def insert_seed_customer(self, record: SeedCustomerRecord) -> SeedCustomerRecord: ...


class InMemorySeedStore:
    """In-memory SeedCustomerStore for offline testing and dry runs."""

    def __init__(self) -> None:
        self.records: list[SeedCustomerRecord] = []
        self._next_id = 1

    def insert_seed_customer(self, record: SeedCustomerRecord) -> SeedCustomerRecord:
        stored = SeedCustomerRecord(
            id=self._next_id,
            client_id=record.client_id,
            business_id=record.business_id,
            outcome=record.outcome,
            deal_value_inr=record.deal_value_inr,
            closed_at=record.closed_at,
            match_confidence=record.match_confidence,
        )
        self._next_id += 1
        self.records.append(stored)
        return stored


class PostgresSeedStore:
    """PostgreSQL-backed SeedCustomerStore writing to `seed_customers`."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def insert_seed_customer(self, record: SeedCustomerRecord) -> SeedCustomerRecord:
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO seed_customers (
                    client_id, business_id, outcome, deal_value_inr, closed_at
                )
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    record.client_id,
                    record.business_id,
                    record.outcome,
                    record.deal_value_inr,
                    record.closed_at,
                ),
            )
            row = cur.fetchone()
        assert row is not None
        return SeedCustomerRecord(
            id=row[0],
            client_id=record.client_id,
            business_id=record.business_id,
            outcome=record.outcome,
            deal_value_inr=record.deal_value_inr,
            closed_at=record.closed_at,
            match_confidence=record.match_confidence,
        )


def load_column_mapping(mapping: dict[str, str] | str | Path | None) -> dict[str, str]:
    """Load a canonical-to-source column mapping from a dict or YAML file."""
    if mapping is None:
        return {}
    if isinstance(mapping, (str, Path)):
        raw = yaml.safe_load(Path(mapping).read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise TypeError("Column mapping YAML must decode to a mapping/dictionary.")
        if "columns" in raw and isinstance(raw["columns"], dict):
            raw = raw["columns"]
        return {str(k): str(v) for k, v in raw.items()}
    return {str(k): str(v) for k, v in mapping.items()}


def _resolve_columns(
    headers: list[str],
    explicit_mapping: dict[str, str],
) -> dict[str, str]:
    """Resolve canonical field names -> actual DataFrame column names."""
    header_lookup = {str(col).strip().lower(): str(col) for col in headers}
    resolved: dict[str, str] = {}

    for canonical in CANONICAL_FIELDS:
        if canonical in explicit_mapping:
            target_col = explicit_mapping[canonical]
            if target_col in headers:
                resolved[canonical] = target_col
            elif target_col.strip().lower() in header_lookup:
                resolved[canonical] = header_lookup[target_col.strip().lower()]
            else:
                raise ValueError(
                    f"Mapped column {target_col!r} for field {canonical!r} "
                    f"not found in file headers: {headers}"
                )
            continue

        for alias in DEFAULT_COLUMN_ALIASES.get(canonical, (canonical,)):
            if alias in header_lookup:
                resolved[canonical] = header_lookup[alias]
                break

    return resolved


def _read_tabular_file(file_path: Path, sheet_name: str | int = 0) -> pd.DataFrame:
    suffix = file_path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(file_path, dtype=str, keep_default_na=False)
    if suffix in {".xlsx", ".xlsm"}:
        return pd.read_excel(
            file_path, sheet_name=sheet_name, dtype=str, keep_default_na=False, engine="openpyxl"
        )
    raise ValueError(f"Unsupported file format {suffix!r}; expected .csv or .xlsx")


def _extract_gstin_error_code(exc: ValidationError) -> str:
    for err in exc.errors():
        msg = err.get("msg", "")
        for code in (
            "INVALID_LENGTH",
            "INVALID_FORMAT",
            "INVALID_STATE_CODE",
            "INVALID_CHECKSUM",
        ):
            if f"[{code}]" in msg:
                return code
    return "VALIDATION_ERROR"


def import_seed_dataframe(
    df: pd.DataFrame,
    client_id: int,
    *,
    column_mapping: dict[str, str] | str | Path | None = None,
    business_store: InMemoryBusinessStore | PostgresBusinessStore | None = None,
    seed_store: SeedCustomerStore | None = None,
    source: str = "seed_import",
) -> ImportReport:
    """Validate, deduplicate, and import seed customer rows from a DataFrame."""
    explicit_map = load_column_mapping(column_mapping)
    col_map = _resolve_columns(list(df.columns), explicit_map)

    biz_store = business_store if business_store is not None else InMemoryBusinessStore()
    cust_store = seed_store if seed_store is not None else InMemorySeedStore()
    deduper = Deduplicator(biz_store)

    report = ImportReport(client_id=client_id, total_rows=len(df))

    for idx, row in df.iterrows():
        # Spreadsheet row number (1-based header = row 1, first data row = row 2)
        row_number = int(idx) + 2 if isinstance(idx, int) else len(report.errors) + 2
        raw_values = {str(k): _clean_str(v) for k, v in row.to_dict().items()}

        if all(val is None for val in raw_values.values()):
            report.skipped_blank_rows += 1
            continue

        mapped_payload = {
            canonical: row[src_col]
            for canonical, src_col in col_map.items()
            if src_col in row
        }

        try:
            validated = SeedRowInput.model_validate(mapped_payload)
        except ValidationError as exc:
            error_code = _extract_gstin_error_code(exc)
            if error_code != "VALIDATION_ERROR":
                report.invalid_gstin_count += 1
            messages = "; ".join(err["msg"] for err in exc.errors())
            report.error_rows += 1
            report.errors.append(
                RowError(
                    row_number=row_number,
                    error_code=error_code,
                    message=messages,
                    raw_values=raw_values,
                )
            )
            continue

        report.valid_rows += 1
        if validated.match_confidence == "low":
            report.low_confidence_rows += 1

        upsert_res = deduper.upsert(
            BusinessRecord(
                gstin=validated.gstin,
                legal_name=validated.name,
                trade_name=validated.trade_name,
                city=validated.city,
                state=validated.state,
                pincode=validated.pincode,
                source=source,
            )
        )
        if upsert_res.created:
            report.inserted_businesses += 1
        else:
            report.merged_duplicates += 1

        cust_store.insert_seed_customer(
            SeedCustomerRecord(
                id=None,
                client_id=client_id,
                business_id=upsert_res.business_id,
                outcome=validated.outcome,
                deal_value_inr=validated.deal_value_inr,
                closed_at=validated.closed_at,
                match_confidence=validated.match_confidence,
            )
        )
        report.inserted_seed_customers += 1

    report.merge_log = list(deduper.merge_log)
    return report


def import_seed_file(
    file_path: str | Path,
    client_id: int,
    *,
    conn: psycopg.Connection | None = None,
    column_mapping: dict[str, str] | str | Path | None = None,
    sheet_name: str | int = 0,
    business_store: InMemoryBusinessStore | PostgresBusinessStore | None = None,
    seed_store: SeedCustomerStore | None = None,
    source: str | None = None,
) -> ImportReport:
    """Import a .csv or .xlsx seed customer file into PostgreSQL or an in-memory store.

    When `conn` is provided, verifies that the client has a signed DPA before importing.
    """
    path = Path(file_path)
    df = _read_tabular_file(path, sheet_name=sheet_name)

    if conn is not None:
        require_signed_dpa(conn, client_id)
        biz_store = business_store or PostgresBusinessStore(conn)
        cust_store = seed_store or PostgresSeedStore(conn)
    else:
        biz_store = business_store
        cust_store = seed_store

    return import_seed_dataframe(
        df,
        client_id,
        column_mapping=column_mapping,
        business_store=biz_store,
        seed_store=cust_store,
        source=source or f"file:{path.name}",
    )
