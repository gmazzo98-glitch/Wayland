"""
The Company Intelligence dossier: every section renders on a fully populated company, nothing the old page, its Raw
Data tab, the Shortlist Gate's per-company block, the Cost Structure X-ray or the assistant offered has gone missing,
cross-page navigation lands on the right company and tab, and the new data layers (statement, flags, gaps, ownership,
export) compute what they claim.
"""

import inspect
import textwrap
from datetime import date, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from streamlit.testing.v1 import AppTest

import dossier_data as dd
import dossier_export
from indicators import seed_indicator_definitions
import valuation_models  # noqa: F401  (registers the valuation tables on Base.metadata)
from models import (Base, Company, CompanyPerson, Competitor, PilotOutcome, RawImportRecord, SignalRecord)
from painpoints import seed_pain_point_definitions

_ROOT = str(Path(__file__).resolve().parent.parent)
NOW = datetime.utcnow()

AIDA_BLOB = {
    "accounts_close": "2024-12-31T00:00:00", "derived_last_accounts_year": 2024.0,
    "revenue_latest": 30000.0, "revenue_y-1": 27000.0, "revenue_y-2": 25000.0,
    "production_value_latest": 32000.0, "production_value_y-1": 28000.0, "production_value_y-2": 26000.0,
    "gross_margin_latest": 15000.0, "gross_margin_y-1": 13000.0, "gross_margin_y-2": 12000.0,
    "ebitda_latest": 6000.0, "ebitda_y-1": 4500.0, "ebitda_y-2": 4000.0,
    "ebit_latest": 5000.0, "ebit_y-1": 3500.0, "ebit_y-2": 3000.0,
    "net_income_latest": 3600.0, "net_income_y-1": 2500.0, "net_income_y-2": 2000.0,
    "total_assets_latest": 22000.0, "total_assets_y-1": 20000.0, "total_assets_y-2": 19000.0,
    "equity_latest": 11000.0, "equity_y-1": 9000.0, "equity_y-2": 8000.0,
    "total_debt_latest": 7000.0, "cash_latest": 5000.0, "cash_y-1": 4000.0, "cash_y-2": 3500.0,
    "employees_latest": 90.0, "employees_y-1": 88.0, "employees_y-2": 80.0,
    "personnel_costs_latest": 7500.0, "materials_latest": 12000.0, "production_costs_latest": 27000.0,
    "interest_coverage_latest": 20.0, "leverage_ratio_latest": 2.0,
    "group_size": 3.0, "consolidation_code": "U1", "bvd_independence_class": "D",
    "shareholders": [{"name": "MR ROSSI", "type": "Persone fisiche o famiglie", "country": "IT", "direct": 75.0, "total": None}],
    "controlling_shareholders": [{"name": "MR ROSSI", "type": "Persone fisiche o famiglie", "level": "1"}],
    "participations": [{"name": "SUB SRL", "type": "Societa"}], "procedures": [],
}
COST_LINES = {"materials": 12000.0, "services": 6000.0, "personnel": 7500.0, "third_party_assets": 300.0, "depreciation": 900.0,
              "inventory_change": -200.0, "risk_provisions": 100.0, "other_provisions": 0.0, "other_operating": 400.0}


def _seed(tmp_path):
    db_path = (tmp_path / "dossier.db").as_posix()
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        seed_indicator_definitions(db)
        seed_pain_point_definitions(db)
        db.add(Company(id="parent1", legal_name="BigCo Holding S.p.A.", registration_number="IT00000000099", country="Italy",
                       segment="Midcap", nace_code="282999", need_score=42.0, readiness_score=58.0))
        c = Company(id="c1", legal_name="Rossi Macchine S.r.l.", registration_number="IT00000000011", country="Italy",
                    segment="SME", nace_code="282999", sector_name="Machinery", region="Lombardia", legal_form="SRL",
                    headcount=90, website_url="https://rossi.example", shortlist_status="shortlisted",
                    parent_company_id="parent1")
        db.add(c)
        db.add(Company(id="c2", legal_name="Bianchi Pompe S.p.A.", registration_number="IT00000000012", country="Italy",
                       segment="SME", nace_code="282999"))
        db.commit()
        db.add(RawImportRecord(company_id="c1", dataset_name="AIDA Raw Exports", raw_row=AIDA_BLOB, source_filename="aida.xls"))
        total = sum(COST_LINES.values())
        lines = {k: {"latest": v, "y-1": v * 0.9, "y-2": v * 0.8, "y-3": v * 0.7} for k, v in COST_LINES.items()}
        totals = {p: total * f for p, f in (("latest", 1), ("y-1", .9), ("y-2", .8), ("y-3", .7))}
        db.add(RawImportRecord(company_id="c1", dataset_name="AIDA Cost Breakdown Supplement", source_filename="Risultati",
                               raw_row={"lines": lines, "total": totals, "detail": {}, "revenue_year_matched": {"latest": 30000.0},
                                        "revenue_ratio_available": True, "reconciliation_gap": {}}))
        db.add(SignalRecord(company_id="c1", signal_key="cost_total_k", numeric_value=total, status="present",
                            source="AIDA Cost Breakdown Supplement", is_simulated=False, fetched_at=NOW))
        for cat, v in COST_LINES.items():
            db.add(SignalRecord(company_id="c1", signal_key=f"cost_{cat}_k", numeric_value=v, status="present",
                                source="AIDA Cost Breakdown Supplement", is_simulated=False, fetched_at=NOW))
            db.add(SignalRecord(company_id="c1", signal_key=f"cost_{cat}_share", numeric_value=v / total * 100, status="present",
                                source="AIDA Cost Breakdown Supplement", is_simulated=False, fetched_at=NOW))
        for i in range(6):  # peers so the sector comparison has a median
            pid = f"p{i}"
            db.add(Company(id=pid, legal_name=f"Peer {i}", registration_number=f"IT0000000010{i}", country="Italy", segment="SME",
                           nace_code="282999"))
            db.flush()
            for cat, v in COST_LINES.items():
                db.add(SignalRecord(company_id=pid, signal_key=f"cost_{cat}_share", numeric_value=v / total * 100 * (1 + i / 20),
                                    status="present", source="AIDA Cost Breakdown Supplement", is_simulated=False, fetched_at=NOW))
        db.add(SignalRecord(company_id="c1", signal_key="revenue_trend", numeric_value=20.0, status="present", source="AIDA Raw Exports",
                            is_simulated=False, fetched_at=NOW))
        db.add(RawImportRecord(company_id="c1", dataset_name="crawler_product_catalog", source_filename="crawler",
                               raw_row={"products_count": 12, "categories": ["pumps"], "crawled_pages_count": 9, "products": []}))
        db.add(RawImportRecord(company_id="c1", dataset_name="crawler_ted_contract_awards", source_filename="crawler",
                               raw_row={"awards": [{"title": "Pump supply", "url": "https://ted.example/1", "buyer": "City", "published": "2025-01-01"}]}))
        db.add(RawImportRecord(company_id="c1", dataset_name="crawler_fda_recalls", source_filename="crawler",
                               raw_row={"recalls": [{"recall_number": "Z-1", "url": "https://fda.example/1", "classification": "Class II",
                                                     "report_date": "20250101", "reason": "label"}]}))
        db.add(RawImportRecord(company_id="c1", dataset_name="crawler_epo_ops_search", source_filename="crawler",
                               raw_row={"total_result_count": 3, "publication_sample": [{"title": "A pump", "publication": "EP1",
                                                                                           "publication_date": "20240101", "applicants": ["Rossi"]}]}))
        db.add(RawImportRecord(company_id="c1", dataset_name="crawler_company_website", source_filename="crawler",
                               raw_row={"product_types": ["pumps"], "product_lines_count": 1, "crawled_pages_count": 9}))
        db.add(Competitor(company_id="c1", name="Rival SpA", homepage_url="https://rival.example"))
        db.add(CompanyPerson(company_id="c1", dataset_name="roster", role_group="Board", full_name="Mario Rossi", role="CEO", age=58,
                             position_in_row=0, raw_fields={}))
        db.add(CompanyPerson(company_id="c1", dataset_name="roster", role_group="Board", full_name="Luca Rossi", role="Director", age=29,
                             position_in_row=1, raw_fields={}))
        db.add(PilotOutcome(company_id="c1", pilot_label="Pilot A", started_at=NOW, outcome_success=True))
        db.commit()
    return db_path


def _page_script(db_path, session_setup=""):
    return textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {_ROOT!r})
        import streamlit as st
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        {session_setup}
        from views.company_dossier import render_company_detail_page
        engine = create_engine({f"sqlite:///{db_path}"!r}, connect_args={{"check_same_thread": False}})
        render_company_detail_page(sessionmaker(bind=engine)())
    """)


def _texts(app):
    out = []
    for kind in ("subheader", "header", "markdown", "caption", "expander", "metric"):
        for el in app.get(kind):
            out.append(str(getattr(el, "value", None) or getattr(el, "label", "")))
            if kind == "expander":
                out.append(el.label)
    return "\n".join(out)


@pytest.fixture(scope="module")
def dossier(tmp_path_factory):
    db_path = _seed(tmp_path_factory.mktemp("dossier"))
    app = AppTest.from_string(_page_script(db_path, 'st.session_state["dossier_open_id"] = "c1"'), default_timeout=120)
    app.run()
    assert not app.exception, [e.value for e in app.exception]
    return app


def _open(app, tab):
    app.session_state["dossier_tab"] = tab
    return app.run()


EXPECTED = {
    "overview": ["Flags & strengths", "Top pain points", "Where the money goes", "AI report", "Data coverage", "Identity & Registry"],
    "financials": ["Financial statement", "Income statement", "Balance sheet", "Financial Profile",
                   "Full Financial & Cost Structure Table", "Indicative valuation"],
    "costs": ["Cost base over the filed years", "This company vs its peers"],
    "diagnosis": ["Pain Points", "Score Breakdown by Category", "Why is Need", "Why is Readiness"],
    "people": ["People & Ownership", "Family & Succession", "Management Composition", "Ownership & group", "Shareholders"],
    "market": ["Named Competitors", "Crawled Intelligence", "Product intelligence", "Public contract evidence",
               "Published US product recalls", "Patent activity snapshot", "Deep crawler coverage", "Parent / Group Relationship"],
    "actions": ["Shortlist & collection", "One-page export", "Ask the AI about this company", "Pilot History",
                "Manually Record a Signal", "Germany-only"],
    "data": ["Data gaps", "Which sources actually populated live data", "Raw datasets", "Scored Signal Audit",
             "Context & Moderator Tags"],
}


@pytest.mark.parametrize("tab", list(EXPECTED))
def test_every_section_renders_on_a_fully_populated_company(dossier, tab):
    app = _open(dossier, tab)
    assert not app.exception, [e.value for e in app.exception]
    text = _texts(app)
    for needle in EXPECTED[tab]:
        assert needle in text, f"{tab}: missing {needle!r}"


def test_the_header_is_always_there_with_name_scores_and_the_section_bar(dossier):
    for tab in ("overview", "data"):
        app = _open(dossier, tab)
        assert any("Rossi Macchine" in md.value for md in app.markdown)
        labels = {m.label for m in app.metric}
        assert {"Need", "Readiness", "Data"} <= labels
        assert any(w.key == "dos_hdr_stage_c1_shortlisted" for w in app.selectbox)  # the stage is changeable from the header
        assert app.session_state["dossier_tab"] == tab


def test_layer_inventory_is_wired_to_real_functions_in_the_right_tab():
    import views.company_dossier as dossier_page
    from views import company_actions, company_detail, cost_structure
    namespaces = [dossier_page, company_detail, company_actions, cost_structure, dd, dossier_export,
                  __import__("views.ai_assistant", fromlist=["x"])]
    for layer, (tab, fn_name) in dossier_page.DOSSIER_LAYERS.items():
        assert tab in dossier_page.SECTION_LABEL, f"{layer}: unknown tab {tab}"
        assert any(hasattr(ns, fn_name) for ns in namespaces), f"{layer}: {fn_name} does not exist"
        renderer = dossier_page.TAB_RENDERERS[tab]
        if fn_name == renderer.__name__:
            continue
        # an indirect caller counts: the tab function may delegate to a helper that calls the section
        sources = inspect.getsource(renderer)
        for helper in ("_snapshot", "_cost_mix_bar", "_render_statement", "_render_ownership"):
            if helper in sources:
                sources += inspect.getsource(getattr(dossier_page, helper))
        assert fn_name in sources, f"{layer}: {fn_name} is not rendered by the {tab} tab"
    assert set(dossier_page.TAB_RENDERERS) == set(dossier_page.SECTION_LABEL)


def test_every_old_page_section_function_is_still_used_by_some_tab():
    """Each sec_* extracted from the old page must be called by exactly the dossier, so none is left orphaned."""
    import views.company_dossier as dossier_page
    from views import company_detail
    body = inspect.getsource(dossier_page)
    for name, obj in inspect.getmembers(company_detail, inspect.isfunction):
        if name.startswith("sec_"):
            assert f"cd.{name}(" in body, f"{name} is no longer rendered anywhere"


# ---------------------------------------------------------------------------------------------- navigation

def test_open_request_selects_the_company_and_the_tab(tmp_path):
    db_path = _seed(tmp_path)
    app = AppTest.from_string(_page_script(db_path, 'st.session_state["dossier_open_id"] = "c2"; st.session_state["dossier_open_tab"] = "costs"'),
                              default_timeout=120).run()
    assert not app.exception, [e.value for e in app.exception]
    sel = next(w for w in app.selectbox if w.label == "Select Target Company")
    assert sel.value.startswith("Bianchi Pompe S.p.A. (IT00000000012)")
    assert app.session_state["dossier_tab"] == "costs"
    assert "dossier_open_id" not in app.session_state  # consumed once, never sticky


def test_open_dossier_button_callback_sets_navigation_state(tmp_path):
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {_ROOT!r})
        from dossier_nav import open_dossier_button
        open_dossier_button("c1", "Go", key="go", tab="actions")
    """)
    app = AppTest.from_string(script).run()
    app.button[0].click().run()
    assert app.session_state["nav_page"] == "🏢 Company Intelligence"
    assert app.session_state["dossier_open_id"] == "c1" and app.session_state["dossier_open_tab"] == "actions"


def test_overview_buttons_switch_section(tmp_path):
    db_path = _seed(tmp_path)
    app = AppTest.from_string(_page_script(db_path, 'st.session_state["dossier_open_id"] = "c1"'), default_timeout=120).run()
    next(b for b in app.button if b.key == "ov_to_costs").click().run()
    assert app.session_state["dossier_tab"] == "costs"


# ------------------------------------------------------------------------------------------------ data layers

def test_financial_statement_labels_years_and_computes_only_what_both_inputs_allow():
    fin = dd.financial_statement(AIDA_BLOB)
    assert fin["labels"] == {"latest": "FY2024", "y-1": "FY2023", "y-2": "FY2022"}
    R = fin["ratios"]
    assert R["revenue_growth"]["latest"] == pytest.approx((30000 - 27000) / 27000 * 100)
    assert R["revenue_growth"]["y-2"] is None                      # no older year to compare with
    assert R["ebitda_margin"]["latest"] == pytest.approx(20.0)
    assert R["equity_ratio"]["latest"] == pytest.approx(50.0)
    assert R["revenue_per_employee"]["latest"] == pytest.approx(30000 / 90)
    assert fin["lines"]["total_debt"]["y-1"] is None and R["equity_ratio"]["y-2"] == pytest.approx(8000 / 19000 * 100)
    assert dd.financial_statement(None) is None
    assert dd.fiscal_year_labels({})["latest"] == "Latest"        # never a guessed year


def test_flags_are_stated_with_their_thresholds_and_sorted_risk_first():
    blob = dict(AIDA_BLOB, **{"equity_latest": -100.0, "net_income_latest": -50.0, "net_income_y-1": -10.0,
                              "revenue_latest": 20000.0, "interest_coverage_latest": 0.8})
    sig = {"distress_procedure": (1.0, "present"), "last_accounts_year": (2022.0, "present")}
    out = dd.flags(dd.financial_statement(blob), sig, today=date(2026, 10, 19))
    texts = " | ".join(f["text"] for f in out)
    for needle in ("distress procedure", "2022", "Negative equity", "second consecutive loss", "fell 26%", "Interest coverage only 0.8x"):
        assert needle in texts, needle
    levels = [f["level"] for f in out]
    assert levels == sorted(levels, key=["risk", "watch", "strength"].index)
    good = dd.flags(dd.financial_statement(AIDA_BLOB), {}, today=date(2026, 10, 19))
    assert any(f["level"] == "strength" for f in good) and not any(f["level"] == "risk" for f in good)


def test_gap_rows_list_only_unchecked_scored_indicators_heaviest_first():
    class S:  # a SignalRecord stand-in
        def __init__(self, status): self.status = status
    defs = {"a": {"label": "A", "axis": "need", "weight": 1.0, "category": "x"},
            "b": {"label": "B", "axis": "readiness", "weight": 3.0, "category": "x"},
            "c": {"label": "C", "axis": "context", "weight": 0.0, "category": "x"},
            "d": {"label": "D", "axis": "need", "weight": 5.0, "category": "x"}}
    rows = dd.gap_rows(defs, {"d": S("present"), "a": S("not_yet_checked")},
                       plan={"b": ("RUN", "the website crawler", "Phase 7")})
    assert [r["Indicator"] for r in rows] == ["B", "A"]            # D is checked, C is context
    assert rows[0]["Source class"] == "RUN" and rows[1]["Source class"] == "UNCLASSIFIED"


def test_ownership_extracts_shareholders_group_and_participations():
    own = dd.ownership(AIDA_BLOB)
    assert own["shareholders"][0]["Name"] == "MR ROSSI" and own["shareholders"][0]["Direct %"] == 75.0
    assert own["group_size"] == 3.0 and own["independence"] == "D" and own["participations"][0]["Name"] == "SUB SRL"
    assert dd.ownership(None)["shareholders"] == []


def test_export_is_escaped_self_contained_and_shows_the_dossier_numbers():
    snap = {"company": {"name": "Evil <script>alert(1)</script> SRL", "vat": "IT1", "status": "Candidate"},
            "scores": {"need": 61.2, "readiness": 40.0, "completeness": 55.0},
            "kpis": [("Revenue", "€30.0M")], "flags": [{"level": "risk", "text": "Negative equity <b>"}],
            "pain_points": [{"label": "Cost pressure", "status": "Severe", "score": 80.0, "headline": "services 40%"}],
            "statement": dd.financial_statement(AIDA_BLOB), "cost": None, "ownership": dd.ownership(AIDA_BLOB),
            "people": [{"name": "Mario Rossi", "role": "CEO", "age": 58}], "brief_findings": ["Exports to Germany"]}
    html = dossier_export.build_html(snap)
    assert "<script>" not in html and "&lt;script&gt;" in html and "&lt;b&gt;" in html
    assert "http://" not in html and "https://" not in html.replace("https://rossi", "")   # nothing loaded from outside
    for needle in ("61", "FY2024", "30,000", "MR ROSSI", "Mario Rossi", "Exports to Germany", "Cost pressure", "No cost breakdown imported"):
        assert needle in html, needle


def test_data_admin_page_keeps_every_import_and_management_tab(tmp_path):
    db_path = _seed(tmp_path)
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {_ROOT!r})
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from views.data_admin import render_data_admin_page
        engine = create_engine({f"sqlite:///{db_path}"!r}, connect_args={{"check_same_thread": False}})
        render_data_admin_page(sessionmaker(bind=engine)())
    """)
    app = AppTest.from_string(script, default_timeout=120).run()
    assert not app.exception, [e.value for e in app.exception]
    assert [t.label for t in app.tabs] == ["➕ Add Single Company", "📁 Bulk CSV Import", "🔗 Flexible Data Import",
                                           "AIDA single company report upload", "Balance sheet upload (PDF)",
                                           "👥 Import People & Ownership", "🧑‍💼 Flexible People Import", "🗑️ Manage Companies"]


def test_app_navigation_exposes_the_dossier_target_key_and_the_admin_page():
    src = (Path(_ROOT) / "app.py").read_text(encoding="utf-8")
    assert 'key="nav_page"' in src and "🛠️ Data Admin" in src and "views.company_dossier" in src
    from dossier_nav import DOSSIER_PAGE
    assert f'"{DOSSIER_PAGE}"' in src


def test_accounts_age_flag_is_calibrated_to_the_filing_calendar():
    """In autumn 2026 the newest normally-filed accounts are FY2025: FY2025 is current (no flag), FY2024 is a watch
    (a year behind), FY2023 or older is stale. Every flag also carries a short header chip."""
    def flag_for(year):
        return dd.flags(None, {"last_accounts_year": (float(year), "present")}, today=date(2026, 10, 19))
    assert flag_for(2025) == []
    watch = flag_for(2024)
    assert [f["level"] for f in watch] == ["watch"] and watch[0]["chip"] == "Accounts FY2024"
    assert "FY2025 is not in AIDA yet" in watch[0]["text"]
    stale = flag_for(2023)
    assert [f["level"] for f in stale] == ["risk"] and "stale" in stale[0]["chip"]
    every = dd.flags(dd.financial_statement(AIDA_BLOB), {"distress_procedure": (1.0, "present")}, today=date(2026, 10, 19))
    assert all(f["chip"] and len(f["chip"]) <= 30 for f in every)


# ------------------------------------------------------------------- one home per feature (no cloned pages)

EXPECTED_NAV = ["⚙️ Pipeline & Source Health", "🏢 Company Intelligence", "🎯 Targets & Shortlist", "📊 Portfolio Analysis",
                "⚖️ Indicator Weights", "🖥️ Crawler Setup", "📖 Site Evidence Reader", "🤖 AI Assistant", "🛠️ Data Admin"]


def _nav_labels():
    import re
    src = (Path(_ROOT) / "app.py").read_text(encoding="utf-8")
    block = src[src.index("page_selection = st.sidebar.radio("):src.index('key="nav_page"')]
    return re.findall(r'^\s+"([^"]+)"', block, flags=re.M)[1:]  # [0] is the radio's own label


def test_sidebar_has_one_page_per_job_and_every_entry_is_routed():
    src = (Path(_ROOT) / "app.py").read_text(encoding="utf-8")
    nav = _nav_labels()
    assert nav == EXPECTED_NAV
    for label in nav:
        assert f'page_selection == "{label}"' in src, f"{label} is in the sidebar but never routed"
    # the pages whose per-company halves moved into Company Intelligence no longer exist on their own
    for gone in ("🩺 Pain Points", "💸 Cost Structure", "💶 Valuation", "💰 Shortlist Gate & Paid Pulls", "🎯 Scored Target Matrix"):
        assert gone not in nav


def test_company_actions_exist_only_in_the_dossier_not_in_a_second_page():
    """A crawl button, the AI report or a paid pull must be rendered from exactly one place: the dossier."""
    users = {}
    for name in ("render_standard_and_deep_dive", "render_paid_pulls", "render_ai_report", "render_shortlist_controls",
                 "render_api_and_crawler_row", "render_company_xray", "render_company_valuation", "render_company_pain_points"):
        users[name] = sorted(p.name for p in (Path(_ROOT) / "views").glob("*.py")
                             if f"{name}(" in p.read_text(encoding="utf-8") and f"def {name}(" not in p.read_text(encoding="utf-8"))
    for name, files in users.items():
        assert files in (["company_dossier.py"], ["company_detail.py"]) or files == sorted(set(files)) and set(files) <= {
            "company_dossier.py", "company_detail.py"}, f"{name} is rendered from {files}"


def _page_app(db_path, body):
    return AppTest.from_string(textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {_ROOT!r})
        import valuation_models  # noqa: F401
        from database import init_db
        init_db()  # some pages (matrix coverage strip, indicator weights) read the app's own DATABASE_URL, not `db`
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        engine = create_engine({f"sqlite:///{db_path}"!r}, connect_args={{"check_same_thread": False}})
        db = sessionmaker(bind=engine)()
    """) + textwrap.dedent(body), default_timeout=180)


def test_targets_page_holds_the_matrix_and_the_gate_and_the_gate_no_longer_clones_company_actions(tmp_path):
    db_path = _seed(tmp_path)
    app = _page_app(db_path, "from views.targets import render_targets_page\nrender_targets_page(db)").run()
    assert not app.exception, [e.value for e in app.exception]
    assert any("Targets & Shortlist" in t.value for t in app.title)
    app.session_state["targets_section"] = "gate"
    app = app.run()
    assert not app.exception, [e.value for e in app.exception]
    text = _texts(app)
    for needle in ("The shortlist", "Candidates recommended for shortlisting", "Parked", "In-depth work on a shortlisted company"):
        assert needle in text, needle
    labels = {b.label for b in app.button}
    assert not any(l.startswith(("Run deep-dive", "Run standard crawl", "Generate / refresh", "📥")) for l in labels), labels
    assert any(w.label == "Open a shortlisted company's dossier" for w in app.selectbox)


def test_portfolio_analysis_has_three_cross_company_sections_and_no_per_company_clones(tmp_path):
    db_path = _seed(tmp_path)
    app = _page_app(db_path, "from views.portfolio_analysis import render_portfolio_analysis_page\nrender_portfolio_analysis_page(db)").run()
    assert not app.exception, [e.value for e in app.exception]
    assert [t.value for t in app.title] == ["📊 Portfolio Analysis"]          # one title: the inner pages stay quiet
    seen = {}
    for section in ("pain", "costs", "valuation"):
        app.session_state["pa_section"] = section
        app = app.run()
        assert not app.exception, (section, [e.value for e in app.exception])
        seen[section] = [t.label for t in app.tabs]
        assert [t.value for t in app.title] == ["📊 Portfolio Analysis"], section
    assert seen["costs"] == ["🏭 Portfolio", "🚨 Outliers & movers", "🗺️ Sector map"]
    assert seen["valuation"] == ["📊 Universe screen", "⚙️ Assumptions & data"]
    assert seen["pain"] == ["📊 Portfolio", "🧩 Pain-point catalog", "🕳️ Data gaps & new indicators"]
    app.session_state["pa_section"] = "costs"
    labels = {w.label for w in app.selectbox}
    assert "Company (type to search)" not in labels                              # the per-company picker moved to the dossier
