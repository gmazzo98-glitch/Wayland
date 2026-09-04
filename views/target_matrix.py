"""
Target Matrix View (Page 1): Interactive 2D Plotly Scatter Plot (Need vs. Readiness)
Section 4 of GG_Dashboard_Technical_Brief.docx

Two things this view deliberately does NOT do, both per explicit findings in
the Brief:
  - Pool Midcap and SME into one ranked list (Section 5) — each segment gets
    its own chart and leaderboard, always.
  - Let the ranked view be read "as if it were complete" with no visibility
    into pipeline/source health (Section 1's named failure mode) — a
    completeness/live-vs-simulated banner sits above everything else on this
    page, and can't be filtered away.
"""

from datetime import datetime
import streamlit as st
import plotly.express as px
import pandas as pd
from sqlalchemy.orm import Session
from models import Company, SignalRecord, SourceHealth
from scoring import calculate_company_scores, rank_companies, is_prime_target, PRIME_NEED_MIN, PRIME_READINESS_BAND
from indicators import fetch_indicator_defs

SEGMENT_COLORS = {"Midcap": "#38BDF8", "SME": "#F59E0B"}
SEGMENT_ORDER = ["Midcap", "SME"]


def _render_completeness_banner(db: Session, companies, indicator_defs):
    # Mirror how scoring.py counts signals_total: a 'need'/'readiness' indicator
    # counts once, a 'both'-axis one counts once per axis (twice), 'context' never.
    scored_count = sum(2 if d["axis"] == "both" else 1 for d in indicator_defs.values() if d["axis"] != "context")
    total_possible = len(companies) * scored_count
    total_checked = 0
    for comp in companies:
        signals = db.query(SignalRecord).filter_by(company_id=comp.id).all()
        total_checked += calculate_company_scores(signals, indicator_defs)["signals_checked"]
    pct_run = (total_checked / total_possible * 100.0) if total_possible else 0.0

    sources = db.query(SourceHealth).all()
    live_count = sum(1 for s in sources if s.mode == "live")
    never_run = sum(1 for s in sources if s.last_status in (None, "idle"))

    if pct_run >= 90 and live_count == len(sources) and sources:
        st.success(
            f"Pipeline coverage: **{pct_run:.0f}%** of all possible signal checks are populated across "
            f"{len(companies)} companies — {live_count}/{len(sources)} sources running live.",
            icon="✅",
        )
    else:
        st.warning(
            f"Pipeline coverage: only **{pct_run:.0f}%** of possible signal checks are populated across "
            f"{len(companies)} companies. **{live_count}/{len(sources) or 0} sources are running live** "
            f"({never_run} never run) — the rest are simulated or not yet checked. "
            f"Scores below are computed only from what's actually present; check **⚙️ Pipeline & Source Health** "
            f"before treating any ranking here as final.",
            icon="⚠️",
        )


def _segment_section(seg_name: str, seg_df: pd.DataFrame):
    company_word = "company" if len(seg_df) == 1 else "companies"
    st.subheader(f"{'🏢' if seg_name == 'Midcap' else '🏬'} {seg_name} Segment ({len(seg_df)} {company_word})")

    if seg_df.empty:
        st.caption("No companies in this segment match the current filters.")
        return

    prime_count = int(seg_df["is_prime"].sum())
    prime_word = "company" if prime_count == 1 else "companies"
    prime_verb = "qualifies" if prime_count == 1 else "qualify"
    st.caption(
        f"Prime target band: Need ≥ {PRIME_NEED_MIN:.0f} and Readiness between "
        f"{PRIME_READINESS_BAND[0]:.0f}–{PRIME_READINESS_BAND[1]:.0f} (moderate-to-high, not just the top corner) "
        f"— **{prime_count}** {seg_name} {prime_word} currently {prime_verb}."
    )

    fig = px.scatter(
        seg_df,
        x="need_score",
        y="readiness_score",
        size="total_completeness_pct",
        color="is_prime",
        hover_name="legal_name",
        hover_data={
            "country": True,
            "registration_number": True,
            "need_score": ":.1f",
            "readiness_score": ":.1f",
            "total_completeness_pct": ":.1f%",
            "shortlist_status": True,
            "is_prime": False,
        },
        labels={
            "need_score": "Need Axis Score (Could Innovate: Margin, Export, Digital)",
            "readiness_score": "Readiness Axis Score (Is Innovating: R&D, IP, Grants)",
        },
        color_discrete_map={True: "#22C55E", False: SEGMENT_COLORS.get(seg_name, "#94A3B8")},
        size_max=30,
    )

    # Shade the actual prime band (high-need + MODERATE-TO-HIGH readiness), not a
    # single high+high corner — Section 4 of the Brief.
    fig.add_shape(
        type="rect", x0=PRIME_NEED_MIN, y0=PRIME_READINESS_BAND[0], x1=100, y1=PRIME_READINESS_BAND[1],
        fillcolor="#22C55E", opacity=0.12, line=dict(width=0), layer="below",
    )
    fig.add_shape(type="line", x0=PRIME_NEED_MIN, y0=0, x1=PRIME_NEED_MIN, y1=100, line=dict(color="#475569", dash="dash"))
    fig.add_annotation(x=75, y=(PRIME_READINESS_BAND[0] + PRIME_READINESS_BAND[1]) / 2, text="🎯 PRIME BAND", showarrow=False, font=dict(color="#22C55E", size=12))

    fig.update_layout(
        xaxis=dict(range=[-5, 105]), yaxis=dict(range=[-5, 105]),
        template="plotly_dark", height=480, showlegend=False,
    )
    st.plotly_chart(fig, use_container_width=True, key=f"scatter_{seg_name}")

    disp_df = seg_df[[
        "legal_name", "country", "registration_number", "need_score", "readiness_score",
        "total_completeness_pct", "signals_checked", "shortlist_status"
    ]]
    st.dataframe(
        disp_df,
        column_config={
            "legal_name": f"{seg_name} Company",
            "country": st.column_config.TextColumn("Country", width="small"),
            "registration_number": "Reg. Number",
            "need_score": st.column_config.NumberColumn("Need Score", format="%.1f"),
            "readiness_score": st.column_config.NumberColumn("Readiness Score", format="%.1f"),
            "total_completeness_pct": st.column_config.ProgressColumn("Data Completeness", format="%.1f%%", min_value=0, max_value=100),
            "signals_checked": "Checked Signals",
            "shortlist_status": "Shortlist Status",
        },
        use_container_width=True,
        hide_index=True,
    )


def render_target_matrix_page(db: Session):
    st.title("🎯 Scored Target Matrix (Need × Readiness)")
    st.caption("2D Positioning Rubric, scored separately per segment — Midcap and SME are never pooled into one ranking (Section 5 of the Brief).")

    companies = db.query(Company).all()
    if not companies:
        st.warning("No target companies found in database. Run seed script or ingestion pipeline.")
        return

    indicator_defs = fetch_indicator_defs(db)
    _render_completeness_banner(db, companies, indicator_defs)
    st.markdown("---")

    matrix_data = []
    now = datetime.utcnow()
    for comp in companies:
        signals = db.query(SignalRecord).filter_by(company_id=comp.id).all()
        scores = calculate_company_scores(signals, indicator_defs)

        # Refresh Company's cached need_score/readiness_score/last_scored_at
        # snapshot — this page computes a score for every company anyway, so
        # it's the natural place to keep the cache from going stale. Nothing
        # else in the app reads FROM these columns to render a score; every
        # view still recomputes live from SignalRecords. The cache exists for
        # external consumers (a future BI/export query, PilotOutcome context)
        # that want "what did we last think" without re-running the engine.
        comp.need_score = scores["need_score"]
        comp.readiness_score = scores["readiness_score"]
        comp.last_scored_at = now

        matrix_data.append({
            "id": comp.id,
            "legal_name": comp.legal_name,
            "country": comp.country or "Germany",
            "registration_number": comp.registration_number,
            "nace_code": comp.nace_code,
            "sector": comp.sector_name,
            "segment": comp.segment,
            "shortlist_status": comp.shortlist_status,
            "need_score": scores["need_score"],
            "readiness_score": scores["readiness_score"],
            "total_completeness_pct": scores["total_completeness_pct"],
            "signals_checked": f"{scores['signals_checked']}/{scores['signals_total']}",
            "is_prime": is_prime_target(scores["need_score"], scores["readiness_score"]),
        })
    db.commit()
    df = pd.DataFrame(matrix_data)

    all_segments = [s for s in SEGMENT_ORDER if s in df["segment"].unique()]
    all_segments += [s for s in sorted(df["segment"].unique()) if s not in all_segments]

    col_f1, col_f2, col_f3, col_f4 = st.columns(4)
    with col_f1:
        country_options = ["All Countries", "Germany 🇩🇪", "Italy 🇮🇹"]
        selected_country = st.selectbox("Filter Country 🌐", country_options)
    with col_f2:
        sectors = ["All Sectors"] + sorted(df["sector"].unique().tolist())
        selected_sec = st.selectbox("Filter Sector", sectors)
    with col_f3:
        min_comp = st.slider("Min Completeness %", 0, 100, 0, step=10)
    with col_f4:
        selected_segments = st.multiselect(
            "Segments (never pooled)",
            options=all_segments, default=all_segments,
        )

    filtered_df = df.copy()
    if selected_country == "Germany 🇩🇪":
        filtered_df = filtered_df[filtered_df["country"] == "Germany"]
    elif selected_country == "Italy 🇮🇹":
        filtered_df = filtered_df[filtered_df["country"] == "Italy"]

    if selected_sec != "All Sectors":
        filtered_df = filtered_df[filtered_df["sector"] == selected_sec]
    filtered_df = filtered_df[filtered_df["total_completeness_pct"] >= min_comp]

    st.markdown("---")

    for seg_name in [s for s in all_segments if s in selected_segments]:
        seg_slice = filtered_df[filtered_df["segment"] == seg_name]
        ranked_seg_df = pd.DataFrame(rank_companies(seg_slice.to_dict("records")), columns=seg_slice.columns)
        _segment_section(seg_name, ranked_seg_df)
        st.markdown("---")
