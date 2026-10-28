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
from dataclasses import dataclass
from typing import Callable
import streamlit as st
import pandas as pd
from datetime import datetime
from sqlalchemy import func
from sqlalchemy.orm import Session
from models import Company, SignalRecord, PilotOutcome, RawImportRecord, CompanyPerson, CompanySourceRun, Competitor, SHORTLIST_STATUSES, is_shortlisted
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

from views.company_actions import SHORTLIST_STATUS_LABELS  # noqa: E402,F401  (kept importable from here)



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
    if not is_shortlisted(company.shortlist_status):
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
    "Site Evidence Reader": "crawler_site_evidence",
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


def _select_company_in_dossier(label: str) -> None:
    """Button callback: make the Company Intelligence picker show `label`, whatever country filter was set."""
    st.session_state["detail_country_filter"] = "All Countries"
    st.session_state["detail_company_select"] = label


def _render_crawled_intelligence(db: Session, company: Company) -> None:
    """Human-readable summary of what each Phase 7 crawler (plus Phase 4's Google News RSS)
    actually found for this company, grouped by theme. The raw blobs behind these are already
    queryable one at a time via the 🔍 Audit dialog on the matching signal, or the Raw Data &
    Mapping tab's st.json dump — both real, but scattered: neither stitches a company's whole
    crawled picture into one place. See docs/CRAWLER_ROADMAP.md T21 for why this exists."""
    records = {r.dataset_name: r.raw_row for r in db.query(RawImportRecord).filter(
        RawImportRecord.company_id == company.id, RawImportRecord.dataset_name.like("crawler_%")
    ).all() if r.raw_row}
    # These two derived-benchmark sources predate the crawler_ naming convention (see their
    # own DATASET_NAME constants in scrapers/competitor_benchmark.py and
    # scrapers/competitor_catalog_benchmark.py) and would otherwise be invisible here.
    for extra in ("competitor_benchmark", "competitor_catalog_benchmark"):
        rec = db.query(RawImportRecord).filter_by(company_id=company.id, dataset_name=extra).first()
        if rec and rec.raw_row:
            records[extra] = rec.raw_row

    sections = []  # (label, render_fn), only the ones with real data to show

    website = records.get("crawler_company_website")
    if website:
        def _render_website(row=website):
            lines = row.get("product_types") or []
            c1, c2, c3 = st.columns(3)
            c1.metric("Product lines found", row.get("product_lines_count") or 0)
            c2.metric("Pages crawled", row.get("crawled_pages_count") or 0)
            c3.metric("Last product update", (row.get("last_product_update_signal") or {}).get("date") or "—")
            if lines:
                shown = ", ".join(lines[:12])
                st.caption(f"Product lines: {shown}" + (f" (+{len(lines) - 12} more)" if len(lines) > 12 else ""))
        sections.append(("🏭 Company Website", _render_website))

    digital = records.get("crawler_digital_maturity")
    if digital:
        def _render_digital(row=digital):
            c1, c2, c3 = st.columns(3)
            c1.metric("CMS", row.get("cms_platform") or "unknown")
            c2.metric("E-commerce", "Yes" if row.get("has_ecommerce") else "No")
            redesign = (row.get("last_major_redesign_estimate") or {}).get("estimated_year")
            stable = (row.get("last_major_redesign_estimate") or {}).get("comparisons") or []
            if redesign:
                c3.metric("Last redesign", str(int(redesign)))
            elif stable:
                oldest = min((c.get("from_year") for c in stable if c.get("from_year")), default=None)
                c3.metric("Last redesign", f"stable since {oldest}" if oldest else "no clear estimate")
            else:
                c3.metric("Last redesign", "no clear estimate")
            socials = row.get("social_presence_links") or []
            if socials:
                st.caption("Social profiles: " + ", ".join(s.get("platform", "?") for s in socials))
            snaps = row.get("snapshot_count_last_5_years")
            if snaps is not None:
                st.caption(f"{snaps} Wayback Machine snapshot(s) in the last 5 years.")
        sections.append(("💻 Digital Presence", _render_digital))

    jobs = records.get("crawler_job_postings")
    if jobs:
        def _render_jobs(row=jobs):
            if not row.get("sources_used"):
                note = (row.get("careers_discovery") or {}).get("note") or "No careers page could be found for this company."
                st.caption(note)
                return
            c1, c2 = st.columns(2)
            total = row.get("total_open_roles")
            digital_n = row.get("technical_digital_roles_count")
            c1.metric("Open roles", total if total is not None else "—")
            c2.metric("Digital/technical roles", digital_n if digital_n is not None else "—")
            titles = [r.get("title") for r in (row.get("roles_sample") or []) if r.get("title")]
            if titles:
                shown = "; ".join(titles[:6])
                st.caption(f"Open roles: {shown}" + (f" (+{len(titles) - 6} more)" if len(titles) > 6 else ""))
            elif total == 0:
                st.caption("Careers page found — advertises no open roles at all.")
        sections.append(("💼 Hiring Signals", _render_jobs))

    directory_rows = {k: v for k, v in records.items() if k.startswith("crawler_directory_")}
    if directory_rows:
        def _render_directories(rows=directory_rows):
            for key, row in sorted(rows.items()):
                label = row.get("directory_name") or key.replace("crawler_directory_", "").title()
                if row.get("appears_in_directory") and not row.get("possible_match"):
                    url = row.get("listing_url")
                    st.markdown(f"- **{label}**: ✅ listed" + (f" — [{url}]({url})" if url else ""))
                elif row.get("possible_match"):
                    st.markdown(f"- **{label}**: fuzzy match only — not counted as confirmed")
                elif row.get("status") == "ok":
                    st.markdown(f"- **{label}**: not listed")
                else:
                    st.markdown(f"- **{label}**: {row.get('status_detail') or row.get('status') or 'could not check'}")
        sections.append(("🗂️ Trade Directories", _render_directories))

    review_rows = {k: v for k, v in records.items() if k.startswith("crawler_reviews_")}
    if review_rows:
        def _render_reviews(rows=review_rows):
            for key, row in sorted(rows.items()):
                src = (row.get("source_type") or key.replace("crawler_reviews_", "")).title()
                rating_key = "avg_rating" if row.get("source_type") == "google" else "avg_employer_rating"
                rating = row.get(rating_key)
                if row.get("status") == "skipped_robots":
                    st.markdown(f"- **{src}**: blocked by robots.txt — not a confirmed absence of reviews")
                elif rating is not None:
                    count = row.get("review_count")
                    st.markdown(f"- **{src}**: {rating:.1f} avg rating" + (f" ({count} review(s))" if count else ""))
                else:
                    st.markdown(f"- **{src}**: {row.get('status_detail') or row.get('status') or 'no rating found'}")
        sections.append(("⭐ Reviews", _render_reviews))

    news = records.get("crawler_news_rss")
    if news:
        def _render_news(row=news):
            mentions = row.get("headlines_about_company")
            if not mentions:
                st.caption("No press coverage matched to this company in the last 24 months.")
                return
            st.metric("Press mentions (last 24 months)", mentions)
            concepts = {k: v for k, v in (row.get("headlines_by_concept") or {}).items() if v}
            if concepts:
                st.caption("By concept: " + ", ".join(f"{k} ({v})" for k, v in concepts.items()))
            for h in (row.get("kept_headlines") or [])[:3]:
                st.caption(f"“{h.get('headline')}” — {h.get('outlet') or 'unknown outlet'}")
        sections.append(("📰 Press & News", _render_news))

    site_evidence = records.get("crawler_site_evidence")
    if site_evidence and site_evidence.get("hits"):
        def _render_site_evidence(row=site_evidence):
            labels = {"university_partnership": "University / research partnership",
                      "external_collaboration": "External collaboration",
                      "open_innovation": "Innovation programme or award",
                      "export_share_pct": "Export share", "export_since_year": "Exporting since"}
            for h in row["hits"]:
                url = h.get("page_url")
                where = h.get("page_title") or url
                archived = " *(archived copy — live site unreadable)*" if h.get("via") == "wayback" else ""
                link = f" — [{where}]({url})" if url else ""
                st.markdown(f"- **{labels.get(h.get('claim'), h.get('claim'))}**{archived}: “{h.get('quote')}”{link}")
                seen = h.get("first_seen") or {}
                if seen.get("status") in ("ok", "bounded") and seen.get("first_seen_date"):
                    st.caption(f"The Wayback Machine has had this text since {seen['first_seen_date']} "
                               f"({seen.get('note')}).")
            cov = row.get("coverage") or {}
            if cov.get("pages_read") is not None:
                st.caption(f"Read from the company's own site: {cov.get('pages_read')} page(s). Nothing found in a claim "
                           "does not mean it is absent — only that these pages do not say it.")
        sections.append(("📑 Own-site Evidence", _render_site_evidence))

    competitors = list(company.competitors or [])
    comp_blobs = {k: records[k] for k in (
        "competitor_benchmark", "competitor_catalog_benchmark", "crawler_competitor_hiring",
        "crawler_competitor_news", "crawler_competitor_reviews") if k in records}
    if competitors or comp_blobs:
        def _render_competitors(peers=competitors, blobs=comp_blobs):
            if not peers:
                st.caption("No named competitors recorded yet — add one or more above to benchmark against them.")
                return
            st.caption("Named: " + ", ".join(p.name for p in peers))
            for key, label in (
                ("competitor_benchmark", "Digital maturity gap"),
                ("competitor_catalog_benchmark", "Product catalog gap"),
                ("crawler_competitor_hiring", "Hiring velocity (unscored — open decision)"),
                ("crawler_competitor_news", "Press share (unscored — open decision)"),
                ("crawler_competitor_reviews", "Reviews (unscored — open decision)"),
            ):
                status = "data collected — see 🔍 Audit or Raw Data & Mapping for detail" if key in blobs else "not yet run"
                st.markdown(f"- **{label}**: {status}")
        sections.append(("🥊 Competitor Benchmarks", _render_competitors))

    # company.parent_company_id (set by scrapers/company_website_discovery.py once a page
    # explicitly discloses the relationship AND the named parent matches a real company row
    # here - never a guess) is the current, authoritative state; the crawler_website_discovery
    # blob's own "parent" field is only a point-in-time snapshot of the run that found it, so
    # it's the fallback for "mentioned but not (yet, or ever) matched to a DB row" only.
    parent_company = company.parent if company.parent_company_id else None
    parent_mention = (records.get("crawler_website_discovery") or {}).get("parent")
    if parent_company or parent_mention:
        def _render_parent(parent_company=parent_company, parent_mention=parent_mention):
            if parent_company:
                flags = {"Germany": "🇩🇪", "Italy": "🇮🇹"}
                st.markdown(f"🔗 Linked as a subsidiary of **{parent_company.legal_name}** — already in this database.")
                c1, c2 = st.columns(2)
                c1.metric("Parent's NEED", f"{parent_company.need_score:.0f}" if parent_company.need_score is not None else "—")
                c2.metric("Parent's READINESS", f"{parent_company.readiness_score:.0f}" if parent_company.readiness_score is not None else "—")
                # A callback, not code after the click: the company picker above is already instantiated in this
                # run, and Streamlit forbids writing a widget's state after that - a callback runs before the next
                # run builds its widgets, which is the one safe moment.
                label = (f"{parent_company.legal_name} ({parent_company.registration_number}) — "
                         f"{flags.get(parent_company.country, '🌐')} {parent_company.country} [{parent_company.segment}]")
                st.button(f"→ Open {parent_company.legal_name}", key="jump_to_parent_company",
                          on_click=_select_company_in_dossier, args=(label,))
            elif parent_mention and parent_mention.get("name"):
                st.markdown(
                    f"This company's crawled pages mention being part of **{parent_mention['name']}** — "
                    "not found as a separate company in this database."
                )
        sections.append(("🏢 Parent / Group Relationship", _render_parent))

    if not sections:
        return
    with st.expander(f"🕷️ Crawled Intelligence ({len(sections)} source group(s) with data)", expanded=False):
        st.caption(
            "Everything the crawlers found for this company, grouped by theme, in one place. "
            "Every figure here traces back to a specific crawler run — open 🔍 Audit on the "
            "matching signal below, or the Raw Data & Mapping tab, for the full evidence trail."
        )
        for i, (label, render_fn) in enumerate(sections):
            st.markdown(f"**{label}**")
            render_fn()
            if i < len(sections) - 1:
                st.markdown("---")




@dataclass
class DossierContext:
    """Everything the dossier sections share, loaded once per rerun."""
    db: Session
    company: Company
    signals: list
    sig_dict: dict
    indicator_defs: dict
    scores: dict
    score_exclusions: dict
    build_row: Callable
    eligible: list
    raw_records: list


def build_context(db: Session, company: Company) -> DossierContext:
    from company_service import eligible_phase7_sources, is_source_applicable
    signals = db.query(SignalRecord).filter_by(company_id=company.id).all()
    sig_dict = {s.signal_key: s for s in signals}
    indicator_defs = fetch_indicator_defs(db)
    scores = calculate_company_scores(signals, indicator_defs, include_detail=True)
    score_exclusions = {e["key"]: e["score_exclusion_reason"]
                        for e in scores["need_detail"] + scores["readiness_detail"]
                        if e.get("score_exclusion_reason")}

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

    raw_records = db.query(RawImportRecord).filter_by(company_id=company.id).order_by(RawImportRecord.dataset_name).all()
    return DossierContext(db=db, company=company, signals=signals, sig_dict=sig_dict, indicator_defs=indicator_defs,
                          scores=scores, score_exclusions=score_exclusions, build_row=_build_row,
                          eligible=eligible_phase7_sources(company), raw_records=raw_records)



def sec_score_cards(ctx: DossierContext) -> None:
    """Need / Readiness / completeness / shortlist status headline metrics."""
    company, scores = ctx.company, ctx.scores
    col_m1, col_m2, col_m3, col_m4 = st.columns(4)
    with col_m1:
        st.metric("Need Axis Score", f"{scores['need_score']}/100", f"Weighted completeness: {scores['need_completeness_pct']:.0f}%")
    with col_m2:
        st.metric("Readiness Axis Score", f"{scores['readiness_score']}/100", f"Weighted completeness: {scores['readiness_completeness_pct']:.0f}%")
    with col_m3:
        st.metric("Overall Completeness", f"{scores['total_completeness_pct']:.1f}%", f"{scores['signals_checked']}/{scores['signals_total']} Signals")
    paid_unlocked = is_shortlisted(company.shortlist_status)
    with col_m4:
        st.metric("Stage", SHORTLIST_STATUS_LABELS.get(company.shortlist_status, company.shortlist_status),
                   delta="Phase 3+ Unlocked" if paid_unlocked else "Phase 1+2 Only")


def sec_score_why(ctx: DossierContext) -> None:
    """Why each axis scored what it did (per-signal breakdown)."""
    scores = ctx.scores
    col_why1, col_why2 = st.columns(2)
    with col_why1:
        _render_score_breakdown("Need", scores["need_score"], scores["need_detail"], scores["need_meta"])
    with col_why2:
        _render_score_breakdown("Readiness", scores["readiness_score"], scores["readiness_detail"], scores["readiness_meta"])


def sec_pain_points(ctx: DossierContext) -> None:
    """Pain points: what the company struggles with, read from the same signals as the scores."""
    db, signals, indicator_defs = ctx.db, ctx.signals, ctx.indicator_defs
    from views.pain_points import render_company_pain_points
    render_company_pain_points(db, signals, indicator_defs)


def sec_identity(ctx: DossierContext) -> None:
    """Identity & registry facts, parent company and subsidiaries."""
    db, company = ctx.db, ctx.company
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
            st.write("**Last Scored:**", company.last_scored_at.strftime("%Y-%m-%d %H:%M") if company.last_scored_at else "Never (open 🎯 Targets & Shortlist)")
            parent = db.query(Company).filter_by(id=company.parent_company_id).first() if company.parent_company_id else None
            st.write("**Parent Company:**", parent.legal_name if parent else "—")

        subsidiaries = db.query(Company).filter_by(parent_company_id=company.id).all()
        if subsidiaries:
            st.write("**Subsidiaries:**", ", ".join(s.legal_name for s in subsidiaries))


def sec_competitors(ctx: DossierContext) -> None:
    """Hand-recorded named competitors (and their review profiles) feeding the competitor benchmarks."""
    db, company = ctx.db, ctx.company
    with st.expander(f"🥊 Named Competitors ({len(company.competitors)}/3)"):
        st.caption(
            "Recorded by hand, not auto-discovered — a search/LLM guess at who counts as a "
            "competitor risks silently benchmarking against the wrong company (the same "
            "risk review-crawler's own search-matching code exists to guard against). "
            "Feeds **Competitor Digital Adoption Gap**: re-runs the Digital Maturity Crawler "
            "(including its homepage vision assessment) against each one's own site and "
            "compares it to this company's own score. Up to 3, matching that indicator's own "
            "'2-3 named direct competitors' definition."
        )
        for comp in company.competitors:
            cc1, cc2, cc3 = st.columns([3, 4, 1])
            with cc1:
                st.write(comp.name)
            with cc2:
                st.write(comp.homepage_url or "— (no homepage recorded, can't be benchmarked)")
            with cc3:
                if st.button("🗑️", key=f"del_competitor_{comp.id}", help="Remove this competitor"):
                    db.delete(comp)
                    db.commit()
                    st.rerun()
            with st.form(f"review_profile_{comp.id}"):
                profile_url = st.text_input(
                    f"{comp.name} review profile URL (optional; Google Maps, Kununu, or Glassdoor)",
                    value=comp.review_profile_url or "",
                )
                if st.form_submit_button("Save review profile"):
                    from scrapers.competitor_review_benchmark import profile_source
                    if profile_url.strip() and not profile_source(profile_url):
                        st.warning("Enter a direct HTTPS Google Maps, Kununu, or Glassdoor profile URL.")
                    else:
                        comp.review_profile_url = profile_url.strip() or None
                        db.commit()
                        st.rerun()
        if len(company.competitors) < 3:
            with st.form(f"add_competitor_{company.id}", clear_on_submit=True):
                fc1, fc2, fc3 = st.columns([3, 4, 1])
                with fc1:
                    new_name = st.text_input("Competitor name", key="new_competitor_name", label_visibility="collapsed", placeholder="Competitor name")
                with fc2:
                    new_url = st.text_input("Homepage URL", key="new_competitor_url", label_visibility="collapsed", placeholder="https://competitor-homepage.example (needed to benchmark)")
                with fc3:
                    submitted = st.form_submit_button("➕ Add", use_container_width=True)
                new_review_url = st.text_input("Review profile URL (optional)",
                                               placeholder="Direct Google Maps, Kununu, or Glassdoor profile")
                if submitted:
                    if not new_name.strip():
                        st.warning("Enter a competitor name.")
                    else:
                        from scrapers.competitor_review_benchmark import profile_source
                        if new_review_url.strip() and not profile_source(new_review_url):
                            st.warning("Enter a direct HTTPS Google Maps, Kununu, or Glassdoor profile URL.")
                        else:
                            db.add(Competitor(company_id=company.id, name=new_name.strip(),
                                              homepage_url=(new_url.strip() or None),
                                              review_profile_url=(new_review_url.strip() or None)))
                            db.commit()
                            st.rerun()
        else:
            st.caption("Maximum of 3 — remove one above to add another.")


def sec_crawler_coverage(ctx: DossierContext) -> None:
    """Which Phase 7 crawlers have completed for this company."""
    db, company = ctx.db, ctx.company
    eligible = ctx.eligible
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


def sec_product_intelligence(ctx: DossierContext) -> None:
    """Product catalog found on the company's own site."""
    db, company = ctx.db, ctx.company
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


def sec_public_contracts(ctx: DossierContext) -> None:
    """TED public-contract awards naming this company."""
    db, company = ctx.db, ctx.company
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


def sec_recalls(ctx: DossierContext) -> None:
    """Published FDA product recalls matching this firm's name."""
    db, company = ctx.db, ctx.company
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


def sec_patents(ctx: DossierContext) -> None:
    """EPO patent-publication snapshot."""
    db, company = ctx.db, ctx.company
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


def sec_financial_profile(ctx: DossierContext) -> None:
    """Plain-language growth headlines and current balance-sheet levels."""
    db, company = ctx.db, ctx.company
    sig_dict, indicator_defs = ctx.sig_dict, ctx.indicator_defs
    st.subheader("💰 Financial Profile")
    raw_records_for_trends = ctx.raw_records

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


def sec_full_financial_table(ctx: DossierContext) -> None:
    """Every Financial Health and Cost Structure indicator, checked or not, with audit buttons."""
    db, company = ctx.db, ctx.company
    sig_dict, indicator_defs = ctx.sig_dict, ctx.indicator_defs
    _build_row = ctx.build_row
    with st.expander("📋 Full Financial & Cost Structure Table (every indicator, with plain-English description)", expanded=False):
        st.caption("Every Financial Health and Cost Structure indicator in the catalog for this company, checked or not. **Value** is the raw figure as computed/injected (monetary financials in thousands, **k**) — not a 0-100 score (see Score Breakdown below for that). 🔍 opens the full audit trail, including the real source and the matching raw record.")
        from indicators import CAT_FINANCIAL, CAT_COST
        fin_keys = sorted(
            [k for k, d in indicator_defs.items() if d["category"] in (CAT_FINANCIAL, CAT_COST)],
            key=lambda k: indicator_defs[k]["label"],
        )
        _render_signal_rows_with_audit(fin_keys, indicator_defs, sig_dict, db, company, _build_row, key_prefix="finprofile")


def sec_valuation(ctx: DossierContext) -> None:
    """Indicative valuation (multiples + DCF) - a separate lens, never blended into Need/Readiness."""
    db, company = ctx.db, ctx.company
    # Indicative valuation — a separate lens, never blended into Need/Readiness.
    with st.expander("💶 Indicative valuation (multiples + DCF)", expanded=False):
        from views.valuation import render_company_valuation
        render_company_valuation(db, company.id, key="cd_val")


def sec_data_trends(ctx: DossierContext) -> None:
    """Every multi-year column group from the raw injected data, charted."""
    raw_records_for_trends = ctx.raw_records
    if raw_records_for_trends:
        with st.expander("📈 Data Trends (from Raw Injected Data)", expanded=False):
            st.caption("Every multi-year figure exactly as uploaded — financial and non-financial, whether or not it was mapped to a scored indicator (financial values in thousands, **k**).")
            for rec in raw_records_for_trends:
                if (rec.raw_row or {}).get("_aida_report"):
                    from views.aida_single_report import render_financial_tables
                    st.markdown(f"**{rec.dataset_name} — actual reporting dates**")
                    render_financial_tables(rec.raw_row["_aida_report"])
                    continue
                if rec.dataset_name.startswith("Balance Sheet PDF "):
                    from views.balance_sheet_upload import render_saved_statement
                    st.markdown(f"**{rec.dataset_name} — printed lines**")
                    render_saved_statement(rec, scope="trend")
                    continue
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
                            field_rows.append({"Field": k, "Value": str(disp_v)})
                        st.dataframe(
                            pd.DataFrame(field_rows),
                            use_container_width=True, hide_index=True,
                        )
                st.markdown("&nbsp;")


def sec_category_breakdown(ctx: DossierContext) -> None:
    """Average normalised score per indicator category."""
    sig_dict, indicator_defs = ctx.sig_dict, ctx.indicator_defs
    with st.expander("📈 Score Breakdown by Category"):
        st.caption(
            "Each checked signal is normalized to 0-100 using its own Raw Min/Raw Max bounds from the Indicator "
            "Weights page (0 = worst end of the configured range, 100 = best), then averaged per category here — "
            "unweighted and axis-blended, for a quick at-a-glance read only. The real weighted Need/Readiness "
            "score (with redundancy dampening and gating) is in 🎯 Targets & Shortlist. "
            "⚠️ If a category's average looks stuck near 0 or 100, its indicators' Raw Min/Max bounds are likely "
            "calibrated for a different data scale than what's actually been injected — check the raw values in "
            "the Financial Profile table above against the bounds shown on the Indicator Weights page."
        )
        st.dataframe(pd.DataFrame(_category_breakdown_rows(sig_dict, indicator_defs)), use_container_width=True, hide_index=True)


def sec_people(ctx: DossierContext) -> None:
    """People & Ownership roster tables."""
    db, company = ctx.db, ctx.company
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
                    "Public profile headline": (p.raw_fields or {}).get("linkedin_profile", {}).get("headline") or "—",
                    "Most recent public role": (p.raw_fields or {}).get("linkedin_profile", {}).get("recent_role") or "—",
                    "Dataset": p.dataset_name,
                } for p in people]
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
                with st.expander(f"Full detail per person ({role_group})", expanded=False):
                    for p in people:
                        st.markdown(f"*{p.full_name or 'Unnamed'}*")
                        st.json(p.raw_fields, expanded=False)


def sec_family_succession(ctx: DossierContext) -> None:
    """Family company / new-generation succession detection."""
    db, company = ctx.db, ctx.company
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


def sec_management_composition(ctx: DossierContext) -> None:
    """Management composition indicators computed from the roster."""
    db, company = ctx.db, ctx.company
    has_aged_people = db.query(CompanyPerson).filter_by(company_id=company.id).filter(CompanyPerson.age.isnot(None)).first() is not None
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


def sec_pilot_history(ctx: DossierContext) -> None:
    """Past pilot outcomes with this company."""
    db, company = ctx.db, ctx.company
    pilot_rows = _pilot_rows(db, company)
    if pilot_rows:
        with st.expander(f"🎯 Pilot History ({len(pilot_rows)})"):
            st.dataframe(pd.DataFrame(pilot_rows), use_container_width=True, hide_index=True)


def sec_signal_audit(ctx: DossierContext) -> None:
    """Every scored indicator grouped by category, with audit and freshness."""
    db, company = ctx.db, ctx.company
    sig_dict, indicator_defs = ctx.sig_dict, ctx.indicator_defs
    _build_row = ctx.build_row
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


def sec_context_tags(ctx: DossierContext) -> None:
    """Informational context and moderator tags."""
    db, company = ctx.db, ctx.company
    sig_dict, indicator_defs = ctx.sig_dict, ctx.indicator_defs
    _build_row = ctx.build_row
    context_defs = {k: d for k, d in indicator_defs.items() if d["axis"] == "context"}
    if context_defs:
        st.markdown("---")
        st.subheader("🏷️ Context & Moderator Tags")
        st.caption("Informational tags — not part of weighted scoring sum.")
        ctx_keys = sorted(context_defs.keys(), key=lambda k: context_defs[k]["label"])
        _render_signal_rows_with_audit(ctx_keys, context_defs, sig_dict, db, company, _build_row, key_prefix="ctxtags")


def sec_raw_data(ctx: DossierContext) -> None:
    """Provenance, every raw dataset exactly as captured, and the column-mapping editor."""
    db, company = ctx.db, ctx.company
    st.subheader("🗂️ Raw Data & Mapping")
    st.caption(
        "Every raw record collected for one company, exactly as captured — independent of whether "
        "it's been mapped to a scored indicator. Use this to audit what was actually collected, and "
        "to correct/teach a column's mapping without re-uploading anything."
    )

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
            if (rec.raw_row or {}).get("_aida_report"):
                from views.aida_single_report import render_report, render_saved_mapping
                render_report(rec.raw_row["_aida_report"], download_key=f"aida_saved_{rec.id}")
                render_saved_mapping(db, rec)
                continue
            if rec.dataset_name.startswith("Balance Sheet PDF "):
                from views.balance_sheet_upload import render_saved_statement
                render_saved_statement(rec, scope="raw")
                continue
            if rec.dataset_name == "Merged Annual Financials":
                from views.balance_sheet_upload import render_merged_history
                render_merged_history(db, company, rec, scope="raw")
                continue
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
