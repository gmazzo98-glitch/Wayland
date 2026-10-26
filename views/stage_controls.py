"""
Changing a company's stage, wherever companies are shown.

  apply_stage         the one bulk writer (ids -> stage), used by every control below
  render_stage_quick  one company: a select that applies the moment you pick (dossier header, Actions tab)
  render_stage_bulk   many companies: pick from the ones shown (or all of them), choose a stage, apply

The stages themselves (names, meanings, what they unlock) come from models.SHORTLIST_STAGES.
"""

import streamlit as st
from sqlalchemy.orm import Session

from models import Company, SHORTLIST_STAGES, SHORTLIST_STATUSES, set_shortlist_status, stage_label


def _stage_help() -> str:
    return "\n\n".join(f"**{label}** - {meaning}" for label, meaning in SHORTLIST_STAGES.values())


def apply_stage(db: Session, company_ids, new_status: str) -> int:
    """Sets the stage of every given company and commits. Returns how many actually changed."""
    ids = list(dict.fromkeys(company_ids))
    if not ids:
        return 0
    changed = 0
    for comp in db.query(Company).filter(Company.id.in_(ids)).all():
        if comp.shortlist_status != new_status:
            set_shortlist_status(comp, new_status)
            changed += 1
    db.commit()
    return changed


def render_stage_quick(db: Session, company: Company, key: str, label: str = "Stage", collapsed: bool = False) -> None:
    """Applies immediately on change. The widget key carries the current stage so it never shows a stale value after
    the stage was changed somewhere else on the page."""
    def _apply(widget_key: str) -> None:
        apply_stage(db, [company.id], st.session_state[widget_key])
        st.toast(f"{company.legal_name} is now {stage_label(st.session_state[widget_key])}.", icon="⭐")

    widget_key = f"{key}_{company.id}_{company.shortlist_status}"
    st.selectbox(label, SHORTLIST_STATUSES, index=SHORTLIST_STATUSES.index(company.shortlist_status),
                 format_func=stage_label, key=widget_key, on_change=_apply, args=(widget_key,), help=_stage_help(),
                 label_visibility="collapsed" if collapsed else "visible")


def render_stage_bulk(db: Session, options: dict, key: str, title: str = "⭐ Set stage for several companies",
                      expanded: bool = False) -> None:
    """`options` maps a display label to a company id: the companies currently shown on the page."""
    with st.expander(title, expanded=expanded):
        if not options:
            st.caption("No companies to choose from.")
            return
        all_shown = st.checkbox(f"All {len(options)} shown", key=f"{key}_all",
                                help="Apply to every company currently listed, after the page's filters.")
        picked = list(options) if all_shown else st.multiselect("Companies", list(options), key=f"{key}_pick")
        c1, c2 = st.columns([2, 1], vertical_alignment="bottom")
        status = c1.selectbox("New stage", SHORTLIST_STATUSES, format_func=stage_label, key=f"{key}_stage",
                              help=_stage_help())
        c1.caption(SHORTLIST_STAGES[status][1])
        n = len(picked)
        if c2.button(f"Apply to {n}", type="primary", disabled=not n, key=f"{key}_apply", use_container_width=True):
            changed = apply_stage(db, [options[label] for label in picked], status)
            st.session_state.pop(f"{key}_pick", None)
            st.session_state.pop(f"{key}_all", None)
            st.toast(f"{changed} of {n} set to {stage_label(status)}" + ("" if changed == n else f" ({n - changed} already were)"),
                     icon="⭐")
            st.rerun()
