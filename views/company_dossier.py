"""
Company Intelligence - the dossier. One company, everything we know, on one page.

A sticky header (identity, Need / Readiness / data completeness, status) stays on screen while a section bar
switches between eight sections. Only the selected section is built, so the page stays fast even though the data
behind it is large:

  Overview            KPIs, red flags & strengths, top pain points, where the money goes, AI findings, data coverage
  Financials          3-year statement + ratios, growth & levels, full financial table, valuation, raw trends
  Costs               the nine-line cost X-ray vs ATECO peers
  Diagnosis           Need / Readiness breakdowns, pain points, category scores
  People & Ownership  roster, family & succession, management mix, shareholders, group, participations
  Market & Digital    competitors, crawled intelligence, products, public contracts, recalls, patents, crawler coverage
  Actions             shortlist, crawls, pilot history, manual entry, paid pulls, ask-the-AI, one-page export
  Data & Audit        data gaps, provenance, raw datasets & mapping, signal audit, evidence, context tags
  Notes               shared live notebook for the team; notes can be sent to the AI (views/company_notes_panel.py)

The section bodies are the existing renderers (views/company_detail.py `sec_*`, views/company_actions.py, the cost
X-ray, valuation, pain points) - moved, not rewritten - plus the layers that did not exist before (Overview,
financial statement, ownership, data gaps, per-company chat, export). DOSSIER_LAYERS at the bottom is the
inventory the migration check asserts against.
"""

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy.orm import Session

import company_brief
import company_service
import dossier_data as dd
import dossier_export
import cost_analytics as ca
from dossier_nav import OPEN_ID_KEY, OPEN_TAB_KEY, company_label
from models import Company, CompanySourceRun, RawImportRecord
from views import company_detail as cd
from views.company_actions import (
    render_ai_report, render_api_and_crawler_row, render_paid_pulls, render_shortlist_controls,
    render_standard_and_deep_dive,
)
from views.stage_controls import render_stage_quick
from views.cost_structure import COLORS as COST_COLORS, _findings as cost_findings, _frame as cost_frame, render_company_xray

TAB_KEY = "dossier_tab"
SECTIONS = [
    ("overview", "🧭 Overview"), ("financials", "💰 Financials"), ("costs", "💸 Costs"), ("diagnosis", "🩺 Diagnosis"),
    ("people", "👥 People & Ownership"), ("market", "🌐 Market & Digital"), ("actions", "🎯 Actions"),
    ("data", "🗄️ Data & Audit"), ("notes", "📝 Notes"),
]
SECTION_LABEL = dict(SECTIONS)
LEVEL_ICON = {"risk": "🔴", "watch": "🟠", "strength": "🟢"}

# The wrapper (not the keyed container) must be sticky: a sticky element only sticks inside its parent.
_CSS = """
<style>
[data-testid="stLayoutWrapper"]:has(> .st-key-dossier_header) { position: sticky; top: 3.75rem; z-index: 90; }
.st-key-dossier_header {
    background: #0F172A; border: 1px solid #334155; border-radius: 10px; padding: 0.55rem 0.9rem 0.35rem;
    margin: 0.25rem 0 0.75rem; gap: 0.35rem; box-shadow: 0 6px 18px rgba(0, 0, 0, 0.35);
}
.st-key-dossier_header h3 { padding: 0 0 0.2rem; }
.st-key-dossier_header [data-testid="stHorizontalBlock"] { padding-bottom: 0.9rem; }
</style>
"""


def _money_k(v):
    """Thousand EUR -> '€30.8M' / '€950k'."""
    if v is None:
        return "—"
    return f"€{v / 1000:,.1f}M" if abs(v) >= 1000 else f"€{v:,.0f}k"


def _pct(v, d=1):
    return "—" if v is None else f"{v:.{d}f}%"


# --------------------------------------------------------------------------------------------------- selection

def _consume_open_request(rows) -> None:
    """Turn a pending 'open this company' request (dossier_nav.open_dossier) into the picker's own state. Runs
    before the picker is built, the only moment its state may be written."""
    pending, tab = st.session_state.pop(OPEN_ID_KEY, None), st.session_state.pop(OPEN_TAB_KEY, None)
    if tab in SECTION_LABEL:
        st.session_state[TAB_KEY] = tab
    if pending:
        hit = next((r for r in rows if r.id == pending), None)
        if hit:
            st.session_state["detail_country_filter"] = "All Countries"
            st.session_state["detail_company_select"] = company_label(hit.legal_name, hit.registration_number,
                                                                      hit.country, hit.segment)


def _go_to_section(tab: str) -> None:
    st.session_state[TAB_KEY] = tab


# ----------------------------------------------------------------------------------------------------- shared data

def _load_shared(ctx: cd.DossierContext) -> dict:
    db, company = ctx.db, ctx.company
    aida = db.query(RawImportRecord).filter_by(company_id=company.id, dataset_name=dd.AIDA_DATASET).first()
    from aida_single_report import DATASET_NAME as REPORT_DATASET
    single_report = db.query(RawImportRecord).filter_by(company_id=company.id, dataset_name=REPORT_DATASET).first()
    # Choose one complete source; never assemble different fiscal periods from
    # unrelated uploads. Prefer the report on a tie as it was reviewed here.
    if single_report and (not aida or str(single_report.raw_row.get("accounts_close", ""))[:10]
                          >= str(aida.raw_row.get("accounts_close", ""))[:10]):
        aida = single_report
    ownership_blob = aida.raw_row if aida else None
    # Statement history merged by fiscal year wins for the financials whenever it reaches at least as far as the
    # chosen source; ownership and group data only exist in the AIDA blob, so they keep reading it.
    from financial_ledger import LEDGER_DATASET
    from balance_sheet_pdf import DATASET_PREFIX
    merged = db.query(RawImportRecord).filter_by(company_id=company.id, dataset_name=LEDGER_DATASET).first()
    if merged and (not aida or str(merged.raw_row.get("accounts_close", ""))[:10] >= str(aida.raw_row.get("accounts_close", ""))[:10]):
        aida = merged
    statements = db.query(RawImportRecord).filter(RawImportRecord.company_id == company.id,
                                                  RawImportRecord.dataset_name.like(DATASET_PREFIX + "%")
                                                  ).order_by(RawImportRecord.dataset_name.desc()).all()
    blob = aida.raw_row if aida else None
    fin = dd.financial_statement(blob)
    sig_map = {k: (s.numeric_value, s.status) for k, s in ctx.sig_dict.items()}
    cost = None
    frame = None
    if db.query(RawImportRecord.id).filter_by(company_id=company.id, dataset_name=ca.DATASET_NAME).first():
        frame = cost_frame(db)
        cost = ca.company_breakdown(db, company, frame)
    pains = company_service.get_company_pain_points(db, company)
    return {"blob": blob, "fin": fin, "flags": dd.flags(fin, sig_map), "cost": cost, "cost_frame": frame, "pains": pains,
            "ownership": dd.ownership(ownership_blob), "merged": merged, "statements": statements}


# ------------------------------------------------------------------------------------------------------- header

def _render_header(ctx: cd.DossierContext, shared: dict) -> None:
    company, scores = ctx.company, ctx.scores
    from views.pain_points import STATUS_META
    with st.container(key="dossier_header"):
        c_name, c_need, c_ready, c_data, c_status = st.columns([4, 1, 1, 1, 2])
        with c_name:
            st.markdown(f"### {company.legal_name}")
            bits = [company.registration_number, company.nace_code, company.region, company.legal_form,
                    f"{company.headcount} staff" if company.headcount else None]
            st.caption(" · ".join(str(b) for b in bits if b)
                       + (f" · [{company.website_url}]({company.website_url})" if company.website_url else ""))
        c_need.metric("Need", f"{scores['need_score']:.0f}", help="Need axis 0-100: how much the company needs help.")
        c_ready.metric("Readiness", f"{scores['readiness_score']:.0f}", help="Readiness axis 0-100: how able it is to act on a pilot. Never blended with Need.")
        c_data.metric("Data", f"{scores['total_completeness_pct']:.0f}%", help="Weighted share of scored indicators with a real value.")
        with c_status:
            st.caption("Stage")
            render_stage_quick(ctx.db, company, "dos_hdr_stage", collapsed=True)
        chips = [f"{LEVEL_ICON[f['level']]} {f['chip']}" for f in shared["flags"] if f["level"] != "strength"][:3]
        chips += [f"{STATUS_META[p['status']][0]} {p['label']}" for p in shared["pains"][:2]]
        if chips:
            st.caption("  ·  ".join(chips))
        st.segmented_control("Section", [k for k, _ in SECTIONS], format_func=SECTION_LABEL.get, key=TAB_KEY,
                             required=True, label_visibility="collapsed", width="stretch")


# -------------------------------------------------------------------------------------------------------- overview

def _cost_mix_bar(cost: dict, key: str) -> None:
    fig = go.Figure()
    for c in cost["categories"]:
        share = c["share_of_costs_pct"]
        if share is None or share <= 0:
            continue
        fig.add_bar(y=[""], x=[share], orientation="h", name=c["label"], marker_color=COST_COLORS[c["category"]],
                    text=[f"{share:.0f}%" if share >= 6 else ""], textposition="inside", hovertemplate=f"{c['label']}: {share:.1f}%<extra></extra>")
    fig.update_layout(template="plotly_dark", barmode="stack", height=130, margin=dict(t=4, b=4, l=0, r=0),
                      legend=dict(orientation="h", y=-0.6, font=dict(size=10)), xaxis=dict(range=[0, 100], visible=False),
                      yaxis=dict(visible=False))
    st.plotly_chart(fig, use_container_width=True, key=key)


def _tab_overview(ctx: cd.DossierContext, shared: dict) -> None:
    db, company, fin, cost = ctx.db, ctx.company, shared["fin"], shared["cost"]
    from views.pain_points import STATUS_META

    # --- KPI strip
    if fin and fin["has_data"]:
        L, R, lab = fin["lines"], fin["ratios"], fin["labels"]["latest"]
        k = st.columns(6)
        g = R["revenue_growth"]["latest"]
        k[0].metric(f"Revenue {lab}", _money_k(L["revenue"]["latest"]), f"{g:+.1f}%" if g is not None else None)
        m = R["ebitda_margin"]["latest"]
        k[1].metric("EBITDA", _money_k(L["ebitda"]["latest"]), f"{m:.1f}% margin" if m is not None else None, delta_color="off")
        k[2].metric("Net income", _money_k(L["net_income"]["latest"]),
                    f"{R['net_margin']['latest']:.1f}% margin" if R["net_margin"]["latest"] is not None else None, delta_color="off")
        total_costs = (cost or {}).get("total_costs_k_eur") or L["production_costs"]["latest"]
        k[3].metric("Production costs", _money_k(total_costs))
        rpe = R["revenue_per_employee"]["latest"]
        k[4].metric("Employees", f"{L['employees']['latest']:,.0f}" if L["employees"]["latest"] is not None else "—",
                    f"€{rpe:,.0f}k revenue / head" if rpe is not None else None, delta_color="off")
        k[5].metric("Equity / assets", _pct(R["equity_ratio"]["latest"], 0),
                    f"cash {_money_k(L['cash']['latest'])}", delta_color="off")
    else:
        st.info("No multi-year financial statement on file for this company yet - see 🗄️ Data & Audit for what would fill it.")

    left, right = st.columns(2)
    with left:
        st.markdown("#### 🚩 Flags & strengths")
        if shared["flags"]:
            for f in shared["flags"]:
                st.markdown(f"{LEVEL_ICON[f['level']]} {f['text']}")
        else:
            st.caption("No flags raised from the filed accounts and registry data on file.")
    with right:
        st.markdown("#### 🩺 Top pain points")
        if shared["pains"]:
            for p in shared["pains"][:3]:
                icon, label = STATUS_META[p["status"]]
                st.markdown(f"{icon} **{p['label']}** — {label} · {p['score']:.0f}/100")
                if p.get("headline"):
                    st.caption(p["headline"])
        else:
            st.caption("No pain point detected in the data available.")
        st.button("→ All pain points & score breakdown", key="ov_to_diag", on_click=_go_to_section, args=("diagnosis",))

    st.markdown("#### 💸 Where the money goes")
    if cost:
        _cost_mix_bar(cost, "ov_costmix")
        notes = cost_findings(cost)
        for n in notes[:3]:
            st.markdown(f"- {n}")
        if not notes:
            st.caption(f"Cost mix is in line with {cost['peer_basis']}.")
        st.button("→ Full cost X-ray", key="ov_to_costs", on_click=_go_to_section, args=("costs",))
    else:
        st.caption("No cost breakdown imported for this company.")

    st.markdown("#### ✨ AI report")
    brief_rec = db.query(RawImportRecord).filter_by(company_id=company.id, dataset_name="crawler_ai_company_brief").first()
    findings = [f.get("text") for f in ((brief_rec.raw_row or {}).get("findings") or []) if f.get("text")] if brief_rec else []
    for t in findings[:4]:
        st.markdown(f"- {t}")
    if not findings:
        st.caption("No AI report generated yet.")
    evidence = company_brief.collect_evidence(db, company)
    if evidence:
        with st.expander(f"✨ AI Intelligence Report ({len(evidence)} evidence item(s) collected)", expanded=False):
            render_ai_report(db, company, key="ai_brief", heading=False)
    else:
        st.caption("No crawled evidence yet - run a crawl from 🎯 Actions to enable the cited AI report.")

    st.markdown("#### 🧩 Data coverage")
    run_rows = {r.source_name: r for r in db.query(CompanySourceRun).filter_by(company_id=company.id, phase=7).all()}
    done = sum(run_rows.get(s) is not None and run_rows[s].status == "live" for s in ctx.eligible)
    gaps = dd.gap_rows(ctx.indicator_defs, ctx.sig_dict, _gap_plan())
    c1, c2, c3 = st.columns(3)
    c1.metric("Scored indicators checked", f"{ctx.scores['signals_checked']}/{ctx.scores['signals_total']}")
    c2.metric("Deep crawlers completed", f"{done}/{len(ctx.eligible)}")
    c3.metric("Scored indicators missing", len(gaps), help="Heaviest first in 🗄️ Data & Audit, with where each would come from.")
    if gaps:
        st.caption("Biggest gaps: " + "; ".join(f"{g['Indicator']} (weight {g['Weight']:.0f})" for g in gaps[:5]))

    cd.sec_identity(ctx)


def _gap_plan() -> dict:
    try:
        from scripts.indicator_gaps import GAP_PLAN
        return GAP_PLAN
    except Exception:  # noqa: BLE001 - the plan is advice; the gap list itself never depends on it
        return {}


# ------------------------------------------------------------------------------------------------------ financials

def _fmt_table(lines, labels, fin, fmt="{:,.0f}"):
    cols = {p: fin["labels"][p] for p in dd.PERIODS}
    rows = []
    for key, label in lines:
        rows.append({"": label, **{cols[p]: (None if fin["lines"][key][p] is None else fmt.format(fin["lines"][key][p])) for p in reversed(dd.PERIODS)}})
    return pd.DataFrame(rows).fillna("—")


def _render_statement(ctx: cd.DossierContext, shared: dict) -> None:
    fin = shared["fin"]
    merged = shared.get("merged")
    is_merged = bool(merged) and shared["blob"] is merged.raw_row
    st.subheader("📑 Financial statement (merged annual history, thousand EUR)" if is_merged
                 else "📑 Financial statement (AIDA, thousand EUR)")
    if not fin or not fin["has_data"]:
        st.caption("No AIDA multi-year statement on file for this company.")
        _render_statement_files(ctx, shared)
        return
    lab = fin["labels"]
    if is_merged:
        from views.balance_sheet_upload import render_merged_summary
        render_merged_summary(merged.raw_row)
    else:
        st.caption("Three filed years straight from the AIDA export - nothing estimated. Ratios need both inputs, otherwise they are blank. "
                   "'Total liabilities' is AIDA's 'total debt' line (it includes trade payables).")
    st.markdown("**Income statement**")
    st.dataframe(_fmt_table(dd.INCOME_LINES, lab, fin), hide_index=True, use_container_width=True)
    st.markdown("**Balance sheet**")
    st.dataframe(_fmt_table(dd.BALANCE_LINES, lab, fin), hide_index=True, use_container_width=True)
    st.markdown("**Margins, returns and productivity**")
    R = fin["ratios"]
    ratio_rows = [("Revenue growth", "revenue_growth", "{:+.1f}%"), ("Gross margin", "gross_margin_pct", "{:.1f}%"),
                  ("EBITDA margin", "ebitda_margin", "{:.1f}%"), ("EBIT margin", "ebit_margin", "{:.1f}%"),
                  ("Net margin", "net_margin", "{:.1f}%"), ("Personnel / revenue", "personnel_pct_revenue", "{:.1f}%"),
                  ("Equity / total assets", "equity_ratio", "{:.1f}%"), ("Cash / revenue", "cash_pct_revenue", "{:.1f}%"),
                  ("Revenue per employee (k EUR)", "revenue_per_employee", "{:,.0f}")]
    rows = [{"": name, **{lab[p]: (None if R[key][p] is None else fmt.format(R[key][p])) for p in reversed(dd.PERIODS)}} for name, key, fmt in ratio_rows]
    other = [{"": label, **{lab[p]: (None if fin["lines"][key][p] is None else f"{fin['lines'][key][p]:,.2f}".rstrip("0").rstrip(".")) for p in reversed(dd.PERIODS)}}
             for key, label in dd.OTHER_LINES]
    st.dataframe(pd.DataFrame(rows + other).fillna("—"), hide_index=True, use_container_width=True)

    years = [lab[p] for p in reversed(dd.PERIODS)]
    g1, g2 = st.columns(2)
    fig = go.Figure()
    for key, name, color in (("revenue", "Revenue", "#4C78A8"), ("ebitda", "EBITDA", "#54A24B"), ("ebit", "EBIT", "#F58518"),
                             ("net_income", "Net income", "#E45756")):
        fig.add_bar(name=name, x=years, y=[fin["lines"][key][p] for p in reversed(dd.PERIODS)], marker_color=color)
    fig.update_layout(template="plotly_dark", barmode="group", height=320, margin=dict(t=30, b=10), title="Earnings (k EUR)",
                      legend=dict(orientation="h", y=-0.2))
    g1.plotly_chart(fig, use_container_width=True, key="dos_fin_earn")
    fig = go.Figure()
    for key, name in (("ebitda_margin", "EBITDA margin"), ("ebit_margin", "EBIT margin"), ("net_margin", "Net margin"),
                      ("equity_ratio", "Equity / assets")):
        fig.add_scatter(name=name, x=years, y=[R[key][p] for p in reversed(dd.PERIODS)], mode="lines+markers")
    fig.update_layout(template="plotly_dark", height=320, margin=dict(t=30, b=10), title="Margins & capitalisation (%)",
                      legend=dict(orientation="h", y=-0.2))
    g2.plotly_chart(fig, use_container_width=True, key="dos_fin_margin")
    _render_statement_files(ctx, shared)


def _render_statement_files(ctx: cd.DossierContext, shared: dict) -> None:
    """Full merged history by year with its source, conflicts the operator can still change, and each uploaded statement."""
    from views.balance_sheet_upload import render_merged_history, render_remove_statement, render_saved_statement
    if shared.get("merged"):
        with st.expander("🗂️ Full annual history, source of every figure, conflicts", expanded=False):
            render_merged_history(ctx.db, ctx.company, shared["merged"], scope="dos")
    for record in shared.get("statements") or []:
        with st.expander(f"📄 Balance sheet {record.raw_row.get('period_end')} — {record.source_filename}", expanded=False):
            render_saved_statement(record, scope="dos")
            render_remove_statement(ctx.db, ctx.company, record, scope="dos")


def _tab_financials(ctx: cd.DossierContext, shared: dict) -> None:
    from views.business_lines import render_business_lines
    render_business_lines(ctx.db, ctx.company, scope="dos")
    _render_statement(ctx, shared)
    cd.sec_financial_profile(ctx)
    cd.sec_full_financial_table(ctx)
    cd.sec_valuation(ctx)
    cd.sec_data_trends(ctx)


# ------------------------------------------------------------------------------------------------ costs / diagnosis

def _tab_costs(ctx: cd.DossierContext, shared: dict) -> None:
    if not shared["cost"]:
        st.info("No cost breakdown imported for this company yet (the AIDA cost-breakdown export has no row for it).")
        return
    render_company_xray(ctx.db, ctx.company, shared["cost_frame"], key="dos_cost")


def _tab_diagnosis(ctx: cd.DossierContext, shared: dict) -> None:
    cd.sec_score_cards(ctx)
    cd.sec_score_why(ctx)
    st.markdown("---")
    cd.sec_pain_points(ctx)
    st.markdown("---")
    cd.sec_category_breakdown(ctx)


# ----------------------------------------------------------------------------------------------------- people

def _render_ownership(shared: dict) -> None:
    own = shared["ownership"]
    st.subheader("🏛️ Ownership & group (AIDA)")
    c1, c2, c3 = st.columns(3)
    c1.metric("Group size", f"{own['group_size']:.0f}" if own["group_size"] is not None else "—", help="Companies in the group, per AIDA.")
    c2.metric("BvD independence class", own["independence"] or "—", help="A = most independent ... D = least independent.")
    c3.metric("Consolidation code", own["consolidation"] or "—")
    if own["procedures"]:
        st.warning("Procedures on record: " + "; ".join(own["procedures"]))
    if own["shareholders"]:
        st.markdown("**Shareholders**")
        st.dataframe(pd.DataFrame(own["shareholders"]), hide_index=True, use_container_width=True)
    else:
        st.caption("No shareholder list on file.")
    if own["controlling"]:
        st.markdown("**Controlling shareholders**")
        st.dataframe(pd.DataFrame(own["controlling"]), hide_index=True, use_container_width=True)
    if own["participations"]:
        st.markdown("**Participations (companies it owns shares in)**")
        st.dataframe(pd.DataFrame(own["participations"]), hide_index=True, use_container_width=True)


def _tab_people(ctx: cd.DossierContext, shared: dict) -> None:
    cd.sec_people(ctx)
    if not cd._management_board_groups(ctx.db, ctx.company):
        st.caption("No board / management roster imported for this company.")
    cd.sec_family_succession(ctx)
    cd.sec_management_composition(ctx)
    _render_ownership(shared)


# ------------------------------------------------------------------------------------------------------ market

def _tab_market(ctx: cd.DossierContext, shared: dict) -> None:
    cd.sec_competitors(ctx)
    cd._render_crawled_intelligence(ctx.db, ctx.company)
    cd.sec_product_intelligence(ctx)
    cd.sec_public_contracts(ctx)
    cd.sec_recalls(ctx)
    cd.sec_patents(ctx)
    cd.sec_crawler_coverage(ctx)


# ----------------------------------------------------------------------------------------------------- actions

def _snapshot(ctx: cd.DossierContext, shared: dict) -> dict:
    from views.pain_points import STATUS_META
    company, fin, db = ctx.company, shared["fin"], ctx.db
    people = []
    for group in cd._management_board_groups(db, company).values():
        for p in group:
            if p.age is not None:
                people.append({"name": p.full_name or "—", "role": p.role or "—", "age": p.age})
    brief = db.query(RawImportRecord).filter_by(company_id=company.id, dataset_name="crawler_ai_company_brief").first()
    parent = db.query(Company).filter_by(id=company.parent_company_id).first() if company.parent_company_id else None
    kpis = []
    if fin and fin["has_data"]:
        L, R = fin["lines"], fin["ratios"]
        kpis = [("Revenue", _money_k(L["revenue"]["latest"])), ("EBITDA margin", _pct(R["ebitda_margin"]["latest"])),
                ("Employees", f"{L['employees']['latest']:,.0f}" if L["employees"]["latest"] is not None else "—")]
    return {
        "company": {"name": company.legal_name, "vat": company.registration_number, "ateco": company.nace_code,
                    "sector": company.sector_name, "region": company.region, "legal_form": company.legal_form,
                    "website": company.website_url, "headcount": company.headcount,
                    "status": cd.SHORTLIST_STATUS_LABELS.get(company.shortlist_status, company.shortlist_status),
                    "parent": parent.legal_name if parent else None},
        "scores": {"need": ctx.scores["need_score"], "readiness": ctx.scores["readiness_score"],
                   "completeness": ctx.scores["total_completeness_pct"]},
        "kpis": kpis, "flags": shared["flags"],
        "pain_points": [{"label": p["label"], "status": STATUS_META[p["status"]][1], "score": p["score"],
                         "headline": p.get("headline")} for p in shared["pains"][:6]],
        "statement": fin, "cost": shared["cost"], "ownership": shared["ownership"], "people": people,
        "brief_findings": [f.get("text") for f in ((brief.raw_row or {}).get("findings") or []) if f.get("text")][:6] if brief else [],
    }


def _tab_actions(ctx: cd.DossierContext, shared: dict) -> None:
    from views.ai_assistant import render_company_chat
    db, company = ctx.db, ctx.company

    st.subheader("⭐ Shortlist & collection")
    c1, c2 = st.columns([1, 2])
    with c1:
        render_shortlist_controls(db, company)
    with c2:
        render_api_and_crawler_row(db, company)
        render_standard_and_deep_dive(db, company, key="dos_indepth", show_standard=False)

    st.markdown("---")
    st.subheader("📄 One-page export")
    html = dossier_export.build_html(_snapshot(ctx, shared))
    st.download_button("⬇️ Download one-pager (HTML — print to PDF)", html.encode("utf-8"),
                       file_name=f"{company.registration_number}_dossier.html", mime="text/html", key="dos_export")
    st.caption("Self-contained page with the header, flags, pain points, financials, cost structure, people and ownership.")

    st.markdown("---")
    st.subheader("💬 Ask the AI about this company")
    st.caption("Read-only. Answers come from this company's records through the same tools the AI Assistant page uses.")
    render_company_chat(db, company)

    st.markdown("---")
    cd.sec_pilot_history(ctx)
    cd._render_manual_entry_form(db, company, ctx.indicator_defs)
    st.markdown("---")
    render_paid_pulls(db, company)


# --------------------------------------------------------------------------------------------------------- data

def _tab_data(ctx: cd.DossierContext, shared: dict) -> None:
    st.subheader("🕳️ Data gaps")
    gaps = dd.gap_rows(ctx.indicator_defs, ctx.sig_dict, _gap_plan())
    if gaps:
        st.caption("Scored indicators with no value for this company, heaviest first, and where the data would come from "
                   "(see scripts/indicator_gaps.py). Filling the top rows moves the scores most.")
        st.dataframe(pd.DataFrame(gaps), hide_index=True, use_container_width=True, height=min(480, 60 + 35 * len(gaps)),
                     column_config={"Where the data would come from": st.column_config.TextColumn(width="large")})
    else:
        st.success("Every scored indicator has a value for this company.")
    st.markdown("---")
    cd.sec_raw_data(ctx)
    st.markdown("---")
    cd.sec_signal_audit(ctx)
    cd._render_signal_evidence(ctx.sig_dict, ctx.indicator_defs)
    cd.sec_context_tags(ctx)


def _tab_notes(ctx: cd.DossierContext, shared: dict) -> None:
    from views.company_notes_panel import render_notes_section
    render_notes_section(ctx.db, ctx.company)


TAB_RENDERERS = {
    "overview": _tab_overview, "financials": _tab_financials, "costs": _tab_costs, "diagnosis": _tab_diagnosis,
    "people": _tab_people, "market": _tab_market, "actions": _tab_actions, "data": _tab_data, "notes": _tab_notes,
}


# --------------------------------------------------------------------------------------------------------- page

def render_company_detail_page(db: Session):
    st.title("🏢 Company Intelligence")
    st.caption("Everything about one company in one place: financials, costs, diagnosis, people, market, actions and the full audit trail.")

    rows = db.query(Company.id, Company.legal_name, Company.registration_number, Company.country, Company.segment) \
        .order_by(Company.legal_name).all()
    if not rows:
        st.warning("No companies found in database. Use **🛠️ Data Admin** to add target companies.")
        return
    _consume_open_request(rows)
    st.session_state.setdefault(TAB_KEY, "overview")

    col_sel1, col_sel2 = st.columns([1, 3])
    with col_sel1:
        country_filter = st.selectbox("Filter by Country", ["All Countries", "Germany 🇩🇪", "Italy 🇮🇹"], key="detail_country_filter")
    filtered = rows
    if country_filter == "Germany 🇩🇪":
        filtered = [r for r in rows if (r.country or "Germany") == "Germany"]
    elif country_filter == "Italy 🇮🇹":
        filtered = [r for r in rows if r.country == "Italy"]
    if not filtered:
        st.info(f"No companies found for {country_filter}.")
        return
    with col_sel2:
        names = {company_label(r.legal_name, r.registration_number, r.country, r.segment): r.id for r in filtered}
        selected = st.selectbox("Select Target Company", list(names), key="detail_company_select")

    company = db.query(Company).filter_by(id=names[selected]).first()
    ctx = cd.build_context(db, company)
    shared = _load_shared(ctx)

    st.markdown(_CSS, unsafe_allow_html=True)
    _render_header(ctx, shared)
    TAB_RENDERERS[st.session_state.get(TAB_KEY) or "overview"](ctx, shared)


# The inventory of every information layer the old Company Intelligence page, its Raw Data tab, the Shortlist Gate's
# per-company block, the Cost Structure X-ray and the assistant offered - and the dossier section that now carries
# it. tests/test_company_dossier.py asserts each entry is rendered; scripts-side checks compare against the old page.
DOSSIER_LAYERS = {
    # old Company Intelligence tab ------------------------------------------------------------------------------
    "score cards (Need / Readiness / completeness / shortlist status)": ("diagnosis", "sec_score_cards"),
    "why Need / why Readiness breakdowns": ("diagnosis", "sec_score_why"),
    "pain points": ("diagnosis", "sec_pain_points"),
    "identity & registry (+ parent, subsidiaries)": ("overview", "sec_identity"),
    "shortlist status select + update": ("actions", "render_shortlist_controls"),
    "sync live APIs / run deep crawlers": ("actions", "render_api_and_crawler_row"),
    "named competitors": ("market", "sec_competitors"),
    "deep crawler coverage": ("market", "sec_crawler_coverage"),
    "product intelligence": ("market", "sec_product_intelligence"),
    "public contract evidence (TED)": ("market", "sec_public_contracts"),
    "published recalls (FDA)": ("market", "sec_recalls"),
    "patent activity": ("market", "sec_patents"),
    "crawled intelligence (+ parent/group card)": ("market", "_render_crawled_intelligence"),
    "AI intelligence report": ("overview", "render_ai_report"),
    "financial profile (growth + levels)": ("financials", "sec_financial_profile"),
    "full financial & cost indicator table": ("financials", "sec_full_financial_table"),
    "indicative valuation": ("financials", "sec_valuation"),
    "data trends from raw data": ("financials", "sec_data_trends"),
    "score breakdown by category": ("diagnosis", "sec_category_breakdown"),
    "people & ownership roster": ("people", "sec_people"),
    "family & succession": ("people", "sec_family_succession"),
    "management composition": ("people", "sec_management_composition"),
    "pilot history": ("actions", "sec_pilot_history"),
    "manual signal entry": ("actions", "_render_manual_entry_form"),
    "scored signal audit by category": ("data", "sec_signal_audit"),
    "signal evidence": ("data", "_render_signal_evidence"),
    "context & moderator tags": ("data", "sec_context_tags"),
    # old Raw Data & Mapping tab --------------------------------------------------------------------------------
    "provenance, raw datasets & mapping editor": ("data", "sec_raw_data"),
    # other pages ------------------------------------------------------------------------------------------------
    "cost structure X-ray (Cost Structure page)": ("costs", "render_company_xray"),
    "standard crawl / deep-dive crawl (Shortlist Gate)": ("actions", "render_standard_and_deep_dive"),
    "paid pulls Bundesanzeiger / Kununu (Shortlist Gate)": ("actions", "render_paid_pulls"),
    "ask the assistant about a company (AI Assistant page)": ("actions", "render_company_chat"),
    # new layers --------------------------------------------------------------------------------------------------
    "overview: KPIs, flags, top pain points, cost mix, AI findings, coverage": ("overview", "_tab_overview"),
    "financial statement + ratios + charts": ("financials", "_render_statement"),
    "ownership & group (AIDA)": ("people", "_render_ownership"),
    "data gaps with sources": ("data", "gap_rows"),
    "one-page export": ("actions", "build_html"),
    "team notes, live collaboration, send-to-AI": ("notes", "render_notes_section"),
}
