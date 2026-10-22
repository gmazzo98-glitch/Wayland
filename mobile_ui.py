"""
Responsive (phone and small tablet) styling, injected once from app.py on every run.

Pure CSS: no widget is removed or reordered, so every feature stays available. Streamlit
already stacks st.columns under 640px; this tightens what is left (page padding, sticky
bars that eat the viewport, the floating crawl card, wide tables, tabs, touch targets).
"""

import streamlit as st

_MOBILE_CSS = """
<style>
/* iOS Safari zooms into inputs below 16px, which also breaks the layout on focus. */
@media (max-width: 768px) {
    html { -webkit-text-size-adjust: 100%; }
    input, textarea, select,
    [data-baseweb="input"] input, [data-baseweb="select"] input,
    [data-baseweb="textarea"] textarea { font-size: 16px !important; }

    /* Reclaim the screen: wide-layout gutters are far too generous on a phone. */
    [data-testid="stMainBlockContainer"], .block-container {
        padding: 3.5rem 0.75rem 6rem !important;
        max-width: 100% !important;
    }
    h1 { font-size: 1.6rem !important; line-height: 1.25 !important; }
    h2 { font-size: 1.3rem !important; }
    h3 { font-size: 1.12rem !important; }
    [data-testid="stCaptionContainer"] { font-size: 0.8rem; }

    /* Touch targets of at least 44px. */
    .stButton > button, .stDownloadButton > button, .stLinkButton > a,
    [data-testid="stFormSubmitButton"] > button {
        min-height: 2.75rem; width: 100%; padding: 0.5rem 0.9rem;
    }
    [data-baseweb="select"] > div { min-height: 2.75rem; }
    [data-testid="stCheckbox"] label, [data-testid="stRadio"] label { min-height: 2.25rem; align-items: center; }
    [data-testid="stSidebar"] [data-testid="stRadio"] label { padding: 0.35rem 0; }

    /* Stacked columns: even gaps, and equal-height controls no longer needed. */
    [data-testid="stHorizontalBlock"] { gap: 0.6rem !important; }
    [data-testid="stColumn"] { min-width: 100% !important; }

    /* Metrics: smaller numbers so long values like EUR amounts do not clip. */
    [data-testid="stMetricValue"] { font-size: 1.4rem !important; }
    [data-testid="stMetricLabel"] p { font-size: 0.8rem !important; }
    [data-testid="stMetric"] { overflow-wrap: anywhere; }

    /* Tabs scroll sideways instead of overflowing the page. */
    [data-baseweb="tab-list"] { overflow-x: auto; flex-wrap: nowrap; scrollbar-width: none; }
    [data-baseweb="tab-list"]::-webkit-scrollbar { display: none; }
    [data-baseweb="tab"] { flex: 0 0 auto; white-space: nowrap; padding: 0.5rem 0.8rem; }

    /* Tables, editors and charts scroll inside their own box, never the whole page. */
    [data-testid="stDataFrame"], [data-testid="stTable"], [data-testid="stDataFrameResizable"] {
        max-width: 100%; overflow-x: auto;
    }
    [data-testid="stPlotlyChart"], [data-testid="stVegaLiteChart"] { max-width: 100%; overflow-x: auto; }
    [data-testid="stMarkdownContainer"] table { display: block; overflow-x: auto; max-width: 100%; }
    pre, code { white-space: pre-wrap; overflow-wrap: anywhere; }
    [data-testid="stMarkdownContainer"] { overflow-wrap: anywhere; }

    /* Sticky bars would swallow a third of a phone screen: let them scroll away. */
    [data-testid="stLayoutWrapper"]:has(> .st-key-dossier_header),
    [data-testid="stLayoutWrapper"]:has(> .st-key-crawl_select_bar) { position: static !important; }
    .st-key-dossier_header, .st-key-crawl_select_bar { box-shadow: none !important; }

    /* Floating crawl card: full width bottom strip, compact, and clear of the browser bar. */
    .st-key-crawl_widget {
        left: 0.5rem !important; right: 0.5rem !important; bottom: 0.5rem !important;
        width: auto !important; max-height: 40vh; overflow-y: auto;
        padding: 0.5rem 0.7rem 0.6rem !important;
        padding-bottom: calc(0.6rem + env(safe-area-inset-bottom)) !important;
    }

    /* Expanders and sidebar breathe a little less. */
    [data-testid="stExpander"] summary { padding: 0.6rem 0.75rem; }
    [data-testid="stSidebar"] { max-width: 88vw; }
    [data-testid="stSidebarUserContent"] { padding: 1rem 0.9rem; }
    [data-testid="stChatInput"] textarea { font-size: 16px !important; }
}

@media (max-width: 480px) {
    [data-testid="stMainBlockContainer"], .block-container { padding-left: 0.5rem !important; padding-right: 0.5rem !important; }
    h1 { font-size: 1.4rem !important; }
}

/* Tablets: keep the columns but trim the gutters. */
@media (min-width: 769px) and (max-width: 1024px) {
    [data-testid="stMainBlockContainer"], .block-container { padding-left: 1.5rem !important; padding-right: 1.5rem !important; }
}
</style>
"""


def inject_mobile_css() -> None:
    """Add the responsive stylesheet (and a proper viewport hint) to the current run."""
    st.markdown(_MOBILE_CSS, unsafe_allow_html=True)
