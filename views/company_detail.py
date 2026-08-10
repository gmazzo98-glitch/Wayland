"""
Company Intelligence & Tri-State Signal View (Page 2)
Sections 2.1 & 2.2 of GG_Dashboard_Technical_Brief.docx
"""

import streamlit as st
import pandas as pd
from datetime import datetime
from sqlalchemy.orm import Session
from models import Company, SignalRecord
from config import SIGNAL_METADATA, FRESHNESS_WINDOWS
from scoring import calculate_company_scores
from utils import get_signal_display_status

STATUS_BADGES = {
    "present": "🟢 Present",
    "absent": "🔴 Confirmed Absent (Zero)",
    "not_yet_checked": "⚪ Not Yet Checked",
    "stale": "🟡 Stale (Refetch Flag)"
}

MODE_BADGES = {True: "🧪 Simulated", False: "🟢 Live"}

def render_company_detail_page(db: Session):
    st.title("🏢 Company Intelligence & Signal Completeness")
    st.caption("Deep-dive company breakdown — tri-state status, signal freshness windows, and raw payload audit.")

    companies = db.query(Company).order_by(Company.legal_name).all()
    if not companies:
        st.warning("No companies found.")
        return

    company_names = {f"{c.legal_name} ({c.registration_number})": c.id for c in companies}
    selected_name = st.selectbox("Select Target Company", list(company_names.keys()))
    selected_id = company_names[selected_name]

    company = db.query(Company).filter_by(id=selected_id).first()
    signals = db.query(SignalRecord).filter_by(company_id=company.id).all()
    scores = calculate_company_scores(signals)

    # Top Header Summary
    col_m1, col_m2, col_m3, col_m4 = st.columns(4)
    with col_m1:
        st.metric("Need Axis Score", f"{scores['need_score']}/100", f"Completeness: {scores['need_completeness_pct']:.0f}%")
    with col_m2:
        st.metric("Readiness Axis Score", f"{scores['readiness_score']}/100", f"Completeness: {scores['readiness_completeness_pct']:.0f}%")
    with col_m3:
        st.metric("Overall Completeness", f"{scores['total_completeness_pct']:.1f}%", f"{scores['signals_checked']}/{scores['signals_total']} Signals")
    with col_m4:
        st.metric("Shortlist Gate", "Gate Passed" if company.is_shortlisted else "Pending Gate", delta="Phase 3+ Unlocked" if company.is_shortlisted else "Phase 1+2 Only")

    st.markdown("---")

    # Metadata Card
    with st.expander("📌 Company Master Record", expanded=True):
        col_c1, col_c2, col_c3 = st.columns(3)
        with col_c1:
            st.write("**Legal Name:**", company.legal_name)
            st.write("**Handelsregister-Nr:**", company.registration_number)
        with col_c2:
            st.write("**NACE / Sector:**", f"{company.nace_code} ({company.sector_name})")
            st.write("**Segment:**", company.segment)
        with col_c3:
            st.write("**Website:**", company.website_url or "N/A")
            st.write("**Country:**", company.country)

    # Tri-State Signal Breakdown Table
    st.subheader("📊 Signal Record Audit & Freshness Breakdown")

    sig_dict = {s.signal_key: s for s in signals}
    table_rows = []

    for sig_key, meta in SIGNAL_METADATA.items():
        sig_rec = sig_dict.get(sig_key)
        
        if sig_rec:
            disp_status = get_signal_display_status(sig_rec.status, sig_key, sig_rec.fetched_at)
            val = sig_rec.numeric_value
            fetched_str = sig_rec.fetched_at.strftime("%Y-%m-%d %H:%M") if sig_rec.fetched_at else "N/A"
            raw_ref = sig_rec.raw_payload_ref or ""
            mode_badge = MODE_BADGES.get(sig_rec.is_simulated, "—")
        else:
            disp_status = "not_yet_checked"
            val = None
            fetched_str = "Never"
            raw_ref = ""
            mode_badge = "—"

        fresh_window = FRESHNESS_WINDOWS.get(sig_key, 90)

        table_rows.append({
            "Axis": meta["axis"].upper(),
            "Signal Name": meta["label"],
            "Source": meta["source"],
            "Phase": f"Phase {meta['phase']}",
            "Status": STATUS_BADGES.get(disp_status, disp_status),
            "Mode": mode_badge,
            "Value": f"{val:.2f}" if val is not None else "—",
            "Freshness Window": f"{fresh_window} days",
            "Last Fetched": fetched_str,
            "Raw Payload": raw_ref
        })

    df_signals = pd.DataFrame(table_rows)

    st.dataframe(
        df_signals,
        column_config={
            "Axis": "Axis",
            "Signal Name": "Signal Name",
            "Source": "Source",
            "Phase": "Phase",
            "Status": "Tri-State Status",
            "Mode": "Live / Simulated",
            "Value": "Populated Value",
            "Freshness Window": "Freshness Window",
            "Last Fetched": "Last Fetched",
            "Raw Payload": st.column_config.TextColumn("Raw Payload Pointer", width="medium")
        },
        use_container_width=True,
        hide_index=True
    )
