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
from adapters import epo_ops, euipo, eu_funding, arbeitsagentur, google_news
from scrapers import handelsregister_free, wappalyzer_local, management_diversity
from config import PHASE_CONFIG, SOURCE_CREDENTIAL_VARS, SOURCE_PAID_ENABLE_FLAGS, has_credentials

MODE_BADGES = {"live": "🟢 Live", "simulated": "🧪 Simulated"}


def _mode_badge(source: SourceHealth) -> str:
    if source.source_name == "EPO OPS" and source.total_calls == 0 and has_credentials("EPO OPS"):
        return "✅ Configured"
    return MODE_BADGES.get(source.mode, source.mode)


FLAG_ENV_VAR_NAMES = {
    "Bundesanzeiger": "BUNDESANZEIGER_PAID_ENABLED",
    "Kununu Reseller": "KUNUNU_RESELLER_ENABLED",
    "LinkedIn Profile Crawler": "LINKEDIN_CRAWLER_ENABLED",
    "Review Crawler": "REVIEW_CRAWLER_MODE_A_ENABLED",
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

    from crawl_jobs import MAX_WORKERS
    from views.crawl_widget import queue_crawl

    api_workers = st.number_input(
        "Companies in parallel (Phase 1 / Phase 4)", min_value=1, max_value=MAX_WORKERS, value=4, key="api_workers",
        help="Phases 1 and 4 run in the background like the deep crawl: keep working, and follow progress in the "
             "widget at the bottom right. Within each company its sources also run at the same time.")

    col_t1, col_t2, col_t3 = st.columns(3)

    from company_service import sync_company_applicable_sources

    with col_t1:
        if st.button("🚀 Run Phase 1 Free APIs Sync", use_container_width=True):
            # In the background, several companies at once. It used to loop over every company on this
            # page's own thread, one at a time, blocking the whole app behind a spinner — for a full list
            # that is hours, which is why these sources had never been run over the imported companies.
            queue_crawl({c.id: c.legal_name for c in target_companies}, workers=int(api_workers), phases=(1,))
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
            queue_crawl({c.id: c.legal_name for c in target_companies}, workers=int(api_workers), phases=(4,))
            st.rerun()

    st.markdown("&nbsp;")
    st.markdown("**🕸️ Phase 7 — Crawler Deep Enrichment (Node-based, slower)**")
    st.caption(
        "Each of the 8 crawlers under Scraper/crawlers/ spawns its own subprocess per company "
        "(Node/Playwright startup, sometimes an LLM call). A company's crawlers run at the same time, so it "
        "takes as long as its slowest one (about 2 minutes) rather than the sum, and several companies run in "
        "parallel on top of that. What may really run together is capped per resource — 4 headless Chromium "
        "instances, 2 Wayback lookups, and ONE company-website extraction at a time — because the free LLM "
        "tier's tokens-per-minute budget, not parallelism, is what limits that crawler: a second free provider "
        "(CRAWLER_LLM_FALLBACK_API_KEY in .env) is the only way to speed it up further. "
        "Cap the batch size below; to hand-pick companies instead, tick them on **🎯 Scored Target Matrix**. "
        "Crawls run in the background: keep working, and follow progress in the widget at the bottom right of any page."
    )
    col_p7a, col_p7b, col_p7c = st.columns([1, 1, 2])
    with col_p7a:
        p7_limit = st.number_input("Max companies this run", min_value=1, max_value=50, value=10, key="p7_limit")
    with col_p7b:
        p7_workers = st.number_input("Companies in parallel", min_value=1, max_value=MAX_WORKERS, value=3, key="p7_workers")
    with col_p7c:
        st.markdown("&nbsp;")
        if st.button("🕸️ Run Phase 7 Crawler Enrichment", use_container_width=True):
            from views.crawl_widget import queue_crawl
            from views.crawler_setup import resolve_batch_targets
            batch = target_companies[:int(p7_limit)]
            where = resolve_batch_targets(db, [c.id for c in batch])
            if where["ok"]:
                queue_crawl({c.id: c.legal_name for c in batch}, workers=int(p7_workers),
                            targets=where["targets"], target_labels=where["target_labels"])
                st.rerun()
            else:
                st.error(where["problem"])

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
                "Mode": _mode_badge(s),
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
                "Mode": "Source status",
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
        st.caption("Configured means EPO OPS is ready to run. Live means a real pull completed. Check Run Status and Last Error for any failed attempt.")
    else:
        st.info("No sources registered yet. Run a trigger above to initialize source health tracking.")
