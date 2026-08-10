"""
Project Vienna — GG Signal Sourcing & Scoring Dashboard
Main Streamlit Application Entrance & Navigation
"""

import streamlit as st
from database import init_db, get_db_session
from models import Company
from seed import seed_database
from views.target_matrix import render_target_matrix_page
from views.company_detail import render_company_detail_page
from views.pipeline_health import render_pipeline_health_page
from views.paid_shortlist_gate import render_paid_shortlist_gate_page

# Page Configuration
st.set_page_config(
    page_title="Project Vienna — GG Signal Sourcing & Scoring Dashboard",
    page_icon="🌾",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Initialize Database & Auto-Seed if empty
init_db()
db = get_db_session()

# Check if companies exist, else seed
comp_count = db.query(Company).count()
if comp_count == 0:
    seed_database()
    st.toast("Database seeded with sample Agrifood company dataset!", icon="🌱")

# Sidebar Header & Navigation
st.sidebar.image("https://img.icons8.com/color/96/wheat.png", width=60)
st.sidebar.title("Project Vienna")
st.sidebar.caption("GG Signal Sourcing & Scoring Dashboard")

st.sidebar.markdown("---")

page_selection = st.sidebar.radio(
    "Navigation Surface",
    [
        "⚙️ Pipeline & Source Health",
        "🏢 Company Intelligence",
        "🎯 Scored Target Matrix",
        "💰 Shortlist Gate & Paid Pulls"
    ]
)
st.sidebar.caption(
    "Ordered to match the Technical Brief's build/attention order: pipeline "
    "visibility and per-company completeness before the ranked view — a ranked "
    "table with no source-health context is the main failure mode it warns against."
)

st.sidebar.markdown("---")

# Quick Dataset Stats
company_count = db.query(Company).count()
midcap_count = db.query(Company).filter_by(segment="Midcap").count()
sme_count = db.query(Company).filter_by(segment="SME").count()

st.sidebar.metric("Target Companies", company_count, f"{midcap_count} Midcaps | {sme_count} SMEs")
st.sidebar.caption("📍 Primary Sector: Agrifood & Agriculture")

# Route Page Rendering
if page_selection == "🎯 Scored Target Matrix":
    render_target_matrix_page(db)
elif page_selection == "🏢 Company Intelligence":
    render_company_detail_page(db)
elif page_selection == "⚙️ Pipeline & Source Health":
    render_pipeline_health_page(db)
elif page_selection == "💰 Shortlist Gate & Paid Pulls":
    render_paid_shortlist_gate_page(db)

# Footer
st.sidebar.markdown("---")
st.sidebar.caption("Project Vienna v1.0 • Streamlit Control Surface")
