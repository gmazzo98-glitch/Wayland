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


FLAG_ENV_VAR_NAMES = {
    "Bundesanzeiger": "BUNDESANZEIGER_PAID_ENABLED",
    "Kununu Reseller": "KUNUNU_RESELLER_ENABLED",
    "LinkedIn Profile Crawler": "LINKEDIN_CRAWLER_ENABLED",
}


def _credential_note(source_name: str) -> str:
    if source_name in SOURCE_PAID_ENABLE_FLAGS:
        flag = FLAG_ENV_VAR_NAMES.get(source_name, "?")
        if has_credentials(source_name):
            return f"{flag}=true"
        return "Real puller not built yet" if source_name == "Bundesanzeiger" else f"Needs: {flag}=true"
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
    
    col_filter, _ = st.columns([2, 2])
    with col_filter:
        country_sync_scope = st.radio("Sync Scope 🌐", ["All Countries", "Germany 🇩🇪 Only", "Italy 🇮🇹 Only"], horizontal=True, key="pipeline_sync_country")

    target_companies = companies
    if country_sync_scope == "Germany 🇩🇪 Only":
        target_companies = [c for c in companies if (c.country or "Germany") == "Germany"]
    elif country_sync_scope == "Italy 🇮🇹 Only":
        target_companies = [c for c in companies if c.country == "Italy"]

    st.caption(f"Triggers below will execute for **{len(target_companies)}** target companies, running only APIs applicable to each company's country.")

    col_t1, col_t2, col_t3 = st.columns(3)

    from company_service import sync_company_applicable_sources

    with col_t1:
        if st.button("🚀 Run Phase 1 Free APIs Sync", use_container_width=True):
            with st.spinner(f"Syncing Phase 1 APIs across {len(target_companies)} companies (activating country-relevant APIs)..."):
                for comp in target_companies:
                    sync_company_applicable_sources(comp, db, phases=[1])
                st.success(f"Phase 1 API sync completed across {len(target_companies)} companies! Check the mode column below.")
                st.rerun()

    with col_t2:
        if st.button("🔍 Run Phase 2 Commercial Register Base", use_container_width=True):
            de_comps = [c for c in target_companies if (c.country or "Germany") == "Germany"]
            with st.spinner(f"Normalizing Handelsregister entity records for {len(de_comps)} German companies..."):
                for comp in de_comps:
                    handelsregister_free.index_handelsregister_snapshot(comp, db)
                st.success(f"Phase 2 pass complete for {len(de_comps)} German companies! (Italian register scraping is backlog).")
                st.rerun()

    with col_t3:
        if st.button("🌐 Run Phase 4 Website & Social Layer", use_container_width=True):
            with st.spinner(f"Running Wappalyzer, own-site scan, and news search for {len(target_companies)} companies..."):
                for comp in target_companies:
                    sync_company_applicable_sources(comp, db, phases=[4])
                st.success(f"Phase 4 pass complete across {len(target_companies)} companies!")
                st.rerun()

    st.markdown("&nbsp;")
    st.markdown("**🕸️ Phase 7 — Crawler Deep Enrichment (Node-based, slower)**")
    st.caption(
        "Each of the 8 crawlers under Scraper/crawlers/ spawns its own subprocess per company "
        "(Node/Playwright startup, sometimes an LLM call) — tens of seconds each, run sequentially. "
        "Cap the batch size below; this is meant for a handful of shortlisted companies at a time, not the full list."
    )
    col_p7a, col_p7b = st.columns([1, 2])
    with col_p7a:
        p7_limit = st.number_input("Max companies this run", min_value=1, max_value=50, value=5, key="p7_limit")
    with col_p7b:
        st.markdown("&nbsp;")
        if st.button("🕸️ Run Phase 7 Crawler Enrichment", use_container_width=True):
            batch = target_companies[:int(p7_limit)]
            with st.spinner(f"Running 8 crawlers for {len(batch)} companies — this can take several minutes..."):
                for comp in batch:
                    sync_company_applicable_sources(comp, db, phases=[7])
                st.success(f"Phase 7 pass complete for {len(batch)} companies! Check the mode column below.")
                st.rerun()

    st.caption(
        "Phase 3 (Bundesanzeiger) and Phase 5 (Kununu) paid pulls are manual, per-company, and live on the "
        "**💰 Shortlist Gate & Paid Pulls** page — never auto-run across the full batch (Section 3 of the Brief). "
        "Phase 6 indicators (org structure, approval chains — anything only a first-contact call can answer) "
        "have no trigger at all; they're recorded by hand on **🏢 Company Intelligence**. "
        "Which indicators matter and how much is tunable on **⚖️ Indicator Weights**."
    )

    st.markdown("---")

    st.subheader("📡 Ingestion Source Status, Mode & Credentials")

    if sources:
        from company_service import GERMAN_ONLY_SOURCES
        health_rows = []
        for s in sources:
            phase_info = PHASE_CONFIG.get(s.phase, {"name": f"Phase {s.phase}"})
            last_run_str = s.last_run_at.strftime("%Y-%m-%d %H:%M") if s.last_run_at else "Never"
            scope_label = "🇩🇪 Germany only" if s.source_name in GERMAN_ONLY_SOURCES else "🌐 EU / Universal"

            health_rows.append({
                "Phase": f"Phase {s.phase}",
                "Source Name": s.source_name,
                "Scope": scope_label,
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
                "Scope": st.column_config.TextColumn("Country Scope", width="medium"),
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
