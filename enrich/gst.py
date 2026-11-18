"""Public GST taxpayer lookup (cached & rate-limited) and offline feature extraction."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from enrich.cache import CachedHttpClient, RawPayloadStore
from ingest.dedupe import BusinessRecord, BusinessStore, normalize_pincode
from ingest.gstin import validate_gstin

GST_SOURCE = "gst_public"

_DATE_FORMATS = ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d-%b-%Y")

_BUSINESS_TYPE_MAP: tuple[tuple[str, str], ...] = (
    ("private limited", "Pvt Ltd"),
    ("pvt ltd", "Pvt Ltd"),
    ("pvt. ltd", "Pvt Ltd"),
    ("public limited", "Public Ltd"),
    ("limited liability partnership", "LLP"),
    ("llp", "LLP"),
    ("proprietorship", "Proprietorship"),
    ("sole proprietor", "Proprietorship"),
    ("partnership", "Partnership"),
    ("hindu undivided family", "HUF"),
    ("huf", "HUF"),
    ("one person company", "OPC"),
    ("opc", "OPC"),
    ("government", "Government"),
    ("society", "Society/Trust"),
    ("trust", "Society/Trust"),
)

_GST_STATUS_MAP: dict[str, str] = {
    "active": "Active",
    "act": "Active",
    "cancelled": "Cancelled",
    "canceled": "Cancelled",
    "can": "Cancelled",
    "suspended": "Suspended",
    "sus": "Suspended",
    "inactive": "Inactive",
    "provisional": "Active",
}


@dataclass(frozen=True)
class GstExtractedFeatures:
    """Structured taxpayer attributes extracted from a GST public payload."""

    gstin: str | None
    legal_name: str | None
    trade_name: str | None
    city: str | None
    state: str | None
    pincode: str | None
    business_type: str | None
    registration_date: date | None
    gst_status: str | None
    filing_regular: bool | None
    turnover_band: str | None
    nature_of_business: tuple[str, ...] = ()


def normalize_business_type(raw: str | None) -> str | None:
    if not raw:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    lowered = cleaned.lower()
    for needle, canonical in _BUSINESS_TYPE_MAP:
        if needle in lowered:
            return canonical
    return cleaned


def normalize_gst_status(raw: str | None) -> str | None:
    if not raw:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    return _GST_STATUS_MAP.get(cleaned.lower(), cleaned.title())


def normalize_turnover_band(raw: str | None) -> str | None:
    if not raw:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    lowered = cleaned.lower()
    if lowered in {"<40l", "40l-1.5cr", "1.5cr-5cr", "5cr-25cr", "25cr-100cr", "100cr-500cr", "500cr+"}:
        return cleaned
    if "40 lakh" in lowered and ("up to" in lowered or "upto" in lowered or "<" in lowered or "below" in lowered):
        return "<40L"
    if "40 lakh" in lowered and "1.5 cr" in lowered:
        return "40L-1.5Cr"
    if "1.5 cr" in lowered and "5 cr" in lowered:
        return "1.5Cr-5Cr"
    if "5 cr" in lowered and "25 cr" in lowered:
        return "5Cr-25Cr"
    if "25 cr" in lowered and "100 cr" in lowered:
        return "25Cr-100Cr"
    if "100 cr" in lowered and "500 cr" in lowered:
        return "100Cr-500Cr"
    if "500 cr" in lowered and ("above" in lowered or "more" in lowered or ">" in lowered or "+" in lowered):
        return "500Cr+"
    if "20 lakh" in lowered and ("up to" in lowered or "upto" in lowered):
        return "<40L"
    return cleaned


def _parse_reg_date(raw: Any) -> date | None:
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    if not raw:
        return None
    text = str(raw).strip().split("T")[0].split(" ")[0]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _derive_filing_regular(payload: dict[str, Any]) -> bool | None:
    if isinstance(payload.get("filing_regular"), bool):
        return payload["filing_regular"]

    filings = payload.get("filingStatus") or payload.get("filings")
    if isinstance(filings, list) and filings:
        # Handle nested list of lists (GST portal format sometimes wraps filingStatus in [[...]])
        flat: list[dict[str, Any]] = []
        for item in filings:
            if isinstance(item, list):
                flat.extend(x for x in item if isinstance(x, dict))
            elif isinstance(item, dict):
                flat.append(item)

        if not flat:
            return None

        filed_count = 0
        unfiled_count = 0
        for entry in flat:
            status = str(entry.get("status") or entry.get("mof") or "").strip().lower()
            if not status:
                continue
            if "not filed" in status or "pending" in status or "overdue" in status:
                unfiled_count += 1
            elif "filed" in status:
                filed_count += 1

        if filed_count + unfiled_count == 0:
            return None
        return unfiled_count == 0 and filed_count > 0

    return None


def extract_gst_features_from_payload(
    payload: dict[str, Any],
    *,
    gstin: str | None = None,
) -> GstExtractedFeatures:
    """Extract normalized GST features purely from a stored raw JSON payload (0 network calls)."""
    # Unwrap common {"data": {...}} or {"taxpayerInfo": {...}} envelope if present
    data = payload
    for key in ("data", "taxpayerInfo", "result"):
        if isinstance(payload.get(key), dict):
            data = payload[key]
            break

    addr = (data.get("pradr") or {}).get("addr") if isinstance(data.get("pradr"), dict) else {}
    if not isinstance(addr, dict):
        addr = {}

    raw_nba = data.get("nba") or data.get("nature_of_business") or []
    if isinstance(raw_nba, str):
        nba_list = tuple(x.strip() for x in re.split(r"[,;]", raw_nba) if x.strip())
    elif isinstance(raw_nba, list):
        nba_list = tuple(str(x).strip() for x in raw_nba if str(x).strip())
    else:
        nba_list = ()

    resolved_gstin = (
        gstin
        or data.get("gstin")
        or data.get("Gstin")
        or payload.get("gstin")
    )
    if resolved_gstin:
        resolved_gstin = str(resolved_gstin).strip().upper()

    legal_name = data.get("lgnm") or data.get("legal_name")
    trade_name = data.get("tradeNam") or data.get("trade_name")
    city = data.get("city") or addr.get("dst") or addr.get("loc") or addr.get("city")
    state = data.get("state") or addr.get("stcd") or addr.get("state")
    pincode = normalize_pincode(data.get("pincode") or addr.get("pncd"))

    return GstExtractedFeatures(
        gstin=resolved_gstin,
        legal_name=str(legal_name).strip() if legal_name else None,
        trade_name=str(trade_name).strip() if trade_name else None,
        city=str(city).strip() if city else None,
        state=str(state).strip() if state else None,
        pincode=pincode,
        business_type=normalize_business_type(data.get("ctb") or data.get("business_type")),
        registration_date=_parse_reg_date(data.get("rgdt") or data.get("registration_date")),
        gst_status=normalize_gst_status(data.get("sts") or data.get("gst_status")),
        filing_regular=_derive_filing_regular(data),
        turnover_band=normalize_turnover_band(
            data.get("aggreTurnOver") or data.get("turnover_band")
        ),
        nature_of_business=nba_list,
    )


def import_manual_gst_payload(
    payload_store: RawPayloadStore,
    gstin: str,
    payload: dict[str, Any],
    *,
    business_id: int | None = None,
    http_status: int = 200,
) -> GstExtractedFeatures:
    """Store a manually exported/assisted GST taxpayer JSON in `raw_payloads` and extract features."""
    validation = validate_gstin(gstin)
    if not validation.is_valid:
        raise ValueError(f"Invalid GSTIN {gstin!r}: [{validation.error_code}] {validation.error_message}")

    client = CachedHttpClient(payload_store)
    client.put_manual_payload(
        GST_SOURCE,
        validation.normalized,
        payload,
        http_status=http_status,
        business_id=business_id,
        cost_inr=Decimal(0),
    )
    return extract_gst_features_from_payload(payload, gstin=validation.normalized)


def reextract_gst_features(
    payload_store: RawPayloadStore,
    gstin: str,
) -> GstExtractedFeatures | None:
    """Re-run GST feature extraction purely from `raw_payloads` without external calls."""
    validation = validate_gstin(gstin)
    if not validation.is_valid:
        raise ValueError(f"Invalid GSTIN {gstin!r}: [{validation.error_code}] {validation.error_message}")

    cached = payload_store.get_latest(GST_SOURCE, validation.normalized)
    if cached is None or cached.http_status >= 400 or not isinstance(cached.payload, dict):
        return None
    return extract_gst_features_from_payload(cached.payload, gstin=validation.normalized)


def lookup_gst_taxpayer(
    gstin: str,
    *,
    http_client: CachedHttpClient,
    business_id: int | None = None,
    endpoint_url: str | None = None,
    cost_inr: Decimal = Decimal(0),
    business_store: BusinessStore | None = None,
) -> GstExtractedFeatures | None:
    """Lookup GST taxpayer details via cache-first rate-limited client and optionally backfill `businesses`."""
    validation = validate_gstin(gstin)
    if not validation.is_valid:
        raise ValueError(f"Invalid GSTIN {gstin!r}: [{validation.error_code}] {validation.error_message}")

    norm_gstin = validation.normalized

    # Check cache first so cached entries work even without an endpoint_url configured
    cached = http_client.payload_store.get_latest(GST_SOURCE, norm_gstin)
    if cached is not None:
        http_client.cache_hits += 1
        if cached.http_status >= 400 or not isinstance(cached.payload, dict):
            return None
        features = extract_gst_features_from_payload(cached.payload, gstin=norm_gstin)
    else:
        resolved_url = endpoint_url or os.getenv("GST_LOOKUP_URL")
        if not resolved_url:
            raise RuntimeError(
                f"No cached GST payload for {norm_gstin} and GST_LOOKUP_URL is not configured. "
                "Use import_manual_gst_payload() or configure GST_LOOKUP_URL."
            )
        target_url = resolved_url.format(gstin=norm_gstin)
        resp = http_client.get_or_fetch(
            GST_SOURCE,
            norm_gstin,
            "GET",
            target_url,
            business_id=business_id,
            cost_inr=cost_inr,
        )
        if resp.http_status >= 400 or not isinstance(resp.payload, dict):
            return None
        features = extract_gst_features_from_payload(resp.payload, gstin=norm_gstin)

    if business_store is not None and business_id is not None:
        backfill_business_from_gst(business_store, business_id, features)

    return features


def backfill_business_from_gst(
    business_store: BusinessStore,
    business_id: int,
    features: GstExtractedFeatures,
) -> None:
    """Fill missing `trade_name`, `city`, `state`, `pincode` (and `legal_name` if empty) on `businesses`."""
    existing: BusinessRecord | None = None
    if features.gstin:
        existing = business_store.find_by_gstin(features.gstin)

    updates: dict[str, object] = {}
    if existing is None:
        # Only update fields that are non-null on features when we don't have a record view
        for attr in ("trade_name", "city", "state", "pincode"):
            val = getattr(features, attr)
            if val:
                updates[attr] = val
    else:
        for attr in ("trade_name", "city", "state", "pincode"):
            if getattr(existing, attr) in (None, "") and getattr(features, attr):
                updates[attr] = getattr(features, attr)

    if updates:
        business_store.update_business_fields(business_id, updates)
