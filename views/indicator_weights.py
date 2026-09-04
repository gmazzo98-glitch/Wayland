"""
Indicator Weights & Catalog Editor (Page 5)

This page IS the "weighting system": every indicator's importance, axis
(need/readiness/both/context), inversion, and normalization bounds are stored
in the IndicatorDefinition table and edited here — no code change or redeploy
needed to change how much a signal matters. Every other page reads these
values fresh on each render, so a saved change is reflected immediately.
"""

import streamlit as st
import pandas as pd
from sqlalchemy import func
from sqlalchemy.orm import Session
from models import IndicatorDefinition, SignalRecord
from indicators import INDICATOR_SEED

AXIS_OPTIONS = ["need", "readiness", "both", "context"]
TIER_OPTIONS = ["T1", "T2", "T3"]
EDITABLE_COLS = ["weight", "axis", "invert", "raw_min", "raw_max", "curve_type",
                  "is_gate", "gate_penalty_multiplier", "is_active", "freshness_days",
                  "redundancy_group", "automation_tier", "axis_modifier"]
READONLY_COLS = ["key", "label", "category", "live_coverage", "live_sources"]


def _live_coverage(db: Session) -> tuple[dict, dict]:
    """
    Per indicator key: how many distinct companies carry a real (non-
    simulated, present) value for it, and which source(s) — pipeline
    adapters, manual entry, or a flexible-import dataset name — put it
    there. This is what makes injected-dataset linkage visible from the
    catalog page itself, rather than only discoverable per-company.
    """
    counts = dict(
        db.query(SignalRecord.signal_key, func.count(func.distinct(SignalRecord.company_id)))
        .filter(SignalRecord.status == "present", SignalRecord.is_simulated == False)  # noqa: E712
        .group_by(SignalRecord.signal_key)
        .all()
    )
    sources = {}
    source_rows = (
        db.query(SignalRecord.signal_key, SignalRecord.source)
        .filter(SignalRecord.status == "present", SignalRecord.is_simulated == False)  # noqa: E712
        .distinct()
        .all()
    )
    for key, source in source_rows:
        sources.setdefault(key, set()).add(source or "—")
    return counts, sources


def _defs_to_df(defs: list[IndicatorDefinition], live_counts: dict, live_sources: dict) -> pd.DataFrame:
    rows = []
    for d in defs:
        rows.append({
            "key": d.key, "label": d.label, "category": d.category,
            "live_coverage": live_counts.get(d.key, 0),
            "live_sources": ", ".join(sorted(live_sources.get(d.key, []))) or "—",
            "weight": d.weight, "axis": d.axis, "invert": d.invert,
            "raw_min": d.raw_min, "raw_max": d.raw_max, "curve_type": d.curve_type,
            "is_gate": d.is_gate, "gate_penalty_multiplier": d.gate_penalty_multiplier,
            "is_active": d.is_active, "freshness_days": d.freshness_days,
            "redundancy_group": d.redundancy_group, "automation_tier": d.automation_tier,
            "axis_modifier": d.axis_modifier,
        })
    return pd.DataFrame(rows)


def render_indicator_weights_page(db: Session):
    st.title("⚖️ Indicator Weights & Catalog")
    st.caption(
        "Every indicator the tool looks for, with its weight, axis, and normalization "
        "bounds — edit inline and save. Changes apply everywhere else in the app on the "
        "next page load, no restart needed."
    )

    all_defs = db.query(IndicatorDefinition).order_by(IndicatorDefinition.category, IndicatorDefinition.label).all()
    if not all_defs:
        st.warning("No indicators loaded yet — restart the app to trigger the initial catalog seed.")
        return

    categories = sorted(set(d.category for d in all_defs))
    col_f1, col_f2 = st.columns([3, 1])
    with col_f1:
        selected_cats = st.multiselect("Categories to show", options=categories, default=categories)
    with col_f2:
        show_inactive = st.checkbox("Include inactive", value=True)

    visible_defs = [d for d in all_defs if d.category in selected_cats and (show_inactive or d.is_active)]
    st.caption(f"Showing {len(visible_defs)} of {len(all_defs)} indicators. Context-axis rows are never scored regardless of weight — see Section on context tags below.")

    live_counts, live_sources = _live_coverage(db)
    df = _defs_to_df(visible_defs, live_counts, live_sources)
    covered = sum(1 for d in visible_defs if live_counts.get(d.key, 0) > 0)
    st.caption(f"🔗 **{covered}** of {len(visible_defs)} shown indicators currently have real (non-simulated) data linked to them, from a pipeline adapter, manual entry, or an injected dataset.")

    edited_df = st.data_editor(
        df,
        key="indicator_weight_editor",
        hide_index=True,
        use_container_width=True,
        num_rows="fixed",
        disabled=READONLY_COLS,
        column_config={
            "key": st.column_config.TextColumn("Key", width="small"),
            "label": st.column_config.TextColumn("Indicator", width="medium"),
            "category": st.column_config.TextColumn("Category", width="medium"),
            "live_coverage": st.column_config.NumberColumn("🔗 Live Coverage", help="Number of companies with a real, non-simulated value for this indicator — from any source, including the Flexible Data Import feeder."),
            "live_sources": st.column_config.TextColumn("Data Source(s)", width="medium", help="Which pipeline adapter, manual entry, or injected dataset name(s) actually populated this indicator's live data."),
            "weight": st.column_config.NumberColumn("Weight", min_value=0.0, max_value=5.0, step=0.5, help="0 = effectively disabled without deleting"),
            "axis": st.column_config.SelectboxColumn("Axis", options=AXIS_OPTIONS, help="'context' rows are excluded from scoring entirely"),
            "invert": st.column_config.CheckboxColumn("Invert", help="Check when a LOW raw value should score HIGH"),
            "raw_min": st.column_config.NumberColumn("Raw Min", help="Raw value mapped to score 0 (or the low edge of the sweet-spot band)"),
            "raw_max": st.column_config.NumberColumn("Raw Max", help="Raw value mapped to score 100 (or the high edge of the sweet-spot band)"),
            "curve_type": st.column_config.SelectboxColumn("Curve", options=["linear", "band"], help="'band' = sweet spot between Raw Min/Max, tapering outside"),
            "is_gate": st.column_config.CheckboxColumn("Gate?", help="When checked-and-unfavorable, multiplies the axis score down instead of just contributing its share"),
            "gate_penalty_multiplier": st.column_config.NumberColumn("Gate Penalty", min_value=0.0, max_value=1.0, step=0.05),
            "is_active": st.column_config.CheckboxColumn("Active"),
            "freshness_days": st.column_config.NumberColumn("Freshness (days)", min_value=1),
            "redundancy_group": st.column_config.TextColumn("Redundancy Group", help="Same-group variables are dampened against each other within an axis — full weight to the highest-weighted member, half the next, a quarter after that. Blank = never dampened."),
            "automation_tier": st.column_config.SelectboxColumn("Tier", options=TIER_OPTIONS, help="T1 = structured register/API, T2 = scrape+extract (carries a confidence), T3 = manual/first-contact only. Informational — Phase (below) is what actually drives ingestion."),
            "axis_modifier": st.column_config.TextColumn("Modifier", help="Free-text tag from the source spreadsheet (NEGATIVE, CAVEAT, GATING, BASELINE, GG-FIT GAP, MODERATOR...) explaining *why* a row behaves as it does. Display only — editing this alone has no scoring effect; the real behavior lives in Invert/Gate above."),
        },
    )

    col_s1, col_s2 = st.columns([1, 3])
    with col_s1:
        if st.button("💾 Save Changes", type="primary", use_container_width=True):
            changed = 0
            for _, row in edited_df.iterrows():
                defn = db.query(IndicatorDefinition).filter_by(key=row["key"]).first()
                if defn is None:
                    continue
                row_changed = False
                for col in EDITABLE_COLS:
                    new_val = row[col]
                    old_val = getattr(defn, col)
                    if pd.isna(new_val):
                        new_val = None
                    if new_val != old_val:
                        setattr(defn, col, new_val)
                        row_changed = True
                if row_changed:
                    changed += 1
            db.commit()
            st.success(f"Saved. {changed} indicator(s) updated.")
            st.rerun()

    with st.expander("⚠️ Reset all indicators to catalog defaults (discards your customizations)"):
        st.warning("This permanently overwrites every weight/axis/bounds edit you've made and restores the original seed values for all indicators.")
        confirm = st.checkbox("I understand this discards my custom weights", key="reset_confirm")
        if st.button("↩️ Reset to Defaults", disabled=not confirm, type="secondary"):
            db.query(IndicatorDefinition).delete()
            db.commit()
            for row in INDICATOR_SEED:
                db.add(IndicatorDefinition(**row))
            db.commit()
            st.success("Reset complete.")
            st.rerun()

    st.markdown("---")
    st.subheader("📖 Indicator Details")
    label_to_def = {f"{d.category} — {d.label}": d for d in all_defs}
    selected_label = st.selectbox("Look up the full rationale for one indicator", list(label_to_def.keys()))
    d = label_to_def[selected_label]
    col_d1, col_d2 = st.columns(2)
    with col_d1:
        st.write("**Proxy:**", d.proxy or "—")
        st.write("**Rationale:**", d.rationale or "—")
        st.write("**Example status seen in practice:**", d.example_status or "—")
    with col_d2:
        st.write("**Comment / caveats:**", d.comment or "—")
        st.write("**Source:**", d.source_description or "—")
        st.write("**Pipeline source system / phase:**", f"{d.source_system or '—'} / Phase {d.phase}")
        st.write("**Automation tier / redundancy group:**", f"{d.automation_tier or '—'} / {d.redundancy_group or 'stands alone'}")
        if d.axis_modifier:
            st.warning(f"**{d.axis_modifier}** — see Comment/caveats above for what this means for this row.", icon="⚠️")
