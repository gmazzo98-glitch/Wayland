"""
Project Vienna — GG Signal Sourcing & Scoring Dashboard
Main Streamlit Application Entrance & Navigation
"""

from collections import Counter
import streamlit as st
from database import init_db, get_db_session
from models import Company
from seed import seed_database
from views.target_matrix import render_target_matrix_page
from views.company_detail import render_company_detail_page
from views.pipeline_health import render_pipeline_health_page
from views.paid_shortlist_gate import render_paid_shortlist_gate_page
from views.indicator_weights import render_indicator_weights_page
from views.crawl_widget import sync_session, render_crawl_widget
from views.crawler_setup import render_crawler_setup_page, render_sidebar_target

# Page Configuration
st.set_page_config(
    page_title="Project Vienna — GG Signal Sourcing & Scoring Dashboard",
    page_icon="🌾",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Initialize Database & Engine
try:
    init_db()
    db = get_db_session()
    # Background deep crawls outlive page runs: pick up anything that finished since this
    # session last ran (refreshing stale rows) before any page reads the database.
    sync_session(db)
except Exception as e:
    from config import SQLALCHEMY_DATABASE_URI
    st.error("### 🚨 Database Connection Error")
    st.markdown(f"**Error Details:**\n```\n{e}\n```")
    
    if not SQLALCHEMY_DATABASE_URI.startswith("sqlite"):
        st.markdown(
            """
            ---
            ### 🛠️ Common Fixes for Cloud PostgreSQL (Supabase / Streamlit Cloud)
            
            1. **Supabase Paused Project (Most Common)**:
               - Free tier projects are automatically paused after a period of inactivity.
               - Log into [Supabase Dashboard](https://supabase.com/dashboard) and click **"Restore project"**.
               
            2. **IPv6 vs IPv4 (Connection Pooler)**:
               - Streamlit Cloud runs on IPv4 infrastructure. Supabase's direct host (`db.[ref].supabase.co:5432`) often resolves only to IPv6 and fails.
               - In Supabase (*Settings → Database → Connection string*), use the **Session Mode Connection Pooler** URI (`aws-0-[region].pooler.supabase.com` on port `6543` or `5432`) with `?sslmode=require`.
               
            3. **Special Characters in Password**:
               - If your password has special characters (`@`, `:`, `#`, `%`, `?`), they must be URL-encoded (e.g., `@` &rarr; `%40`, `#` &rarr; `%23`).
               
            4. **Missing SSL Mode**:
               - Append `?sslmode=require` to your `DATABASE_URL`.
               
            5. **Check Unredacted Logs**:
               - In the bottom right corner of Streamlit Cloud, click **"Manage app" &rarr; "Logs"** to inspect the raw database driver logs.
            """
        )
    st.stop()

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
        "💰 Shortlist Gate & Paid Pulls",
        "⚖️ Indicator Weights",
        "🖥️ Crawler Setup"
    ]
)

st.sidebar.markdown("---")

# Which computer the deep crawls run on (this server, or a helper's PC with the worker installed).
render_sidebar_target(db)

st.sidebar.markdown("---")

# Quick Dataset Stats
company_count = db.query(Company).count()
midcap_count = db.query(Company).filter_by(segment="Midcap").count()
sme_count = db.query(Company).filter_by(segment="SME").count()
de_count = db.query(Company).filter(Company.country.in_(["Germany", None])).count()
it_count = db.query(Company).filter_by(country="Italy").count()

st.sidebar.metric("Target Companies", company_count, f"{midcap_count} Midcaps | {sme_count} SMEs")
st.sidebar.caption(f"🌐 **Coverage:** 🇩🇪 {de_count} Germany | 🇮🇹 {it_count} Italy")
if company_count > 0:
    # Mode of sector_name — dataset is small, a plain Counter is simplest and clear.
    sector_counts = Counter(c.sector_name for c in db.query(Company.sector_name).all())
    top_sector_name = sector_counts.most_common(1)[0][0]
    st.sidebar.caption(f"📍 **Primary Sector:** {top_sector_name}")
else:
    st.sidebar.caption("📍 No companies loaded yet")

# Route Page Rendering
if page_selection == "🎯 Scored Target Matrix":
    render_target_matrix_page(db)
elif page_selection == "🏢 Company Intelligence":
    render_company_detail_page(db)
elif page_selection == "⚙️ Pipeline & Source Health":
    render_pipeline_health_page(db)
elif page_selection == "💰 Shortlist Gate & Paid Pulls":
    render_paid_shortlist_gate_page(db)
elif page_selection == "⚖️ Indicator Weights":
    render_indicator_weights_page(db)
elif page_selection == "🖥️ Crawler Setup":
    render_crawler_setup_page(db)

# Footer
st.sidebar.markdown("---")
st.sidebar.caption("Project Vienna v1.0 • Streamlit Control Surface")

# Floating deep-crawl progress card — drawn on every page, after the page itself.
render_crawl_widget()
