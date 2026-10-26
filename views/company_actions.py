"""
Everything you can DO to one company, in one module - used by the Company Intelligence dossier (Actions tab and
header) and by the Shortlist Gate page, so a crawl button or the AI report never exists in two diverging copies.

  render_shortlist_controls   status select + save (candidate / shortlisted / in pilot / rejected)
  render_api_and_crawler_row  Sync Live APIs + Run Deep Crawlers (the Phase 7 background queue)
  render_standard_and_deep_dive   standard Phase 7 crawl + the single-company deep-dive website crawl
  render_ai_report            the cited AI Intelligence Report (generate / refresh / read)
  render_paid_pulls           Phase 3 Bundesanzeiger / Phase 5 Kununu (Germany-only, kept for a future German cohort)

Moved verbatim from views/company_detail.py and views/paid_shortlist_gate.py; only the widget keys carry a
caller-supplied prefix so two callers can never collide.
"""

from datetime import datetime

import streamlit as st
from sqlalchemy.orm import Session

import company_brief
import config
from config import BUNDESANZEIGER_PAID_ENABLED, KUNUNU_RESELLER_ENABLED
from models import Company, RawImportRecord, SHORTLIST_STATUSES, SHORTLIST_STAGES, set_shortlist_status, stage_label

# Kept importable under the old name; the labels now come from the one stage table in models.py.
SHORTLIST_STATUS_LABELS = {s: stage_label(s) for s in SHORTLIST_STATUSES}


def render_shortlist_controls(db: Session, company: Company, key: str = "shortlist_status_select") -> None:
    """The one place a single company's stage is changed (the Shortlist section changes several at once)."""
    new_status = st.selectbox(
        "Stage", SHORTLIST_STATUSES,
        index=SHORTLIST_STATUSES.index(company.shortlist_status),
        format_func=stage_label, key=key,
        help="\n\n".join(f"**{label}** - {meaning}" for label, meaning in SHORTLIST_STAGES.values()),
    )
    st.caption(SHORTLIST_STAGES[new_status][1])
    if st.button("💾 Update Stage", use_container_width=True, disabled=(new_status == company.shortlist_status),
                 key=f"{key}_save"):
        set_shortlist_status(company, new_status)
        db.commit()
        st.success(f"{company.legal_name} is now **{stage_label(new_status)}**.")
        st.rerun()


def render_api_and_crawler_row(db: Session, company: Company, key: str = "act") -> None:
    from company_service import eligible_phase7_sources, sync_company_applicable_sources
    eligible = eligible_phase7_sources(company)
    c1, c2 = st.columns(2)
    with c1:
        if st.button("⚡ Sync Live APIs", use_container_width=True, key=f"{key}_sync",
                     help=f"Run applicable APIs for {company.country}"):
            with st.spinner(f"Syncing applicable APIs for {company.legal_name} ({company.country})..."):
                sync_company_applicable_sources(company, db, phases=[1, 4])
            st.success(f"Synced applicable APIs for {company.legal_name}!")
            st.rerun()
    with c2:
        if st.button("🕸️ Run Deep Crawlers", use_container_width=True, key=f"{key}_crawl",
                     # Count, not a hand-maintained list of names: Phase 7 has grown twice already
                     # (TED/FDA, then Competitor Benchmark) and this text went stale both times.
                     # eligible_phase7_sources already reflects exactly what will run for THIS
                     # company (e.g. no Job Postings/Digital Maturity without a website_url).
                     help=f"Phase 7 — {len(eligible)} source(s) applicable to this company (site crawl, "
                          "product catalog, jobs, reviews, news, directories, innovation participation, "
                          "digital maturity incl. vision assessment, competitor benchmark, TED public "
                          "contracts, sector-relevant FDA recalls). Takes a few minutes; runs in the "
                          "background — follow it in the widget at the bottom right."):
            _queue_standard_crawl(db, company)


def _queue_standard_crawl(db: Session, company: Company) -> None:
    from views.crawl_widget import queue_crawl
    from views.crawler_setup import resolve_crawl_target
    where = resolve_crawl_target(db)
    if where["ok"]:
        queue_crawl({company.id: company.legal_name}, workers=1, target=where["target"])
        st.rerun()
    else:
        st.error(where["problem"])


def render_standard_and_deep_dive(db: Session, company: Company, key: str = "indepth", show_standard: bool = True) -> None:
    """The standard Phase 7 crawl (skip with show_standard=False when the 'Run Deep Crawlers' button already sits
    on the same page - it queues exactly the same job) and the synchronous deep-dive website crawl."""
    from scrapers import company_website_crawler
    col_c1, col_c2 = st.columns(2) if show_standard else (None, st.container())
    if show_standard:
        with col_c1:
            st.markdown("#### 🕸️ Standard Crawl")
            st.caption("The normal Phase 7 pass (site crawl, products, jobs, reviews, news, directories, "
                       "digital maturity, competitor benchmark, ...) — runs in the background, a few minutes, "
                       "follow it in the widget at the bottom right.")
            if st.button(f"Run standard crawl for {company.legal_name}", use_container_width=True, key=f"{key}_std"):
                _queue_standard_crawl(db, company)

    with col_c2:
        st.markdown("#### 🔬 Deep-Dive Crawl")
        st.caption(f"Re-crawls the company's own website with a much higher budget — "
                   f"~{company_website_crawler.DEEP_MAX_PAGES_PER_DOMAIN} pages instead of the standard 15, "
                   "more concurrent LLM extraction. Takes several minutes; runs synchronously (this page waits "
                   "for it), not in the background queue — a deliberate single-company action, not a batch job.")
        if not config.crawler_llm_settings()["api_key"]:
            st.caption("Configure CRAWLER_LLM_API_KEY to enable this.")
        elif st.button(f"Run deep-dive crawl for {company.legal_name}", use_container_width=True, key=f"{key}_deep"):
            with st.spinner(f"Deep-crawling {company.legal_name}'s website — this takes a few minutes..."):
                result = company_website_crawler.sync_company_website(company, db, deep=True)
            if result["mode"] == "live":
                st.success(f"Deep-dive crawl complete for {company.legal_name}.")
            else:
                st.warning(f"Deep-dive crawl fell back to simulated — see Pipeline Health for the error: "
                           f"{result.get('error', 'unknown')}")
            st.rerun()


def render_ai_report(db: Session, company: Company, key: str = "ai_brief", heading: bool = True) -> None:
    """The cited AI Intelligence Report. Needs crawled evidence; the LLM call only happens on the button."""
    if heading:
        st.markdown("#### ✨ AI Intelligence Report")
    brief_evidence = company_brief.collect_evidence(db, company)
    if not brief_evidence:
        st.info("No crawled evidence on file yet for this company — run a crawl first.")
        return
    st.caption(
        f"{len(brief_evidence)} evidence item(s) collected. An AI-written summary of everything the "
        "crawlers found — website content, digital presence, hiring, directory listings, press, reviews, "
        "and any disclosed parent/group relationship — each claim citing the specific record it came "
        "from. On-demand only (it costs an LLM call); it does not change Need or Readiness scores."
    )
    brief_record = db.query(RawImportRecord).filter_by(
        company_id=company.id, dataset_name="crawler_ai_company_brief").first()
    brief = brief_record.raw_row if brief_record else None
    # A fresh read, not a frozen module attribute - see config.crawler_llm_settings()'s docstring.
    if config.crawler_llm_settings()["api_key"]:
        if st.button("Generate / refresh report", key=f"{key}_{company.id}"):
            try:
                with st.spinner("Reading everything collected for this company..."):
                    brief = company_brief.generate_brief(db, company)
            except Exception as exc:
                st.error(f"Report could not be generated: {exc}")
    else:
        st.caption("Configure CRAWLER_LLM_API_KEY to generate a report from these records.")
    if brief:
        if brief.get("evidence_fingerprint") != company_brief.evidence_fingerprint(brief_evidence):
            st.warning("New source evidence is available. Refresh this report before using it.")
        by_id = {item["id"]: item for item in brief.get("evidence") or []}
        for finding in brief.get("findings") or []:
            refs = []
            for evidence_id in finding.get("evidence_ids") or []:
                item = by_id.get(evidence_id)
                if item:
                    refs.append(f"[{evidence_id}]({item['url']})" if item.get("url") else evidence_id)
            if refs:
                st.markdown(f"- {finding.get('text', '')}  **Sources:** {', '.join(refs)}")
        st.caption("Check the source records before using any claim in outreach. "
                   "Evidence coverage per category is bounded (see company_brief.py).")


def render_paid_pulls(db: Session, company: Company) -> None:
    from scrapers.bundesanzeiger_paid import pull_bundesanzeiger_filing
    from scrapers.kununu_light import pull_kununu_rating
    with st.expander("💳 Germany-only / not yet built for this portfolio (Phase 3 & 5 paid pulls)", expanded=False):
        st.caption("Portfolio is currently 100% Italian — Bundesanzeiger doesn't apply, and Kununu's "
                   "puller was never finished (no company→profile mapping exists). Left here, not removed, "
                   "in case a German cohort is added later.")
        col_p1, col_p2 = st.columns(2)

        with col_p1:
            st.markdown("#### Phase 3: Bundesanzeiger Full Financial Filing")
            st.write("Retrieves full financial statements to calculate margin compression, R&D ratio, and interest coverage.")
            st.write("**Est. Cost:** €5.00 per document pull")
            if not BUNDESANZEIGER_PAID_ENABLED:
                st.caption("🧪 Real filing retrieval isn't built yet (needs a Playwright + pdfplumber pipeline) — this will write a clearly-tagged simulated placeholder and **won't** add to cumulative spend, since no real pull happens.")
            if st.button(f"📥 Pull Bundesanzeiger Filing for {company.legal_name}", use_container_width=True, key=f"paid_bz_{company.id}"):
                with st.spinner("Executing Bundesanzeiger document pull..."):
                    result = pull_bundesanzeiger_filing(company, db)
                    if result["mode"] == "live":
                        st.success(f"Pulled real Bundesanzeiger filing for {company.legal_name}! Incurred €5.00 cost.")
                    else:
                        st.info(f"Simulated placeholder written for {company.legal_name} (real puller not built yet — no cost incurred). See Pipeline Health for details.")
                    st.rerun()

        with col_p2:
            st.markdown("#### Phase 5: Kununu Culture & Reseller API")
            st.write("Retrieves employer ratings and sentiment scores via a compliant reseller.")
            st.write("**Est. Cost:** €2.50 per company query")
            if not KUNUNU_RESELLER_ENABLED:
                st.caption("🧪 No company→Kununu-profile mapping exists yet (Kununu has no public search API) — this will write a clearly-tagged simulated placeholder and **won't** add to cumulative spend, since no real query happens.")
            if st.button(f"📥 Query Kununu Reseller for {company.legal_name}", use_container_width=True, key=f"paid_kn_{company.id}"):
                with st.spinner("Querying Kununu..."):
                    result = pull_kununu_rating(company, db)
                    if result["mode"] == "live":
                        st.success(f"Retrieved real Kununu data for {company.legal_name}! Incurred €2.50 cost.")
                    else:
                        st.info(f"Simulated placeholder written for {company.legal_name} (real puller not built yet — no cost incurred). See Pipeline Health for details.")
                    st.rerun()
