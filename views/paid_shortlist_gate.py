"""
Shortlist Gate & Paid Pull Trigger View (Page 4)
Section 3 & 7 of GG_Dashboard_Technical_Brief.docx & Phase 3/5 of Sourcing Plan
"""

from datetime import datetime
import streamlit as st
import pandas as pd
from sqlalchemy.orm import Session
from models import Company, SignalRecord, SourceHealth
from scoring import calculate_company_scores
from indicators import fetch_indicator_defs
from scrapers.bundesanzeiger_paid import pull_bundesanzeiger_filing
from scrapers.kununu_light import pull_kununu_rating
from config import (
    SHORTLIST_MIN_COMPLETENESS_PCT, SHORTLIST_MIN_PRELIMINARY_SCORE,
    BUNDESANZEIGER_PAID_ENABLED, KUNUNU_RESELLER_ENABLED,
)

def render_paid_shortlist_gate_page(db: Session):
    st.title("💰 Shortlist Gate & Paid Document Triggers")
    st.caption("Enforces the Shortlist Gate — companies must clear Phase 1+2 baseline scores before Phase 3+ paid pulls can be executed.")

    companies = db.query(Company).all()
    if not companies:
        st.warning("No companies found.")
        return

    # Aggregate Paid Spend Summary
    sources = db.query(SourceHealth).all()
    total_paid_calls = sum(s.total_calls for s in sources if s.phase in (3, 5))
    total_paid_spend = sum(s.total_cost for s in sources if s.phase in (3, 5))

    col_g1, col_g2, col_g3 = st.columns(3)
    with col_g1:
        st.metric("Total Paid Document Pulls", total_paid_calls, help="Bundesanzeiger & paid reseller API calls")
    with col_g2:
        st.metric("Cumulative Paid Spend", f"€{total_paid_spend:.2f}", help="Tracks per-document and reseller fees")
    with col_g3:
        shortlisted_count = sum(1 for c in companies if c.shortlist_status in ("shortlisted", "in_pilot"))
        st.metric("Shortlisted Candidates", f"{shortlisted_count}/{len(companies)}", "Gated for Paid Ingestion")

    st.markdown("---")

    # Shortlist Eligibility Gate Table
    st.subheader("🚧 Target Shortlist Qualification Gate")
    st.write("Review candidate companies clearing the Phase 1+2 free data threshold. Promote to shortlist to enable Phase 3+ paid document pulls.")

    indicator_defs = fetch_indicator_defs(db)
    gate_rows = []
    for comp in companies:
        signals = db.query(SignalRecord).filter_by(company_id=comp.id).all()
        scores = calculate_company_scores(signals, indicator_defs)

        preliminary_score = (scores["need_score"] + scores["readiness_score"]) / 2.0
        eligible = (
            scores["total_completeness_pct"] >= SHORTLIST_MIN_COMPLETENESS_PCT and
            preliminary_score >= SHORTLIST_MIN_PRELIMINARY_SCORE
        )

        gate_rows.append({
            "id": comp.id,
            "legal_name": comp.legal_name,
            "registration_number": comp.registration_number,
            "segment": comp.segment,
            "need_score": scores["need_score"],
            "readiness_score": scores["readiness_score"],
            "preliminary_score": round(preliminary_score, 1),
            "completeness_pct": scores["total_completeness_pct"],
            "is_eligible": eligible,
            "shortlist_status": comp.shortlist_status,
        })

    df_gate = pd.DataFrame(gate_rows)

    st.dataframe(
        df_gate.drop(columns=["id"]),
        column_config={
            "legal_name": "Company Legal Name",
            "registration_number": "Handelsregister-Nr.",
            "segment": "Segment",
            "need_score": st.column_config.NumberColumn("Need Score", format="%.1f"),
            "readiness_score": st.column_config.NumberColumn("Readiness Score", format="%.1f"),
            "preliminary_score": st.column_config.NumberColumn("Prelim Score", format="%.1f"),
            "completeness_pct": st.column_config.ProgressColumn("Phase 1+2 Completeness", format="%.1f%%", min_value=0, max_value=100),
            "is_eligible": "Gate Eligible",
            "shortlist_status": "Shortlist Status",
        },
        use_container_width=True,
        hide_index=True
    )

    # Promotion control — nothing else in the app writes shortlist_status to
    # 'shortlisted' at runtime (Company Intelligence can set any status
    # ad hoc, but this is the dedicated gate action tied to eligibility above).
    promotable = [r for r in gate_rows if r["is_eligible"] and r["shortlist_status"] == "candidate"]
    if promotable:
        col_p1, col_p2 = st.columns([3, 1])
        with col_p1:
            promote_options = {f"{r['legal_name']} ({r['registration_number']})": r["id"] for r in promotable}
            promote_label = st.selectbox("Promote an eligible candidate to Shortlisted", list(promote_options.keys()))
        with col_p2:
            st.markdown("&nbsp;")
            if st.button("⬆️ Promote to Shortlisted", use_container_width=True, type="primary"):
                comp = db.query(Company).filter_by(id=promote_options[promote_label]).first()
                comp.shortlist_status = "shortlisted"
                comp.shortlisted_at = datetime.utcnow()
                db.commit()
                st.success(f"{comp.legal_name} is now shortlisted — Phase 3+ paid pulls unlocked below.")
                st.rerun()
    else:
        st.caption("No gate-eligible candidates awaiting promotion right now.")

    st.markdown("---")

    # Manual Paid Pull Execution Form
    st.subheader("💳 Execute Gated Paid Document Pull (Phase 3 & 5)")

    shortlisted_companies = [c for c in companies if c.shortlist_status in ("shortlisted", "in_pilot")]
    if not shortlisted_companies:
        st.info("No companies currently shortlisted. Qualify candidates above first to unlock paid triggers.")
        return

    comp_options = {f"{c.legal_name} ({c.registration_number})": c for c in shortlisted_companies}
    selected_comp_name = st.selectbox("Select Shortlisted Target for Paid Ingestion", list(comp_options.keys()))
    target_comp = comp_options[selected_comp_name]

    col_p1, col_p2 = st.columns(2)

    with col_p1:
        st.markdown("#### Phase 3: Bundesanzeiger Full Financial Filing")
        st.write("Retrieves full financial statements to calculate margin compression, R&D ratio, and interest coverage.")
        st.write("**Est. Cost:** €5.00 per document pull")
        if not BUNDESANZEIGER_PAID_ENABLED:
            st.caption("🧪 Real filing retrieval isn't built yet (needs a Playwright + pdfplumber pipeline) — this will write a clearly-tagged simulated placeholder and **won't** add to cumulative spend, since no real pull happens.")
        if st.button(f"📥 Pull Bundesanzeiger Filing for {target_comp.legal_name}", use_container_width=True):
            with st.spinner("Executing Bundesanzeiger document pull..."):
                result = pull_bundesanzeiger_filing(target_comp, db)
                if result["mode"] == "live":
                    st.success(f"Pulled real Bundesanzeiger filing for {target_comp.legal_name}! Incurred €5.00 cost.")
                else:
                    st.info(f"Simulated placeholder written for {target_comp.legal_name} (real puller not built yet — no cost incurred). See Pipeline Health for details.")
                st.rerun()

    with col_p2:
        st.markdown("#### Phase 5: Kununu Culture & Reseller API")
        st.write("Retrieves employer ratings and sentiment scores via a compliant reseller.")
        st.write("**Est. Cost:** €2.50 per company query")
        if not KUNUNU_RESELLER_ENABLED:
            st.caption("🧪 No company→Kununu-profile mapping exists yet (Kununu has no public search API) — this will write a clearly-tagged simulated placeholder and **won't** add to cumulative spend, since no real query happens.")
        if st.button(f"📥 Query Kununu Reseller for {target_comp.legal_name}", use_container_width=True):
            with st.spinner("Querying Kununu..."):
                result = pull_kununu_rating(target_comp, db)
                if result["mode"] == "live":
                    st.success(f"Retrieved real Kununu data for {target_comp.legal_name}! Incurred €2.50 cost.")
                else:
                    st.info(f"Simulated placeholder written for {target_comp.legal_name} (real puller not built yet — no cost incurred). See Pipeline Health for details.")
                st.rerun()
