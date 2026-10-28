"""Preview and read a single-company AIDA report as a report."""
import hashlib
import json

import pandas as pd
import streamlit as st

from aida_single_report import DATASET_NAME, import_report, parse_report, plan_import


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
        plan = plan_import(db, report)
    except Exception as exc:
        st.error(f"Could not read this AIDA report: {exc}")
        return
    render_report(report, download_key=f"aida_preview_download_{signature}")
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
            result = import_report(db, report, replace_report=replace, replace_other_signals=replace_signals)
        except Exception as exc:
            st.error(f"Import failed; no report changes were saved: {exc}")
            return
        st.success(f"Imported {report['company']['legal_name']}: {result['people']} people, "
                   f"{result['signals_updated']} indicators updated. "
                   "The complete report is available in Company Intelligence → Data.")
        if result["signals_skipped"]:
            st.info(f"Kept existing values for {len(result['signals_skipped'])} indicators from other sources.")
