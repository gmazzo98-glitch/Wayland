"""
Pipeline Visibility & Job Orchestration View (Page 1 — leads the nav on purpose).
Section 3 of GG_Dashboard_Technical_Brief.docx

Beyond run/error/cost visibility, this page surfaces one more dimension the
original build didn't have at all: whether each source is actually calling its
real API right now, or quietly running in simulated/demo mode because
credentials aren't configured. Nothing on the other pages should be trusted as
"real" without checking here first.
"""

import streamlit as st
import pandas as pd
from sqlalchemy.orm import Session
from models import SourceHealth, Company
from adapters import epo_ops, euipo, destatis, eu_funding, arbeitsagentur, google_news
from scrapers import handelsregister_free, wappalyzer_local, management_diversity
from config import PHASE_CONFIG, SOURCE_CREDENTIAL_VARS, SOURCE_PAID_ENABLE_FLAGS, has_credentials

MODE_BADGES = {"live": "🟢 Live", "simulated": "🧪 Simulated"}


def _credential_note(source_name: str) -> str:
    if source_name in SOURCE_PAID_ENABLE_FLAGS:
        flag = "BUNDESANZEIGER_PAID_ENABLED" if source_name == "Bundesanzeiger" else "KUNUNU_RESELLER_ENABLED"
        return "Real puller not built yet" if not has_credentials(source_name) else f"{flag}=true"
    required = SOURCE_CREDENTIAL_VARS.get(source_name, [])
    if not required:
        return "None required"
    if has_credentials(source_name):
        return "✅ configured"
    return "Needs: " + ", ".join(required)


def render_pipeline_health_page(db: Session):
    st.title("⚙️ Pipeline Visibility & Source Health")
    st.caption("Control surface for the multi-source ingestion pipeline — rate limits, call counters, error surfacing, live-vs-simulated status, and manual execution controls.")

    sources = db.query(SourceHealth).order_by(SourceHealth.phase, SourceHealth.source_name).all()
    companies = db.query(Company).all()

    # Top-line summary — this is the visibility the ranked view on its own can't give.
    live_count = sum(1 for s in sources if s.mode == "live")
    error_count = sum(1 for s in sources if s.last_status == "error")
    never_run = sum(1 for s in sources if s.last_status in (None, "idle"))
    col_s1, col_s2, col_s3, col_s4 = st.columns(4)
    col_s1.metric("Sources Tracked", len(sources))
    col_s2.metric("Running Live", f"{live_count}/{len(sources)}")
    col_s3.metric("Currently Erroring", error_count)
    col_s4.metric("Never Run", never_run)

    st.markdown("---")

    st.subheader("⚡ Pipeline Trigger Controls")
    col_t1, col_t2, col_t3 = st.columns(3)

    with col_t1:
        if st.button("🚀 Run Phase 1 Free APIs Sync", use_container_width=True):
            with st.spinner("Syncing Phase 1 APIs (EPO, EUIPO, Destatis, EU Funding, Arbeitsagentur)..."):
                for comp in companies:
                    epo_ops.sync_company_patents(comp, db)
                    euipo.sync_company_trademarks(comp, db)
                    destatis.sync_sector_export_exposure(comp, db)
                    eu_funding.sync_company_grants(comp, db)
                    arbeitsagentur.sync_job_velocity(comp, db)
                st.success(f"Phase 1 API sync completed across {len(companies)} companies! Check the mode column below for live vs. simulated.")
                st.rerun()

    with col_t2:
        if st.button("🔍 Run Phase 2 Handelsregister Base", use_container_width=True):
            with st.spinner("Normalizing Handelsregister entity records..."):
                for comp in companies:
                    handelsregister_free.index_handelsregister_snapshot(comp, db)
                st.success("Phase 2 entity-resolution pass complete! (Snapshot event scraping itself — filing types/dates — is still backlog, not yet built.)")
                st.rerun()

    with col_t3:
        if st.button("🌐 Run Phase 4 Website & Social Layer", use_container_width=True):
            with st.spinner("Running Wappalyzer, own-site management scan, and partnership-news search..."):
                for comp in companies:
                    wappalyzer_local.sync_tech_stack(comp, db)
                    management_diversity.sync_management_diversity(comp, db)
                    google_news.sync_partnership_news(comp, db)
                st.success(f"Phase 4 pass complete across {len(companies)} companies!")
                st.rerun()

    st.caption("Phase 3 (Bundesanzeiger) and Phase 5 (Kununu) paid pulls are manual, per-company, and live on the **💰 Shortlist Gate & Paid Pulls** page — never auto-run across the full batch (Section 3 of the Brief).")

    st.markdown("---")

    st.subheader("📡 Ingestion Source Status, Mode & Credentials")

    if sources:
        health_rows = []
        for s in sources:
            phase_info = PHASE_CONFIG.get(s.phase, {"name": f"Phase {s.phase}"})
            last_run_str = s.last_run_at.strftime("%Y-%m-%d %H:%M") if s.last_run_at else "Never"

            health_rows.append({
                "Phase": f"Phase {s.phase}",
                "Source Name": s.source_name,
                "Mode": MODE_BADGES.get(s.mode, s.mode),
                "Run Status": "🟢 Healthy" if s.last_status in ("success", "idle") else ("🔴 Error" if s.last_status == "error" else s.last_status),
                "Credentials": _credential_note(s.source_name),
                "Total Calls": s.total_calls,
                "Estimated Cost": f"€{s.total_cost:.2f}",
                "Last Run": last_run_str,
                "Error Count": s.error_count,
                "Last Error": s.last_error_message or "None",
            })

        df_health = pd.DataFrame(health_rows)

        st.dataframe(
            df_health,
            column_config={
                "Phase": "Phase",
                "Source Name": "Source Name",
                "Mode": "Live / Simulated",
                "Run Status": "Run Status",
                "Credentials": "Credential Status",
                "Total Calls": "API Calls",
                "Estimated Cost": "Cost (€)",
                "Last Run": "Last Run",
                "Error Count": "Errors",
                "Last Error": st.column_config.TextColumn("Last Error Trace", width="large"),
            },
            use_container_width=True,
            hide_index=True,
        )
        st.caption("A source in Simulated mode is not broken — it just doesn't have credentials configured yet (or, for Bundesanzeiger/Kununu, the real puller isn't built). See the Credential Status column, and each adapter module's docstring, for how to enable it.")
    else:
        st.info("No sources registered yet. Run a trigger above to initialize source health tracking.")
