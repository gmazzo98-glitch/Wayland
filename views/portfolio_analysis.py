"""
Portfolio Analysis: the views that compare companies with each other, in one place.

Pain points, cost structure and valuation each used to be a page of their own; their per-company halves now live in
Company Intelligence (Diagnosis, Costs, Financials), so what remains here is only what a single company's page cannot
show - rankings, outliers, sector norms, the screen, and the catalogs/assumptions behind them. Only the selected
section is built (same pattern as the dossier), so the page opens fast.
"""

import streamlit as st
from sqlalchemy.orm import Session

from views.cost_structure import render_cost_structure_page
from views.pain_points import render_pain_points_page
from views.valuation import render_valuation_page

SECTION_KEY = "pa_section"
SECTIONS = [("pain", "🩺 Pain points"), ("costs", "💸 Costs"), ("valuation", "💶 Valuation")]
LABEL = dict(SECTIONS)
RENDERERS = {"pain": render_pain_points_page, "costs": render_cost_structure_page, "valuation": render_valuation_page}


def render_portfolio_analysis_page(db: Session):
    st.title("📊 Portfolio Analysis")
    st.caption("Compare companies with each other: who is in pain, whose cost mix stands out, what the portfolio is worth. "
               "Everything about ONE company is in 🏢 Company Intelligence.")
    st.session_state.setdefault(SECTION_KEY, "pain")
    st.segmented_control("Analysis", [k for k, _ in SECTIONS], format_func=LABEL.get, key=SECTION_KEY, required=True,
                         label_visibility="collapsed")
    RENDERERS[st.session_state[SECTION_KEY]](db, show_title=False)
