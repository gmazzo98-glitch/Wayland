"""
Shortlist: the one page that shows the funnel and moves companies along it.

  candidate -> shortlisted -> in pilot        (parked = ruled out for now, reversible)

Cross-company only. Everything you can DO to one company (crawls, deep-dive, AI report, paid pulls, changing its
own stage) lives in Company Intelligence > Actions, so it exists once; this page ends with a one-click way to open
a shortlisted company's dossier. The stage meanings come from models.SHORTLIST_STAGES, nowhere else.
"""

from collections import defaultdict
import streamlit as st
import pandas as pd
from sqlalchemy.orm import Session
from models import (Company, SignalRecord, SourceHealth, SHORTLIST_STAGES, is_parked, is_shortlisted,
                    set_shortlist_status, stage_label)
from scoring import calculate_company_scores
from indicators import fetch_indicator_defs
from dossier_nav import render_open_dossier_picker
from config import SHORTLIST_MIN_COMPLETENESS_PCT, SHORTLIST_MIN_PRELIMINARY_SCORE


def _plural(n: int) -> str:
    return "company" if n == 1 else "companies"


def render_paid_shortlist_gate_page(db: Session, show_title: bool = True):
    if show_title:
        st.title("⭐ Shortlist")
    st.caption("Every company is in one stage: " + " · ".join(f"**{label}** - {meaning}" for label, meaning in SHORTLIST_STAGES.values())
               + " The in-depth work itself happens in 🏢 Company Intelligence > Actions.")

    companies = db.query(Company).all()
    if not companies:
        st.warning("No companies found.")
        return

    # Aggregate Paid Spend Summary
    sources = db.query(SourceHealth).all()
    total_paid_calls = sum(s.total_calls for s in sources if s.phase in (3, 5))
    total_paid_spend = sum(s.total_cost for s in sources if s.phase in (3, 5))

    counts = {s: sum(1 for c in companies if c.shortlist_status == s) for s in SHORTLIST_STAGES}
    cols = st.columns(len(SHORTLIST_STAGES))
    for col, (status, (label, meaning)) in zip(cols, SHORTLIST_STAGES.items()):
        col.metric(label, f"{counts[status]}/{len(companies)}", help=meaning)
    st.caption(f"Paid document pulls so far: {total_paid_calls} · cumulative paid spend €{total_paid_spend:.2f}")

    indicator_defs = fetch_indicator_defs(db)
    # One query for every signal, grouped here: the old one-query-per-company loop was ~950 round trips to the database.
    signals_by_company = defaultdict(list)
    for sig in db.query(SignalRecord).all():
        signals_by_company[sig.company_id].append(sig)
    gate_rows = []
    for comp in companies:
        scores = calculate_company_scores(signals_by_company.get(comp.id, []), indicator_defs)
        preliminary_score = (scores["need_score"] + scores["readiness_score"]) / 2.0
        recommended = (
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
            "recommended": recommended,
            "shortlist_status": comp.shortlist_status,
            "stage": stage_label(comp.shortlist_status),
            "since": comp.shortlisted_at.strftime("%Y-%m-%d") if comp.shortlisted_at else None,
        })

    score_config = {
        "legal_name": "Company Legal Name",
        "registration_number": "Reg. Number",
        "segment": "Segment",
        "need_score": st.column_config.NumberColumn("Need", format="%.1f"),
        "readiness_score": st.column_config.NumberColumn("Readiness", format="%.1f"),
        "preliminary_score": st.column_config.NumberColumn("Prelim Score", format="%.1f"),
        "completeness_pct": st.column_config.ProgressColumn("Phase 1+2 Completeness", format="%.1f%%", min_value=0, max_value=100),
        "stage": "Stage",
        "since": "Shortlisted on",
    }

    # ---- the shortlist itself --------------------------------------------------------------------------------
    st.markdown("---")
    shortlisted_rows = [r for r in gate_rows if is_shortlisted(r["shortlist_status"])]
    st.subheader(f"⭐ The shortlist ({len(shortlisted_rows)})")
    shortlisted_companies = [c for c in companies if is_shortlisted(c.shortlist_status)]
    if shortlisted_rows:
        st.dataframe(
            pd.DataFrame(shortlisted_rows)[["legal_name", "registration_number", "segment", "stage", "need_score",
                                            "readiness_score", "completeness_pct", "since"]],
            column_config=score_config, use_container_width=True, hide_index=True)

        # Quick removal - the inverse of "Add to shortlist" on Pain Points. Goes back to the pool (candidate);
        # use Park below to rule a company out.
        remove_options = {f"{c.legal_name} ({c.registration_number})": c for c in shortlisted_companies}
        picked_for_removal = st.multiselect("Select companies to remove", list(remove_options), key="shortlist_remove_pick",
                                            help="Removed companies return to Candidate. Use Park (below) to rule one out.")
        if st.button("Remove selected from shortlist", disabled=not picked_for_removal, key="shortlist_remove_btn"):
            removed = [remove_options[label] for label in picked_for_removal]
            for comp in removed:
                set_shortlist_status(comp, "candidate")
            db.commit()
            st.success(f"Removed {len(removed)} {_plural(len(removed))} from the shortlist.")
            st.rerun()

        st.markdown("**🔬 In-depth work on a shortlisted company**")
        st.caption("Crawls, the deep-dive website crawl, the AI report, financials, costs, people and paid pulls are all in the dossier.")
        render_open_dossier_picker({f"{c.legal_name} ({c.registration_number})": c.id for c in shortlisted_companies},
                                   key="gate_open", tab="actions", label="Open a shortlisted company's dossier")
    else:
        st.info("Nobody is shortlisted yet. Promote a recommended candidate below, or add companies from Pain Points.")

    # ---- candidates: who to promote --------------------------------------------------------------------------
    st.markdown("---")
    st.subheader("🚧 Candidates recommended for shortlisting")
    st.caption(f"A recommendation, not a rule: completeness ≥ {SHORTLIST_MIN_COMPLETENESS_PCT:.0f}% and the average of Need and "
               f"Readiness ≥ {SHORTLIST_MIN_PRELIMINARY_SCORE:.0f}. Any company can still be shortlisted from its dossier or Pain Points.")
    promotable = [r for r in gate_rows if r["recommended"] and r["shortlist_status"] == "candidate"]
    if promotable:
        st.dataframe(
            pd.DataFrame(promotable)[["legal_name", "registration_number", "segment", "need_score", "readiness_score",
                                      "preliminary_score", "completeness_pct"]].sort_values("preliminary_score", ascending=False),
            column_config=score_config, use_container_width=True, hide_index=True)
        col_p1, col_p2 = st.columns([3, 1])
        with col_p1:
            promote_options = {f"{r['legal_name']} ({r['registration_number']})": r["id"] for r in promotable}
            promote_label = st.selectbox("Promote a recommended candidate to Shortlisted", list(promote_options.keys()))
        with col_p2:
            st.markdown("&nbsp;")
            if st.button("⬆️ Promote to Shortlisted", use_container_width=True, type="primary"):
                comp = db.query(Company).filter_by(id=promote_options[promote_label]).first()
                set_shortlist_status(comp, "shortlisted")
                db.commit()
                st.success(f"{comp.legal_name} is now shortlisted - paid pulls unlocked in its dossier.")
                st.rerun()
    else:
        st.caption("No recommended candidates awaiting promotion right now.")

    # ---- parked ----------------------------------------------------------------------------------------------
    st.markdown("---")
    parked_companies = [c for c in companies if is_parked(c.shortlist_status)]
    with st.expander(f"🅿️ Parked ({len(parked_companies)})"):
        st.caption(SHORTLIST_STAGES["rejected"][1])
        park_pool = {f"{c.legal_name} ({c.registration_number})": c for c in companies if not is_parked(c.shortlist_status)}
        to_park = st.multiselect("Park companies", list(park_pool), key="shortlist_park_pick")
        if st.button("Park selected", disabled=not to_park, key="shortlist_park_btn"):
            for label in to_park:
                set_shortlist_status(park_pool[label], "rejected")
            db.commit()
            st.success(f"Parked {len(to_park)} {_plural(len(to_park))}.")
            st.rerun()
        reopen_pool = {f"{c.legal_name} ({c.registration_number})": c for c in parked_companies}
        to_reopen = st.multiselect("Reopen parked companies (they return to Candidate)", list(reopen_pool), key="shortlist_reopen_pick")
        if st.button("Reopen selected", disabled=not to_reopen, key="shortlist_reopen_btn"):
            for label in to_reopen:
                set_shortlist_status(reopen_pool[label], "candidate")
            db.commit()
            st.success(f"Reopened {len(to_reopen)} {_plural(len(to_reopen))}.")
            st.rerun()
