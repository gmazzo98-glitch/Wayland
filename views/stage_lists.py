"""
My lists: one list per stage (Candidate, Shortlisted, In pilot, Parked) to work through.

Each list is a table you can tick rows in. The same sticky bar as the matrix acts on the ticked rows: crawl them (one
or many), set their stage, or take them off the list. Open any company's dossier from the picker under the table.
The selection is shared with the matrix, so ticks made there carry over here.
"""

import pandas as pd
import streamlit as st
from sqlalchemy.orm import Session

from dossier_nav import render_open_dossier_picker
from models import SHORTLIST_STAGES, SHORTLIST_STATUSES, stage_label
from views.stage_controls import apply_stage
from views.target_matrix import (SELECTION_KEY, _SELECT_BAR_CSS, _render_crawl_bar, _selectable_table, matrix_frame)
from views.crawl_widget import flash

STAGE_KEY = "lists_stage"
SEARCH_KEY = "lists_search"
# What "take off this list" means per stage: back to the pool, except the pool itself, where it means parking.
LEAVE_TO = {"shortlisted": "candidate", "in_pilot": "candidate", "rejected": "candidate", "candidate": "rejected"}
LEAVE_LABEL = {"shortlisted": "Remove from shortlist", "in_pilot": "Remove from shortlist",
               "rejected": "Reopen (back to Candidate)", "candidate": "Park selected"}


def list_counts(df: pd.DataFrame) -> dict:
    return {s: int((df["stage_key"] == s).sum()) for s in SHORTLIST_STATUSES}


def list_frame(df: pd.DataFrame, stage: str, search: str = "") -> pd.DataFrame:
    """The companies in one stage, best Need first, optionally narrowed by a name / registration number search."""
    out = df[df["stage_key"] == stage]
    text = (search or "").strip().lower()
    if text:
        hit = out["legal_name"].str.lower().str.contains(text, na=False, regex=False) | \
              out["registration_number"].str.lower().str.contains(text, na=False, regex=False)
        out = out[hit]
    return out.sort_values(["need_score", "readiness_score"], ascending=False, kind="stable").reset_index(drop=True)


def render_stage_lists(db: Session, show_title: bool = True) -> None:
    if show_title:
        st.title("📋 My lists")
    df = matrix_frame(db)
    if df is None:
        st.warning("No companies found in the database.")
        return

    counts = list_counts(df)
    labels = {s: f"{stage_label(s)} ({counts[s]})" for s in SHORTLIST_STATUSES}
    st.session_state.setdefault(STAGE_KEY, "shortlisted")
    stage = st.segmented_control("List", SHORTLIST_STATUSES, format_func=labels.get, key=STAGE_KEY, required=True,
                                 label_visibility="collapsed")
    st.caption(SHORTLIST_STAGES[stage][1])
    search = st.text_input("Search this list", key=SEARCH_KEY, placeholder="name or registration number…")

    shown = list_frame(df, stage, search)
    if shown.empty:
        st.info(f"No companies in **{stage_label(stage)}**" + (" match that search." if search.strip() else
                " yet. Set a stage from the matrix, the dossier or any portfolio table."))
        return

    st.html(_SELECT_BAR_CSS)
    bar = st.container(key="crawl_select_bar")
    disp = shown[["legal_name", "country", "segment", "registration_number", "need_score", "readiness_score",
                  "total_completeness_pct", "signals_checked"]]
    _selectable_table(
        f"list_table_{stage}", shown, disp,
        column_config={
            "legal_name": "Company",
            "country": st.column_config.TextColumn("Country", width="small"),
            "segment": st.column_config.TextColumn("Segment", width="small"),
            "registration_number": "Reg. Number",
            "need_score": st.column_config.NumberColumn("Need", format="%.1f"),
            "readiness_score": st.column_config.NumberColumn("Readiness", format="%.1f"),
            "total_completeness_pct": st.column_config.ProgressColumn("Data Completeness", format="%.1f%%", min_value=0, max_value=100),
            "signals_checked": "Checked Signals",
        },
    )
    _render_crawl_bar(db, bar, df, {stage: shown})

    # Take the ticked companies off this list.
    ticked = [cid for cid in shown["id"] if cid in st.session_state.get(SELECTION_KEY, set())]
    target = LEAVE_TO[stage]
    if st.button(f"{LEAVE_LABEL[stage]} ({len(ticked)})", disabled=not ticked, key=f"list_leave_{stage}",
                 help=f"Moves the ticked companies to {stage_label(target)}."):
        changed = apply_stage(db, ticked, target)
        st.session_state[SELECTION_KEY] = st.session_state.get(SELECTION_KEY, set()) - set(ticked)
        flash(f"{changed} moved to {stage_label(target)}.", "⭐")
        st.rerun()

    st.markdown("---")
    render_open_dossier_picker({f"{r.legal_name} ({r.registration_number})": r.id for r in shown.itertuples()},
                               key=f"list_open_{stage}", label=f"Open a company's dossier ({stage_label(stage)} list)")
    st.caption("To crawl one company, tick just that one and press ▶ Crawl selected, or use Run Deep Crawlers in its dossier. "
               "Tick many (or use ☑ All / ⚡ Bulk) to crawl the whole list at once.")
