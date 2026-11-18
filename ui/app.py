"""Stage 3.6b: Minimal Streamlit lead review page (strictly scoped — no extra dashboards)."""

from __future__ import annotations

from dataclasses import dataclass
from profile.weights import PostgresProfileStore, ProfileRecord
from typing import Any

import pandas as pd
import psycopg

from db.connection import get_connection
from score.export import export_leads_csv_string
from score.scorer import ScoredLead


@dataclass(frozen=True)
class ClientOption:
    id: int
    name: str
    product_sold: str


def load_clients(conn: psycopg.Connection) -> list[ClientOption]:
    """Load available clients from PostgreSQL."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, name, product_sold FROM clients ORDER BY id ASC")
        rows = cur.fetchall()
    return [ClientOption(id=int(r[0]), name=r[1], product_sold=r[2]) for r in rows]


def load_profiles_for_client(
    conn: psycopg.Connection, client_id: int
) -> list[ProfileRecord]:
    """Load all ICP profile versions for the selected client."""
    return PostgresProfileStore(conn).list_versions(client_id)


def load_ranked_leads_for_profile(
    conn: psycopg.Connection, profile_id: int
) -> list[ScoredLead]:
    """Load scored leads for a specific profile_id strictly scoped to that profile."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                ls.id,
                ls.profile_id,
                ls.business_id,
                ls.score,
                ls.reasons,
                b.legal_name,
                b.trade_name,
                b.gstin,
                b.city,
                bf.industry,
                bf.gst_status,
                bf.website,
                bf.business_contact
            FROM lead_scores ls
            JOIN businesses b ON b.id = ls.business_id
            LEFT JOIN LATERAL (
                SELECT industry, gst_status, website, business_contact
                FROM business_features bf_inner
                WHERE bf_inner.business_id = ls.business_id
                ORDER BY bf_inner.enriched_at DESC, bf_inner.id DESC
                LIMIT 1
            ) bf ON TRUE
            WHERE ls.profile_id = %s
            ORDER BY ls.score DESC, b.legal_name ASC
            """,
            (profile_id,),
        )
        rows = cur.fetchall()

    leads: list[ScoredLead] = []
    for r in rows:
        reasons_obj: dict[str, Any] = dict(r[4]) if isinstance(r[4], dict) else {}
        top_traits = tuple(
            str(item.get("label", ""))
            for item in reasons_obj.get("top_traits", [])
            if isinstance(item, dict) and item.get("label")
        )
        leads.append(
            ScoredLead(
                lead_score_id=int(r[0]),
                profile_id=int(r[1]),
                business_id=int(r[2]),
                score=int(r[3]),
                business_name=r[6] or r[5],
                gstin=r[7],
                city=r[8],
                industry=r[9],
                gst_status=r[10],
                website=r[11],
                business_contact=r[12],
                reasons_summary=str(reasons_obj.get("summary", "")),
                top_reasons=top_traits,
                raw_score=float(reasons_obj.get("raw_score", 0.0)),
            )
        )
    return leads


def format_top_traits_dataframe(feature_weights: dict[str, float], top_n: int = 10) -> pd.DataFrame:
    """Format profile feature weights into a clean display table."""
    items = sorted(feature_weights.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n]
    rows = []
    for key, weight in items:
        feat, val = key.split("=", 1) if "=" in key else (key, "")
        rows.append({"Trait": feat.replace("_", " "), "Value": val, "Weight": round(weight, 3)})
    return pd.DataFrame(rows)


def format_leads_dataframe(leads: list[ScoredLead]) -> pd.DataFrame:
    """Format ranked ScoredLead entries into a tabular review DataFrame."""
    rows = []
    for idx, lead in enumerate(leads, start=1):
        rows.append(
            {
                "Rank": idx,
                "Score": lead.score,
                "Business Name": lead.business_name,
                "GSTIN": lead.gstin or "",
                "GST Status": lead.gst_status or "Unknown",
                "City": lead.city or "",
                "Industry": lead.industry or "",
                "Top Reasons": ", ".join(lead.top_reasons) or lead.reasons_summary,
                "Website": lead.website or "",
                "Business Contact": lead.business_contact or "",
            }
        )
    return pd.DataFrame(rows)


def run_streamlit_app() -> None:
    """Render the Streamlit Lead Review Page."""
    import streamlit as st

    st.set_page_config(page_title="Lookalike Engine — Lead Review", layout="wide")
    st.title("Lookalike Engine — Lead Review")

    try:
        conn = get_connection()
    except (RuntimeError, psycopg.Error) as exc:
        st.error(
            f"Database connection not available: {exc}. "
            "Configure DATABASE_URL in .env and apply db/schema.sql."
        )
        return

    with conn:
        clients = load_clients(conn)
        if not clients:
            st.info("No clients found in PostgreSQL yet.")
            return

        client_map = {f"{c.id}: {c.name} ({c.product_sold})": c for c in clients}
        selected_label = st.sidebar.selectbox("Client", list(client_map.keys()))
        selected_client = client_map[selected_label]

        profiles = load_profiles_for_client(conn, selected_client.id)
        if not profiles:
            st.warning(f"No ICP profiles found for client {selected_client.name}.")
            return

        profile_map = {f"v{p.version} ({p.created_at.date()})": p for p in reversed(profiles)}
        selected_profile_label = st.sidebar.selectbox("Profile Version", list(profile_map.keys()))
        selected_profile = profile_map[selected_profile_label]

        assert selected_profile.id is not None
        leads = load_ranked_leads_for_profile(conn, selected_profile.id)

    st.subheader(f"Ideal Customer Profile (v{selected_profile.version})")
    st.info(selected_profile.description_text)

    if selected_profile.feature_weights:
        st.subheader("Top Profile Traits")
        st.dataframe(
            format_top_traits_dataframe(selected_profile.feature_weights),
            use_container_width=True,
            hide_index=True,
        )

    st.subheader(f"Ranked Leads ({len(leads)})")
    if not leads:
        st.info("No scored leads saved for this profile version yet.")
        return

    top_n = st.sidebar.number_input("Top N Leads to Display / Export", min_value=1, max_value=500, value=50)
    visible_leads = leads[: int(top_n)]

    st.dataframe(
        format_leads_dataframe(visible_leads),
        use_container_width=True,
        hide_index=True,
    )

    csv_data = export_leads_csv_string(leads, top_n=int(top_n))
    st.download_button(
        label=f"Download Top {len(visible_leads)} Leads CSV",
        data=csv_data,
        file_name=f"client_{selected_client.id}_profile_v{selected_profile.version}_top_{len(visible_leads)}.csv",
        mime="text/csv",
    )


if __name__ == "__main__":
    run_streamlit_app()
