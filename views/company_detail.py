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
import hashlib
import streamlit as st
import pandas as pd
from datetime import datetime
from sqlalchemy import func
from sqlalchemy.orm import Session
from models import Company, SignalRecord, PilotOutcome, RawImportRecord, CompanyPerson, CompanySourceRun, SHORTLIST_STATUSES
from indicators import SRC_AIDA, fetch_indicator_defs
from scoring import calculate_company_scores
from utils import get_signal_display_status

STATUS_BADGES = {
    "present": "🟢 Present",
    "absent": "🔴 Confirmed Absent (Zero)",
    "not_yet_checked": "⚪ Not Yet Checked",
    "stale": "🟡 Stale (Refetch Flag)"
}

MODE_BADGES = {True: "🧪 Simulated", False: "🟢 Live"}


def mode_badge_for(sig) -> str:
    """Live / Simulated badge for a SignalRecord. An unchecked row is an empty placeholder whose
    is_simulated is just the column default, so it carries no data to call simulated."""
    if sig is None or sig.status == "not_yet_checked":
        return "⚪ Not collected"
    return MODE_BADGES.get(sig.is_simulated, "—")

SHORTLIST_STATUS_LABELS = {
    "candidate": "Candidate (Phase 1+2 only)",
    "shortlisted": "Shortlisted (Phase 3+ unlocked)",
    "in_pilot": "In Pilot",
    "rejected": "Rejected",
}


def _progress_reporter(label: str = "row"):
    """Creates a live st.progress bar + status line and returns a callback
    of the shape company_service's import functions expect
    (progress_callback(rows_done, total_rows)) to drive them."""
    bar = st.progress(0)
    status = st.empty()

    def _callback(done: int, total: int):
        pct = min(1.0, done / total) if total else 1.0
        bar.progress(pct)
        status.caption(f"Processing {label} {done} of {total}...")

    return bar, status, _callback


def _mapping_signature(mapping: dict) -> str:
    """Short stable token used in Streamlit widget keys to avoid stale dropdown state."""
    try:
        payload = json.dumps(mapping or {}, sort_keys=True, default=str)
    except TypeError:
        payload = str(mapping or {})
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


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
        "recognize and lets you link each physical source column to a company field, a canonical "
        "financial fact, or a direct indicator. Map periods separately—for example Revenue — latest, "
        "Revenue — year -1, and Revenue — year -2. Trends are then calculated automatically from "
        "those reviewed facts. The mapping is saved under the dataset name for reuse."
    )

    from company_service import (
        parse_uploaded_file, suggest_column_mapping,
        valid_targets_for_column, apply_data_import, save_mapping_profile,
        load_mapping_profile, create_ad_hoc_indicator, list_mapping_profiles,
        AUTO_DERIVED_TRENDS,
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
    profile_mapping = load_mapping_profile(db, dataset_name) if dataset_name.strip() else {}
    mapping_context_sig = (uploaded.name, uploaded.size, dataset_name.strip(), _mapping_signature(profile_mapping))
    if st.session_state.get("flex_mapping_context_sig") != mapping_context_sig:
        try:
            uploaded.seek(0)
            df = parse_uploaded_file(uploaded, filename=uploaded.name)
        except Exception as e:
            st.error(f"Could not read file: {e}")
            return
        st.session_state["flex_file_sig"] = file_sig
        st.session_state["flex_mapping_context_sig"] = mapping_context_sig
        st.session_state["flex_df"] = df
        st.session_state["flex_mapping"] = suggest_column_mapping(db, list(df.columns), existing_profile=profile_mapping)
        st.session_state["flex_widget_sig"] = _mapping_signature(st.session_state["flex_mapping"])
        st.session_state["flex_new_labels"] = {}
        st.session_state["flex_preview"] = None

    df = st.session_state["flex_df"]
    mapping_state = st.session_state["flex_mapping"]
    flex_widget_sig = st.session_state.get("flex_widget_sig", _mapping_signature(mapping_state))
    new_labels_state = st.session_state["flex_new_labels"]

    st.markdown(f"##### Detected **{len(df.columns)}** source column(s) across **{len(df)}** row(s)")
    st.dataframe(df.head(5), use_container_width=True)
    st.markdown("###### Column Mapping")

    options = valid_targets_for_column(db)
    for source_column in df.columns:
        option_keys = list(options.keys()) + [NEW_INDICATOR_OPTION]
        option_labels = {**options, NEW_INDICATOR_OPTION: "+ Create New Indicator..."}

        current = mapping_state.get(source_column)
        if current not in option_keys:
            current = ""
        default_idx = option_keys.index(current)

        row_c1, row_c2 = st.columns([2, 3])
        with row_c1:
            st.markdown(f"**{source_column}**")
            st.caption("One source column → one fact")
        with row_c2:
            picked = st.selectbox(
                f"Map '{source_column}' to", options=option_keys,
                format_func=lambda k: option_labels.get(k, k),
                index=default_idx, key=f"flex_map_{flex_widget_sig}_{source_column}", label_visibility="collapsed",
            )
            mapping_state[source_column] = picked
            if picked == NEW_INDICATOR_OPTION:
                new_labels_state[source_column] = st.text_input(
                    f"New indicator label for '{source_column}'",
                    value=new_labels_state.get(source_column, source_column.replace("_", " ").title()),
                    key=f"flex_new_label_{source_column}",
                )
    st.session_state["flex_mapping"] = mapping_state
    st.session_state["flex_new_labels"] = new_labels_state

    mapped_variables = {
        target.split(":", 1)[1]
        for target in mapping_state.values()
        if isinstance(target, str) and target.startswith("variable:")
    }
    automatic_trends = []
    defs = fetch_indicator_defs(db)
    for base, signal_key in AUTO_DERIVED_TRENDS.items():
        prior_periods = {period for period in ("y-1", "y-2") if f"{base}_{period}" in mapped_variables}
        ready = f"{base}_latest" in mapped_variables and bool(prior_periods)
        if signal_key == "margin_compression":
            ready = ready and "revenue_latest" in mapped_variables and any(
                f"revenue_{period}" in mapped_variables for period in prior_periods
            )
        if ready:
            automatic_trends.append(defs.get(signal_key, {}).get("label", signal_key.replace("_", " ").title()))
    if automatic_trends:
        st.info("Calculated automatically from the mapped facts: " + ", ".join(automatic_trends) + ".")

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
                bar, status, progress_cb = _progress_reporter("company")
                file_sig = st.session_state.get("flex_file_sig")
                result = apply_data_import(
                    db, df, resolved_mapping, dataset_name, country=flex_country,
                    overwrite_conflicts=overwrite, dry_run=False,
                    source_filename=file_sig[0] if file_sig else None,
                    progress_callback=progress_cb,
                )
                save_mapping_profile(db, dataset_name, flex_country, resolved_mapping)
                bar.empty()
                status.empty()
                st.success(
                    f"✅ **Import complete — {len(df)} row(s) processed.** "
                    f"**{result['created']}** created, **{result['merged']}** merged, "
                    f"**{result['overwritten']}** overwritten, **{len(result['conflicts'])}** skipped as conflicts, "
                    f"**{len(result.get('unmatched', []))}** unmatched names."
                )
                if result["errors"]:
                    with st.expander("⚠️ Import Warnings & Skipped Rows", expanded=True):
                        for err in result["errors"]:
                            st.warning(err)
                for k in ("flex_df", "flex_file_sig", "flex_mapping", "flex_new_labels",
                          "flex_mapping_context_sig", "flex_widget_sig",
                          "flex_preview", "flex_resolved_mapping"):
                    st.session_state.pop(k, None)
                st.rerun()


def _render_flexible_people_import_tab(db: Session):
    st.subheader("🧑‍💼 Flexible People Import")
    st.caption(
        "For sources that give ONE ROW PER PERSON in any column layout — e.g. output from a "
        "LinkedIn-profile extraction pipeline. Mirrors 🔗 Flexible Data Import's own approach "
        "(any column names, auto-suggested + reviewed mapping, saved under the dataset name) "
        "but for people instead of company signals — far more robust than the stacked-cell "
        "'👥 Import People & Ownership' tab for a source that isn't natively shaped like AIDA's "
        "exports, since there's no embedded-newline CSV quoting to get right. Company Legal "
        "Name is repeated on every person's row. Matches to an existing company by **exact "
        "legal name** — never creates a new company from this file; unmatched names are listed."
    )

    from company_service import (
        parse_uploaded_file, suggest_flat_person_mapping, apply_person_data_import,
        save_flat_person_mapping_profile, load_flat_person_mapping_profile,
        list_flat_person_mapping_profile_names, PERSON_FLAT_TARGET_LABELS,
    )

    existing_names = list_flat_person_mapping_profile_names(db)
    dataset_name = st.text_input(
        "Dataset Name *", placeholder="e.g. LinkedIn Roster",
        help="Same scoping rule as the other import tabs: re-uploading under the SAME name "
             "flags companies that already have it (with an overwrite option); a DIFFERENT "
             "name just adds in.",
        key="flatppl_dataset_name",
    )
    if existing_names:
        st.caption(f"Previously saved dataset names: {', '.join(existing_names)}")

    uploaded = st.file_uploader("Upload Dataset (.csv or .xlsx)", type=["csv", "xlsx", "xls"], key="flatppl_uploader")

    if uploaded is None:
        st.session_state.pop("flatppl_df", None)
        return

    file_sig = (uploaded.name, uploaded.size)
    if st.session_state.get("flatppl_file_sig") != file_sig:
        try:
            uploaded.seek(0)
            df = parse_uploaded_file(uploaded, filename=uploaded.name)
        except Exception as e:
            st.error(f"Could not read file: {e}")
            return
        st.session_state["flatppl_file_sig"] = file_sig
        st.session_state["flatppl_df"] = df
        profile_mapping = load_flat_person_mapping_profile(db, dataset_name) if dataset_name.strip() else {}
        st.session_state["flatppl_mapping"] = suggest_flat_person_mapping(list(df.columns), existing_profile=profile_mapping)
        st.session_state["flatppl_preview"] = None

    df = st.session_state["flatppl_df"]
    mapping_state = st.session_state.setdefault("flatppl_mapping", suggest_flat_person_mapping(list(df.columns)))

    st.markdown(f"##### {len(df)} row(s) — one person per row")
    st.dataframe(df.head(5), use_container_width=True)
    st.markdown("###### Column Mapping")

    option_keys = list(PERSON_FLAT_TARGET_LABELS.keys())
    for col in df.columns:
        current = mapping_state.get(col, "")
        if current not in option_keys:
            current = ""
        default_idx = option_keys.index(current)

        row_c1, row_c2 = st.columns([2, 3])
        with row_c1:
            st.caption(f"**{col}**")
        with row_c2:
            picked = st.selectbox(
                f"Map '{col}' to", options=option_keys,
                format_func=lambda k: PERSON_FLAT_TARGET_LABELS.get(k, k),
                index=default_idx, key=f"flatppl_map_{col}", label_visibility="collapsed",
            )
            mapping_state[col] = picked
    st.session_state["flatppl_mapping"] = mapping_state

    match_cols = [c for c, t in mapping_state.items() if t == "match:legal_name"]
    has_match_key = len(match_cols) == 1
    if not has_match_key:
        st.warning("Map exactly one column to **Match: Company Legal Name** before previewing.")

    st.markdown("---")

    if st.button(
        "🔍 Preview Import", key="flatppl_preview_btn",
        disabled=not has_match_key or not dataset_name.strip(), use_container_width=True,
    ):
        with st.spinner("Analyzing..."):
            preview = apply_person_data_import(db, df, mapping_state, dataset_name, dry_run=True)
        st.session_state["flatppl_preview"] = preview

    preview = st.session_state.get("flatppl_preview")
    if preview:
        if preview["errors"] and not (preview["matched"] or preview["conflicts"]):
            for err in preview["errors"]:
                st.error(err)
        else:
            pc1, pc2, pc3 = st.columns(3)
            pc1.metric("Matched People (rows)", preview["matched"])
            pc2.metric("⚠️ Unmatched Names", len(preview["unmatched"]))
            pc3.metric("⚠️ Conflicts", len(preview["conflicts"]))

            if preview["unmatched"]:
                with st.expander(f"⚠️ {len(preview['unmatched'])} legal name(s) with no matching company", expanded=False):
                    for name in preview["unmatched"]:
                        st.write(f"- {name}")
            if preview["errors"]:
                with st.expander(f"⚠️ {len(preview['errors'])} row(s) with issues", expanded=False):
                    for err in preview["errors"]:
                        st.warning(err)

            overwrite = False
            if preview["conflicts"]:
                with st.expander(
                    f"⚠️ {len(preview['conflicts'])} companies already have a '{dataset_name}' roster loaded",
                    expanded=True,
                ):
                    for c in preview["conflicts"]:
                        st.write(f"- {c['legal_name']} ({c['registration_number']})")
                    overwrite = st.checkbox(
                        f"Overwrite existing '{dataset_name}' roster for the companies listed above",
                        value=False, key="flatppl_overwrite_checkbox",
                    )

            if st.button("🚀 Confirm Import", key="flatppl_confirm_btn", use_container_width=True):
                bar, status, progress_cb = _progress_reporter("person")
                result = apply_person_data_import(
                    db, df, mapping_state, dataset_name,
                    source_filename=file_sig[0], overwrite_conflicts=overwrite, dry_run=False,
                    progress_callback=progress_cb,
                )
                save_flat_person_mapping_profile(db, dataset_name, mapping_state)
                bar.empty()
                status.empty()
                st.success(
                    f"✅ **Import complete — {len(df)} row(s) processed.** "
                    f"**{result['matched']}** people matched to companies — "
                    f"**{result['people_created']}** created, **{result['people_updated']}** updated."
                )
                if result["unmatched"]:
                    with st.expander(f"⚠️ {len(result['unmatched'])} legal name(s) with no matching company", expanded=False):
                        for name in result["unmatched"]:
                            st.write(f"- {name}")
                if result["errors"]:
                    with st.expander("⚠️ Import Warnings & Skipped Rows", expanded=True):
                        for err in result["errors"]:
                            st.warning(err)
                for k in ("flatppl_df", "flatppl_file_sig", "flatppl_mapping", "flatppl_preview"):
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
        "and from matching stacked-cell patterns where it doesn't. Each group's sub-columns are "
        "pre-mapped onto the matching person field below (name/role/age/gender/...) — review or "
        "change any of them; the mapping is saved under the dataset name so re-uploading the same "
        "shape later re-applies it automatically (same behavior as Flexible Data Import's mapping "
        "presets). Matches rows to existing companies by **legal name** (not the file's own BvD ID "
        "column — verified against real data that it doesn't reliably match the registration numbers "
        "already on file). Never creates a new company from this file alone; unmatched names are "
        "listed so you can reconcile them."
    )

    from company_service import (
        parse_roster_file, detect_person_groups, import_company_people, strip_person_column_prefix,
        suggest_person_mapping, save_person_mapping_profile, load_person_mapping_profile,
        list_person_mapping_profile_names, PERSON_TARGET_LABELS,
    )

    existing_names = list_person_mapping_profile_names(db)
    dataset_name = st.text_input(
        "Dataset Name *", placeholder="e.g. Directors & Board C28",
        help="Same scoping rule as Flexible Data Import: re-uploading under the SAME name flags "
             "companies that already have it (with an overwrite option); a DIFFERENT name just adds in.",
        key="people_dataset_name",
    )
    if existing_names:
        st.caption(f"Previously saved dataset names: {', '.join(existing_names)}")
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
        groups = detect_person_groups(df)
        profile_mapping = load_person_mapping_profile(db, dataset_name) if dataset_name.strip() else {}
        st.session_state["people_mapping"] = suggest_person_mapping(groups, existing_profile=profile_mapping)

    df = st.session_state["people_df"]
    groups = detect_person_groups(df)
    mapping_state = st.session_state.setdefault("people_mapping", suggest_person_mapping(groups))

    st.markdown(f"##### {len(df)} row(s), **{len(groups)}** person/entity group(s) detected")
    if groups:
        st.markdown("###### Column Mapping (per detected group)")
        option_keys = list(PERSON_TARGET_LABELS.keys())
        for role_group, cols in groups.items():
            with st.expander(f"**{role_group}** — {len(cols)} column(s)", expanded=True):
                for col in cols:
                    sub_label = strip_person_column_prefix(col)
                    map_key = f"{role_group}::{sub_label}"
                    current = mapping_state.get(map_key, "")
                    if current not in option_keys:
                        current = ""
                    default_idx = option_keys.index(current)

                    row_c1, row_c2 = st.columns([2, 3])
                    with row_c1:
                        st.caption(f"**{sub_label}**  \n`{col}`")
                    with row_c2:
                        picked = st.selectbox(
                            f"Map '{sub_label}' to", options=option_keys,
                            format_func=lambda k: PERSON_TARGET_LABELS.get(k, k),
                            index=default_idx, key=f"people_map_{role_group}_{sub_label}",
                            label_visibility="collapsed",
                        )
                        mapping_state[map_key] = picked
        st.session_state["people_mapping"] = mapping_state
    else:
        st.warning("No multi-value stacked-cell column groups detected in this file's headers/content.")
    st.dataframe(df.head(5), use_container_width=True)

    st.markdown("---")

    if st.button("🔍 Preview Import", key="people_preview_btn", disabled=not dataset_name.strip() or not groups, use_container_width=True):
        with st.spinner("Analyzing..."):
            preview = import_company_people(
                db, df, dataset_name, legal_name_column=legal_name_col,
                field_overrides=mapping_state, dry_run=True,
            )
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
                bar, status, progress_cb = _progress_reporter("company")
                result = import_company_people(
                    db, df, dataset_name, legal_name_column=legal_name_col,
                    source_filename=file_sig[0], overwrite_conflicts=overwrite, dry_run=False,
                    progress_callback=progress_cb, field_overrides=mapping_state,
                )
                save_person_mapping_profile(db, dataset_name, mapping_state)
                bar.empty()
                status.empty()
                st.success(
                    f"✅ **Import complete — {len(df)} row(s) processed.** **{result['matched']}** companies matched — "
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
                for k in ("people_df", "people_file_sig", "people_preview", "people_mapping"):
                    st.session_state.pop(k, None)
                st.rerun()


def _render_manage_companies_tab(db: Session):
    st.subheader("🗑️ Manage Master Company Database")
    st.caption(
        "Every company currently in the database, regardless of how it was added "
        "(seed data, manual entry, CSV or flexible import). Select rows to delete "
        "permanently — this also removes that company's signal history and any "
        "recorded pilot outcomes. Website is editable directly in the table below "
        "— useful when it points at the wrong site (e.g. a spare-parts/B2B sub-portal "
        "instead of the company's real one, which throws off every crawler that reads it)."
    )

    from company_service import delete_companies, update_company_websites

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
            "Website": c.website_url or "",
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
        column_order=["Delete", "Legal Name", "Registration #", "Country", "Segment", "Website",
                      "Shortlist Status", "Need Score", "Readiness Score",
                      "Present Signals", "Pilot Outcomes"],
        disabled=["Legal Name", "Registration #", "Country", "Segment", "Shortlist Status",
                  "Need Score", "Readiness Score", "Present Signals", "Pilot Outcomes"],
        column_config={
            "Delete": st.column_config.CheckboxColumn("🗑️ Delete", help="Check to mark for deletion"),
            "Website": st.column_config.TextColumn(
                "🌐 Website", help="The site every crawler reads for this company — correct it here if it "
                                    "points at the wrong page (e.g. a spare-parts/B2B sub-portal)."),
            "Need Score": st.column_config.NumberColumn(format="%.1f"),
            "Readiness Score": st.column_config.NumberColumn(format="%.1f"),
        },
    )

    website_changes = {
        row["_id"]: row["Website"]
        for _, row in edited.iterrows()
        if (row["Website"] or "").strip() != (df.loc[df["_id"] == row["_id"], "Website"].iloc[0] or "").strip()
    }
    if website_changes:
        plural = "y" if len(website_changes) == 1 else "ies"
        st.info(f"**{len(website_changes)}** website URL{'' if len(website_changes) == 1 else 's'} changed for "
                f"{len(website_changes)} compan{plural} — not saved yet.")
        if st.button("💾 Save Website Changes", use_container_width=True):
            result = update_company_websites(db, website_changes)
            st.success(f"Updated website for {len(result['updated'])} compan{plural}. "
                       "Re-run the relevant crawlers (Company Intelligence → 🕸️ Run Deep Crawlers) to "
                       "refresh data pulled from the old URL.")
            st.rerun()

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


def _trend_groups_from_raw(raw_row: dict, mapping_snapshot: dict = None) -> dict:
    """
    Detects multi-year column groups (e.g. revenue_latest/revenue_y-1/
    revenue_y-2) within one company's raw injected row — financial or not,
    scored or not — using the same convention the import mapping step
    relies on. Returns {base_label: [(year_label, value), ...]} ordered
    oldest -> newest, ready to chart. Only groups with 2+ timepoints.
    """
    from company_service import detect_column_groups
    groups = detect_column_groups(list(raw_row.keys()))
    # A source can call its columns anything. Point-level mappings provide the
    # canonical names needed to chart Ricavi ultimo anno / anno -1 / anno -2
    # as one Revenue series without renaming or modifying the uploaded row.
    canonical_row = {
        target.split(":", 1)[1]: raw_row.get(source_column)
        for source_column, target in (mapping_snapshot or {}).items()
        if isinstance(target, str) and target.startswith("variable:") and source_column in raw_row
    }
    if canonical_row:
        canonical_groups = detect_column_groups(list(canonical_row.keys()))
        for base, group in canonical_groups.items():
            group["_canonical_row"] = canonical_row
            groups[base] = group
    trends = {}
    for base, group in groups.items():
        if not group["is_timeseries"]:
            continue
        points = group["points"]
        y_suffixes = sorted([s for s in points if s.startswith("y-")], key=lambda s: -int(s.split("-")[1]))
        ordered = y_suffixes + (["latest"] if "latest" in points else [])
        series = []
        for suf in ordered:
            source_row = group.get("_canonical_row", raw_row)
            raw_val = source_row.get(points[suf])
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
_TREND_KEY_TO_RAW_BASE = {
    "revenue_trend": "revenue", "ebit_trend": "ebit", "ebitda_trend": "ebitda",
    "margin_compression": "gross_margin", "employee_growth": "employees",
}

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

# Units for indicators whose name would otherwise be misread by the keyword heuristics in _format_indicator_value
# (e.g. "ebit_margin" contains "ebit", so it used to render as a k EUR amount). Checked FIRST. Values: (suffix, decimals).
INDICATOR_UNITS = {
    "ebit_margin": ("%", 1), "net_margin": ("%", 1), "cash_to_revenue": (" months", 2), "intangibles_share": ("%", 1),
    "employee_growth": ("%", 1), "labour_cost": ("%", 1), "materials_cost": ("%", 1), "capex_ratio": ("%", 2),
    "family_ownership_share": ("%", 0), "foreign_ownership_share": ("%", 0), "margin_compression": (" pp", 2),
    "average_salary": ("k", 1), "revenue_per_employee": ("k", 0), "cogs": ("k", 0), "material_capex": ("k", 0), "immaterial_capex": ("k", 0),
    "group_size": ("", 0), "bvd_independence": ("", 0), "last_accounts_year": ("", 0), "distress_procedure": ("", 0),
    "subsidiary_participations": ("", 0),
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


def _render_score_breakdown(axis_label: str, axis_score: float, detail: list, meta: dict):
    """
    The "why is this score X" view: every signal that fed this axis, in the exact
    numbers _evaluate_axis used to compute axis_score — never a re-derived estimate,
    since this is the same `detail`/`meta` scoring.py returned alongside the score
    itself. Two questions this answers that a single number can't:
      - What's actually dragging this down? -> sort by normalized score ascending;
        a signal with a LOW score and a HIGH weight is doing the most damage, and a
        contribution-based sort would hide that (0 contribution looks the same
        whether a signal is unchecked or checked-and-terrible).
      - What would move it? -> the "not yet checked" table, sorted by the weight
        it would carry if populated — the highest-leverage gaps to go fill first.
    """
    checked = [e for e in detail if e["status"] in ("present", "absent", "stale")]
    unchecked = [e for e in detail if e["status"] == "not_yet_checked"]

    with st.expander(f"🔍 Why is {axis_label} {axis_score}/100?", expanded=False):
        gate_mult = meta["gate_multiplier"]
        if gate_mult != 1.0:
            st.markdown(
                f"**Weighted average of {len(checked)} checked signal(s): {meta['pre_gate_score']}** "
                f"→ × **{gate_mult}** gate penalty → **{axis_score}**"
            )
            for g in meta["fired_gates"]:
                st.warning(
                    f"🚧 Gate **{g['label']}** is {g['raw_status']} (normalized {g['normalized_score']}/100, "
                    f"below the 50 threshold) → readiness multiplied by **×{g['gate_penalty_multiplier']}**",
                    icon="🚧",
                )
        else:
            st.markdown(f"**Weighted average of {len(checked)} checked signal(s) = {axis_score}** (no gate penalties fired)")

        if not checked:
            st.info("No signals checked yet on this axis — nothing to break down.")
        else:
            rows = []
            for e in sorted(checked, key=lambda x: (x["normalized_score"] if x["normalized_score"] is not None else 0)):
                dampened = e["effective_weight"] < e["base_weight"] - 1e-9
                rows.append({
                    "Signal": e["label"],
                    "Category": e["category"],
                    "Raw Value": "—" if e["raw_value"] is None else round(e["raw_value"], 2),
                    "Score (0-100)": e["normalized_score"],
                    "Weight": f"{e['effective_weight']:.2f}" + (f" (dampened from {e['base_weight']:.1f})" if dampened else ""),
                    "Share of Axis": f"{e['contribution_pct_of_axis']:.1f}%",
                    "Status": e["status"],
                    "Gate": "🚧 fired" if e["gate_fired"] else "",
                    "Source": e["source"] or "—",
                    "What was found": e["summary"] or "—",
                })
            st.caption("Sorted worst-scoring first — a low score on a high-weight row is what's dragging this axis down.")
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        if unchecked:
            top_gaps = sorted(unchecked, key=lambda x: -x["base_weight"])[:10]
            with st.expander(f"⚪ {len(unchecked)} signal(s) not yet checked — highest-weight gaps shown first"):
                st.caption("None of these count for or against the score yet. Checking the top ones would move it the most.")
                st.dataframe(pd.DataFrame([
                    {"Signal": e["label"], "Category": e["category"], "Weight if checked": e["base_weight"],
                     "Reason": e.get("score_exclusion_reason") or "No eligible observation yet"}
                    for e in top_gaps
                ]), use_container_width=True, hide_index=True)


# Fixed-name Phase 7 crawlers write SignalRecord.source as their own
# SOURCE_NAME constant (e.g. "Job Postings Crawler") but persist their raw
# capture under a differently-named RawImportRecord.dataset_name (e.g.
# "crawler_job_postings") — see scrapers/*_crawler.py and
# scrapers/node_crawler_base.save_crawler_blob. File-import sources (AIDA,
# any flexible-import dataset) don't need this map at all: their
# SignalRecord.source already equals the RawImportRecord.dataset_name
# directly (see company_service.apply_data_import / aida_import.py).
_CRAWLER_SOURCE_TO_DATASET = {
    "Digital Maturity Crawler": "crawler_digital_maturity",
    "Company Website Crawler": "crawler_company_website",
    "Product Catalog Crawler": "crawler_product_catalog",
    "Job Postings Crawler": "crawler_job_postings",
    "Innovation Participation Crawler": "crawler_innovation_participation",
    "News Signals Crawler": "crawler_news_signals",
    "Google News RSS": "crawler_news_rss",
    "TED Contract Awards Crawler": "crawler_ted_contract_awards",
    "FDA Recalls Crawler": "crawler_fda_recalls",
}
# Directory Listing / Review crawlers fan out one RawImportRecord per
# sub-source they check (e.g. "crawler_directory_mecspe") rather than one
# fixed dataset — matched by prefix instead of an exact name.
_CRAWLER_SOURCE_PREFIXES = {
    "Directory Listing Crawler": "crawler_directory_",
    "Review Crawler": "crawler_reviews_",
}


def _raw_records_for_source(db: Session, company: Company, source: str) -> list:
    """Best-effort resolve which RawImportRecord row(s) actually back a given
    SignalRecord.source, for the Audit dialog and the Raw Data tab's deep link.
    Not every source has a raw blob (e.g. a plain API adapter with no
    separate capture step) — callers must handle an empty result."""
    if not source:
        return []
    q = db.query(RawImportRecord).filter_by(company_id=company.id)
    direct = q.filter_by(dataset_name=source).all()
    if direct:
        return direct
    mapped = _CRAWLER_SOURCE_TO_DATASET.get(source)
    if mapped:
        matched = q.filter_by(dataset_name=mapped).all()
        if matched:
            return matched
    prefix = _CRAWLER_SOURCE_PREFIXES.get(source)
    if prefix:
        return q.filter(RawImportRecord.dataset_name.like(f"{prefix}%")).all()
    return []


def _render_evidence_block(payload: dict):
    """
    Renders whatever per-signal provenance a payload carries, regardless of
    which of the two shapes its producer used: a nested {"evidence": {...}}
    block (crawlers, AIDA's financial derivations), or a flat dict at the
    payload's own top level (Board Roster Analysis's {"basis", "n",
    "person_ids", "person_names"}, sync_succession_signal's {"surname",
    "young_manager", ...}). Shared by the page-level Evidence section and the
    per-signal Audit dialog so both read every producer's shape the same way.
    """
    if not payload:
        st.caption("No further detail recorded for this value.")
        return
    ev = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else payload

    method = ev.get("method") or ev.get("basis")
    if method:
        st.markdown(f"**How it was derived:** {method}")

    inputs = ev.get("inputs")
    if inputs:
        st.markdown("**Inputs used:**")
        for k, v in inputs.items():
            st.markdown(f"- {k}: {v}")

    raw_fields = ev.get("raw_fields") or payload.get("raw_fields") or []
    if raw_fields:
        st.markdown("**Recorded input fields:**")
        st.dataframe(pd.DataFrame([
            {
                "Source file": item.get("source_file") or payload.get("source_file") or payload.get("dataset") or "—",
                "Original file header": item.get("source_header") if item.get("source_header_is_verbatim") else "Not retained/verified",
                "Stored field": item.get("column") or item.get("field") or "—",
                "Raw value": item.get("raw_value"),
            }
            for item in raw_fields
        ]), use_container_width=True, hide_index=True)

    names = ev.get("person_names") or payload.get("person_names")
    if names:
        st.markdown(f"**People counted ({len(names)}):**")
        for name in names:
            st.markdown(f"- {name}")

    cols = payload.get("source_columns") or ev.get("source_columns")
    if cols:
        st.markdown(f"**Stored column key(s):** {', '.join(cols)}")

    items = ev.get("found") or ev.get("open_roles_sample") or []
    if items:
        label_hdr = "Items counted" if ev.get("found") else "Open roles found (sample)"
        st.markdown(f"**{label_hdr}:**")
        for it in items:
            lbl = it.get("label") or "—"
            st.markdown(f"- [{lbl}]({it['url']})" if it.get("url") else f"- {lbl}")

    for social in (ev.get("social_profiles") or []):
        st.markdown(f"- {social.get('label')}: {social.get('url')}")

    for url in (ev.get("source_urls") or []):
        st.markdown(f"**Check against:** {url}")

    reason = payload.get("fallback_reason")
    if reason:
        st.warning(f"⚠️ Simulated because: {reason}")

    note = ev.get("note")
    if note:
        st.info(note)

    rest_keys = {"method", "basis", "inputs", "person_ids", "person_names", "found",
                 "open_roles_sample", "source_urls", "note", "social_profiles",
                 "source_columns", "evidence", "simulated", "fallback_reason",
                 "dataset", "signal_key", "raw_fields", "n"}
    rest = {k: v for k, v in payload.items() if k not in rest_keys}
    if isinstance(payload.get("evidence"), dict):
        rest.update({k: v for k, v in payload["evidence"].items() if k not in rest_keys})
    if rest:
        st.json(rest, expanded=False)


@st.dialog("🔍 Signal Audit", width="large")
def _audit_dialog(defn: dict, sig, company: Company, db: Session):
    """Full traceability for one signal value: real source, fetch time,
    live/simulated status (with the TRUE reason when simulated), whatever
    evidence its producer recorded, and the matching raw record if one
    exists — the "double-click any datapoint" drill-down."""
    from config import has_credentials, SOURCE_CREDENTIAL_VARS, SOURCE_PAID_ENABLE_FLAGS

    st.markdown(f"#### {defn['label']}")
    if defn.get("proxy"):
        st.caption(defn["proxy"])

    if sig is None or sig.status == "not_yet_checked":
        st.info("⚪ Not yet checked — no data has been collected for this signal yet.")
        return

    value_str = _format_indicator_value(defn["key"], sig.numeric_value, defn) \
        if sig.numeric_value is not None else (sig.text_value or "—")
    col1, col2, col3 = st.columns(3)
    col1.metric("Value", value_str)
    col2.metric("Mode", mode_badge_for(sig))
    col3.metric("Confidence", f"{sig.confidence:.0%}" if sig.confidence is not None else "—")

    st.markdown(f"**Real source:** {sig.source or '—'}")
    st.markdown(f"**Fetched:** {sig.fetched_at.strftime('%Y-%m-%d %H:%M') if sig.fetched_at else '—'}")

    if sig.source in SOURCE_CREDENTIAL_VARS or sig.source in SOURCE_PAID_ENABLE_FLAGS:
        configured = has_credentials(sig.source)
        st.markdown(f"**Credentials configured for {sig.source}:** {'✅ Yes' if configured else '❌ No'}")
        if sig.is_simulated and configured:
            st.caption(
                "⚠️ Credentials ARE configured in this process, but the value is still simulated — "
                "the live call itself failed (see the reason below), not a missing-credentials case."
            )

    payload = {}
    if sig.raw_payload_ref:
        try:
            payload = json.loads(sig.raw_payload_ref)
        except (ValueError, TypeError):
            payload = {}

    st.markdown("---")
    st.markdown("##### How it was derived")
    _render_evidence_block(payload)

    st.markdown("---")
    st.markdown("##### Matching raw data")
    raw_recs = _raw_records_for_source(db, company, sig.source)
    if not raw_recs:
        st.caption(
            "No raw record is linked to this source — likely a direct API call with no separate "
            "capture step. Check the 🗂️ Raw Data & Mapping tab for everything actually collected "
            "for this company."
        )
    else:
        for rec in raw_recs:
            st.markdown(
                f"**{rec.dataset_name}** — {rec.source_filename or 'filename not recorded'} · "
                f"updated {rec.updated_at.strftime('%Y-%m-%d %H:%M') if rec.updated_at else '—'}"
            )
            st.json(rec.raw_row, expanded=False)


def _render_signal_rows_with_audit(keys: list, defs: dict, sig_dict: dict, db: Session,
                                    company: Company, build_row_fn, key_prefix: str = "rows"):
    """
    One row per signal with a trailing 🔍 Audit button that opens
    _audit_dialog. st.dataframe can't host a real button per cell, and this
    app has already been burned by st.dataframe row-selection state getting
    wiped across a sort (see the crawl-queue/matrix selection work) — so this
    uses a plain per-row st.columns layout instead of a selection-based
    table. Less dense than a dataframe by design: the columns dropped here
    (Redundancy Group, Modifier, Caveat, Freshness Window) are shown in the
    dialog instead, where there's room to present them properly.
    """
    widths = [2.4, 1.0, 1.3, 1.8, 1.2, 0.5]
    header_cols = st.columns(widths)
    for h, label in zip(header_cols, ["Signal", "Value", "Status", "Mode · Source", "Last Fetched", ""]):
        h.markdown(f"**{label}**")
    for key in keys:
        defn = defs[key]
        row = build_row_fn(key, defn)
        c1, c2, c3, c4, c5, c6 = st.columns(widths)
        c1.write(row["Signal Name"])
        c2.write(row["Value"])
        c3.write(row["Status"])
        c4.write(f"{row['Mode']} · {row['Source']}")
        c5.write(row["Last Fetched"])
        if c6.button("🔍", key=f"audit_btn_{key_prefix}_{key}", help="Audit this signal — full traceability"):
            _audit_dialog(defn, sig_dict.get(key), company, db)


def _render_signal_evidence(sig_dict: dict, indicator_defs: dict):
    """
    Item-by-item provenance for every signal that carries it: the specific roles,
    partners, directories or review figures behind the number, each with a link to
    check it against. A counter on its own ("1 digital role") isn't verifiable;
    this is the difference between reading a score and auditing it.
    """
    with_evidence = []
    for key, sig in sig_dict.items():
        defn = indicator_defs.get(key)
        if not defn or not sig or sig.status == "not_yet_checked" or not sig.raw_payload_ref:
            continue
        try:
            payload = json.loads(sig.raw_payload_ref)
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict) and payload.get("evidence"):
            with_evidence.append((key, defn, sig, payload))

    if not with_evidence:
        return

    st.markdown("---")
    st.subheader("🔍 Evidence — what exactly was counted")
    st.caption(
        f"{len(with_evidence)} signal(s) carry item-level provenance. Everything below was retrieved from the "
        "named source; follow the links to verify any number yourself. Use the 🔍 Audit button next to any "
        "signal above for the full traceability view, including the matching raw record."
    )

    for key, defn, sig, payload in sorted(with_evidence, key=lambda x: x[1]["label"]):
        value_str = _format_indicator_value(key, sig.numeric_value, defn)
        with st.expander(f"**{defn['label']}** — {value_str}  ·  {sig.text_value or ''}"):
            st.caption(f"Source: {sig.source} · fetched {sig.fetched_at:%Y-%m-%d %H:%M} · "
                        f"{mode_badge_for(sig)}")
            _render_evidence_block(payload)


def _format_indicator_value(key: str, val, defn: dict = None) -> str:
    """Formats an indicator/field value with appropriate units, adding 'k' for monetary financials in thousands."""
    if val is None or val == "" or val == "—":
        return "—"
    if not isinstance(val, (int, float)):
        return str(val)
    k = key.lower()
    if k in INDICATOR_UNITS:
        suffix, decimals = INDICATOR_UNITS[k]
        return f"{val:.{decimals}f}{suffix}" if k == "last_accounts_year" else f"{val:,.{decimals}f}{suffix}"
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
            groups = _trend_groups_from_raw(rec.raw_row, rec.mapping_snapshot)
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
        scores = calculate_company_scores(signals, indicator_defs, include_detail=True)
        score_exclusions = {e["key"]: e["score_exclusion_reason"]
                            for e in scores["need_detail"] + scores["readiness_detail"]
                            if e.get("score_exclusion_reason")}

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
                if sig_key in score_exclusions:
                    disp_status = "not_yet_checked"
                # text_value now doubles as the provenance summary written by the
                # Phase 7 crawlers, so prefer a real number when there is one — only
                # fall back to text for the context rows that are genuinely textual
                # (manual entries like product_type_tag, which carry no number).
                if sig_rec.numeric_value is not None:
                    val = sig_rec.numeric_value
                elif defn["axis"] == "context" and sig_rec.text_value:
                    val = sig_rec.text_value
                else:
                    val = None
                fetched_str = sig_rec.fetched_at.strftime("%Y-%m-%d %H:%M") if sig_rec.fetched_at else "N/A"
                raw_ref = sig_rec.text_value or ""
                if sig_key in score_exclusions:
                    raw_ref = f"Not scored: {score_exclusions[sig_key]}. " + raw_ref
                mode_badge = mode_badge_for(sig_rec)
            else:
                disp_status, val, fetched_str, raw_ref, mode_badge = "not_yet_checked", None, "Never", "", "—"

            fresh_window = defn.get("freshness_days") or 90
            modifier = defn.get("axis_modifier") or ""
            caveat_note = f"⚠️ {defn.get('comment')}" if modifier == "CAVEAT" and defn.get("comment") else "—"
            # The REAL source of this datapoint, not the catalog's generic
            # pipeline tag — sig_rec.source is what actually produced the
            # value (e.g. "AIDA Raw Exports", "Board Roster Analysis"); many
            # indicators still carry a source_system tag written when this
            # catalog was designed around German data (Handelsregister/
            # Bundesanzeiger), which is only ever a fallback label for a
            # not-yet-checked/country-N/A row, never a claim about where a
            # real value came from.
            real_source = sig_rec.source if (sig_rec and has_real_data) else source_sys
            return {
                "Category": defn["category"],
                "Signal Name": defn["label"],
                "Weight": "—" if defn["axis"] == "context" else f"{defn.get('weight', 0):.1f}",
                "Redundancy Group": defn.get("redundancy_group") or "—",
                "Source": real_source,
                "Tier / Phase": f"{defn.get('automation_tier') or '—'} / Phase {defn.get('phase')}",
                "Status": STATUS_BADGES.get(disp_status, disp_status),
                "Mode": mode_badge,
                "Value": _format_indicator_value(sig_key, val, defn),
                "Modifier": modifier or "—",
                "Caveat": caveat_note,
                "Freshness Window": f"{fresh_window} days",
                "Last Fetched": fetched_str,
                "What was counted": raw_ref,
            }

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

        col_why1, col_why2 = st.columns(2)
        with col_why1:
            _render_score_breakdown("Need", scores["need_score"], scores["need_detail"], scores["need_meta"])
        with col_why2:
            _render_score_breakdown("Readiness", scores["readiness_score"], scores["readiness_detail"], scores["readiness_meta"])

        st.markdown("---")

        # Pain points: what the company is struggling with, read from the same signals as the scores
        # (see painpoints.py) — a diagnosis alongside Need/Readiness, never part of either.
        from views.pain_points import render_company_pain_points
        render_company_pain_points(db, signals, indicator_defs)

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
            col_s1, col_s2, col_s3, col_s4 = st.columns([2, 1, 1, 1])
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
            with col_s4:
                st.markdown("&nbsp;")
                if st.button("🕸️ Run Deep Crawlers", use_container_width=True,
                             help="Phase 7 — 9 site/news crawlers plus TED public contracts and "
                                  "sector-relevant FDA recalls "
                                  "(company site, product catalog, jobs, reviews, news, directories, "
                                  "innovation participation, digital maturity). Takes a few "
                                  "minutes; runs in the background — follow it in the widget at the bottom right."):
                    from views.crawl_widget import queue_crawl
                    from views.crawler_setup import resolve_crawl_target
                    where = resolve_crawl_target(db)
                    if where["ok"]:
                        queue_crawl({company.id: company.legal_name}, workers=1, target=where["target"])
                        st.rerun()
                    else:
                        st.error(where["problem"])

        from company_service import eligible_phase7_sources
        eligible = eligible_phase7_sources(company)
        run_rows = {r.source_name: r for r in db.query(CompanySourceRun).filter_by(
            company_id=company.id, phase=7).all()}
        with st.expander(f"🕸️ Deep crawler coverage ({sum(run_rows.get(s) is not None and run_rows[s].status == 'live' for s in eligible)}/{len(eligible)} sources completed)"):
            st.caption("A completed crawl can legitimately find no scored signal. Failed and unavailable sources stay eligible for retry.")
            st.dataframe(pd.DataFrame([{
                "Source": source,
                "Status": run_rows[source].status if source in run_rows else "not run",
                "Last run": run_rows[source].finished_at.strftime("%Y-%m-%d %H:%M") if source in run_rows else "—",
                "Error": run_rows[source].error_message or "—" if source in run_rows else "—",
            } for source in sorted(eligible)]), use_container_width=True, hide_index=True)

        catalog_record = db.query(RawImportRecord).filter_by(
            company_id=company.id, dataset_name="crawler_product_catalog").first()
        if catalog_record and catalog_record.raw_row:
            catalog = catalog_record.raw_row
            if catalog.get("products_count"):
                st.subheader("📦 Product intelligence")
                st.write(catalog.get("catalog_narrative") or
                         "Catalog categories found: " + ", ".join(catalog.get("categories") or []))
                coverage = "Partial site sample" if catalog.get("is_partial") else "Within the crawler's page limit"
                st.caption(f"{catalog['products_count']} products found across "
                           f"{catalog.get('crawled_pages_count') or 0} crawled pages · {coverage}. "
                           "This describes the public catalog; it is not a scored company fact.")
                examples = [(p.get("product_name"), p.get("source_url")) for p in catalog.get("products") or []]
                examples = [(name, url) for name, url in examples if name and url][:5]
                if examples:
                    st.write("Examples: " + " · ".join(f"[{name}]({url})" for name, url in examples))

        award_record = db.query(RawImportRecord).filter_by(
            company_id=company.id, dataset_name="crawler_ted_contract_awards").first()
        if award_record and award_record.raw_row:
            awards = (award_record.raw_row or {}).get("awards") or []
            st.subheader("🏛️ Public contract evidence")
            if awards:
                st.caption(f"{len(awards)} TED notice(s) naming this company as a winning supplier. "
                           "A notice can have multiple winners; contract values are not attributed to this company.")
                for award in awards[:10]:
                    title = award.get("title") or award.get("publication_number") or "Award notice"
                    st.markdown(f"- [{title}]({award['url']}) — buyer: {award.get('buyer') or 'not stated'}; "
                                f"published {str(award.get('published') or 'date unknown')[:10]}")
            else:
                st.caption("No matching TED award was found in this bounded search. "
                           "This says nothing about private or nationally published contracts.")

        recall_record = db.query(RawImportRecord).filter_by(
            company_id=company.id, dataset_name="crawler_fda_recalls").first()
        if recall_record and recall_record.raw_row:
            recalls = (recall_record.raw_row or {}).get("recalls") or []
            if recalls:
                st.subheader("⚠️ Published US product recalls")
                st.caption("FDA enforcement reports matching this firm's name. Check the linked record for identity, scope, "
                           "date and current status; this is not an EU compliance finding.")
                for recall in recalls[:8]:
                    st.markdown(f"- [{recall['recall_number']}]({recall['url']}) — "
                                f"{recall.get('classification') or 'unclassified'}, "
                                f"{str(recall.get('report_date') or 'date unknown')[:8]}: "
                                f"{recall.get('reason') or recall.get('product') or 'see record'}")

        patent_record = db.query(RawImportRecord).filter_by(
            company_id=company.id, dataset_name="crawler_epo_ops_search").first()
        if patent_record and patent_record.raw_row:
            patent_payload = patent_record.raw_row
            sample = patent_payload.get("publication_sample") or []
            if sample:
                st.subheader("🔬 Patent activity snapshot")
                st.caption(f"{patent_payload.get('total_result_count', 0)} matching publication records. "
                           "Titles below come from the first search page; multiple publications may belong "
                           "to one patent family. This is not a count of distinct inventions.")
                for patent in sample[:8]:
                    title = patent.get("title") or "Untitled patent publication"
                    coapplicants = ", ".join(patent.get("applicants") or [])
                    st.markdown(f"- **{title}** — {patent.get('publication') or 'publication unknown'} "
                                f"({str(patent.get('publication_date') or 'date unknown')[:8]})"
                                + (f" · applicants: {coapplicants}" if coapplicants else ""))

        from company_brief import collect_evidence, evidence_fingerprint, generate_brief
        from config import CRAWLER_LLM_API_KEY
        brief_evidence = collect_evidence(db, company)
        if brief_evidence:
            with st.expander("✨ AI evidence brief", expanded=False):
                st.caption("On-demand interpretation of cited records. It does not change Need or Readiness scores.")
                brief_record = db.query(RawImportRecord).filter_by(
                    company_id=company.id, dataset_name="crawler_ai_company_brief").first()
                brief = brief_record.raw_row if brief_record else None
                if CRAWLER_LLM_API_KEY:
                    if st.button("Generate / refresh brief", key=f"ai_brief_{company.id}"):
                        try:
                            with st.spinner("Reading the collected evidence..."):
                                brief = generate_brief(db, company)
                        except Exception as exc:
                            st.error(f"Brief could not be generated: {exc}")
                else:
                    st.caption("Configure CRAWLER_LLM_API_KEY to generate a brief from these records.")
                if brief:
                    if brief.get("evidence_fingerprint") != evidence_fingerprint(brief_evidence):
                        st.warning("New source evidence is available. Refresh this brief before using it.")
                    by_id = {item["id"]: item for item in brief.get("evidence") or []}
                    for finding in brief.get("findings") or []:
                        refs = []
                        for evidence_id in finding.get("evidence_ids") or []:
                            item = by_id.get(evidence_id)
                            if item:
                                refs.append(f"[{evidence_id}]({item['url']})" if item.get("url") else evidence_id)
                        if refs:
                            st.markdown(f"- {finding.get('text', '')}  **Sources:** {', '.join(refs)}")
                    st.caption("Check the source records before using any claim in outreach. "
                               "Patent samples and source coverage are bounded.")

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
            st.caption("Every Financial Health and Cost Structure indicator in the catalog for this company, checked or not. **Value** is the raw figure as computed/injected (monetary financials in thousands, **k**) — not a 0-100 score (see Score Breakdown below for that). 🔍 opens the full audit trail, including the real source and the matching raw record.")
            from indicators import CAT_FINANCIAL, CAT_COST
            fin_keys = sorted(
                [k for k, d in indicator_defs.items() if d["category"] in (CAT_FINANCIAL, CAT_COST)],
                key=lambda k: indicator_defs[k]["label"],
            )
            _render_signal_rows_with_audit(fin_keys, indicator_defs, sig_dict, db, company, _build_row, key_prefix="finprofile")

        # Indicative valuation — a separate lens, never blended into Need/Readiness.
        with st.expander("💶 Indicative valuation (multiples + DCF)", expanded=False):
            from views.valuation import render_company_valuation
            render_company_valuation(db, company.id, key="cd_val")

        # Data Trends — every multi-year column group from the raw injected
        # data, financial or not, charted regardless of whether it was ever
        # mapped to a scored indicator.
        if raw_records_for_trends:
            with st.expander("📈 Data Trends (from Raw Injected Data)", expanded=False):
                st.caption("Every multi-year figure exactly as uploaded — financial and non-financial, whether or not it was mapped to a scored indicator (financial values in thousands, **k**).")
                for rec in raw_records_for_trends:
                    trend_groups = _trend_groups_from_raw(rec.raw_row, rec.mapping_snapshot)
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

        # Data Provenance + Raw Injected Data moved to the dedicated
        # 🗂️ Raw Data & Mapping tab (render_company_detail_page) — that's
        # also where a raw column's indicator mapping can be corrected.
        st.caption(
            "📜 Which sources populated this company's live data, and 📦 every raw dataset collected for it "
            "(files + all crawlers), are in the **🗂️ Raw Data & Mapping** tab — including a mapping editor "
            "to reassign a raw column to a different indicator."
        )

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

        # Family & Succession — derived from the same CompanyPerson roster
        # (only rows with a known age, so shareholder/subsidiary entities
        # exploded from the ownership imports are naturally excluded) plus
        # Company.legal_name/incorporation_date. See company_service.
        # detect_family_and_succession for the exact rule.
        has_aged_people = db.query(CompanyPerson).filter_by(company_id=company.id).filter(CompanyPerson.age.isnot(None)).first() is not None
        if has_aged_people:
            from company_service import sync_succession_signal
            with st.expander("🧬 Family & Succession"):
                succession = sync_succession_signal(db, company)
                if succession["is_family_company"]:
                    st.info(f"🏠 **Family company** — shared surname(s) in management: {', '.join(succession['family_surnames'])}")
                else:
                    st.caption("No shared management surname detected — not flagged as a family company.")

                ng = succession["new_generation"]
                if ng["detected"]:
                    yr = ng["years_since_handover"]
                    st.success(
                        f"🌱 **New Generation Management detected** — surname **{ng['surname']}** appears in the company "
                        f"name; {ng['young_manager'].full_name} (age {ng['young_manager'].age}) is running it with no "
                        f"older **{ng['surname']}** still in management, and the company's incorporation date predates "
                        f"them — a generational handover, not a founding."
                    )
                    if yr is not None:
                        st.metric("Years since handover (estimated)", f"{yr:.1f}")
                        st.caption(
                            "✅ Scored: the **New Generation of Management** indicator has been updated with this value."
                            if succession["signal_written"] else
                            "Detected, but the indicator wasn't written — check the app logs."
                        )
                    else:
                        st.caption(
                            "⚠️ Detected, but the young manager's appointment date isn't in the data, so the "
                            "**New Generation of Management** indicator can't be scored with a real number yet — "
                            "add a 'Data nomina' value for this person (re-import the board roster) to activate it."
                        )
                if st.button("🔄 Recompute Family & Succession", key="recompute_succession_btn"):
                    sync_succession_signal(db, company)
                    st.rerun()

        # Management Composition — the rest of the Leadership & Succession
        # indicators (age/gender/nationality mix, turnover, tenure,
        # non-family headcount) computed from the same CompanyPerson roster.
        # See company_service.sync_management_composition_signals for why
        # Management Cultural Diversity / Level & Diversity of Education
        # aren't included here (no roster column captures either yet).
        if has_aged_people:
            from company_service import sync_management_composition_signals
            with st.expander("🧑‍💼 Management Composition Indicators"):
                st.caption(
                    "Computed directly from the imported board/management roster above — nothing "
                    "manually entered. Re-run after re-importing an updated roster; a dash means the "
                    "roster doesn't carry that field for anyone yet."
                )
                composition = sync_management_composition_signals(db, company)
                metric_specs = [
                    ("management_age", "Management Age (avg, yrs)", "{:.1f}"),
                    ("mgmt_gender_diversity", "Gender Diversity (% women)", "{:.0f}%"),
                    ("mgmt_national_diversity", "National Diversity (%)", "{:.0f}%"),
                    ("management_turnover", "Turnover (changes, last 3y)", "{:.0f}"),
                    ("senior_mgmt_tenure", "Avg. Tenure (yrs)", "{:.1f}"),
                    ("independent_board_members", "Independent (Non-Family)", "{:.0f}"),
                ]
                metric_cols = st.columns(3)
                for i, (key, label, fmt) in enumerate(metric_specs):
                    entry = composition.get(key, {})
                    with metric_cols[i % 3]:
                        st.metric(label, fmt.format(entry["value"]) if entry.get("written") else "—")
                st.caption(
                    "Not computed (no data source): Management Cultural Diversity, Level of Education, "
                    "Diversity of Education — no roster column captures education or cultural background."
                )
                if st.button("🔄 Recompute Management Composition", key="recompute_mgmt_composition_btn"):
                    sync_management_composition_signals(db, company)
                    st.rerun()

        # Pilot History
        pilot_rows = _pilot_rows(db, company)
        if pilot_rows:
            with st.expander(f"🎯 Pilot History ({len(pilot_rows)})"):
                st.dataframe(pd.DataFrame(pilot_rows), use_container_width=True, hide_index=True)

        _render_manual_entry_form(db, company, indicator_defs)

        st.markdown("---")

        # Scored signals, grouped by category
        st.subheader("📊 Scored Signal Audit & Freshness Breakdown")
        st.caption(f"Every indicator feeding the Need/Readiness score for {company.legal_name} ({company.country}).")

        scored_defs = {k: d for k, d in indicator_defs.items() if d["axis"] != "context"}
        categories = sorted(set(d["category"] for d in scored_defs.values()))
        for cat in categories:
            cat_keys = sorted(
                [k for k, d in scored_defs.items() if d["category"] == cat],
                key=lambda k: scored_defs[k]["label"],
            )
            cat_checked = sum(1 for k in cat_keys if sig_dict.get(k) and sig_dict[k].status != "not_yet_checked")
            with st.expander(f"{cat} ({cat_checked}/{len(cat_keys)} checked)", expanded=(cat_checked > 0)):
                _render_signal_rows_with_audit(cat_keys, scored_defs, sig_dict, db, company, _build_row, key_prefix="catloop")

        _render_signal_evidence(sig_dict, indicator_defs)

        # Context tags
        context_defs = {k: d for k, d in indicator_defs.items() if d["axis"] == "context"}
        if context_defs:
            st.markdown("---")
            st.subheader("🏷️ Context & Moderator Tags")
            st.caption("Informational tags — not part of weighted scoring sum.")
            ctx_keys = sorted(context_defs.keys(), key=lambda k: context_defs[k]["label"])
            _render_signal_rows_with_audit(ctx_keys, context_defs, sig_dict, db, company, _build_row, key_prefix="ctxtags")


def _render_raw_data_tab(db: Session):
    """
    Everything raw collected for one company — every uploaded-file dataset
    (AIDA, flexible imports) AND every Phase 7 crawler capture, both already
    unified in RawImportRecord — plus which sources actually produced live
    data. For spreadsheet-shaped datasets (not crawler captures, which are
    already interpreted into structured evidence rather than free-form
    columns), a column can be reassigned to a different indicator here:
    that fixes THIS company's data immediately (via
    company_service.reapply_mapping_to_raw_record) and saves the mapping
    for future imports of the same dataset shape (via save_mapping_profile)
    — the "train the model for the future" ask.
    """
    st.subheader("🗂️ Raw Data & Mapping")
    st.caption(
        "Every raw record collected for one company, exactly as captured — independent of whether "
        "it's been mapped to a scored indicator. Use this to audit what was actually collected, and "
        "to correct/teach a column's mapping without re-uploading anything."
    )

    companies = db.query(Company).order_by(Company.legal_name).all()
    if not companies:
        st.info("No companies yet — add one from the ➕ Add Single Company tab.")
        return

    country_flags = {"Germany": "🇩🇪", "Italy": "🇮🇹"}
    company_names = {
        f"{c.legal_name} ({c.registration_number}) — {country_flags.get(c.country, '🌐')} {c.country} [{c.segment}]": c.id
        for c in companies
    }
    name_list = list(company_names.keys())
    # Defaults to whichever company is selected on the Company Intelligence
    # tab, when that selection is still valid here (different tab, so it
    # needs its own selectbox/key — Streamlit doesn't let two widgets share
    # one key in the same run — but there's no reason to make the user
    # re-pick the company they were just looking at).
    default_name = st.session_state.get("detail_company_select")
    default_idx = name_list.index(default_name) if default_name in name_list else 0
    selected_name = st.selectbox("Select Target Company", name_list, index=default_idx, key="raw_data_company_select")
    company = db.query(Company).filter_by(id=company_names[selected_name]).first()

    st.markdown("---")
    st.markdown("##### 📜 Which sources actually populated live data")
    prov_rows = _provenance_rows(db, company)
    if prov_rows:
        st.dataframe(pd.DataFrame(prov_rows), use_container_width=True, hide_index=True)
    else:
        st.caption("No live (non-simulated) signals recorded yet for this company.")

    raw_records = db.query(RawImportRecord).filter_by(company_id=company.id).order_by(RawImportRecord.dataset_name).all()
    if not raw_records:
        st.info(f"No raw datasets captured yet for {company.legal_name}.")
        return

    st.markdown("---")
    st.markdown(f"##### 📦 Raw datasets ({len(raw_records)})")

    from company_service import (
        suggest_column_mapping, valid_targets_for_column,
        save_mapping_profile, reapply_mapping_to_raw_record,
    )
    indicator_defs = fetch_indicator_defs(db)

    for rec in raw_records:
        is_crawler = rec.dataset_name.startswith("crawler_")
        is_pipeline_managed = rec.dataset_name == SRC_AIDA
        updated_str = rec.updated_at.strftime("%Y-%m-%d %H:%M") if rec.updated_at else "—"
        header = f"{rec.dataset_name} — {rec.source_filename or 'filename not recorded'} · updated {updated_str}"
        with st.expander(header, expanded=False):
            if is_crawler:
                st.caption(
                    "Crawler capture — already interpreted into per-signal evidence directly (see the 🔍 "
                    "Audit button next to the relevant signal in the Company Intelligence tab), not "
                    "free-form spreadsheet columns, so there's no column-mapping control here."
                )
                st.json(rec.raw_row, expanded=False)
                continue

            st.caption(
                "Every column exactly as uploaded, not just the ones mapped to an indicator. Reassign a "
                "column below to fix this company's data now and teach future imports of this shape."
            )
            st.json(rec.raw_row, expanded=False)

            if is_pipeline_managed:
                st.info(
                    "AIDA indicators such as Revenue Trend are calculated from the individual facts below. "
                    "Changes here apply to this company and are retained when the AIDA files are imported again. "
                    "Open an indicator's 🔍 Audit view for its original file headers, raw values, and formula."
                )

            source_columns = list((rec.raw_row or {}).keys())
            if not source_columns:
                st.caption("No mappable columns detected in this raw row.")
                continue

            current_mapping = dict(rec.mapping_snapshot or {})
            suggested_mapping = suggest_column_mapping(db, source_columns, existing_profile=current_mapping)
            suggested_sig = _mapping_signature(suggested_mapping)
            new_mapping = {}
            st.markdown("###### Column mapping — one source column to one fact")
            options = valid_targets_for_column(db)
            option_keys = list(options.keys())
            option_labels = dict(options)
            for source_column in source_columns:
                current = suggested_mapping.get(source_column) or ""
                if current not in option_keys:
                    current = ""
                row_c1, row_c2 = st.columns([2, 3])
                with row_c1:
                    st.markdown(f"**{source_column}**")
                    st.caption(f"Raw value: {(rec.raw_row or {}).get(source_column)!r}")
                with row_c2:
                    picked = st.selectbox(
                        f"Map '{source_column}' to", options=option_keys,
                        format_func=lambda k: option_labels.get(k, k),
                        index=option_keys.index(current),
                        key=f"raw_map_{rec.id}_{suggested_sig}_{source_column}", label_visibility="collapsed",
                    )
                    new_mapping[source_column] = picked

            if st.button("💾 Apply mapping", key=f"raw_apply_{rec.id}", use_container_width=True):
                result = reapply_mapping_to_raw_record(db, rec, new_mapping, indicator_defs)
                if not is_pipeline_managed:
                    save_mapping_profile(db, rec.dataset_name, company.country or "Germany", new_mapping)
                if result["skipped"]:
                    st.warning(f"Skipped: {'; '.join(f'{b} ({r})' for b, r in result['skipped'])}")
                st.success(
                    f"Updated {result['updated']} signal(s) for {company.legal_name}. "
                    + ("This company's AIDA mapping will be retained on re-import."
                       if is_pipeline_managed else f"Saved the mapping for future '{rec.dataset_name}' imports.")
                )
                st.rerun()


def render_company_detail_page(db: Session):
    st.title("🏢 Company Intelligence & Management")
    st.caption("Deep-dive company breakdown, tri-state signal audits, single company creation, and bulk CSV ingestion.")

    tab_detail, tab_raw, tab_add, tab_import, tab_flex, tab_people, tab_flat_people, tab_manage = st.tabs([
        "🏢 Company Intelligence & Audit",
        "🗂️ Raw Data & Mapping",
        "➕ Add Single Company",
        "📁 Bulk CSV Import",
        "🔗 Flexible Data Import",
        "👥 Import People & Ownership",
        "🧑‍💼 Flexible People Import",
        "🗑️ Manage Companies"
    ])

    # --------------------------------------------------------------------------
    # TAB 1: Company Intelligence & Deep Dive
    # --------------------------------------------------------------------------
    with tab_detail:
        _render_tab1_content(db)

    # --------------------------------------------------------------------------
    # TAB 1b: Raw Data & Mapping
    # --------------------------------------------------------------------------
    with tab_raw:
        _render_raw_data_tab(db)

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
                    bar, status, progress_cb = _progress_reporter("company")
                    result = import_companies_from_csv(db, uploaded_file, auto_sync=auto_sync_csv, progress_callback=progress_cb)
                    bar.empty()
                    status.empty()

                    st.success(f"✅ **Import complete — {len(preview_df)} row(s) processed.** **{result['created']}** companies created, **{result['skipped']}** skipped.")
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
    # TAB 6: Flexible (one-row-per-person) People Import
    # --------------------------------------------------------------------------
    with tab_flat_people:
        _render_flexible_people_import_tab(db)

    # --------------------------------------------------------------------------
    # TAB 7: Manage / Delete Companies
    # --------------------------------------------------------------------------
    with tab_manage:
        _render_manage_companies_tab(db)
