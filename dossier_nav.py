"""
Navigation into the Company Intelligence dossier from anywhere in the app.

Every page that lists companies (Target Matrix, Pain Points, Shortlist Gate, Cost Structure, Valuation...)
offers an "Open dossier" control built from `render_open_dossier_picker`. It writes two session-state keys in a
button callback (callbacks run BEFORE the next script run builds its widgets, which is the only moment a widget's
own state may be set):

  nav_page           the sidebar radio in app.py            -> jumps to the Company Intelligence page
  dossier_open_id    consumed once by the dossier           -> selects that company, clears the country filter

No Streamlit widget state is touched outside a callback, and nothing here imports a view (no import cycles).
"""

from typing import Dict, Optional, Sequence

import streamlit as st

NAV_PAGE_KEY = "nav_page"
DOSSIER_PAGE = "🏢 Company Intelligence"
OPEN_ID_KEY = "dossier_open_id"
OPEN_TAB_KEY = "dossier_open_tab"
COUNTRY_FLAGS = {"Germany": "🇩🇪", "Italy": "🇮🇹"}


def company_label(legal_name: str, registration_number: str, country: Optional[str], segment: Optional[str]) -> str:
    """The dossier selector's option text. The format is relied on by tests and by the parent-card jump button."""
    return f"{legal_name} ({registration_number}) — {COUNTRY_FLAGS.get(country, '🌐')} {country} [{segment}]"


def open_dossier(company_id: str, tab: Optional[str] = None) -> None:
    """Button callback: go to the Company Intelligence page with this company selected (optionally on a tab)."""
    st.session_state[NAV_PAGE_KEY] = DOSSIER_PAGE
    st.session_state[OPEN_ID_KEY] = company_id
    if tab:
        st.session_state[OPEN_TAB_KEY] = tab


def open_dossier_button(company_id: str, label: str = "🏢 Open dossier", key: Optional[str] = None,
                        tab: Optional[str] = None, **kwargs) -> None:
    st.button(label, key=key or f"open_dossier_{company_id}_{tab or 'overview'}", on_click=open_dossier,
              args=(company_id, tab), **kwargs)


def render_open_dossier_picker(options: Dict[str, str], key: str, label: str = "Open a company's dossier",
                               tab: Optional[str] = None) -> None:
    """A compact 'pick a company -> open its dossier' row. `options` maps display text -> company id."""
    if not options:
        return
    names: Sequence[str] = list(options)
    c1, c2 = st.columns([4, 1])
    picked = c1.selectbox(label, names, key=f"{key}_pick", index=None, placeholder="Type a company name...")
    c2.markdown("&nbsp;", unsafe_allow_html=True)
    c2.button("🏢 Open dossier", key=f"{key}_go", disabled=picked is None, use_container_width=True,
              on_click=open_dossier, args=(options.get(picked), tab))
