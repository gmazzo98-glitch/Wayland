"""
Cost Structure (portfolio view): where the money goes across the portfolio, across the nine AIDA production-cost lines.

Every category is shown three ways - absolute thousand EUR, % of total production costs, % of revenue
(only where the revenue year is proven to match; otherwise blank, never estimated) - plus four fiscal
years of history and a comparison against the company's ATECO peers.

Three tabs, each answering a question a spreadsheet makes slow:
  Portfolio   - "rank/filter all companies on any cost line, in k EUR or in %"  (with a per-row cost-mix chart)
  Outliers & movers - "which companies have an unusual mix, and whose mix is shifting fastest?"
  Sector map  - "what does a typical cost structure look like in each ATECO code?"
One company's full cost X-ray (render_company_xray, below) is shown in Company Intelligence > Costs, not here.

All numbers come from cost_analytics.py (the same module the AI assistant's get_cost_breakdown tool uses).
"""

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy.orm import Session

import cost_analytics as ca
import cost_breakdown as cb
from dossier_nav import render_open_dossier_picker
from views.stage_controls import render_stage_bulk
from models import Company

PALETTE = ["#4C78A8", "#F58518", "#54A24B", "#B279A2", "#E45756", "#72B7B2", "#EECA3B", "#9D755D", "#BAB0AC"]
COLORS = dict(zip(cb.CATEGORY_KEYS, PALETTE))
BASIS = {
    "Thousand EUR (absolute)": ("k", "k EUR", "%.0f"),
    "% of total production costs": ("share", "% of costs", "%.1f%%"),
    "% of revenue": ("rev", "% of revenue", "%.1f%%"),
}
PERIOD_LABEL = {"latest": "Latest", "y-1": "Year -1", "y-2": "Year -2", "y-3": "Year -3"}


@st.cache_data(ttl=300, show_spinner="Loading cost data...")
def _frame(_db: Session) -> pd.DataFrame:
    return ca.load_cost_frame(_db)


@st.cache_data(ttl=300, show_spinner=False)
def _blobs(_db: Session):
    return ca.load_blobs(_db)


def _fmt_money(v):
    return "-" if v is None or pd.isna(v) else f"{v:,.0f}k"


# ---------------------------------------------------------------------------------------------- Portfolio
def _render_portfolio(db: Session, frame: pd.DataFrame):
    st.markdown("Every company, every cost line. Pick the basis, filter, then click any column header to sort.")
    c1, c2, c3 = st.columns([2, 2, 2])
    basis_name = c1.radio("Show each line as", list(BASIS), key="cs_basis")
    suffix, unit, fmt = BASIS[basis_name]
    sectors = sorted(frame["nace_code"].dropna().unique())
    picked = c2.multiselect("ATECO code", sectors, key="cs_nace", placeholder="All sectors")
    text = c3.text_input("Search name / VAT", key="cs_search")

    d = frame
    if picked:
        d = d[d["nace_code"].isin(picked)]
    if text.strip():
        t = text.strip().lower()
        d = d[d["legal_name"].str.lower().str.contains(t, na=False) | d["registration_number"].str.lower().str.contains(t, na=False)]
    lo, hi = float(frame["total_k"].min()), float(frame["total_k"].max())
    if lo < hi:  # a slider needs a range: with one company (or all equal totals) there is nothing to filter on
        size = st.slider("Total production costs (k EUR)", lo, hi, (lo, hi), key="cs_size")
        d = d[d["total_k"].between(*size)]

    if suffix == "rev":
        have = d[[f"{c}_rev" for c in cb.CATEGORY_KEYS]].notna().any(axis=1)
        st.caption(f"Revenue ratios exist only where the revenue year provably matches the cost year: "
                   f"{int(have.sum())} of the {len(d)} companies shown. The rest are blank, not estimated.")
        d = d[have]

    cols = [f"{c}_{suffix}" for c in cb.CATEGORY_KEYS]
    out = pd.DataFrame({"Company": d["legal_name"], "VAT": d["registration_number"], "ATECO": d["nace_code"],
                        "Total costs (k EUR)": d["total_k"]})
    mix_cols = [f"{c}_share" for c in cb.CATEGORY_KEYS]
    out["Cost mix (% of costs)"] = [[max(0.0, 0.0 if pd.isna(v) else float(v)) for v in row] for row in d[mix_cols].to_numpy()]
    for c, col in zip(cb.CATEGORY_KEYS, cols):
        out[f"{cb.LABELS[c]} ({unit})"] = d[col]
    out = out.sort_values("Total costs (k EUR)", ascending=False)

    cfg = {"Total costs (k EUR)": st.column_config.NumberColumn(format="%.0f"),
           "Cost mix (% of costs)": st.column_config.BarChartColumn(
               "Cost mix", help="Nine bars, left to right: " + ", ".join(cb.LABELS[c] for c in cb.CATEGORY_KEYS), y_min=0)}
    for c in cb.CATEGORY_KEYS:
        cfg[f"{cb.LABELS[c]} ({unit})"] = st.column_config.NumberColumn(format=fmt)
    st.dataframe(out, hide_index=True, use_container_width=True, column_config=cfg, height=560)
    st.download_button("⬇️ Download this table (CSV)", out.drop(columns=["Cost mix (% of costs)"]).to_csv(index=False).encode("utf-8"),
                       file_name="cost_structure.csv", mime="text/csv")

    render_open_dossier_picker({f"{r.legal_name} ({r.registration_number})": r.company_id for r in d.itertuples()},
                               key="cs_port", tab="costs")
    render_stage_bulk(db, {f"{r.legal_name} ({r.registration_number})": r.company_id for r in d.itertuples()}, key="cs_stage")

    with st.expander("📊 Portfolio-wide distribution of each line"):
        stat = pd.DataFrame([{"Line": cb.LABELS[c], **{k: v for k, v in ca._stats(d[f'{c}_{suffix}']).items()}} for c in cb.CATEGORY_KEYS])
        stat = stat.rename(columns={"n": "Companies", "median": "Median", "p25": "P25", "p75": "P75", "p90": "P90"})
        st.dataframe(stat, hide_index=True, use_container_width=True,
                     column_config={k: st.column_config.NumberColumn(format=fmt) for k in ("Median", "P25", "P75", "P90")})


# ---------------------------------------------------------------------------------------------- X-ray
def _findings(b: dict) -> list:
    msgs = []
    for c in b["categories"]:
        share, gap, pct, tr = c["share_of_costs_pct"], c["gap_vs_peers_pp"], c["peer_percentile"], c["share_change_pp_3y"]
        if share is None:
            continue
        if gap is not None and abs(gap) >= 8 and pct is not None and (pct >= 90 or pct <= 10):
            word = "above" if gap > 0 else "below"
            msgs.append(f"**{c['label']}** is {share:.1f}% of costs - {abs(gap):.1f} pp {word} the peer median "
                        f"({c['peer_median_share_pct']:.1f}%), at the {pct:.0f}th percentile.")
        if tr is not None and abs(tr) >= 8:
            msgs.append(f"**{c['label']}** share moved {tr:+.1f} pp over the filed years "
                        f"({'rising' if tr > 0 else 'falling'} as a part of the cost base).")
    return msgs


def render_company_xray(db: Session, company: Company, frame: pd.DataFrame = None, key: str = "cs") -> None:
    """One company's full cost structure (used by this page's X-ray tab and by the Company Intelligence dossier)."""
    frame = _frame(db) if frame is None else frame
    b = ca.company_breakdown(db, company, frame)
    if not b:
        st.info("No cost breakdown imported for this company yet.")
        return
    cats = b["categories"]
    with_amount = [c for c in cats if c["k_eur"] is not None]
    biggest = max(with_amount, key=lambda c: c["k_eur"]) if with_amount else None
    total = b["total_costs_k_eur"]
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total production costs", f"{total:,.0f}k EUR" if total is not None else "—")
    m2.metric("Biggest line", biggest["label"] if biggest else "—",
              f"{biggest['share_of_costs_pct']:.1f}% of costs" if biggest and biggest["share_of_costs_pct"] is not None else None,
              delta_color="off")
    svc = next(c for c in cats if c["category"] == "services")
    m3.metric("Purchased services", f"{svc['k_eur']:,.0f}k EUR" if svc["k_eur"] is not None else "-",
              f"{svc['pct_of_revenue']:.1f}% of revenue" if svc["pct_of_revenue"] is not None else "revenue year not matched",
              delta_color="off")
    m4.metric("Compared with", b["peer_basis"])

    findings = _findings(b)
    if findings:
        st.markdown("#### What stands out")
        for m in findings:
            st.markdown(f"- {m}")
    else:
        st.caption("Nothing stands out: every line is within 8 pp of its peers and no share moved 8 pp.")

    periods = [p for p in reversed(cb.PERIODS) if b["total_by_period_k_eur"].get(p) is not None]
    st.markdown("#### Cost base over the filed years")
    g1, g2 = st.columns(2)
    for col, mode in ((g1, "abs"), (g2, "pct")):
        fig = go.Figure()
        for c in cats:
            ys = [(c["k_eur_by_period"].get(p) if mode == "abs" else c["share_by_period_pct"].get(p)) for p in periods]
            fig.add_bar(name=c["label"], x=[PERIOD_LABEL[p] for p in periods], y=ys, marker_color=COLORS[c["category"]])
        fig.update_layout(template="plotly_dark", barmode="relative", height=380, margin=dict(t=30, b=10),
                          title="Thousand EUR" if mode == "abs" else "% of total production costs",
                          legend=dict(orientation="h", y=-0.2))
        col.plotly_chart(fig, use_container_width=True, key=f"{key}_hist_{mode}")

    st.markdown("#### This company vs its peers (share of production costs)")
    peers, _ = ca.peer_group(frame, company.nace_code)
    fig = go.Figure()
    y = [c["label"] for c in cats]
    p25 = [peers[f"{c['category']}_share"].quantile(.25) for c in cats]
    p75 = [peers[f"{c['category']}_share"].quantile(.75) for c in cats]
    fig.add_bar(y=y, x=[b - a for a, b in zip(p25, p75)], base=p25, orientation="h", name="Peer middle half (P25-P75)",
                marker_color="rgba(160,160,160,.35)")
    fig.add_scatter(y=y, x=[c["peer_median_share_pct"] for c in cats], mode="markers", name="Peer median",
                    marker=dict(symbol="line-ns", size=16, color="white", line=dict(width=2, color="white")))
    fig.add_scatter(y=y, x=[c["share_of_costs_pct"] for c in cats], mode="markers", name=company.legal_name,
                    marker=dict(size=13, color="#F58518", line=dict(width=1, color="black")))
    fig.update_layout(template="plotly_dark", height=430, margin=dict(t=20, b=10), xaxis_title="% of total production costs",
                      yaxis=dict(autorange="reversed"), legend=dict(orientation="h", y=-0.15))
    st.plotly_chart(fig, use_container_width=True, key=f"{key}_peer_chart")

    table = pd.DataFrame([{
        "Line": c["label"], "k EUR": c["k_eur"], "% of costs": c["share_of_costs_pct"], "% of revenue": c["pct_of_revenue"],
        "Peer median %": c["peer_median_share_pct"], "Gap (pp)": c["gap_vs_peers_pp"], "Peer percentile": c["peer_percentile"],
        "3-yr share change (pp)": c["share_change_pp_3y"]} for c in cats])
    st.dataframe(table, hide_index=True, use_container_width=True, column_config={
        "k EUR": st.column_config.NumberColumn(format="%.0f"),
        "% of costs": st.column_config.NumberColumn(format="%.1f%%"), "% of revenue": st.column_config.NumberColumn(format="%.1f%%"),
        "Peer median %": st.column_config.NumberColumn(format="%.1f%%"), "Gap (pp)": st.column_config.NumberColumn(format="%+.1f"),
        "Peer percentile": st.column_config.ProgressColumn(format="%.0f", min_value=0, max_value=100),
        "3-yr share change (pp)": st.column_config.NumberColumn(format="%+.1f")})
    st.caption(b["note"] + ("" if b["revenue_ratio_available"] else
               " This company's older revenue figures are from a different fiscal year than its latest costs, so no revenue ratio is shown."))


# ---------------------------------------------------------------------------------------------- Outliers
def _render_outliers(db: Session, frame: pd.DataFrame):
    st.markdown("#### Unusual cost structures")
    st.caption("A robust z-score compares each line with the company's ATECO peers (whole portfolio when a sector has fewer than "
               f"{ca.MIN_NACE_PEERS} companies). The line must also be at least the chosen number of percentage points away, so tiny lines don't flood the list.")
    c1, c2, c3 = st.columns(3)
    basis = c1.radio("Compare on", ["share", "rev"], format_func=lambda k: {"share": "% of production costs", "rev": "% of revenue"}[k], key="cs_out_basis", horizontal=True)
    gap = c2.slider("Minimum gap vs peers (pp)", 2.0, 25.0, 8.0, 1.0, key="cs_out_gap")
    cat = c3.selectbox("Only this line", ["(all)"] + cb.CATEGORY_KEYS, format_func=lambda k: "All lines" if k == "(all)" else cb.LABELS[k], key="cs_out_cat")
    t = ca.anomaly_table(frame, basis=basis, top=400, min_gap_pp=gap)
    if cat != "(all)" and not t.empty:
        t = t[t["category"] == cat]
    if t.empty:
        st.info("No company clears that threshold.")
    else:
        st.dataframe(t.head(60).drop(columns=["company_id", "category"]), hide_index=True, use_container_width=True, column_config={
            "Value %": st.column_config.NumberColumn(format="%.1f%%"), "Sector median %": st.column_config.NumberColumn(format="%.1f%%"),
            "Gap (pp)": st.column_config.NumberColumn(format="%+.1f"), "Robust z": st.column_config.NumberColumn(format="%+.1f"),
            "Total costs (k EUR)": st.column_config.NumberColumn(format="%.0f")})

    st.divider()
    st.markdown("#### Whose cost mix is shifting fastest")
    st.caption("Change in a line's share of production costs between the oldest filed year available and the latest.")
    m1, m2 = st.columns(2)
    mcat = m1.selectbox("Cost line", cb.CATEGORY_KEYS, format_func=lambda k: cb.LABELS[k], key="cs_mov_cat", index=1)
    rising = m2.radio("Direction", ["Rising share", "Falling share"], horizontal=True, key="cs_mov_dir") == "Rising share"
    names = dict(zip(frame["company_id"], frame["legal_name"]))
    mv = ca.share_movers(_blobs(db), names, mcat, top=30, rising=rising)
    if mv.empty:
        st.info("No history available.")
    else:
        st.dataframe(mv, hide_index=True, use_container_width=True, column_config={
            "Share then %": st.column_config.NumberColumn(format="%.1f%%"), "Share now %": st.column_config.NumberColumn(format="%.1f%%"),
            "Change (pp)": st.column_config.NumberColumn(format="%+.1f"), "Costs now (k EUR)": st.column_config.NumberColumn(format="%.0f")})


# ---------------------------------------------------------------------------------------------- Sector map
def _render_sector_map(frame: pd.DataFrame):
    st.markdown("Median cost structure by ATECO code - the 'normal' a company is compared against.")
    basis = st.radio("Basis", ["share", "rev"], format_func=lambda k: {"share": "% of production costs", "rev": "% of revenue"}[k],
                     horizontal=True, key="cs_map_basis")
    hm = ca.sector_heatmap(frame, basis=basis)
    if hm.empty:
        st.info("No sector has enough companies.")
        return
    z = hm.drop(columns="Companies")
    fig = px.imshow(z.round(1), text_auto=".1f", aspect="auto", color_continuous_scale="Viridis",
                    labels=dict(color="median %"), y=[f"{i}  (n={n})" for i, n in zip(hm.index, hm["Companies"])])
    fig.update_layout(template="plotly_dark", height=max(320, 34 * len(hm) + 120), margin=dict(t=20))
    fig.update_xaxes(side="top")
    st.plotly_chart(fig, use_container_width=True, key="cs_sector_map")
    st.caption(f"Sectors with at least {ca.MIN_NACE_PEERS} companies. Each cell is the median of that line within the sector.")


def render_cost_structure_page(db: Session, show_title: bool = True):
    """Cross-company cost analysis. One company's cost X-ray lives in Company Intelligence > Costs."""
    if show_title:
        st.title("💸 Cost Structure")
    st.caption("The nine AIDA production-cost lines across the portfolio - absolute thousand EUR, share of costs and share of revenue, "
               "with peer comparison. One company's full X-ray is in 🏢 Company Intelligence > Costs.")
    frame = _frame(db)
    if frame.empty:
        st.warning("No cost-breakdown data imported yet. Run `python scripts/import_cost_breakdown.py --file <export> --apply`.")
        return
    t1, t2, t3 = st.tabs(["🏭 Portfolio", "🚨 Outliers & movers", "🗺️ Sector map"])
    with t1:
        _render_portfolio(db, frame)
    with t2:
        _render_outliers(db, frame)
    with t3:
        _render_sector_map(frame)
