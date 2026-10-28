"""Preview and read a single-company AIDA report as a report."""
import hashlib
import json

import pandas as pd
import streamlit as st

from aida_single_report import DATASET_NAME, import_report, parse_report, plan_import
from aida_report_mapping import (
    DEFAULT_PROFILE, PROFILE_PREFIX, NEW_INDICATOR, load_report_profile,
    report_sources, report_targets, suggest_report_mapping,
)


def render_financial_tables(report):
    for table in report["financial_tables"]:
        with st.expander(table["section"], expanded=False):
            st.caption(f"{table['basis']}. Values and unavailable markers as reported. Monetary amounts: "
                       + ", ".join(sorted(set(str(u) for u in table["units"].values()))))
            rows = [{"Line": row["label"], **{d: str(row["values"][d]["value"]) if row["values"][d]["value"] is not None else ""
                                                      for d in table["dates"]}}
                    for row in table["rows"]]
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")


def render_report(report, download_key="aida_report_download"):
    st.markdown(f"**{report['company']['legal_name']}** · {report['company']['registration_number']}")
    a, b, c = st.columns(3)
    a.metric("Source cells read", report["cell_count"])
    b.metric("Reporting dates", len(report["dates"]))
    c.metric("People", len(report["people"]))
    for warning in report["warnings"]:
        st.warning(warning)
    with st.expander("Company details", expanded=True):
        st.dataframe(pd.DataFrame([{"Field": k.replace("_", " ").title(), "Value": str(v)}
                                   for k, v in report["company"].items() if v is not None]),
                     hide_index=True, width="stretch")
    render_financial_tables(report)
    if report["people"]:
        with st.expander("People and appointments", expanded=False):
            st.dataframe(pd.DataFrame([{"Name": p["full_name"], "Role": p["role"], "AIDA person ID": p.get("person_id", "")}
                                       for p in report["people"]]), hide_index=True, width="stretch")
    # All source sections remain browsable, including address blocks, ownership,
    # unavailable modules and unrecognised text. No mapping controls for metadata.
    with st.expander("Full report text and source references", expanded=False):
        for section in report["sections"]:
            st.markdown(f"**{section['title']}** ({section['sheet']})")
            rows = [{"Reference": f"{section['sheet']}!{row['row']}",
                     "Report content": " | ".join(str(c["value"]) for c in row["cells"])} for row in section["rows"]]
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    st.download_button("Download complete parsed report (JSON)", json.dumps(report, ensure_ascii=False, indent=2),
                       file_name="aida_company_report.json", mime="application/json", key=download_key)


def render_mapping_editor(db, report, prefix, initial_mapping=None, initial_profile=None):
    """The same company/fact/indicator targets as Flexible Data Import."""
    from company_service import list_mapping_profiles
    st.markdown("##### Connect report fields")
    st.caption("Connect each source value to a company field, a financial fact, or a direct indicator. "
               "Calculated trends follow the connected financial facts. Ignored values stay in the full report. "
               "Direct indicators use the numeric value in the source unit.")
    profile_name = st.text_input("Mapping profile name", value=initial_profile or DEFAULT_PROFILE, key=f"{prefix}_profile")
    profiles = [p.dataset_name[len(PROFILE_PREFIX):] for p in list_mapping_profiles(db) if p.dataset_name.startswith(PROFILE_PREFIX)]
    if profiles:
        st.caption("Saved report profiles: " + ", ".join(profiles))
    if not profile_name.strip():
        st.warning("Enter a mapping profile name before saving.")
        return None, {}, None
    profile = load_report_profile(db, profile_name)
    seed = initial_mapping if initial_mapping is not None and profile_name == (initial_profile or DEFAULT_PROFILE) else profile
    sources = report_sources(report)
    signature = hashlib.sha256(json.dumps([report["sha256"], profile_name, seed], sort_keys=True).encode()).hexdigest()[:16]
    state_key = f"{prefix}_mapping_state"
    if st.session_state.get(f"{prefix}_mapping_context") != signature:
        st.session_state[f"{prefix}_mapping_context"] = signature
        st.session_state[state_key] = suggest_report_mapping(report, seed)
        st.session_state[f"{prefix}_new_labels"] = {}
    mapping = st.session_state[state_key]
    new_labels = st.session_state[f"{prefix}_new_labels"]
    target_labels = report_targets(db, report)
    sections = ["All sections"] + list(dict.fromkeys(s["section"] for s in sources.values()))
    section = st.selectbox("Report section", sections, key=f"{prefix}_section")
    periods = sorted({s["period"] for s in sources.values() if s["period"]},
                     key=lambda p: 0 if p == "latest" else int(p.split("-")[1]))
    selected_periods = st.multiselect("Reporting periods", periods, default=periods, key=f"{prefix}_periods",
                                     help="Every reporting date in the file is listed, counted downward from the latest year "
                                          "(Y-1, Y-2 ...). Trends and ratios use only the latest three.")
    search = st.text_input("Find a report field", key=f"{prefix}_search").strip().casefold()
    unmapped = st.checkbox("Show only unconnected fields", key=f"{prefix}_unmapped")
    filtered = [(sid, src) for sid, src in sources.items()
                if (section == "All sections" or src["section"] == section)
                and (not src["period"] or src["period"] in selected_periods)
                and (not search or search in f"{src['label']} {src['section']} {src['date']}".casefold())
                and (not unmapped or not mapping.get(sid))]
    connected = sum(bool(t) for t in mapping.values())
    st.caption(f"{len(sources)} source values · {connected} connected · {len(filtered)} match the current filters. "
               "Connections on other pages and periods are retained.")
    page_signature = hashlib.sha256(json.dumps([signature, section, selected_periods, search, unmapped]).encode()).hexdigest()[:12]
    pages = max(1, (len(filtered) + 19) // 20)
    page = st.selectbox("Mapping page", list(range(pages)), format_func=lambda p: f"{p + 1} of {pages}",
                        key=f"{prefix}_page_{page_signature}")
    for sid, src in filtered[page * 20:(page + 1) * 20]:
        token = hashlib.sha256(sid.encode()).hexdigest()[:16]
        left, right = st.columns([2, 3])
        with left:
            st.markdown(f"**{src['label']}**")
            st.caption(f"{src['date'] or src['section']} · {src['unit'] or ''} · {src['value']!r}")
            if src.get("cell"):
                st.caption(f"{src['sheet']}!{src['cell']}")
        with right:
            if src.get("locked"):
                st.caption("Fixed report identity: " + target_labels[src["default"]])
                continue
            choices = [t for t in target_labels if t not in {"company:registration_number", "company:legal_name"}
                       and (not t.startswith("variable:") or not src["period"] or t.endswith("_" + src["period"]))]
            choices.append(NEW_INDICATOR)
            current = mapping.get(sid) or ""
            if current not in choices:
                current = ""
            picked = st.selectbox(f"Connect {src['label']} ({src['date'] or src['section']})", choices,
                                  index=choices.index(current),
                                  format_func=lambda t: "+ Create New Indicator..." if t == NEW_INDICATOR else target_labels[t],
                                  key=f"{prefix}_map_{signature}_{token}")
            mapping[sid] = picked or None
            if picked == NEW_INDICATOR:
                new_labels[sid] = st.text_input("New indicator label", value=new_labels.get(sid, f"{src['label']} ({src['period'] or src['section']})"),
                                                key=f"{prefix}_new_{signature}_{token}")
    return dict(mapping), dict(new_labels), profile_name.strip()


def render_connection_preview(plan):
    for warning in plan["warnings"]:
        st.warning(warning)
    with st.expander("Connected indicators and calculations", expanded=False):
        st.dataframe(pd.DataFrame([{k: str(v) if v is not None else "" for k, v in row.items() if k != "sources"}
                                   for row in plan["connections"]]), hide_index=True, width="stretch")


def render_saved_mapping(db, record):
    report = record.raw_row["_aida_report"]
    initial_profile = record.raw_row.get("_aida_mapping_profile") or DEFAULT_PROFILE
    mapping, new_labels, profile_name = render_mapping_editor(db, report, f"aida_saved_map_{record.id}",
                                                             record.mapping_snapshot, initial_profile)
    if mapping is None:
        return
    try:
        plan = plan_import(db, report, mapping, new_labels)
    except ValueError as exc:
        st.error(str(exc))
        return
    render_connection_preview(plan)
    if st.button("Apply report mapping", key=f"aida_apply_mapping_{record.id}", width="stretch"):
        try:
            result = import_report(db, report, replace_report=True, mapping=mapping, new_labels=new_labels,
                                   profile_name=profile_name, remap_only=True)
        except Exception as exc:
            st.error(f"Could not apply report mapping: {exc}")
            return
        st.success(f"Updated {result['signals_updated']} indicators and saved profile '{profile_name}'.")
        if result["signals_skipped"]:
            st.info(f"Kept {len(result['signals_skipped'])} indicators supplied by other sources.")


def render_upload(db):
    st.subheader("AIDA single company report upload")
    st.caption("Upload the Excel report for one company. Review company details, financial statements by actual reporting date, "
               "people and ownership before importing. Every source section is retained.")
    uploaded = st.file_uploader("Upload AIDA company report (.xlsx)", type=["xlsx"], key="aida_single_upload")
    if uploaded is None:
        return
    signature = hashlib.sha256(uploaded.getvalue()).hexdigest()
    try:
        if st.session_state.get("aida_single_signature") != signature:
            with st.spinner("Reading the company report..."):
                report = parse_report(uploaded, uploaded.name)
            st.session_state["aida_single_report"] = report
            st.session_state["aida_single_signature"] = signature
        report = st.session_state["aida_single_report"]
    except Exception as exc:
        st.error(f"Could not read this AIDA report: {exc}")
        return
    render_report(report, download_key=f"aida_preview_download_{signature}")
    mapping, new_labels, profile_name = render_mapping_editor(db, report, "aida_upload")
    if mapping is None:
        return
    try:
        plan = plan_import(db, report, mapping, new_labels)
    except ValueError as exc:
        st.error(str(exc))
        return
    render_connection_preview(plan)
    st.info(f"{plan['action']}: {report['company']['legal_name']} ({report['company']['registration_number']}).")
    if plan["changes"]:
        with st.expander("Company fields that will be updated", expanded=False):
            st.dataframe(pd.DataFrame([{k: str(v) if v is not None else "" for k, v in row.items()}
                                       for row in plan["changes"]]), hide_index=True, width="stretch")
    replace = False
    if plan["existing_report"]:
        replace = st.checkbox("Replace the existing AIDA single company report and its imported people", key=f"aida_replace_{signature}")
    replace_signals = False
    if plan["conflicts"]:
        st.warning("Some calculated indicators already have data from another source. Review the comparison below.")
        st.dataframe(pd.DataFrame(plan["conflicts"]), hide_index=True, width="stretch")
        replace_signals = st.checkbox("Use this report for the listed indicators, replacing their existing values", key=f"aida_signals_{signature}")
    if st.button("Import AIDA single company report", key="aida_single_confirm",
                 disabled=plan["existing_report"] and not replace, width="stretch"):
        try:
            result = import_report(db, report, replace_report=replace, replace_other_signals=replace_signals,
                                   mapping=mapping, new_labels=new_labels, profile_name=profile_name)
        except Exception as exc:
            st.error(f"Import failed; no report changes were saved: {exc}")
            return
        st.success(f"Imported {report['company']['legal_name']}: {result['people']} people, "
                   f"{result['signals_updated']} indicators updated. "
                   "The complete report is available in Company Intelligence → Data.")
        if result["signals_skipped"]:
            st.info(f"Kept existing values for {len(result['signals_skipped'])} indicators from other sources.")
