"""
Data Admin: everything that adds, imports or removes companies and rosters - kept apart from the Company
Intelligence dossier so that page is only ever about reading one company.

Moved verbatim from views/company_detail.py's old tab strip: Add Single Company, Bulk CSV Import, Flexible Data
Import, Import People & Ownership, Flexible People Import, Manage Companies.
"""

import json
import hashlib

import pandas as pd
import streamlit as st
from datetime import datetime
from sqlalchemy import func
from sqlalchemy.orm import Session

from models import Company, SignalRecord, CompanyPerson, RawImportRecord, PilotOutcome, stage_label
from indicators import fetch_indicator_defs


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

    uploaded = st.file_uploader("Upload Dataset (.csv, .xlsx or a PDF of financial statements)", type=["csv", "xlsx", "xls", "pdf"],
                                key="flex_uploader")

    if uploaded is None:
        st.session_state.pop("flex_df", None)
        return
    if uploaded.name.lower().endswith(".pdf"):
        from views.balance_sheet_upload import render_upload as render_document_import
        st.info("For a PDF the country and dataset name above are not used: the company is matched by the fiscal code printed in the "
                "statement (Italy), every statement is stored under its own date, and the figures join the company's merged annual "
                "history, which Company Intelligence reads.")
        render_document_import(db, uploaded)
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
            "Stage": stage_label(c.shortlist_status),
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
                      "Stage", "Need Score", "Readiness Score",
                      "Present Signals", "Pilot Outcomes"],
        disabled=["Legal Name", "Registration #", "Country", "Segment", "Stage",
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

    from views.stage_controls import render_stage_bulk
    render_stage_bulk(db, {f"{c.legal_name} ({c.registration_number})": c.id for c in companies}, key="admin_stage")

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


def _render_add_company_tab(db: Session):
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


def _render_bulk_csv_tab(db: Session):
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

def render_data_admin_page(db: Session):
    st.title("🛠️ Data Admin")
    st.caption("Add, import and remove companies and people. Reading a company happens in 🏢 Company Intelligence.")

    tab_add, tab_import, tab_flex, tab_aida, tab_people, tab_flat_people, tab_manage = st.tabs([
        "➕ Add Single Company",
        "📁 Bulk CSV Import",
        "🔗 Flexible Data Import",
        "AIDA single company report upload",
        "👥 Import People & Ownership",
        "🧑‍💼 Flexible People Import",
        "🗑️ Manage Companies",
    ])
    with tab_add:
        _render_add_company_tab(db)
    with tab_import:
        _render_bulk_csv_tab(db)
    with tab_flex:
        _render_flexible_import_tab(db)
    with tab_aida:
        from views.aida_single_report import render_upload
        render_upload(db)
    with tab_people:
        _render_people_import_tab(db)
    with tab_flat_people:
        _render_flexible_people_import_tab(db)
    with tab_manage:
        _render_manage_companies_tab(db)
