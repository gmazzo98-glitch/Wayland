"""
Shortlist Gate: which candidates qualify for in-depth work, promote them, take them off again.

Cross-company only. Everything you can DO to one company (crawls, deep-dive, AI report, paid pulls) lives in
Company Intelligence > Actions, so it exists once; this page ends with a one-click way to open a shortlisted
company's dossier.
"""

from collections import defaultdict
from datetime import datetime
import streamlit as st
import pandas as pd
from sqlalchemy.orm import Session
from models import Company, RawImportRecord, SignalRecord, SourceHealth
from scoring import calculate_company_scores
from indicators import fetch_indicator_defs
from dossier_nav import render_open_dossier_picker
from config import SHORTLIST_MIN_COMPLETENESS_PCT, SHORTLIST_MIN_PRELIMINARY_SCORE

def render_paid_shortlist_gate_page(db: Session, show_title: bool = True):
    if show_title:
        st.title("⭐ Shortlist Gate")
    st.caption("Companies must clear the Phase 1+2 baseline before they are shortlisted for in-depth work and Phase 3+ paid pulls. "
               "The in-depth work itself happens in 🏢 Company Intelligence > Actions.")

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
    # One query for every signal, grouped here: the old one-query-per-company loop was ~950 round trips to the database.
    signals_by_company = defaultdict(list)
    for sig in db.query(SignalRecord).all():
        signals_by_company[sig.company_id].append(sig)
    for comp in companies:
        scores = calculate_company_scores(signals_by_company.get(comp.id, []), indicator_defs)

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

    shortlisted_companies = [c for c in companies if c.shortlist_status in ("shortlisted", "in_pilot")]
    if not shortlisted_companies:
        st.info("No companies currently shortlisted. Qualify candidates above first to unlock these actions.")
        return

    # Quick removal — the inverse of the "add to shortlist" action on Pain Points, same
    # shortlist_status field every other shortlisting control in this app already uses. Sets
    # 'candidate' (back to the pool), never 'rejected' — a stronger status this action doesn't claim.
    st.subheader("➖ Remove from Shortlist")
    remove_options = {f"{c.legal_name} ({c.registration_number})": c for c in shortlisted_companies}
    picked_for_removal = st.multiselect("Select companies to remove", list(remove_options), key="shortlist_remove_pick")
    if st.button("Remove selected from shortlist", disabled=not picked_for_removal, key="shortlist_remove_btn"):
        removed = [remove_options[label] for label in picked_for_removal]
        for comp in removed:
            comp.shortlist_status = "candidate"
        db.commit()
        st.success(f"Removed {len(removed)} compan{'y' if len(removed) == 1 else 'ies'} from the shortlist.")
        st.rerun()

    st.markdown("---")

    st.subheader("🔬 In-depth work on a shortlisted company")
    st.caption("Crawls, the deep-dive website crawl, the AI report, financials, costs, people and paid pulls are all in the dossier.")
    render_open_dossier_picker({f"{c.legal_name} ({c.registration_number})": c.id for c in shortlisted_companies},
                               key="gate_open", tab="actions", label="Open a shortlisted company's dossier")
