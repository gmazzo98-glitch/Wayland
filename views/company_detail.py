"""
Company Intelligence & Tri-State Signal View (Page 2)
Sections 2.1 & 2.2 of GG_Dashboard_Technical_Brief.docx

Groups the full indicator catalog by category, shows context/moderator tags
separately from the scored table (they were never meant to be summed — see
indicators.py), and gives a manual-entry form for the indicators that can only
be answered from a first-contact call (Phase 6 — no scraper will ever fetch
"depth of internal approval chain").
"""

import json
import streamlit as st
import pandas as pd
from datetime import datetime
from sqlalchemy import func
from sqlalchemy.orm import Session
from models import Company, SignalRecord, PilotOutcome, RawImportRecord, CompanyPerson, SHORTLIST_STATUSES
from indicators import fetch_indicator_defs
from scoring import calculate_company_scores
from utils import get_signal_display_status

STATUS_BADGES = {
    "present": "🟢 Present",
    "absent": "🔴 Confirmed Absent (Zero)",
    "not_yet_checked": "⚪ Not Yet Checked",
    "stale": "🟡 Stale (Refetch Flag)"
}

MODE_BADGES = {True: "🧪 Simulated", False: "🟢 Live"}

SHORTLIST_STATUS_LABELS = {
    "candidate": "Candidate (Phase 1+2 only)",
    "shortlisted": "Shortlisted (Phase 3+ unlocked)",
    "in_pilot": "In Pilot",
    "rejected": "Rejected",
}


def _upsert_manual_signal(db: Session, company_id: str, key: str, defn: dict, status: str,
                           numeric_value, text_value):
    sig = db.query(SignalRecord).filter_by(company_id=company_id, signal_key=key).first()
    if not sig:
        sig = SignalRecord(company_id=company_id, signal_key=key, source=defn.get("source_system") or "Manual Entry")
        db.add(sig)
    sig.status = status
    sig.numeric_value = numeric_value
    sig.text_value = text_value
    sig.confidence = 1.0
    sig.is_simulated = False  # a human-entered real observation, not a fallback
    sig.fetched_at = datetime.utcnow()
    sig.source = defn.get("source_system") or "Manual Entry"
    sig.raw_payload_ref = json.dumps({"manual_entry": True, "simulated": False, "signal_key": key})
    db.commit()


def _render_manual_entry_form(db: Session, company: Company, indicator_defs: dict):
    # Section 4.3 of the Indicator Prompt: T3 manual entry should surface
    # "specifically at the point a company moves into active first-contact/
    # Diagnosi status, not before" — gated on shortlist_status rather than
    # always available, now that the status field exists to gate on.
    if company.shortlist_status not in ("shortlisted", "in_pilot"):
        st.info(
            f"Manual first-contact entry unlocks once **{company.legal_name}** is Shortlisted or In Pilot "
            "(set above) — these fields (org structure, approval chains, first-contact impressions) are "
            "collected during active outreach, not while still a candidate.",
            icon="🔒",
        )
        return

    with st.expander("✍️ Manually Record a Signal (e.g. from a first-contact call)"):
        st.caption(
            "For indicators no scraper will ever reach — org structure, approval chains, "
            "first-contact impressions — record what you learned directly. Saved as a real, "
            "non-simulated observation."
        )
        options = sorted(indicator_defs.items(), key=lambda kv: (kv[1]["category"], kv[1]["label"]))
        label_to_key = {f"{d['category']} — {d['label']}": k for k, d in options}
        selected_label = st.selectbox("Indicator", list(label_to_key.keys()), key="manual_entry_select")
        selected_key = label_to_key[selected_label]
        defn = indicator_defs[selected_key]
        st.caption(f"Proxy: {defn.get('proxy') or '—'}")

        col1, col2 = st.columns(2)
        with col1:
            manual_status = st.radio("Status", ["present", "absent"], horizontal=True, key="manual_entry_status")
        with col2:
            if defn["axis"] == "context":
                manual_text = st.text_input("Value (text)", key="manual_entry_text") if manual_status == "present" else ""
                manual_numeric = None
            else:
                manual_numeric = st.number_input("Value (numeric)", value=0.0, step=0.5, key="manual_entry_value") if manual_status == "present" else None
                manual_text = None

        if st.button("💾 Save Signal", key="manual_entry_save"):
            _upsert_manual_signal(db, company.id, selected_key, defn, manual_status, manual_numeric, manual_text)
            st.success(f"Saved {defn['label']} for {company.legal_name}.")
            st.rerun()


def _render_flexible_import_tab(db: Session):
    st.subheader("🔗 Flexible Data Import")
    st.caption(
        "Upload a dataset with ANY column layout — the app detects columns it doesn't "
        "recognize and lets you link them to existing fields/indicators (or create new "
        "ones). The mapping is saved under the dataset name so re-uploading the same "
        "shape later reuses it. Columns named like revenue_latest/revenue_y-1/revenue_y-2 "
        "are auto-grouped as a time series so trend indicators can be computed from them."
    )

    from company_service import (
        parse_uploaded_file, detect_column_groups, suggest_mapping,
        valid_targets_for_group, apply_data_import, save_mapping_profile,
        load_mapping_profile, create_ad_hoc_indicator, list_mapping_profiles,
    )

    NEW_INDICATOR_OPTION = "__new__"

    col_c1, col_c2 = st.columns(2)
    with col_c1:
        flex_country = st.radio("Country 🌐", ["Germany", "Italy"], horizontal=True, key="flex_country")
    with col_c2:
        existing_names = [p.dataset_name for p in list_mapping_profiles(db)]
        dataset_name = st.text_input(
            "Dataset Name *",
            placeholder="e.g. AIDA Financials",
            help="Names this dataset type. Re-uploading under the SAME name flags companies "
                 "that already have it (with an overwrite option); a DIFFERENT name just adds "
                 "in as new information for the same companies.",
            key="flex_dataset_name",
        )
        if existing_names:
            st.caption(f"Previously saved dataset names: {', '.join(existing_names)}")

    uploaded = st.file_uploader("Upload Dataset (.csv or .xlsx)", type=["csv", "xlsx", "xls"], key="flex_uploader")

    if uploaded is None:
        st.session_state.pop("flex_df", None)
        return

    file_sig = (uploaded.name, uploaded.size)
    if st.session_state.get("flex_file_sig") != file_sig:
        try:
            uploaded.seek(0)
            df = parse_uploaded_file(uploaded, filename=uploaded.name)
        except Exception as e:
            st.error(f"Could not read file: {e}")
            return
        st.session_state["flex_file_sig"] = file_sig
        st.session_state["flex_df"] = df
        groups = detect_column_groups(list(df.columns))
        profile_mapping = load_mapping_profile(db, dataset_name) if dataset_name.strip() else {}
        st.session_state["flex_mapping"] = suggest_mapping(db, groups, existing_profile=profile_mapping)
        st.session_state["flex_new_labels"] = {}
        st.session_state["flex_preview"] = None

    df = st.session_state["flex_df"]
    groups = detect_column_groups(list(df.columns))
    mapping_state = st.session_state["flex_mapping"]
    new_labels_state = st.session_state["flex_new_labels"]

    st.markdown(f"##### Detected **{len(groups)}** column group(s) across **{len(df)}** row(s)")
    st.dataframe(df.head(5), use_container_width=True)
    st.markdown("###### Column Mapping")

    for base in sorted(groups.keys()):
        group = groups[base]
        points_desc = ", ".join(f"{suf} ({col})" for suf, col in sorted(group["points"].items()))
        options = valid_targets_for_group(db, group)
        option_keys = list(options.keys()) + [NEW_INDICATOR_OPTION]
        option_labels = {**options, NEW_INDICATOR_OPTION: "+ Create New Indicator..."}

        current = mapping_state.get(base)
        if current not in option_keys:
            current = ""
        default_idx = option_keys.index(current)

        row_c1, row_c2 = st.columns([2, 3])
        with row_c1:
            tag = "📈 time series" if group["is_timeseries"] else "•"
            st.markdown(f"**{base}** {tag}")
            st.caption(points_desc)
        with row_c2:
            picked = st.selectbox(
                f"Map '{base}' to", options=option_keys,
                format_func=lambda k: option_labels.get(k, k),
                index=default_idx, key=f"flex_map_{base}", label_visibility="collapsed",
            )
            mapping_state[base] = picked
            if picked == NEW_INDICATOR_OPTION:
                new_labels_state[base] = st.text_input(
                    f"New indicator label for '{base}'",
                    value=new_labels_state.get(base, base.replace("_", " ").title()),
                    key=f"flex_new_label_{base}",
                )
    st.session_state["flex_mapping"] = mapping_state
    st.session_state["flex_new_labels"] = new_labels_state

    reg_targets = [b for b, t in mapping_state.items() if t == "company:registration_number"]
    name_targets = [b for b, t in mapping_state.items() if t == "company:legal_name"]
    has_match_key = len(reg_targets) == 1 or len(name_targets) == 1
    if not has_match_key:
        st.warning("Map exactly one column to **Company: Registration Number** or **Company: Legal Name** before previewing.")
    elif len(reg_targets) != 1:
        st.caption(
            "⚠️ Matching by **Legal Name** (no Registration Number column mapped) — safer when a file's own ID "
            "column doesn't reliably match what's already on file (e.g. AIDA's BvD ID often doesn't). This mode "
            "never creates new companies from unmatched names — they're reported instead."
        )

    st.markdown("---")

    if st.button(
        "🔍 Preview Import", key="flex_preview_btn",
        disabled=not has_match_key or not dataset_name.strip(), use_container_width=True,
    ):
        resolved_mapping = {}
        for base, target in mapping_state.items():
            if target == NEW_INDICATOR_OPTION:
                new_key = create_ad_hoc_indicator(db, new_labels_state.get(base, base), dataset_name=dataset_name, source_column=base)
                resolved_mapping[base] = f"indicator:{new_key}" if new_key else None
            else:
                resolved_mapping[base] = target or None
        st.session_state["flex_resolved_mapping"] = resolved_mapping
        with st.spinner("Analyzing..."):
            preview = apply_data_import(db, df, resolved_mapping, dataset_name, country=flex_country, dry_run=True)
        st.session_state["flex_preview"] = preview

    preview = st.session_state.get("flex_preview")
    if preview:
        if preview["errors"] and not (preview["created"] or preview["merged"] or preview["conflicts"]):
            for err in preview["errors"]:
                st.error(err)
        else:
            pc1, pc2, pc3, pc4 = st.columns(4)
            pc1.metric("New Companies", preview["created"])
            pc2.metric("Existing — Will Merge", preview["merged"])
            pc3.metric("⚠️ Conflicts", len(preview["conflicts"]))
            pc4.metric("⚠️ Unmatched Names", len(preview.get("unmatched", [])))
            if preview.get("unmatched"):
                with st.expander(f"⚠️ {len(preview['unmatched'])} legal name(s) with no matching company (not created)", expanded=False):
                    for name in preview["unmatched"]:
                        st.write(f"- {name}")
            if preview["errors"]:
                with st.expander(f"⚠️ {len(preview['errors'])} row(s) with issues", expanded=False):
                    for err in preview["errors"]:
                        st.warning(err)

            overwrite = False
            if preview["conflicts"]:
                with st.expander(
                    f"⚠️ {len(preview['conflicts'])} companies already have a '{dataset_name}' dataset loaded",
                    expanded=True,
                ):
                    for c in preview["conflicts"]:
                        st.write(f"- {c['legal_name']} ({c['registration_number']})")
                    overwrite = st.checkbox(
                        f"Overwrite existing '{dataset_name}' data for the companies listed above",
                        value=False, key="flex_overwrite_checkbox",
                    )

            if st.button("🚀 Confirm Import", key="flex_confirm_btn", use_container_width=True):
                resolved_mapping = st.session_state.get("flex_resolved_mapping", {})
                with st.spinner("Importing..."):
                    file_sig = st.session_state.get("flex_file_sig")
                    result = apply_data_import(
                        db, df, resolved_mapping, dataset_name, country=flex_country,
                        overwrite_conflicts=overwrite, dry_run=False,
                        source_filename=file_sig[0] if file_sig else None,
                    )
                    save_mapping_profile(db, dataset_name, flex_country, resolved_mapping)
                st.success(
                    f"✅ Import complete! **{result['created']}** created, **{result['merged']}** merged, "
                    f"**{result['overwritten']}** overwritten, **{len(result['conflicts'])}** skipped as conflicts, "
                    f"**{len(result.get('unmatched', []))}** unmatched names."
                )
                if result["errors"]:
                    with st.expander("⚠️ Import Warnings & Skipped Rows", expanded=True):
                        for err in result["errors"]:
                            st.warning(err)
                for k in ("flex_df", "flex_file_sig", "flex_mapping", "flex_new_labels",
                          "flex_preview", "flex_resolved_mapping"):
                    st.session_state.pop(k, None)
                st.rerun()


def _render_people_import_tab(db: Session):
    st.subheader("👥 Import People & Ownership")
    st.caption(
        "For source files that pack MULTIPLE people or entities into a single cell, one line per "
        "person/entity (AIDA's own export convention — e.g. a 'DM' column group holding every "
        "director's name, role, age, etc. newline-stacked in matching order; the same convention "
        "AIDA uses for shareholders, ultimate owners, and subsidiaries). Groups are detected "
        "automatically — from a shared column-header prefix where the file uses one consistently, "
        "and from matching stacked-cell patterns where it doesn't — no manual mapping step. Matches "
        "rows to existing companies by **legal name** (not the file's own BvD ID column — verified "
        "against real data that it doesn't reliably match the registration numbers already on file). "
        "Never creates a new company from this file alone; unmatched names are listed so you can "
        "reconcile them."
    )

    from company_service import parse_roster_file, detect_person_groups, import_company_people, strip_person_column_prefix

    dataset_name = st.text_input(
        "Dataset Name *", placeholder="e.g. Directors & Board C28",
        help="Same scoping rule as Flexible Data Import: re-uploading under the SAME name flags "
             "companies that already have it (with an overwrite option); a DIFFERENT name just adds in.",
        key="people_dataset_name",
    )
    legal_name_col = st.text_input(
        "Legal Name Column", value="Ragione sociale",
        help="The column in your file holding each company's legal name, used to match rows to existing companies.",
        key="people_legal_name_col",
    )
    uploaded = st.file_uploader("Upload Dataset (.xls, .xlsx or .csv)", type=["xls", "xlsx", "csv"], key="people_uploader")

    if uploaded is None:
        st.session_state.pop("people_df", None)
        return

    file_sig = (uploaded.name, uploaded.size)
    if st.session_state.get("people_file_sig") != file_sig:
        try:
            uploaded.seek(0)
            df = parse_roster_file(uploaded, filename=uploaded.name)
        except Exception as e:
            st.error(f"Could not read file: {e}")
            return
        st.session_state["people_file_sig"] = file_sig
        st.session_state["people_df"] = df
        st.session_state["people_preview"] = None

    df = st.session_state["people_df"]
    groups = detect_person_groups(df)

    st.markdown(f"##### {len(df)} row(s), **{len(groups)}** person/entity group(s) detected")
    if groups:
        for name, cols in groups.items():
            sub_labels = ", ".join(strip_person_column_prefix(c) for c in cols[:5])
            st.caption(f"**{name}**: {len(cols)} columns — {sub_labels}{', ...' if len(cols) > 5 else ''}")
    else:
        st.warning("No multi-value stacked-cell column groups detected in this file's headers/content.")
    st.dataframe(df.head(5), use_container_width=True)

    st.markdown("---")

    if st.button("🔍 Preview Import", key="people_preview_btn", disabled=not dataset_name.strip() or not groups, use_container_width=True):
        with st.spinner("Analyzing..."):
            preview = import_company_people(db, df, dataset_name, legal_name_column=legal_name_col, dry_run=True)
        st.session_state["people_preview"] = preview

    preview = st.session_state.get("people_preview")
    if preview:
        if preview["errors"]:
            for err in preview["errors"]:
                st.error(err)
        else:
            pc1, pc2, pc3 = st.columns(3)
            pc1.metric("Matched Companies", preview["matched"])
            pc2.metric("Unmatched Names", len(preview["unmatched"]))
            pc3.metric("⚠️ Conflicts", len(preview["conflicts"]))

            if preview["unmatched"]:
                with st.expander(f"⚠️ {len(preview['unmatched'])} legal name(s) with no matching company", expanded=False):
                    for name in preview["unmatched"]:
                        st.write(f"- {name}")

            overwrite = False
            if preview["conflicts"]:
                with st.expander(f"⚠️ {len(preview['conflicts'])} companies already have a '{dataset_name}' roster loaded", expanded=True):
                    for c in preview["conflicts"]:
                        st.write(f"- {c['legal_name']} ({c['registration_number']})")
                    overwrite = st.checkbox(
                        f"Overwrite existing '{dataset_name}' roster for the companies listed above",
                        value=False, key="people_overwrite_checkbox",
                    )

            if st.button("🚀 Confirm Import", key="people_confirm_btn", use_container_width=True):
                with st.spinner("Importing..."):
                    result = import_company_people(
                        db, df, dataset_name, legal_name_column=legal_name_col,
                        source_filename=file_sig[0], overwrite_conflicts=overwrite, dry_run=False,
                    )
                st.success(
                    f"✅ Import complete! **{result['matched']}** companies matched — "
                    f"**{result['people_created']}** people created, **{result['people_updated']}** updated."
                )
                if result["unmatched"]:
                    with st.expander(f"⚠️ {len(result['unmatched'])} unmatched name(s)", expanded=False):
                        for name in result["unmatched"]:
                            st.write(f"- {name}")
                if result["errors"]:
                    with st.expander("⚠️ Errors", expanded=True):
                        for err in result["errors"]:
                            st.warning(err)
                for k in ("people_df", "people_file_sig", "people_preview"):
                    st.session_state.pop(k, None)
                st.rerun()


def _render_manage_companies_tab(db: Session):
    st.subheader("🗑️ Manage Master Company Database")
    st.caption(
        "Every company currently in the database, regardless of how it was added "
        "(seed data, manual entry, CSV or flexible import). Select rows to delete "
        "permanently — this also removes that company's signal history and any "
        "recorded pilot outcomes."
    )

    from company_service import delete_companies

    companies = db.query(Company).order_by(Company.legal_name).all()
    if not companies:
        st.info("No companies in the database.")
        return

    signal_counts = dict(
        db.query(SignalRecord.company_id, func.count(SignalRecord.id))
        .filter(SignalRecord.status == "present")
        .group_by(SignalRecord.company_id)
        .all()
    )
    pilot_counts = dict(
        db.query(PilotOutcome.company_id, func.count(PilotOutcome.id))
        .group_by(PilotOutcome.company_id)
        .all()
    )

    rows = []
    for c in companies:
        rows.append({
            "Delete": False,
            "Legal Name": c.legal_name,
            "Registration #": c.registration_number,
            "Country": c.country,
            "Segment": c.segment,
            "Shortlist Status": c.shortlist_status,
            "Need Score": c.need_score,
            "Readiness Score": c.readiness_score,
            "Present Signals": signal_counts.get(c.id, 0),
            "Pilot Outcomes": pilot_counts.get(c.id, 0),
            "_id": c.id,
        })
    df = pd.DataFrame(rows)

    edited = st.data_editor(
        df,
        key="manage_companies_editor",
        hide_index=True,
        use_container_width=True,
        num_rows="fixed",
        column_order=["Delete", "Legal Name", "Registration #", "Country", "Segment",
                      "Shortlist Status", "Need Score", "Readiness Score",
                      "Present Signals", "Pilot Outcomes"],
        disabled=["Legal Name", "Registration #", "Country", "Segment", "Shortlist Status",
                  "Need Score", "Readiness Score", "Present Signals", "Pilot Outcomes"],
        column_config={
            "Delete": st.column_config.CheckboxColumn("🗑️ Delete", help="Check to mark for deletion"),
            "Need Score": st.column_config.NumberColumn(format="%.1f"),
            "Readiness Score": st.column_config.NumberColumn(format="%.1f"),
        },
    )

    selected = edited[edited["Delete"]]
    if len(selected) > 0:
        plural = "y" if len(selected) == 1 else "ies"
        st.warning(
            f"**{len(selected)}** compan{plural} selected — "
            f"**{int(selected['Present Signals'].sum())}** signal record(s) and "
            f"**{int(selected['Pilot Outcomes'].sum())}** pilot outcome(s) will be permanently "
            f"deleted along with them."
        )
        confirm = st.checkbox(
            f"I understand this permanently deletes {len(selected)} compan{plural} and cannot be undone",
            key="delete_companies_confirm",
        )
        if st.button("🗑️ Delete Selected Companies", type="primary", disabled=not confirm, use_container_width=True):
            result = delete_companies(db, selected["_id"].tolist())
            st.success(
                f"Deleted {result['deleted']} compan{plural} — "
                f"{result['signals_deleted']} signal record(s), "
                f"{result['pilot_outcomes_deleted']} pilot outcome(s), "
                f"{result['raw_import_records_deleted']} raw import blob(s) removed."
            )
            st.rerun()
    else:
        st.caption("Check the 🗑️ Delete column next to any row(s), then confirm below to remove them.")


def _trend_groups_from_raw(raw_row: dict) -> dict:
    """
    Detects multi-year column groups (e.g. revenue_latest/revenue_y-1/
    revenue_y-2) within one company's raw injected row — financial or not,
    scored or not — using the same convention the import mapping step
    relies on. Returns {base_label: [(year_label, value), ...]} ordered
    oldest -> newest, ready to chart. Only groups with 2+ timepoints.
    """
    from company_service import detect_column_groups
    groups = detect_column_groups(list(raw_row.keys()))
    trends = {}
    for base, group in groups.items():
        if not group["is_timeseries"]:
            continue
        points = group["points"]
        y_suffixes = sorted([s for s in points if s.startswith("y-")], key=lambda s: -int(s.split("-")[1]))
        ordered = y_suffixes + (["latest"] if "latest" in points else [])
        series = []
        for suf in ordered:
            raw_val = raw_row.get(points[suf])
            try:
                val = float(raw_val) if raw_val is not None else None
            except (TypeError, ValueError):
                val = None
            series.append((suf.replace("y-", "Y-") if suf != "latest" else "Latest", val))
        trends[base] = series
    return trends


def _single_point_fields_from_raw(raw_row: dict) -> dict:
    """Every column from a raw injected row that is NOT part of a detected
    time series — the flat financial and non-financial fields, as injected."""
    from company_service import detect_column_groups
    groups = detect_column_groups(list(raw_row.keys()))
    fields = {}
    for base, group in groups.items():
        if group["is_timeseries"]:
            continue
        col = next(iter(group["points"].values()))
        fields[base] = raw_row.get(col)
    return fields


# Reverse of company_service.TREND_BASE_ALIASES — which raw column-group
# base name (from a RawImportRecord) underlies each computed trend indicator.
_TREND_KEY_TO_RAW_BASE = {"revenue_trend": "revenue", "ebit_trend": "ebit", "margin_compression": "gross_margin"}

# Keys representing monetary financial levels / amounts uploaded in thousands (k)
MONETARY_INDICATOR_KEYS = {
    "total_assets", "cash_position", "debt_level", "revenue", "ebit", "gross_margin", "turnover",
}

PERCENTAGE_INDICATOR_KEYS = {
    "revenue_trend", "ebit_trend", "margin_compression", "capex_ratio",
    "materials_cost", "labour_cost", "logistics_cost", "energy_cost",
    "cogs_ratio", "service_costs", "rd_expense_ratio",
}

RATIO_INDICATOR_KEYS = {
    "leverage_ratio", "interest_coverage_ratio",
}


def _is_financial_field(name: str) -> bool:
    """Returns True if a raw column name or metric represents a monetary financial quantity (uploaded in thousands, k)."""
    norm = str(name).strip().lower().replace(" ", "_")
    keywords = [
        "revenue", "ebit", "turnover", "fatturato", "ricavi", "assets", "attivo",
        "debt", "debiti", "cash", "cassa", "liquidita", "patrimonio", "ebitda",
        "valore_produzione", "sales", "gross_margin", "operating_profit", "net_income",
        "utile", "perdita", "cost_of_goods", "capex", "purchases", "acquisti",
    ]
    if any(non in norm for non in ["ratio", "trend", "pct", "percent", "rate", "count", "days", "turnover_rate"]):
        return False
    return any(kw in norm for kw in keywords)


def _format_indicator_value(key: str, val, defn: dict = None) -> str:
    """Formats an indicator/field value with appropriate units, adding 'k' for monetary financials in thousands."""
    if val is None or val == "" or val == "—":
        return "—"
    if not isinstance(val, (int, float)):
        return str(val)
    k = key.lower()
    if k in MONETARY_INDICATOR_KEYS or _is_financial_field(k):
        return f"{val:,.2f}k"
    proxy_str = (defn.get("proxy") or "") if defn else ""
    if k in PERCENTAGE_INDICATOR_KEYS or "%" in proxy_str:
        return f"{val:,.2f}%"
    if k in RATIO_INDICATOR_KEYS or "ratio" in k:
        return f"{val:,.2f}x"
    return f"{val:,.2f}"


def _trend_headline(indicator_key: str, sig, raw_records: list) -> dict:
    """
    Builds a plain-language 'is it growing, by how much' view for one trend
    indicator: prefers the actual before/after figures from a RawImportRecord
    blob when one exists; falls back to just the stored % change (still
    correctly directional, just without the underlying numbers) for imports
    that predate the raw-blob feature.
    """
    base_name = _TREND_KEY_TO_RAW_BASE.get(indicator_key)
    if base_name:
        for rec in raw_records:
            groups = _trend_groups_from_raw(rec.raw_row)
            series = groups.get(base_name)
            if series:
                values = [(label, v) for label, v in series if v is not None]
                if len(values) >= 2:
                    base_val, latest_val = values[0][1], values[-1][1]
                    delta = latest_val - base_val
                    pct = None
                    if base_val != 0:
                        raw_pct = delta / abs(base_val) * 100
                        if abs(raw_pct) <= 999:
                            pct = raw_pct
                    return {
                        "has_raw": True, "latest": latest_val, "base": base_val, "delta": delta,
                        "pct": pct, "series": values, "dataset": rec.dataset_name,
                    }
    # Fallback: no raw blob for this indicator — only the pre-computed % survives.
    if sig and sig.status != "not_yet_checked" and sig.numeric_value is not None:
        pct = sig.numeric_value if abs(sig.numeric_value) <= 999 else None
        return {"has_raw": False, "pct": pct, "raw_pct": sig.numeric_value, "dataset": sig.source}
    return {"has_raw": False, "pct": None, "raw_pct": None, "dataset": None}


def _financial_profile_rows(sig_dict: dict, indicator_defs: dict) -> list:
    """Every Financial Health & Cost Structure indicator for this company,
    live or not — the explicit 'all the financials must be viewable' ask."""
    from indicators import CAT_FINANCIAL, CAT_COST
    keys = [k for k, d in indicator_defs.items() if d["category"] in (CAT_FINANCIAL, CAT_COST)]
    rows = []
    for k in sorted(keys, key=lambda k: (indicator_defs[k]["category"], indicator_defs[k]["label"])):
        defn = indicator_defs[k]
        sig = sig_dict.get(k)
        if sig and sig.status != "not_yet_checked":
            value = sig.numeric_value
            status = get_signal_display_status(sig.status, defn.get("freshness_days"), sig.fetched_at)
            mode = MODE_BADGES.get(sig.is_simulated, "—")
            source = sig.source or "—"
            fetched = sig.fetched_at.strftime("%Y-%m-%d") if sig.fetched_at else "—"
        else:
            value, status, mode, source, fetched = None, "not_yet_checked", "—", "—", "—"
        rows.append({
            "Category": defn["category"], "Indicator": defn["label"],
            "What this measures": defn.get("proxy") or "—",
            "Value": _format_indicator_value(k, value, defn),
            "Status": STATUS_BADGES.get(status, status), "Mode": mode,
            "Source": source, "Last Updated": fetched,
        })
    return rows


def _category_breakdown_rows(sig_dict: dict, indicator_defs: dict) -> list:
    """
    Per-category completeness + average normalized (0-100) score among
    checked scored signals. Informational only — an unweighted, axis-blended
    view to show at a glance what's driving the score; the real Need/
    Readiness math (weights, redundancy dampening, gating) stays in
    scoring.py and is not reproduced here.
    """
    from scoring import normalize_indicator_value
    agg = {}
    for k, defn in indicator_defs.items():
        if defn["axis"] == "context":
            continue
        sig = sig_dict.get(k)
        status = get_signal_display_status(sig.status, defn.get("freshness_days"), sig.fetched_at) if sig else "not_yet_checked"
        entry = agg.setdefault(defn["category"], {"checked": 0, "total": 0, "score_sum": 0.0})
        entry["total"] += 1
        if status in ("present", "stale"):
            entry["checked"] += 1
            entry["score_sum"] += normalize_indicator_value(sig.numeric_value, defn)
        elif status == "absent":
            entry["checked"] += 1
    rows = []
    for cat in sorted(agg.keys()):
        e = agg[cat]
        avg = (e["score_sum"] / e["checked"]) if e["checked"] else None
        rows.append({
            "Category": cat, "Checked": f"{e['checked']}/{e['total']}",
            "Avg Normalized Score": f"{avg:.1f}/100" if avg is not None else "—",
        })
    return rows


def _provenance_rows(db: Session, company: Company) -> list:
    """Which source(s) — pipeline adapter, manual entry, or an injected
    dataset name — actually populated this company's live data, and when."""
    rows = (
        db.query(SignalRecord.source, func.count(SignalRecord.id), func.max(SignalRecord.fetched_at))
        .filter(SignalRecord.company_id == company.id, SignalRecord.status == "present")
        .group_by(SignalRecord.source)
        .order_by(func.max(SignalRecord.fetched_at).desc())
        .all()
    )
    return [
        {
            "Source": src or "—", "Live Signals": cnt,
            "Last Updated": ts.strftime("%Y-%m-%d %H:%M") if ts else "—",
        }
        for src, cnt, ts in rows
    ]


def _management_board_groups(db: Session, company: Company) -> dict:
    """{role_group: [CompanyPerson, ...]} for this company, across every
    imported roster dataset, ordered for stable display."""
    people = (
        db.query(CompanyPerson)
        .filter_by(company_id=company.id)
        .order_by(CompanyPerson.role_group, CompanyPerson.dataset_name, CompanyPerson.position_in_row)
        .all()
    )
    groups = {}
    for p in people:
        groups.setdefault(p.role_group, []).append(p)
    return groups


def _pilot_rows(db: Session, company: Company) -> list:
    outcomes = db.query(PilotOutcome).filter_by(company_id=company.id).order_by(PilotOutcome.started_at.desc()).all()
    rows = []
    for o in outcomes:
        rows.append({
            "Pilot": o.pilot_label,
            "Started": o.started_at.strftime("%Y-%m-%d") if o.started_at else "—",
            "Ended": o.ended_at.strftime("%Y-%m-%d") if o.ended_at else "Ongoing",
            "Outcome": "✅ Success" if o.outcome_success else ("❌ Unsuccessful" if o.outcome_success is False else "⏳ Pending"),
            "Metric": f"{o.outcome_metric:.2f}" if o.outcome_metric is not None else "—",
            "Need @ Start": f"{o.need_score_at_start:.1f}" if o.need_score_at_start is not None else "—",
            "Readiness @ Start": f"{o.readiness_score_at_start:.1f}" if o.readiness_score_at_start is not None else "—",
        })
    return rows


def _render_tab1_content(db: Session):
    # Split out of render_company_detail_page() so its early "no companies"/
    # "no companies for this country" returns only skip THIS tab's content —
    # a bare `return` inside a `with tab_detail:` block nested directly in
    # render_company_detail_page would exit the whole function, silently
    # blanking every other tab (Add/Import/Flexible/Manage) too. That was a
    # real, previously-masked bug: it only showed once the database actually
    # had zero companies (exactly the moment those other tabs are needed to
    # add the first one).
        companies = db.query(Company).order_by(Company.legal_name).all()
        if not companies:
            st.warning("No companies found in database. Use the **➕ Add Single Company** or **📁 Bulk CSV Import** tabs above to add target companies.")
            return

        # Country Filter in Selector
        col_sel1, col_sel2 = st.columns([1, 3])
        with col_sel1:
            country_filter = st.selectbox("Filter by Country", ["All Countries", "Germany 🇩🇪", "Italy 🇮🇹"], key="detail_country_filter")

        filtered_comps = companies
        if country_filter == "Germany 🇩🇪":
            filtered_comps = [c for c in companies if (c.country or "Germany") == "Germany"]
        elif country_filter == "Italy 🇮🇹":
            filtered_comps = [c for c in companies if c.country == "Italy"]

        if not filtered_comps:
            st.info(f"No companies found for {country_filter}.")
            return

        with col_sel2:
            country_flags = {"Germany": "🇩🇪", "Italy": "🇮🇹"}
            company_names = {
                f"{c.legal_name} ({c.registration_number}) — {country_flags.get(c.country, '🌐')} {c.country} [{c.segment}]": c.id
                for c in filtered_comps
            }
            selected_name = st.selectbox("Select Target Company", list(company_names.keys()), key="detail_company_select")
            selected_id = company_names[selected_name]

        company = db.query(Company).filter_by(id=selected_id).first()
        signals = db.query(SignalRecord).filter_by(company_id=company.id).all()
        sig_dict = {s.signal_key: s for s in signals}
        indicator_defs = fetch_indicator_defs(db)
        scores = calculate_company_scores(signals, indicator_defs)

        # Top Header Summary
        col_m1, col_m2, col_m3, col_m4 = st.columns(4)
        with col_m1:
            st.metric("Need Axis Score", f"{scores['need_score']}/100", f"Weighted completeness: {scores['need_completeness_pct']:.0f}%")
        with col_m2:
            st.metric("Readiness Axis Score", f"{scores['readiness_score']}/100", f"Weighted completeness: {scores['readiness_completeness_pct']:.0f}%")
        with col_m3:
            st.metric("Overall Completeness", f"{scores['total_completeness_pct']:.1f}%", f"{scores['signals_checked']}/{scores['signals_total']} Signals")
        paid_unlocked = company.shortlist_status in ("shortlisted", "in_pilot")
        with col_m4:
            st.metric("Shortlist Status", SHORTLIST_STATUS_LABELS.get(company.shortlist_status, company.shortlist_status),
                       delta="Phase 3+ Unlocked" if paid_unlocked else "Phase 1+2 Only")

        st.markdown("---")

        # Identity & Registry
        with st.expander("📌 Identity & Registry", expanded=True):
            col_c1, col_c2, col_c3, col_c4 = st.columns(4)
            with col_c1:
                st.write("**Legal Name:**", company.legal_name)
                flag = "🇩🇪" if company.country == "Germany" else ("🇮🇹" if company.country == "Italy" else "🌐")
                st.write("**Country:**", f"{flag} {company.country}")
                st.write("**Registration Number:**", company.registration_number)
                st.write("**External Reference ID:**", company.external_ref_id or "—")
            with col_c2:
                st.write("**NACE / ATECO Code:**", company.nace_code)
                st.write("**Sector:**", company.sector_name)
                st.write("**Segment:**", company.segment)
                st.write("**Headcount:**", f"{company.headcount} ({company.headcount_source_tier})" if company.headcount else "Not yet checked")
            with col_c3:
                st.write("**Region / Province:**", f"{company.region or '—'} / {company.province or '—'}")
                st.write("**Legal Form:**", company.legal_form or "—")
                st.write("**Incorporation Date:**", company.incorporation_date.strftime("%Y-%m-%d") if company.incorporation_date else "—")
                st.write("**Registry Status:**", company.registry_status or "—")
            with col_c4:
                st.write("**Website:**", company.website_url or "—")
                st.write("**VIENNA Level:**", company.vienna_level or "Not classified (Section 5.4 deferred)")
                st.write("**Last Scored:**", company.last_scored_at.strftime("%Y-%m-%d %H:%M") if company.last_scored_at else "Never (visit Scored Target Matrix)")
                parent = db.query(Company).filter_by(id=company.parent_company_id).first() if company.parent_company_id else None
                st.write("**Parent Company:**", parent.legal_name if parent else "—")

            subsidiaries = db.query(Company).filter_by(parent_company_id=company.id).all()
            if subsidiaries:
                st.write("**Subsidiaries:**", ", ".join(s.legal_name for s in subsidiaries))

            st.markdown("&nbsp;")
            col_s1, col_s2, col_s3 = st.columns([2, 1, 1])
            with col_s1:
                new_status = st.selectbox(
                    "Shortlist status", SHORTLIST_STATUSES,
                    index=SHORTLIST_STATUSES.index(company.shortlist_status),
                    format_func=lambda s: SHORTLIST_STATUS_LABELS.get(s, s), key="shortlist_status_select",
                )
            with col_s2:
                st.markdown("&nbsp;")
                if st.button("💾 Update Status", use_container_width=True, disabled=(new_status == company.shortlist_status)):
                    company.shortlist_status = new_status
                    if new_status in ("shortlisted", "in_pilot") and not company.shortlisted_at:
                        company.shortlisted_at = datetime.utcnow()
                    db.commit()
                    st.success(f"{company.legal_name} is now **{SHORTLIST_STATUS_LABELS.get(new_status, new_status)}**.")
                    st.rerun()
            with col_s3:
                st.markdown("&nbsp;")
                if st.button("⚡ Sync Live APIs", use_container_width=True, help=f"Run applicable APIs for {company.country}"):
                    from company_service import sync_company_applicable_sources
                    with st.spinner(f"Syncing applicable APIs for {company.legal_name} ({company.country})..."):
                        sync_company_applicable_sources(company, db, phases=[1, 4])
                    st.success(f"Synced applicable APIs for {company.legal_name}!")
                    st.rerun()

        # Financial Profile — plain-language headline metrics + the complete table
        st.subheader("💰 Financial Profile")
        raw_records_for_trends = db.query(RawImportRecord).filter_by(company_id=company.id).order_by(RawImportRecord.dataset_name).all()

        trend_keys = ["revenue_trend", "ebit_trend"]
        level_keys = ["leverage_ratio", "total_assets", "cash_position", "debt_level"]

        st.markdown("**Is it growing?** (vs. the earliest year in the source data)")
        tcols = st.columns(len(trend_keys))
        for i, hk in enumerate(trend_keys):
            defn = indicator_defs.get(hk)
            if not defn:
                continue
            sig = sig_dict.get(hk)
            info = _trend_headline(hk, sig, raw_records_for_trends)
            with tcols[i]:
                if info.get("has_raw"):
                    delta_str = f"{info['delta']:+,.0f}k"
                    delta_str += f" ({info['pct']:+.0f}%)" if info["pct"] is not None else " (base too small for a % figure)"
                    st.metric(defn["label"], f"{info['latest']:,.0f}k", delta=delta_str, help=defn.get("proxy"))
                    st.caption(f"Was **{info['base']:,.0f}k** → now **{info['latest']:,.0f}k** · source: {info['dataset']}")
                elif info.get("pct") is not None:
                    st.metric(defn["label"], f"{info['pct']:+.1f}%", help=defn.get("proxy"))
                    st.caption(f"⚠️ Only the % change survived from this import ({info.get('dataset') or '—'}) — the yearly figures behind it weren't retained. Re-upload the source file to see the before/after breakdown.")
                elif sig and sig.status != "not_yet_checked":
                    st.metric(defn["label"], "N/A", help=defn.get("proxy"))
                    st.caption(f"⚠️ The stored change ({info.get('raw_pct'):,.0f}%) is too extreme to be meaningful — almost certainly a near-zero prior-year base, not a real signal. Re-upload the source file to get the underlying figures.")
                else:
                    st.metric(defn["label"], "—", help=defn.get("proxy"))
                    st.caption("Not yet checked.")

        st.markdown("**Current levels**")
        lcols = st.columns(len(level_keys))
        for i, hk in enumerate(level_keys):
            defn = indicator_defs.get(hk)
            if not defn:
                continue
            sig = sig_dict.get(hk)
            has_val = sig and sig.status != "not_yet_checked" and sig.numeric_value is not None
            with lcols[i]:
                formatted_level = _format_indicator_value(hk, sig.numeric_value, defn) if has_val else "—"
                st.metric(defn["label"], formatted_level, help=defn.get("proxy"))
                st.caption(f"Source: {sig.source} · {sig.fetched_at.strftime('%Y-%m-%d')}" if has_val and sig.fetched_at else ("Not yet checked." if not has_val else f"Source: {sig.source}"))

        with st.expander("📋 Full Financial & Cost Structure Table (every indicator, with plain-English description)", expanded=False):
            st.caption("Every Financial Health and Cost Structure indicator in the catalog for this company, checked or not. **Value** is the raw figure as computed/injected (monetary financials in thousands, **k**) — not a 0-100 score (see Score Breakdown below for that).")
            fin_df = pd.DataFrame(_financial_profile_rows(sig_dict, indicator_defs))
            st.dataframe(fin_df, use_container_width=True, hide_index=True)

        # Data Trends — every multi-year column group from the raw injected
        # data, financial or not, charted regardless of whether it was ever
        # mapped to a scored indicator.
        if raw_records_for_trends:
            with st.expander("📈 Data Trends (from Raw Injected Data)", expanded=False):
                st.caption("Every multi-year figure exactly as uploaded — financial and non-financial, whether or not it was mapped to a scored indicator (financial values in thousands, **k**).")
                for rec in raw_records_for_trends:
                    trend_groups = _trend_groups_from_raw(rec.raw_row)
                    flat_fields = _single_point_fields_from_raw(rec.raw_row)
                    st.markdown(f"**{rec.dataset_name}**")
                    if trend_groups:
                        trend_cols = st.columns(min(3, len(trend_groups)))
                        for i, (base, series) in enumerate(sorted(trend_groups.items())):
                            chart_df = pd.DataFrame(
                                [v for _, v in series], index=[label for label, _ in series], columns=[base]
                            )
                            with trend_cols[i % len(trend_cols)]:
                                tag_k = " (k)" if _is_financial_field(base) else ""
                                st.caption(f"{base.replace('_', ' ').title()}{tag_k}")
                                st.line_chart(chart_df, height=180)
                    else:
                        st.caption("No multi-year (Latest/Y-1/Y-2 style) columns detected in this dataset.")
                    if flat_fields:
                        with st.expander(f"All other fields from {rec.dataset_name}", expanded=False):
                            field_rows = []
                            for k, v in sorted(flat_fields.items()):
                                if _is_financial_field(k) and isinstance(v, (int, float)):
                                    disp_v = f"{v:,.2f}k"
                                elif _is_financial_field(k) and str(v).replace(".", "", 1).replace("-", "", 1).isdigit():
                                    try:
                                        disp_v = f"{float(v):,.2f}k"
                                    except Exception:
                                        disp_v = f"{v}k"
                                else:
                                    disp_v = v
                                field_rows.append({"Field": k, "Value": disp_v})
                            st.dataframe(
                                pd.DataFrame(field_rows),
                                use_container_width=True, hide_index=True,
                            )
                    st.markdown("&nbsp;")

        # Score Breakdown by Category
        with st.expander("📈 Score Breakdown by Category"):
            st.caption(
                "Each checked signal is normalized to 0-100 using its own Raw Min/Raw Max bounds from the Indicator "
                "Weights page (0 = worst end of the configured range, 100 = best), then averaged per category here — "
                "unweighted and axis-blended, for a quick at-a-glance read only. The real weighted Need/Readiness "
                "score (with redundancy dampening and gating) is on the Scored Target Matrix page. "
                "⚠️ If a category's average looks stuck near 0 or 100, its indicators' Raw Min/Max bounds are likely "
                "calibrated for a different data scale than what's actually been injected — check the raw values in "
                "the Financial Profile table above against the bounds shown on the Indicator Weights page."
            )
            st.dataframe(pd.DataFrame(_category_breakdown_rows(sig_dict, indicator_defs)), use_container_width=True, hide_index=True)

        # Data Provenance
        with st.expander("📜 Data Sources for This Company"):
            prov_rows = _provenance_rows(db, company)
            if prov_rows:
                st.dataframe(pd.DataFrame(prov_rows), use_container_width=True, hide_index=True)
            else:
                st.caption("No live (non-simulated) signals recorded yet for this company.")

        # Raw Injected Data — the blob side: full original rows, untouched
        # by whatever got mapped at import time.
        raw_records = db.query(RawImportRecord).filter_by(company_id=company.id).order_by(RawImportRecord.dataset_name).all()
        if raw_records:
            with st.expander(f"📦 Raw Injected Data ({len(raw_records)} dataset(s))"):
                st.caption("The complete original row for this company from each dataset upload, exactly as injected — every column, not just the ones mapped to an indicator. Useful for applying a new mapping idea later without re-uploading the file.")
                for rec in raw_records:
                    st.markdown(f"**{rec.dataset_name}** — {rec.source_filename or 'filename not recorded'} · updated {rec.updated_at.strftime('%Y-%m-%d %H:%M') if rec.updated_at else '—'}")
                    st.json(rec.raw_row, expanded=False)

        # People & Ownership — exploded from newline-stacked multi-value
        # cells (see company_service.import_company_people). Covers whatever
        # role_group(s) have been imported for this company: board/
        # management (DM/ADV), shareholders (Azionisti/CSH), subsidiaries
        # (Partecipate), etc. — driven entirely by what's in the data.
        people_groups = _management_board_groups(db, company)
        if people_groups:
            with st.expander(f"👥 People & Ownership ({sum(len(v) for v in people_groups.values())})"):
                for role_group, people in people_groups.items():
                    st.markdown(f"**{role_group}** ({len(people)})")
                    rows = [{
                        "Name": p.full_name or "—",
                        "Role": p.role or "—",
                        "Age": p.age if p.age is not None else "—",
                        "Gender": p.gender or "—",
                        "Nationality": p.nationality or "—",
                        "Status": p.current_or_former or "—",
                        "Dataset": p.dataset_name,
                    } for p in people]
                    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
                    with st.expander(f"Full detail per person ({role_group})", expanded=False):
                        for p in people:
                            st.markdown(f"*{p.full_name or 'Unnamed'}*")
                            st.json(p.raw_fields, expanded=False)

        # Pilot History
        pilot_rows = _pilot_rows(db, company)
        if pilot_rows:
            with st.expander(f"🎯 Pilot History ({len(pilot_rows)})"):
                st.dataframe(pd.DataFrame(pilot_rows), use_container_width=True, hide_index=True)

        _render_manual_entry_form(db, company, indicator_defs)

        st.markdown("---")

        from company_service import is_source_applicable

        def _build_row(sig_key, defn):
            sig_rec = sig_dict.get(sig_key)
            source_sys = defn.get("source_system") or "—"
            is_app = is_source_applicable(source_sys, company.country or "Germany")
            # A signal can carry real, observed data (e.g. from the flexible
            # data feeder) even when the catalog's own automated-pipeline
            # source_system isn't applicable for this country — that's the
            # normal case for an Italian company fed via a file upload rather
            # than the German-only Bundesanzeiger adapter. Only fall back to
            # the country-scope "N/A" placeholder when nothing real exists.
            has_real_data = sig_rec is not None and sig_rec.status != "not_yet_checked"

            if not is_app and not has_real_data:
                disp_status = f"⚪ N/A ({company.country})"
                val = "—"
                fetched_str = "N/A (Country scope)"
                raw_ref = f"Source {source_sys} not applicable for {company.country}"
                mode_badge = "—"
            elif sig_rec:
                disp_status = get_signal_display_status(sig_rec.status, defn.get("freshness_days"), sig_rec.fetched_at)
                val = sig_rec.text_value if defn["axis"] == "context" and sig_rec.text_value else sig_rec.numeric_value
                fetched_str = sig_rec.fetched_at.strftime("%Y-%m-%d %H:%M") if sig_rec.fetched_at else "N/A"
                raw_ref = sig_rec.raw_payload_ref or ""
                mode_badge = MODE_BADGES.get(sig_rec.is_simulated, "—")
            else:
                disp_status, val, fetched_str, raw_ref, mode_badge = "not_yet_checked", None, "Never", "", "—"

            fresh_window = defn.get("freshness_days") or 90
            modifier = defn.get("axis_modifier") or ""
            caveat_note = f"⚠️ {defn.get('comment')}" if modifier == "CAVEAT" and defn.get("comment") else "—"
            return {
                "Category": defn["category"],
                "Signal Name": defn["label"],
                "Weight": "—" if defn["axis"] == "context" else f"{defn.get('weight', 0):.1f}",
                "Redundancy Group": defn.get("redundancy_group") or "—",
                "Source": source_sys,
                "Tier / Phase": f"{defn.get('automation_tier') or '—'} / Phase {defn.get('phase')}",
                "Status": STATUS_BADGES.get(disp_status, disp_status),
                "Mode": mode_badge,
                "Value": _format_indicator_value(sig_key, val, defn),
                "Modifier": modifier or "—",
                "Caveat": caveat_note,
                "Freshness Window": f"{fresh_window} days",
                "Last Fetched": fetched_str,
                "Raw Payload": raw_ref,
            }

        # Scored signals, grouped by category
        st.subheader("📊 Scored Signal Audit & Freshness Breakdown")
        st.caption(f"Every indicator feeding the Need/Readiness score for {company.legal_name} ({company.country}).")

        scored_defs = {k: d for k, d in indicator_defs.items() if d["axis"] != "context"}
        categories = sorted(set(d["category"] for d in scored_defs.values()))
        for cat in categories:
            cat_keys = [k for k, d in scored_defs.items() if d["category"] == cat]
            cat_checked = sum(1 for k in cat_keys if sig_dict.get(k) and sig_dict[k].status != "not_yet_checked")
            with st.expander(f"{cat} ({cat_checked}/{len(cat_keys)} checked)", expanded=(cat_checked > 0)):
                rows = [_build_row(k, scored_defs[k]) for k in sorted(cat_keys, key=lambda k: scored_defs[k]["label"])]
                df_cat = pd.DataFrame(rows).drop(columns=["Category"])
                st.dataframe(
                    df_cat,
                    column_config={
                        "Weight": "Weight",
                        "Redundancy Group": st.column_config.TextColumn("Redundancy Group", help="Dampened against other checked members of the same group within this axis."),
                        "Status": "Tri-State Status",
                        "Mode": "Live / Simulated",
                        "Value": "Populated Value",
                        "Modifier": st.column_config.TextColumn("Modifier"),
                        "Caveat": st.column_config.TextColumn("Caveat", width="large"),
                        "Raw Payload": st.column_config.TextColumn("Raw Payload Pointer", width="medium"),
                    },
                    use_container_width=True,
                    hide_index=True,
                )

        # Context tags
        context_defs = {k: d for k, d in indicator_defs.items() if d["axis"] == "context"}
        if context_defs:
            st.markdown("---")
            st.subheader("🏷️ Context & Moderator Tags")
            st.caption("Informational tags — not part of weighted scoring sum.")
            rows = [_build_row(k, context_defs[k]) for k in sorted(context_defs.keys(), key=lambda k: context_defs[k]["label"])]
            df_ctx = pd.DataFrame(rows)[["Signal Name", "Status", "Value", "Last Fetched"]]
            st.dataframe(df_ctx, use_container_width=True, hide_index=True)


def render_company_detail_page(db: Session):
    st.title("🏢 Company Intelligence & Management")
    st.caption("Deep-dive company breakdown, tri-state signal audits, single company creation, and bulk CSV ingestion.")

    tab_detail, tab_add, tab_import, tab_flex, tab_people, tab_manage = st.tabs([
        "🏢 Company Intelligence & Audit",
        "➕ Add Single Company",
        "📁 Bulk CSV Import",
        "🔗 Flexible Data Import",
        "👥 Import People & Ownership",
        "🗑️ Manage Companies"
    ])

    # --------------------------------------------------------------------------
    # TAB 1: Company Intelligence & Deep Dive
    # --------------------------------------------------------------------------
    with tab_detail:
        _render_tab1_content(db)

    # --------------------------------------------------------------------------
    # TAB 2: Add Single Company
    # --------------------------------------------------------------------------
    with tab_add:
        st.subheader("➕ Register a New Target Company")
        st.caption("Add a company to the evaluation pipeline. Normalizes registration IDs, initializes signal records, and activates country-relevant APIs.")

        from company_service import create_company, SUPPORTED_COUNTRIES

        with st.form("add_company_form", clear_on_submit=False):
            col_f1, col_f2 = st.columns(2)
            with col_f1:
                form_country = st.radio("Country 🌐", ["Germany", "Italy"], horizontal=True, key="add_country")
                reg_help = "e.g. HRB 123456 or HRA 98765" if form_country == "Germany" else "e.g. IT01234567890 (Partita IVA), Codice Fiscale, or REA MI-1234567"
                reg_placeholder = "HRB 104928" if form_country == "Germany" else "IT09876543210"
                form_reg_nr = st.text_input(
                    f"Registration Identifier ({'Handelsregister-Nr.' if form_country == 'Germany' else 'P.IVA / CF / REA'}) *",
                    placeholder=reg_placeholder, help=reg_help
                )
                form_name = st.text_input("Legal Company Name *", placeholder="e.g. BioTech Agrar Solutions GmbH")
                form_website = st.text_input("Website URL", placeholder="https://example.com")

            with col_f2:
                form_nace = st.text_input("NACE Code", value="A01.1", help="Economic sector classification (e.g. A01.11, C10.51)")
                form_sector = st.text_input("Sector Name", value="Agrifood & Smart Farming")
                col_seg1, col_seg2 = st.columns(2)
                with col_seg1:
                    form_segment = st.selectbox("Segment", ["Midcap", "SME"], help="SME (<250 employees) or Midcap (250-3000)")
                with col_seg2:
                    form_headcount = st.number_input("Headcount (Employees)", min_value=1, max_value=50000, value=150, step=10)
                form_auto_sync = st.checkbox("⚡ Immediately run Phase 1 & 4 APIs for this company", value=True)

            st.markdown("&nbsp;")
            submitted = st.form_submit_button("🚀 Add Target Company", use_container_width=True)

            if submitted:
                comp_data = {
                    "legal_name": form_name,
                    "registration_number": form_reg_nr,
                    "country": form_country,
                    "nace_code": form_nace,
                    "sector_name": form_sector,
                    "website_url": form_website,
                    "segment": form_segment,
                    "headcount": form_headcount,
                }
                new_comp, err = create_company(db, comp_data, auto_sync=form_auto_sync)
                if err:
                    st.error(f"❌ {err}")
                else:
                    st.success(f"✅ Successfully added **{new_comp.legal_name}** ({new_comp.registration_number}) for {new_comp.country}!")
                    st.rerun()

    # --------------------------------------------------------------------------
    # TAB 3: Bulk CSV Import
    # --------------------------------------------------------------------------
    with tab_import:
        st.subheader("📁 Bulk CSV Ingestion")
        st.caption("Upload a batch of German and/or Italian companies via CSV to populate the target pipeline.")

        from company_service import import_companies_from_csv, get_csv_template

        col_imp1, col_imp2 = st.columns([2, 1])
        with col_imp1:
            uploaded_file = st.file_uploader("Upload Company CSV", type=["csv"], key="company_csv_uploader")
        with col_imp2:
            st.markdown("**Download Template:**")
            template_csv = get_csv_template()
            st.download_button(
                "📥 Download CSV Template",
                data=template_csv,
                file_name="target_companies_template.csv",
                mime="text/csv",
                use_container_width=True,
            )

        if uploaded_file is not None:
            try:
                preview_df = pd.read_csv(uploaded_file)
                st.markdown("##### Preview Data to Import:")
                st.dataframe(preview_df.head(10), use_container_width=True)
                st.caption(f"Found **{len(preview_df)}** rows in uploaded file.")

                auto_sync_csv = st.checkbox("⚡ Automatically sync live APIs for all imported companies", value=False, key="csv_auto_sync")

                if st.button("🚀 Process & Import Companies", key="btn_process_csv", use_container_width=True):
                    uploaded_file.seek(0)
                    with st.spinner(f"Importing {len(preview_df)} companies..."):
                        result = import_companies_from_csv(db, uploaded_file, auto_sync=auto_sync_csv)

                    st.success(f"✅ Import complete! **{result['created']}** companies created, **{result['skipped']}** skipped.")
                    if result["errors"]:
                        with st.expander("⚠️ Import Warnings & Skipped Rows", expanded=True):
                            for err in result["errors"]:
                                st.warning(err)
                    st.rerun()
            except Exception as e:
                st.error(f"Error reading CSV: {e}")

    # --------------------------------------------------------------------------
    # TAB 4: Flexible Column-Mapping Import
    # --------------------------------------------------------------------------
    with tab_flex:
        _render_flexible_import_tab(db)

    # --------------------------------------------------------------------------
    # TAB 5: Board & Management Roster Import
    # --------------------------------------------------------------------------
    with tab_people:
        _render_people_import_tab(db)

    # --------------------------------------------------------------------------
    # TAB 6: Manage / Delete Companies
    # --------------------------------------------------------------------------
    with tab_manage:
        _render_manage_companies_tab(db)
