"""Stage 3.6a: Ranked lead CSV exporter."""

from __future__ import annotations

import csv
import io
from pathlib import Path

from score.scorer import ScoredLead

EXPORT_COLUMNS: tuple[str, ...] = (
    "rank",
    "score",
    "business_name",
    "gstin",
    "city",
    "industry",
    "reasons",
    "website",
    "business_contact",
)


def leads_to_export_rows(
    leads: list[ScoredLead],
    *,
    top_n: int | None = None,
) -> list[dict[str, object]]:
    """Rank scored leads and convert top N into export-ready dictionaries."""
    ranked = sorted(
        leads,
        key=lambda lead: (-lead.score, -lead.raw_score, lead.business_name.lower()),
    )
    if top_n is not None:
        ranked = ranked[:top_n]

    rows: list[dict[str, object]] = []
    for rank_idx, lead in enumerate(ranked, start=1):
        rows.append(
            {
                "rank": rank_idx,
                "score": lead.score,
                "business_name": lead.business_name,
                "gstin": lead.gstin or "",
                "city": lead.city or "",
                "industry": lead.industry or "",
                "reasons": lead.reasons_summary,
                "website": lead.website or "",
                "business_contact": lead.business_contact or "",
            }
        )
    return rows


def export_leads_csv_string(
    leads: list[ScoredLead],
    *,
    top_n: int | None = None,
) -> str:
    """Render top N ranked leads to a CSV string."""
    rows = leads_to_export_rows(leads, top_n=top_n)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(EXPORT_COLUMNS))
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def export_leads_csv(
    leads: list[ScoredLead],
    output_path: str | Path,
    *,
    top_n: int | None = None,
) -> Path:
    """Write top N ranked leads to a CSV file and return the output path."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(export_leads_csv_string(leads, top_n=top_n), encoding="utf-8")
    return path
