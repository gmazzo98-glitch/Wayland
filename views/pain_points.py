"""
Pain Points (Page 6) and the per-company pain-point panel on Company Intelligence.

Every claim here is computed by painpoints.py from indicator values — see its module
docstring for the rules (real observations only, "not checked" is never "no pain",
severity capped by evidence coverage). This file only presents the results and lets the
thresholds be edited; it contains no detection logic of its own.
"""

from collections import defaultdict

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy import func
from sqlalchemy.orm import Session

from indicators import fetch_indicator_defs
from models import Company, PainPointDefinition, SignalRecord
from painpoints import (
    CONFIRMED_STATUSES, EMERGING_FROM, MODERATE_FROM, PAIN_POINT_SEED, PROPOSED_INDICATORS,
    SEVERE_FROM, STATUS_ORDER, collect_gaps, evaluate_pain_points, fetch_pain_point_defs, rank_detected,
    summarize_portfolio, validate_rules,
)
from scoring import build_signal_map, calculate_company_scores
from views.indicator_weights import _live_coverage

STATUS_META = {
    "severe": ("🔴", "Severe"),
    "moderate": ("🟠", "Moderate"),
    "emerging": ("🟡", "Emerging"),
    "clear": ("🟢", "Not detected"),
    "insufficient_data": ("⚪", "Can't assess yet"),
}

# Severity is an ordered magnitude, so the chart uses ONE hue stepped light->dark (validated as an
# ordinal ramp against the dark surface with the dataviz skill's validator). On the app's dark
# background the brightest step reads as strongest.
SEVERITY_COLORS = {"emerging": "#184f95", "moderate": "#3987e5", "severe": "#9ec5f4"}
SURFACE = "#0F172A"

KIND_LABELS = {"derivation": "derive from data already held", "crawler": "crawler / adapter extension",
               "api": "new data source", "needs_source": "needs a real source", "manual": "manual entry",
               "aida_export": "tick it in the next AIDA export"}
PROPOSAL_STATUS_LABELS = {"ready_to_build": "🟢 Ready to build", "needs_decision": "🟠 Needs a decision first",
                          "needs_source": "🔵 Needs a new source"}


def _status_text(status: str) -> str:
    icon, label = STATUS_META[status]
    return f"{icon} {label}"


def _producer_text(driver: dict) -> str:
    p = driver["producer"]
    if p.get("proposed"):
        return f"proposed ({KIND_LABELS.get(p['kind'], p['kind'])}): {p['how']}"
    if p.get("source_system"):
        return f"{p['source_system']} ({p.get('tier') or '?'}, phase {p.get('phase')})"
    return "no producer yet"


# =============================================================================================
# Per-company panel (Company Intelligence)
# =============================================================================================

def _evidence_table(result: dict) -> pd.DataFrame:
    rows = []
    for d in result["drivers"]:
        if not d["assessed"]:
            continue
        as_of = d["fetched_at"].strftime("%Y-%m-%d") if d["fetched_at"] else "—"
        notes = " · ".join(x for x in (d["summary"], d["note"]) if x)
        rows.append({
            "Indicator": d["label"],
            "Reading": d["value_text"] or "—",
            "Pain rule": d["threshold_text"],
            "Pain strength": round(d["intensity"] * 100),
            "Weight": d["weight"],
            "Source": d["source"] or "—",
            "As of": as_of + (" ⏳ stale" if d["is_stale"] else ""),
            "Notes": notes or "—",
        })
    return pd.DataFrame(rows)


def _render_pain_point_card(result: dict):
    icon, label = STATUS_META[result["status"]]
    # A capped result (e.g. strength 100 on one of seven drivers) says so up front, so the band and the
    # number don't look contradictory.
    tail = f"strength {result['score']:.0f}/100 on thin evidence" if result["capped"] else f"{result['score']:.0f}/100"
    title = f"{icon} **{result['label']}** — {label} · {tail}"
    with st.expander(title, expanded=result["status"] in CONFIRMED_STATUSES):
        st.markdown(result["description"] or "")
        if result["headline"]:
            st.markdown(f"**Evidence:** {result['headline']}")

        cov = f"{result['coverage']:.0%}"
        basis = f"Based on {result['drivers_assessed']} of {result['drivers_total']} drivers ({cov} of the evidence weight)."
        if result["capped"]:
            basis += " Severity is held back because too little of the evidence could be checked."
        st.caption(("✅ Well evidenced. " if result["confidence"] == "well_evidenced" else "⚠️ Partial evidence. ") + basis)

        st.dataframe(
            _evidence_table(result), hide_index=True, width="stretch",
            column_config={
                "Pain strength": st.column_config.NumberColumn(
                    "Pain strength", format="%d",
                    help="0 = no pain by this rule, 100 = full strength. The score above is the weight-averaged strength of the drivers that could be checked."),
                "Notes": st.column_config.TextColumn("Notes", width="large"),
            },
        )
        missing = [d for d in result["drivers"] if not d["assessed"]]
        if missing:
            st.markdown("**Not counted (no usable evidence):**")
            for d in missing:
                st.markdown(f"- ⚪ {d['label']} — {d['unassessed_text']}")
        if result["pilot_angle"]:
            st.markdown(f"**Pilot angle:** {result['pilot_angle']}")
        if result["caveat"]:
            st.info(result["caveat"], icon="⚠️")


def render_company_pain_points(db: Session, signals: list, indicator_defs: dict):
    """The 'what is this company struggling with' section. `signals` are the company's SignalRecords."""
    pain_defs = fetch_pain_point_defs(db)
    if not pain_defs:
        return
    results = evaluate_pain_points(build_signal_map(signals, indicator_defs), indicator_defs, pain_defs)
    detected = rank_detected(results)
    clear = [r for r in results if r["status"] == "clear"]
    blind = [r for r in results if r["status"] == "insufficient_data"]
    confirmed = [r for r in detected if r["status"] in CONFIRMED_STATUSES]

    st.subheader("🩺 Pain Points")
    st.caption(
        "What this company appears to be struggling with — read only from its indicators, never guessed. "
        "Only real observations count (a simulated placeholder is never used), and a pain point that can't be "
        "checked is listed as a gap, not as “no pain”. This is a diagnosis alongside Need and Readiness, not part of either score."
    )
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Confirmed", len(confirmed), help="Moderate or severe.")
    m2.metric("Emerging", len(detected) - len(confirmed), help="Early signs — weaker than moderate.")
    m3.metric("Checked, not detected", len(clear))
    m4.metric("Can't assess yet", len(blind), help="No usable indicator data — see the table below for what would fill it.")

    if not detected:
        st.info("No pain point detected in the data available for this company." if len(blind) < len(results)
                else "None of the pain points can be assessed yet — no usable indicator data for this company.")
    for r in detected:
        _render_pain_point_card(r)

    if clear:
        with st.expander(f"🟢 Checked, not detected ({len(clear)})"):
            st.caption("Nothing fired on the evidence available — but check the coverage: a pain point seen through "
                       "one driver out of five is a weaker all-clear.")
            st.dataframe(pd.DataFrame([{
                "Pain point": r["label"], "Evidence coverage": f"{r['coverage']:.0%}",
                "Drivers checked": f"{r['drivers_assessed']} of {r['drivers_total']}",
                "Score": r["score"],
            } for r in clear]), hide_index=True, width="stretch")
    if blind:
        with st.expander(f"⚪ Can't assess yet ({len(blind)}) — and what would fill each"):
            st.caption("These aren't “clear” — there's simply no usable data. The last column is where the data would come from.")
            st.dataframe(pd.DataFrame([{
                "Pain point": r["label"],
                "Missing indicators": "; ".join(d["label"] for d in r["drivers"]),
                "Would come from": "; ".join(_producer_text(d) for d in r["drivers"]),
            } for r in blind]), hide_index=True, width="stretch",
                column_config={"Would come from": st.column_config.TextColumn(width="large")})


# =============================================================================================
# Portfolio evaluation (cached)
# =============================================================================================

_SIGNAL_COLS = (
    SignalRecord.company_id, SignalRecord.signal_key, SignalRecord.status, SignalRecord.numeric_value,
    SignalRecord.fetched_at, SignalRecord.is_simulated, SignalRecord.source,
)


def _signals_fingerprint(db: Session):
    return tuple(db.query(func.count(SignalRecord.id), func.max(SignalRecord.fetched_at)).one())


@st.cache_data(ttl=300, show_spinner="Reading pain points across the portfolio…")
def _evaluate_portfolio(_db: Session, _indicator_defs: dict, _pain_defs: list, signals_fp, defs_fp):
    """
    Evaluates every company from ONE bulk signal query. Returns only compact per-company rows (status,
    score, coverage, headline per pain point) plus the portfolio-wide gap analysis — the full per-driver
    evidence is only ever needed for one company at a time and is rebuilt on Company Intelligence.
    The two fingerprint args are the cache key (a pipeline write or a catalog edit invalidates it);
    the underscore args are excluded from hashing.
    """
    by_company = defaultdict(list)
    for row in _db.query(*_SIGNAL_COLS).all():
        by_company[row.company_id].append(row._asdict())

    companies = {}
    results_by_company = {}
    for c in _db.query(Company.id, Company.legal_name, Company.registration_number, Company.segment,
                       Company.country, Company.sector_name).all():
        sigs = by_company.get(c.id, [])
        results = evaluate_pain_points(build_signal_map(sigs, _indicator_defs), _indicator_defs, _pain_defs)
        results_by_company[c.id] = results
        scores = calculate_company_scores(sigs, _indicator_defs)
        companies[c.id] = {
            "id": c.id, "name": c.legal_name, "reg": c.registration_number, "segment": c.segment or "—",
            "country": c.country or "Germany", "sector": c.sector_name or "—",
            "need": scores["need_score"], "readiness": scores["readiness_score"],
            "pp": {r["key"]: {k: r[k] for k in ("status", "score", "coverage", "confidence", "headline", "capped")}
                   for r in results},
        }
    return {"companies": companies, "gaps": collect_gaps(results_by_company)}


def _portfolio_summary(companies: dict, pain_defs: list) -> list:
    """Same shape as painpoints.summarize_portfolio, from the compact cached rows."""
    fake = {cid: [dict(v, key=k) for k, v in c["pp"].items()] for cid, c in companies.items()}
    return summarize_portfolio(fake, pain_defs)


# =============================================================================================
# Page
# =============================================================================================

def _render_portfolio_tab(companies: dict, pain_defs: list):
    all_rows = list(companies.values())
    col_a, col_b = st.columns(2)
    countries = sorted({c["country"] for c in all_rows})
    segments = sorted({c["segment"] for c in all_rows})
    sel_countries = col_a.multiselect("Country", countries, default=countries, key="pp_countries")
    sel_segments = col_b.multiselect("Segment (never pooled into one ranking)", segments, default=segments, key="pp_segments")
    in_view = {cid: c for cid, c in companies.items() if c["country"] in sel_countries and c["segment"] in sel_segments}
    if not in_view:
        st.info("No companies match the current filters.")
        return

    summary = _portfolio_summary(in_view, pain_defs)
    with_any = sum(1 for c in in_view.values() if any(v["status"] in CONFIRMED_STATUSES for v in c["pp"].values()))
    checkable = [s for s in summary if s["assessed"] > 0]
    m1, m2, m3 = st.columns(3)
    m1.metric("Companies in view", len(in_view))
    m2.metric("With ≥1 confirmed pain point", with_any, f"{with_any / len(in_view):.0%} of those in view", delta_color="off",
              help="Moderate or severe.")
    m3.metric("Pain points with any data", f"{len(checkable)} of {len(summary)}",
              help="The rest can't be assessed for any company yet — see the Data gaps tab.")

    st.markdown("##### Where the pain is")
    st.caption("Companies per pain point, by severity. Only companies whose evidence could be checked are counted — "
               "the table below shows how many that is for each.")
    plotted = sorted((s for s in summary if s["severe"] + s["moderate"] + s["emerging"] > 0),
                     key=lambda s: -(s["severe"] * 1000 + s["moderate"] * 10 + s["emerging"]))
    if plotted:
        order = [s["label"] for s in reversed(plotted)]
        fig = go.Figure()
        # Severe first: it sits on the common baseline, so it is the segment that compares across bars.
        for status in ("severe", "moderate", "emerging"):
            fig.add_trace(go.Bar(
                y=[s["label"] for s in plotted], x=[s[status] for s in plotted], orientation="h",
                name=STATUS_META[status][1], marker=dict(color=SEVERITY_COLORS[status], line=dict(color=SURFACE, width=2)),
                hovertemplate="%{y}<br>" + STATUS_META[status][1] + ": %{x} companies<extra></extra>",
            ))
        fig.update_layout(
            template="plotly_dark", barmode="stack", height=max(220, 44 * len(plotted) + 90),
            margin=dict(l=10, r=10, t=10, b=10), legend=dict(orientation="h", y=1.08, x=0),
            xaxis=dict(title="Companies", gridcolor="rgba(148,163,184,0.15)"),
            yaxis=dict(categoryorder="array", categoryarray=order, title=None),
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        )
        st.plotly_chart(fig, use_container_width=True, key="pp_portfolio_chart")
    else:
        st.info("No pain point detected for any company in view.")

    st.dataframe(pd.DataFrame([{
        "Pain point": s["label"], "Category": s["category"],
        "Severe": s["severe"], "Moderate": s["moderate"], "Emerging": s["emerging"],
        "Not detected": s["clear"], "Can't assess yet": s["insufficient_data"],
        "Assessable for": f"{s['assessed']} of {s['total']}",
    } for s in summary]), hide_index=True, width="stretch")

    st.markdown("---")
    st.markdown("##### Which companies")
    by_label = {p["label"]: p for p in pain_defs}
    pick = st.selectbox("Pain point", list(by_label), key="pp_drill_pick")
    key = by_label[pick]["key"]
    statuses = st.multiselect(
        "Show", ["severe", "moderate", "emerging", "clear", "insufficient_data"],
        default=["severe", "moderate"], format_func=lambda s: STATUS_META[s][1], key="pp_drill_status")
    rows = [c for c in in_view.values() if c["pp"][key]["status"] in statuses]
    rows.sort(key=lambda c: (-STATUS_ORDER.index(c["pp"][key]["status"]) if c["pp"][key]["status"] in STATUS_ORDER else 1,
                             -(c["pp"][key]["score"] or 0)))
    assessed_n = next(s["assessed"] for s in summary if s["key"] == key)
    if assessed_n == 0:
        st.warning("No company in view has usable data for this pain point yet — see the Data gaps tab for what would fill it.")
    st.caption(f"{len(rows)} compan{'y' if len(rows) == 1 else 'ies'} shown · Need and Readiness are alongside for context "
               "and are never combined with the pain score.")
    if rows:
        st.dataframe(pd.DataFrame([{
            "Company": c["name"], "Reg. no.": c["reg"], "Segment": c["segment"],
            "Status": _status_text(c["pp"][key]["status"]),
            "Pain score": c["pp"][key]["score"],
            "Evidence coverage": round(c["pp"][key]["coverage"] * 100),
            "Evidence": c["pp"][key]["headline"] + (" (severity held back: thin evidence)" if c["pp"][key]["capped"] else ""),
            "Need": c["need"], "Readiness": c["readiness"],
        } for c in rows]), hide_index=True, width="stretch", column_config={
            "Pain score": st.column_config.NumberColumn("Pain score", format="%.0f",
                                                        help="0-100: weight-averaged strength of the drivers that could be checked."),
            "Evidence coverage": st.column_config.ProgressColumn("Evidence coverage", min_value=0, max_value=100, format="%d%%",
                                                                 help="Share of this pain point's driver weight that had usable data."),
            "Evidence": st.column_config.TextColumn(width="large"),
            "Need": st.column_config.NumberColumn(format="%.1f"), "Readiness": st.column_config.NumberColumn(format="%.1f"),
        })
        st.caption("Open a company on **🏢 Company Intelligence** (search by registration number) for the full evidence behind each row.")


def _render_catalog_tab(db: Session):
    st.caption(
        "Every pain point is a set of indicator rules. Edit the thresholds and weights here — changes apply everywhere on the next "
        f"load. Intensity is 0 at the **warn** value and 100 at the **severe** value, linear in between; a pain point's score is the "
        f"weight-average over the drivers that could be checked. Bands: emerging ≥ {EMERGING_FROM:.0f}, moderate ≥ {MODERATE_FROM:.0f}, "
        f"severe ≥ {SEVERE_FROM:.0f}."
    )
    defs = fetch_pain_point_defs(db, include_inactive=True)
    by_label = {f"{d['category']} — {d['label']}": d for d in defs}
    choice = st.selectbox("Pain point to edit", list(by_label), key="pp_edit_pick")
    d = by_label[choice]

    live_counts, _ = _live_coverage(db)
    catalog = fetch_indicator_defs(db)
    rule_rows = []
    for r in d["rules"]:
        ind = r["indicator"]
        if ind in catalog:
            data = f"{live_counts.get(ind, 0)} companies with real data"
        else:
            prop = PROPOSED_INDICATORS.get(ind)
            data = "not in catalog yet — " + (KIND_LABELS.get(prop["kind"], prop["kind"]) if prop else "unknown indicator")
        rule_rows.append({
            "indicator": ind, "worse_when": r["worse_when"], "warn": r["warn"], "severe": r["severe"],
            "weight": r["weight"], "unit": r.get("unit", ""), "absent_means": r.get("absent_means", "ignore"),
            "active": r.get("active", True), "data": data,
        })

    with st.form(f"pp_edit_form_{d['key']}"):
        active = st.checkbox("Active", value=d["is_active"])
        label = st.text_input("Label", d["label"])
        description = st.text_area("What it means", d["description"], height=80)
        pilot_angle = st.text_area("Pilot angle (what kind of startup pilot this points to)", d["pilot_angle"] or "", height=68)
        caveat = st.text_area("Caveat", d["caveat"] or "", height=68)
        edited = st.data_editor(
            pd.DataFrame(rule_rows), hide_index=True, width="stretch", num_rows="fixed",
            disabled=["indicator", "worse_when", "unit", "data"], key=f"pp_rules_{d['key']}",
            column_config={
                "indicator": st.column_config.TextColumn("Indicator"),
                "worse_when": st.column_config.TextColumn("Worse when", help="Which direction of the raw value is painful."),
                "warn": st.column_config.NumberColumn("Warn", help="Where pain begins (strength 0)."),
                "severe": st.column_config.NumberColumn("Severe", help="Where pain is at full strength (100)."),
                "weight": st.column_config.NumberColumn("Weight", min_value=0.0, step=0.5),
                "absent_means": st.column_config.SelectboxColumn(
                    "If 'checked, none found'", options=["ignore", "zero", "pain"],
                    help="ignore = no evidence either way; zero = count it as a value of 0; pain = full-strength evidence."),
                "active": st.column_config.CheckboxColumn("Use"),
                "data": st.column_config.TextColumn("Data today", width="medium"),
            },
        )
        saved = st.form_submit_button("💾 Save", type="primary")

    if saved:
        by_indicator = {row["indicator"]: row for _, row in edited.iterrows()}
        new_rules = []
        for orig in d["rules"]:
            row = by_indicator[orig["indicator"]]
            merged = dict(orig)   # keeps valid_range / negative_is_severe / note, which aren't edited here
            merged.update(warn=float(row["warn"]), severe=float(row["severe"]), weight=float(row["weight"]),
                          absent_means=row["absent_means"], active=bool(row["active"]))
            new_rules.append(merged)
        problems = validate_rules(new_rules)
        if problems:
            for p in problems:
                st.error(p)
        else:
            obj = db.query(PainPointDefinition).filter_by(key=d["key"]).first()
            obj.label, obj.description, obj.pilot_angle, obj.caveat = label, description, pilot_angle or None, caveat or None
            obj.is_active = active
            obj.rules = new_rules   # a fresh list: SQLAlchemy doesn't see in-place JSON mutation
            db.commit()
            st.success("Saved.")
            st.rerun()

    with st.expander("⚠️ Reset every pain point to the catalog defaults (discards your edits)"):
        confirm = st.checkbox("I understand this discards my threshold and weight edits", key="pp_reset_confirm")
        if st.button("↩️ Reset to defaults", disabled=not confirm, key="pp_reset_btn"):
            db.query(PainPointDefinition).delete()
            db.commit()
            for row in PAIN_POINT_SEED:
                db.add(PainPointDefinition(**row))
            db.commit()
            st.success("Reset complete.")
            st.rerun()


def _render_gaps_tab(gap_data: dict, n_companies: int):
    st.caption(
        "Where more pain points could be assessed if more evidence existed. A driver appears here when its indicator has no "
        "real value yet. Once a producer — an API, a crawler step, a derivation or a manual entry — writes that signal, "
        "the pain point starts using it automatically; nothing in the detection needs to change."
    )

    st.markdown("##### Proposed indicators")
    st.caption("Indicators the pain-point rules already name but that don't exist (or have no valid producer) yet.")
    prop_rows = []
    for key, p in sorted(PROPOSED_INDICATORS.items(), key=lambda kv: ("ready_to_build", "needs_decision", "needs_source").index(kv[1]["status"])):
        prop_rows.append({
            "Status": PROPOSAL_STATUS_LABELS[p["status"]], "Indicator": p["label"], "Key": key,
            "Kind": KIND_LABELS.get(p["kind"], p["kind"]), "How": p["how"], "What we know": p["evidence"],
            "Would feed": ", ".join(p["feeds"]),
        })
    st.dataframe(pd.DataFrame(prop_rows), hide_index=True, width="stretch", column_config={
        "How": st.column_config.TextColumn(width="large"), "What we know": st.column_config.TextColumn(width="large")})

    st.markdown("##### Missing evidence, ranked by what it would unlock")
    st.caption("**Impact** sums, over companies, the share of each pain point's total driver weight this one indicator would add — "
               f"so it favours indicators that are missing for many companies and carry much of a pain point. Across all {n_companies} companies.")
    if gap_data["gaps"]:
        rows = []
        for g in gap_data["gaps"]:
            p, prop = g["producer"], g["proposal"]
            if prop:   # a written plan beats the catalog's nominal source (e.g. Bundesanzeiger, which has no Italian filings)
                src = f"proposed — {KIND_LABELS.get(prop['kind'], prop['kind'])}"
            elif p.get("source_system"):
                src = f"{p['source_system']} ({p.get('tier') or '?'}, phase {p.get('phase')})"
            else:
                src = "—"
            rows.append({
                "Indicator": g["label"], "Missing for": f"{g['companies_missing']} of {g['companies_total']}",
                "Impact": g["unlock"], "Feeds": ", ".join(g["pain_points"]), "Would come from": src,
            })
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", column_config={
            "Impact": st.column_config.NumberColumn(format="%.0f"), "Feeds": st.column_config.TextColumn(width="large")})
    else:
        st.success("Every driver of every pain point has data.")

    if gap_data["quality"]:
        st.markdown("##### Values excluded as implausible")
        st.caption("These indicators have data, but some values were ignored because they fall outside a plausible range "
                   "(usually a trend measured from a near-zero base year).")
        st.dataframe(pd.DataFrame([{
            "Indicator": q["label"], "Companies affected": q["companies"], "Pain points": ", ".join(q["pain_points"]),
        } for q in gap_data["quality"]]), hide_index=True, width="stretch")


def render_pain_points_page(db: Session):
    st.title("🩺 Pain Points")
    st.caption(
        "What the target companies are struggling with, detected automatically from their indicators. Every detection cites the "
        "indicator, value, threshold and source behind it. Only real observations count; a pain point with no usable data is "
        "reported as a gap, never as “no pain”. Pain points sit alongside Need and Readiness — they don't change either score."
    )
    pain_defs = fetch_pain_point_defs(db)
    if not pain_defs:
        st.warning("No pain points loaded yet — restart the app to trigger the initial catalog seed.")
        return
    if db.query(Company).count() == 0:
        st.warning("No companies loaded yet.")
        return

    indicator_defs = fetch_indicator_defs(db)
    data = _evaluate_portfolio(
        db, indicator_defs, pain_defs, _signals_fingerprint(db),
        repr(sorted((p["key"], str(p["updated_at"]), p["is_active"]) for p in pain_defs)) + repr(sorted(indicator_defs)),
    )
    tab_port, tab_cat, tab_gaps = st.tabs(["📊 Portfolio", "🧩 Pain-point catalog", "🕳️ Data gaps & new indicators"])
    with tab_port:
        _render_portfolio_tab(data["companies"], pain_defs)
    with tab_cat:
        _render_catalog_tab(db)
    with tab_gaps:
        _render_gaps_tab(data["gaps"], len(data["companies"]))
