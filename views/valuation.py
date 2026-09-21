"""
Valuation (Page 7) — an indicative enterprise/equity value range for every company that has
financials, built from two methods shown side by side (listed-sector multiples re-based for
risk, and a 5-year DCF). A third read beside NEED and READINESS, deliberately never blended
into either; it also turns the margin gap to peers into euros ("value at stake").

Everything shown traces to an imported company figure, a dated reference value or a named
assumption — see valuation.py for the method and valuation_data.py for the sources. This page
only presents; it computes nothing itself.
"""

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy.orm import Session

import valuation_data as vd
import valuation_service as vs
from valuation import ValuationResult, value_company, value_universe

DISCLAIMER = (
    "**Indicative screening value, not a fairness opinion.** It is computed from imported financials, published "
    "market data and named assumptions — nothing is estimated by a language model and nothing is invented. "
    "It cannot see what a real analyst would adjust for (owner pay, one-offs, leases, pensions, customer "
    "concentration, order book). Read the range, the method gap and the caveats, not just the midpoint."
)
AUTO = "Auto (company's own country)"
GRADE_HELP = ("Input quality, A–D: how complete and plausible the company's own data is (years of history, margin "
              "stability, peer benchmark, sector match, unit sanity). It says nothing about whether the model is "
              "right — that is the method gap.")


# ------------------------------------------------------------------ formatting

def _m(x, d: int = 1) -> str:
    return "—" if x is None else f"€{x / 1e6:,.{d}f}M"


def _pct(x, d: int = 0) -> str:
    return "—" if x is None else f"{x:.{d}%}"


def _k(x) -> str:
    return "—" if x is None else f"{x / 1e3:,.0f}"


def _gap_label(gap) -> str:
    if gap is None:
        return "—"
    return f"{gap:.0%} " + ("(close)" if gap <= 0.15 else "(moderate)" if gap <= 0.35 else "(wide)")


# ------------------------------------------------------------------ cached universe

@st.cache_data(ttl=600, show_spinner="Valuing every company…")
def _cached_universe(_db: Session, fingerprint):
    """Companies, their financials, the valuation context and every company's result — one
    pass, cached until anything a valuation depends on changes (the fingerprint is the key)."""
    companies, fins, ctx = vs.load_universe(_db)
    return companies, fins, ctx, value_universe(companies, fins, ctx)


def _universe(db: Session):
    return _cached_universe(db, vs.universe_fingerprint(db))


def _clear_cache():
    _cached_universe.clear()


# ------------------------------------------------------------------ football field

def _football(res: ValuationResult) -> go.Figure:
    rows = [(m.label, m.ev_low, m.ev_base, m.ev_high, "#60A5FA") for m in res.methods]
    rows.append(("Headline range", res.ev_low, res.ev_base, res.ev_high, "#F59E0B"))
    fig = go.Figure()
    for label, lo, base, hi, color in rows:
        fig.add_trace(go.Bar(y=[label], x=[max(hi - lo, 0) / 1e6], base=[lo / 1e6], orientation="h",
                             marker_color=color, opacity=0.9, showlegend=False,
                             hovertemplate=f"{label}<br>€{lo/1e6:,.1f}M – €{hi/1e6:,.1f}M<extra></extra>"))
        fig.add_trace(go.Scatter(x=[base / 1e6], y=[label], mode="markers", showlegend=False,
                                 marker=dict(symbol="diamond", size=12, color="white", line=dict(color="#111", width=1.5)),
                                 hovertemplate=f"{label} base: €{base/1e6:,.1f}M<extra></extra>"))
    fig.update_layout(height=130 + 46 * len(rows), margin=dict(l=10, r=10, t=10, b=10),
                      xaxis=dict(title="Enterprise value (€M)", automargin=True),
                      yaxis=dict(autorange="reversed", automargin=True),
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", bargap=0.35)
    return fig


# ------------------------------------------------------------------ the per-company panel

def _reference_missing_box(db: Session, key: str):
    st.info("Market reference data (Damodaran sector multiples, cost of capital and country risk) has not been "
            "loaded yet. It is free and takes a few seconds.")
    if st.button("⬇️ Load market reference data", key=f"{key}_load_ref", type="primary"):
        _refresh(db)


def _refresh(db: Session):
    try:
        with st.spinner("Downloading Damodaran's Europe datasets…"):
            summary = vd.refresh_reference_data(db)
    except Exception as e:  # noqa: BLE001 — network / layout problems must be shown, not crash the page
        st.error(f"Could not refresh the reference data — nothing was changed.\n\n`{type(e).__name__}: {e}`")
        return
    _clear_cache()
    st.success("Loaded: " + ", ".join(f"{s['dataset']} ({s['rows']} rows, as of {s['as_of']})" for s in summary))
    st.rerun()


def _render_result(res: ValuationResult, ctx, key: str):
    A = ctx.assumptions
    if res.status != "valued":
        st.warning(f"**Not valued.** {res.reason}")
        if res.reason_code == "no_reference":
            st.caption("Load the reference data on the Valuation → Assumptions tab.")
        return

    # ---- headline
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Enterprise value (base)", _m(res.ev_base),
              f"{_m(res.ev_low)} – {_m(res.ev_high)}", delta_color="off",
              help="Weighted mix of the DCF and the multiples methods; the range spans both methods and their low/high cases.")
    if res.equity_base is not None:
        c2.metric("Equity value (base)", _m(res.equity_base), f"{_m(res.equity_low)} – {_m(res.equity_high)}",
                  delta_color="off", help="Enterprise value minus imported debt plus imported cash.")
    else:
        c2.metric("Equity value", "withheld", help=res.equity_withheld)
    c3.metric("Implied EV / normalised EBITDA", f"{res.implied_ev_ebitda:.1f}×",
              help="Headline base ÷ normalised EBITDA — the number to sanity-check against deals you know.")
    c4.metric("Input quality", res.confidence_grade, f"Method gap {_gap_label(res.disagreement)}",
              delta_color="off", help=GRADE_HELP)
    if res.equity_withheld:
        st.info(f"**Equity value withheld.** {res.equity_withheld}")

    st.plotly_chart(_football(res), use_container_width=True, key=f"{key}_football")
    st.caption("Bars = low–high case of each method; ◆ = base case. The headline range stretches to cover every method.")

    for w in res.warnings:
        if "differ by" in w or "overridden" in w or "assumed" in w or "units" in w:
            st.warning(w)

    # ---- value at stake
    v = res.value_at_stake
    if v:
        st.markdown("#### 🎯 Value at stake in the margin gap")
        s1, s2, s3, s4 = st.columns(4)
        s1.metric("Normalised EBITDA margin", _pct(res.margin_norm, 1))
        s2.metric(f"Peer median ({v['peers']:,} peers)", _pct(v["peer_median_margin"], 1), f"−{v['gap_pp'] * 100:.1f} pp gap",
                  delta_color="inverse")
        s3.metric("EBITDA if the gap closed", _m(v["ebitda_uplift"], 2))
        s4.metric("Enterprise value at stake", _m(v["ev_uplift"]), help=f"EBITDA uplift × the headline implied multiple ({v['multiple']:.1f}×).")
        st.caption("Not part of the valuation above (the base case gives no turnaround credit). It is what closing the margin gap "
                   "to the median peer in this database would be worth — the NEED axis, in euros.")
    elif res.peer:
        st.caption(f"At or above the peer median margin ({_pct(res.peer.median_margin, 1)}, {res.peer.n:,} peers) — "
                   "no margin-gap value at stake.")

    # ---- how it was built
    with st.expander("🧮 How this value was built", expanded=False):
        st.markdown("**1 · Normalised earnings**")
        hist = pd.DataFrame([{"Period": h["period"], "Revenue (€k)": _k(h["revenue"]), "EBITDA (€k)": _k(h["ebitda"]),
                              "EBIT (€k)": _k(h["ebit"]), "EBITDA margin": _pct(h["margin"], 1),
                              "Cash (€k)": _k(h["cash"]), "Debt as imported (€k)": _k(h["debt"])} for h in res.history])
        st.dataframe(hist, hide_index=True, use_container_width=True)
        w = ctx.weights()
        st.caption(f"Normalised margin {_pct(res.margin_norm, 2)} = weighted average of {res.margin_years} year(s) "
                   f"(weights {w[0]:.1f}/{w[1]:.1f}/{w[2]:.1f}) × latest revenue {_m(res.revenue, 2)} = "
                   f"**normalised EBITDA {_m(res.ebitda_norm, 2)}** (latest reported {_m(res.ebitda_latest, 2)}). "
                   f"Source dataset: {res.dataset}.")

        d = res.discount
        st.markdown("**2 · Discount rate (company WACC)**")
        st.dataframe(pd.DataFrame([
            {"Step": f"Sector cost of capital — {res.industry} (listed, Europe, EUR)", "Rate": _pct(d.sector_wacc, 2)},
            {"Step": f"+ Country risk ({res.param_country}: ERP {_pct(d.country_erp, 2)} vs Europe {_pct(d.region_erp, 2)})",
             "Rate": f"{d.country_adjustment:+.2%}"},
            {"Step": "+ Size & illiquidity premium (assumption)", "Rate": _pct(d.size_premium, 2)},
            {"Step": "= Company discount rate", "Rate": _pct(d.wacc, 2)},
        ]), hide_index=True, use_container_width=True)

        st.markdown("**3 · Multiples method** — a listed multiple, re-based for the higher discount rate")
        mrows = []
        for m in res.methods:
            if m.family != "multiples":
                continue
            ms = m.detail["multiples"]
            mrows.append({"Method": m.label, "Listed multiple": f"{m.detail['listed_multiple']:.1f}×" if m.detail.get("listed_multiple") else "—",
                          "Applied low / base / high": f"{ms['low']:.1f}× / {ms['base']:.1f}× / {ms['high']:.1f}×",
                          m.detail["metric_label"]: _m(m.detail["metric"], 2),
                          "EV low": _m(m.ev_low), "EV base": _m(m.ev_base), "EV high": _m(m.ev_high)})
        st.dataframe(pd.DataFrame(mrows), hide_index=True, use_container_width=True)
        st.caption("adjusted multiple = listed × (sector rate − growth) ÷ (company rate − growth). A listed company's multiple "
                   "assumes low risk and easy resale; this removes that premium using the same rate as the DCF.")

        dcf = res.method("dcf")
        if dcf:
            st.markdown("**4 · DCF method** — five-year free cash flow + terminal value")
            i = dcf.detail["inputs"]
            st.caption(f"Year-1 growth {_pct(i.growth1, 1)} (own past growth {_pct(dcf.detail['own_growth'], 1)} blended with peers "
                       f"{_pct(dcf.detail['peer_growth'], 1)}, fading to {_pct(dcf.detail['terminal_growth'], 1)}) · margin "
                       f"{_pct(i.margin0, 1)} → {_pct(i.margin_target, 1)} · D&A {_pct(i.da_pct, 1)} and capex {_pct(i.capex_pct, 1)} of revenue · "
                       f"tax {_pct(i.tax_rate, 1)} · working capital {_pct(i.nwc_pct, 1)} of revenue growth · "
                       f"terminal value = {_pct(dcf.detail['terminal_value_share'])} of the DCF.")
            t = res.dcf_table
            st.dataframe(pd.DataFrame({
                "€k": ["Revenue", "growth", "EBITDA margin", "EBITDA", "D&A", "EBIT", "Tax", "Capex", "Δ working capital",
                       "Free cash flow", "Present value"],
                **{f"Year {r['year']}": [_k(r["revenue"]), _pct(r["growth"], 1), _pct(r["ebitda_margin"], 1), _k(r["ebitda"]),
                                          _k(r["da"]), _k(r["ebit"]), _k(r["tax"]), _k(r["capex"]), _k(r["d_nwc"]),
                                          _k(r["fcf"]), _k(r["pv"])] for r in t}}),
                hide_index=True, use_container_width=True)
            sens = res.sensitivity
            st.markdown("DCF enterprise value (€M) — discount rate × long-run growth")
            st.dataframe(pd.DataFrame(
                [[("—" if e is None else f"{e / 1e6:,.1f}") for e in line] for line in sens["ev"]],
                index=[f"{w:.1%}" for w in sens["wacc"]], columns=[f"g {g:.1%}" for g in sens["growth"]]),
                use_container_width=True)

        st.markdown("**5 · Everything used, with its source**")
        st.dataframe(pd.DataFrame(res.inputs_used), hide_index=True, use_container_width=True)

    if res.peer:
        st.caption(f"Peer benchmark: {res.peer.n:,} companies in {res.country}, NACE division {str(res.nace_code)[:2]} — median "
                   f"normalised EBITDA margin {_pct(res.peer.median_margin, 1)}, median revenue growth {_pct(res.peer.median_growth, 1)}.")

    with st.expander("⚠️ Caveats and input-quality notes", expanded=False):
        st.markdown("**Always true of this valuation:**")
        for line in (
            "Earnings are normalised mechanically (weighted 3-year margin). Owner pay, one-offs and other adjustments an "
            "analyst would make are not in the data.",
            "Only three years of history are available, and the peer benchmark is the companies in *this database* "
            "(a selection, not the whole market).",
            "Listed-company multiples carry a growth and quality premium a small private company may not earn; the DCF "
            "prices its risk in full. That is why both are shown and the range spans them.",
            "The size & illiquidity premium is a judgment call and the most influential assumption (Assumptions tab).",
        ):
            st.markdown(f"- {line}")
        if res.confidence_reasons:
            st.markdown("**Input quality " + res.confidence_grade + f" ({res.confidence_score}/100) — deductions:**")
            for pts, text in res.confidence_reasons:
                st.markdown(f"- {pts} · {text}")
        else:
            st.markdown(f"**Input quality {res.confidence_grade}** ({res.confidence_score}/100) — no deductions.")
        for w in res.warnings:
            st.markdown(f"- {w}")


def render_company_valuation(db: Session, company_id: str, key: str = "val"):
    """The valuation of one company — used on the Valuation page and inside Company Intelligence."""
    companies, fins, ctx, results = _universe(db)
    if not ctx.reference.loaded:
        _reference_missing_box(db, key)
        return
    countries = sorted(((ctx.reference.data.get("country_risk") or {}).get("countries") or {}).keys())
    options = [AUTO] + [c for c in ("Italy", "Germany") if c in countries] + [c for c in countries if c not in ("Italy", "Germany")]
    meta = next((c for c in companies if c["id"] == company_id), None)
    if meta is None:
        st.warning("Company not found.")
        return
    left, right = st.columns([1, 3])
    choice = left.selectbox("Country parameters", options, key=f"{key}_country",
                            help="Which country's tax rate and country risk to apply. Auto uses the company's own recorded "
                                 "country (falling back to the default when none is recorded).")
    override = None if choice == AUTO else choice
    if override:
        res = value_company(meta, fins.get(company_id), ctx, country_override=override)
    else:
        res = next(r for r in results if r.company_id == company_id)
    right.caption(f"Applied: **{res.param_country or ctx.default_country()}**"
                  + (" (assumed — no country recorded)" if res.country_assumed else "")
                  + f" · sector reference: **{res.industry or '—'}**"
                  + (f" ({res.industry_note})" if res.industry_note else "")
                  + f" · market data as of {ctx.reference.as_of('multiples') or '—'}")
    _render_result(res, ctx, key)


# ------------------------------------------------------------------ tab: universe screen

def _universe_frame(companies, results) -> pd.DataFrame:
    by_id = {c["id"]: c for c in companies}
    rows = []
    for r in results:
        c = by_id.get(r.company_id, {})
        valued = r.status == "valued"
        rows.append({
            "Company": r.company_name, "Country": r.country, "Sector": r.industry or "—",
            "Status": "Valued" if valued else "Not valued",
            "Why not": "" if valued else r.reason,
            "Revenue (€M)": (r.revenue / 1e6) if r.revenue else None,
            "Norm. EBITDA (€M)": (r.ebitda_norm / 1e6) if valued else None,
            "EV low (€M)": (r.ev_low / 1e6) if valued else None,
            "EV base (€M)": (r.ev_base / 1e6) if valued else None,
            "EV high (€M)": (r.ev_high / 1e6) if valued else None,
            "EV / EBITDA": r.implied_ev_ebitda if valued else None,
            "Method gap": r.disagreement if valued else None,
            "Equity base (€M)": (r.equity_base / 1e6) if valued and r.equity_base is not None else None,
            "Value at stake (€M)": (r.value_at_stake["ev_uplift"] / 1e6) if valued and r.value_at_stake else None,
            "Input quality": r.confidence_grade if valued else "",
            "Need": c.get("need_score"), "Readiness": c.get("readiness_score"),
        })
    return pd.DataFrame(rows)


def _render_universe_tab(db: Session):
    companies, fins, ctx, results = _universe(db)
    if not ctx.reference.loaded:
        _reference_missing_box(db, "uni")
        return
    df = _universe_frame(companies, results)
    valued = df[df["Status"] == "Valued"]
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Companies", f"{len(df):,}")
    m2.metric("Valued", f"{len(valued):,}")
    m3.metric("Not valued", f"{len(df) - len(valued):,}")
    m4.metric("Median EV (base)", _m(valued["EV base (€M)"].median() * 1e6) if len(valued) else "—")
    refused = df[df["Status"] != "Valued"]["Why not"].value_counts()
    if len(refused):
        st.caption("Not valued because: " + "; ".join(f"{n} × {why}" for why, n in refused.items()))

    f1, f2, f3 = st.columns([2, 1, 1])
    q = f1.text_input("Search company", key="uni_q", placeholder="name contains…")
    status = f2.selectbox("Show", ["All", "Valued only", "Not valued only"], key="uni_status")
    min_ev = f3.number_input("Min EV base (€M)", min_value=0.0, value=0.0, step=1.0, key="uni_minev")
    view = df
    if q:
        view = view[view["Company"].str.contains(q, case=False, na=False)]
    if status == "Valued only":
        view = view[view["Status"] == "Valued"]
    elif status == "Not valued only":
        view = view[view["Status"] != "Valued"]
    if min_ev:
        view = view[view["EV base (€M)"].fillna(0) >= min_ev]

    st.dataframe(
        view.sort_values("EV base (€M)", ascending=False, na_position="last"),
        hide_index=True, use_container_width=True, height=560,
        column_config={
            "Revenue (€M)": st.column_config.NumberColumn(format="%.1f"),
            "Norm. EBITDA (€M)": st.column_config.NumberColumn(format="%.2f"),
            "EV low (€M)": st.column_config.NumberColumn(format="%.1f"),
            "EV base (€M)": st.column_config.NumberColumn(format="%.1f"),
            "EV high (€M)": st.column_config.NumberColumn(format="%.1f"),
            "EV / EBITDA": st.column_config.NumberColumn(format="%.1f×"),
            "Method gap": st.column_config.NumberColumn(format="percent", help="|DCF − multiples| ÷ their average"),
            "Equity base (€M)": st.column_config.NumberColumn(format="%.1f", help="Withheld until the debt definition is confirmed (Assumptions tab)."),
            "Value at stake (€M)": st.column_config.NumberColumn(format="%.1f", help="Enterprise value of closing the margin gap to the peer median — not part of EV."),
            "Need": st.column_config.NumberColumn(format="%.0f"), "Readiness": st.column_config.NumberColumn(format="%.0f"),
        },
    )
    st.download_button("⬇️ Download this table (CSV)", view.to_csv(index=False).encode("utf-8"),
                       "vienna_valuation_screen.csv", "text/csv")
    st.caption("Sorted by base enterprise value. Need/Readiness are shown for reference only — valuation is a separate lens and never feeds either score.")


# ------------------------------------------------------------------ tab: assumptions & data

def _render_assumptions_tab(db: Session):
    companies, fins, ctx, results = _universe(db)
    ref, A = ctx.reference, ctx.assumptions

    st.markdown("### 📚 Market reference data")
    if ref.loaded:
        rows = [{"Dataset": vd.REFERENCE_LABELS[n], "Published (as of)": ref.as_of(n) or "—",
                 "Fetched": (ref.meta[n].get("fetched_at").strftime("%Y-%m-%d") if ref.meta[n].get("fetched_at") else "—"),
                 "Rows": len((ref.data[n].get("industries") or ref.data[n].get("countries") or {})),
                 "Source": ref.meta[n].get("source_url")} for n in vd.REFERENCE_FILES]
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True,
                     column_config={"Source": st.column_config.LinkColumn("Source")})
    else:
        st.info("Not loaded yet.")
    st.caption("Prof. Damodaran's free industry datasets (NYU Stern), Europe edition — listed companies, refreshed each January. "
               "A refresh replaces all four datasets together, or none if any download fails.")
    if st.button("🔄 Refresh from Damodaran", key="ref_refresh"):
        _refresh(db)

    st.markdown("---")
    st.markdown("### 🧾 Net-debt definition")
    verified = bool(A.num("net_debt_definition_verified"))
    st.markdown(A.rows[("net_debt_definition_verified", "*")]["rationale"])
    new_verified = st.checkbox("I have confirmed the imported 'total debt' is financial debt — show equity value",
                               value=verified, key="verified_box")
    if new_verified != verified:
        vd.set_net_debt_verified(db, new_verified)
        _clear_cache()
        st.rerun()

    st.markdown("---")
    st.markdown("### ⚙️ Assumptions")
    st.caption("Every number the model uses that is neither a company figure nor published market data. **Source** says where it comes "
               "from — where it says *Judgment*, nobody published it and it is yours to replace. Percent values are shown in %.")
    order = {a["key"]: i for i, a in enumerate(vd.DEFAULT_ASSUMPTIONS)}
    keys = sorted([k for (k, s), r in A.rows.items() if s == "*" and k not in ("net_debt_definition_verified", "default_country")
                   and r.get("value_num") is not None], key=lambda k: order.get(k, 999))
    table = []
    for k in keys:
        r = A.rows[(k, "*")]
        is_pct = r["unit"] == "pct"
        table.append({"key": k, "Assumption": r["label"], "Value": r["value_num"] * (100 if is_pct else 1),
                      "Unit": "%" if is_pct else r["unit"], "Source": r["source"], "Why": r["rationale"]})
    tdf = pd.DataFrame(table)
    edited = st.data_editor(tdf, hide_index=True, use_container_width=True, key="assump_editor",
                            disabled=["key", "Assumption", "Unit", "Source", "Why"],
                            column_config={"key": None, "Value": st.column_config.NumberColumn(format="%.4g")})
    dc = st.text_input("Default country (used only when a company has none recorded)", value=A.text("default_country") or "Italy",
                       key="default_country_in")
    if st.button("💾 Save assumptions", type="primary", key="assump_save"):
        changes = {new["key"]: float(new["Value"]) / (100 if new["Unit"] == "%" else 1)
                   for new in edited.to_dict("records") if pd.notna(new["Value"])}
        n = vd.apply_assumption_changes(db, changes, dc)
        _clear_cache()
        st.success(f"Saved — {n} assumption(s) changed.")
        st.rerun()

    st.markdown("---")
    st.markdown("### 🏭 Manual sector multiples")
    st.caption("If you know what companies like these actually sell for — deal comps, a broker's view — enter the EV/EBITDA here. "
               "It replaces the re-based listed multiple for that sector (the EV/EBIT method and the DCF are unaffected); "
               f"the band around it is ±{A.num('override_band'):.0%}.")
    ov = A.scoped(vd.OVERRIDE_KEY)
    ov_df = pd.DataFrame([{"Sector": s, "EV/EBITDA": v} for s, v in sorted(ov.items())], columns=["Sector", "EV/EBITDA"])
    ov_edit = st.data_editor(ov_df, num_rows="dynamic", hide_index=True, use_container_width=True, key="override_editor",
                             column_config={"Sector": st.column_config.SelectboxColumn(options=ref.industries or None, required=True),
                                            "EV/EBITDA": st.column_config.NumberColumn(min_value=0.5, max_value=40.0, format="%.1f×", required=True)})
    if st.button("💾 Save manual multiples", key="override_save"):
        new = {r["Sector"]: float(r["EV/EBITDA"]) for r in ov_edit.to_dict("records") if r.get("Sector") and pd.notna(r.get("EV/EBITDA"))}
        vd.set_sector_overrides(db, new)
        _clear_cache()
        st.success("Saved.")
        st.rerun()

    st.markdown("---")
    st.markdown("### 🗺️ NACE / ATECO → sector reference")
    st.caption("Which Damodaran industry stands in for each code prefix (longest matching prefix wins). A company whose code matches "
               "nothing is reported as *not valued* rather than compared with a guessed peer group. Financial firms (NACE 64–66) are "
               "deliberately unmapped.")
    map_df = pd.DataFrame([{"Prefix": p, "Industry": i, "Note": n} for p, (i, n) in sorted(ctx.sector_map.items())])
    map_edit = st.data_editor(map_df, num_rows="dynamic", hide_index=True, use_container_width=True, height=360, key="map_editor",
                              column_config={"Prefix": st.column_config.TextColumn(required=True, help="Digits only, e.g. 28 or 2829"),
                                             "Industry": st.column_config.SelectboxColumn(options=ref.industries or None, required=True)})
    if st.button("💾 Save sector map", key="map_save"):
        n = vd.replace_sector_map(db, {str(r.get("Prefix") or ""): (r.get("Industry"), r.get("Note") or "")
                                       for r in map_edit.to_dict("records")})
        _clear_cache()
        st.success(f"Saved — {n} prefixes.")
        st.rerun()


# ------------------------------------------------------------------ the page

def render_valuation_page(db: Session):
    st.title("💶 Company Valuation")
    st.caption("A third lens beside NEED and READINESS — what a company is roughly worth, and how much of that is at stake in the gaps we score. "
               "Never blended into either score.")
    st.info(DISCLAIMER)
    tab_company, tab_screen, tab_assump = st.tabs(["🏢 Company valuation", "📊 Universe screen", "⚙️ Assumptions & data"])

    with tab_company:
        companies, _, ctx, results = _universe(db)
        if not companies:
            st.warning("No companies in the database yet.")
        else:
            valued_ids = {r.company_id for r in results if r.status == "valued"}
            labels = {f"{c['legal_name']} ({c['registration_number']}) — {c['country'] or '—'}"
                      + ("" if c["id"] in valued_ids else "  · not valued"): c["id"] for c in companies}
            pick = st.selectbox("Company", list(labels), key="val_company", help="Type to search.")
            render_company_valuation(db, labels[pick], key="valpage")
    with tab_screen:
        _render_universe_tab(db)
    with tab_assump:
        _render_assumptions_tab(db)
