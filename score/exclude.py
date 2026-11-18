"""Stage 3.5a: Exclusion filter for existing customers, inactive GSTINs, and duplicates."""

from __future__ import annotations

from dataclasses import dataclass, field

import psycopg

from enrich.pipeline import BusinessFeaturesRecord
from ingest.dedupe import BusinessRecord, normalize_business_name, normalize_pincode

INACTIVE_GST_STATUSES: frozenset[str] = frozenset(
    {"cancelled", "canceled", "suspended", "inactive"}
)


@dataclass(frozen=True)
class ExcludedCandidate:
    """Audit record for a candidate filtered out before scoring."""

    business_id: int | None
    legal_name: str
    reason: str


@dataclass
class ExclusionResult:
    """Filtered eligible candidates and exclusion diagnostics."""

    eligible: list[BusinessRecord] = field(default_factory=list)
    excluded: list[ExcludedCandidate] = field(default_factory=list)


def load_client_seed_business_ids(conn: psycopg.Connection, client_id: int) -> set[int]:
    """Load all business_ids present in `seed_customers` for a single client_id."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT business_id FROM seed_customers WHERE client_id = %s",
            (client_id,),
        )
        return {int(row[0]) for row in cur.fetchall()}


def filter_candidates(
    candidates: list[BusinessRecord],
    *,
    seed_business_ids: set[int],
    features_by_business: dict[int, BusinessFeaturesRecord],
    bypass_business_ids: set[int] | None = None,
) -> ExclusionResult:
    """Exclude existing seed customers, inactive GSTINs, and remaining duplicates."""
    bypass = bypass_business_ids or set()
    result = ExclusionResult()
    seen_ids: set[int] = set()
    seen_gstins: set[str] = set()
    seen_name_pins: set[tuple[str, str]] = set()

    for biz in candidates:
        if biz.id is None:
            result.excluded.append(
                ExcludedCandidate(
                    business_id=None,
                    legal_name=biz.legal_name,
                    reason="missing_business_id",
                )
            )
            continue

        # 1. Existing customer exclusion (unless explicitly bypassed for holdout backtest)
        if biz.id in seed_business_ids and biz.id not in bypass:
            result.excluded.append(
                ExcludedCandidate(
                    business_id=biz.id,
                    legal_name=biz.legal_name,
                    reason="existing_seed_customer",
                )
            )
            continue

        # 2. Inactive / cancelled / suspended GST status exclusion
        feat = features_by_business.get(biz.id)
        if (
            feat
            and feat.gst_status
            and feat.gst_status.strip().lower() in INACTIVE_GST_STATUSES
        ):
            result.excluded.append(
                ExcludedCandidate(
                    business_id=biz.id,
                    legal_name=biz.legal_name,
                    reason=f"inactive_gst_status:{feat.gst_status}",
                )
            )
            continue

        # 3. Remaining duplicate exclusion (business_id, GSTIN, or normalized name + pincode)
        if biz.id in seen_ids:
            result.excluded.append(
                ExcludedCandidate(
                    business_id=biz.id,
                    legal_name=biz.legal_name,
                    reason="duplicate_business_id",
                )
            )
            continue

        norm_gstin = biz.gstin.strip().upper() if biz.gstin else None
        if norm_gstin:
            if norm_gstin in seen_gstins:
                result.excluded.append(
                    ExcludedCandidate(
                        business_id=biz.id,
                        legal_name=biz.legal_name,
                        reason="duplicate_gstin",
                    )
                )
                continue
            seen_gstins.add(norm_gstin)

        norm_name = normalize_business_name(biz.legal_name)
        norm_pin = normalize_pincode(biz.pincode)
        if norm_name and norm_pin:
            name_pin_key = (norm_name, norm_pin)
            if name_pin_key in seen_name_pins:
                result.excluded.append(
                    ExcludedCandidate(
                        business_id=biz.id,
                        legal_name=biz.legal_name,
                        reason="duplicate_name_pincode",
                    )
                )
                continue
            seen_name_pins.add(name_pin_key)

        seen_ids.add(biz.id)
        result.eligible.append(biz)

    return result
