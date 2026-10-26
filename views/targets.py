"""
Targets & Shortlist: pick who to work on (the Need x Readiness matrix), work the list of each stage (crawl, restage,
open dossiers), and decide who qualifies (the shortlist gate).

The two used to be separate pages although the shortlist is chosen from the matrix. Each section is the unchanged
existing page; only the selected one is built.
"""

import streamlit as st
from sqlalchemy.orm import Session

from views.paid_shortlist_gate import render_paid_shortlist_gate_page
from views.stage_lists import render_stage_lists
from views.target_matrix import render_target_matrix_page

SECTION_KEY = "targets_section"
SECTIONS = [("matrix", "🎯 Scored matrix (Need × Readiness)"), ("lists", "📋 My lists"), ("gate", "⭐ Shortlist")]
LABEL = dict(SECTIONS)
RENDERERS = {"matrix": render_target_matrix_page, "lists": render_stage_lists, "gate": render_paid_shortlist_gate_page}


def render_targets_page(db: Session):
    st.title("🎯 Targets & Shortlist")
    st.session_state.setdefault(SECTION_KEY, "matrix")
    st.segmented_control("Section", [k for k, _ in SECTIONS], format_func=LABEL.get, key=SECTION_KEY, required=True,
                         label_visibility="collapsed")
    RENDERERS[st.session_state[SECTION_KEY]](db, show_title=False)
