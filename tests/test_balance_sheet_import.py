import json
import re

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import balance_sheet_pdf as bs
import financial_ledger as fl
from aida_single_report import import_report, parse_report
from indicators import seed_indicator_definitions
from models import Base, Company, RawImportRecord, SignalRecord
from test_report_upload import report_file

FISCAL = "06038441215"


def pages(year, current, prior, language="it", fmt=lambda n: f"{n:,}".replace(",", ".")):
    """A statement shaped like the Mondial Bony PDFs (two columns, bracketed expenses, a wrapped label)."""
    d1, d2 = f"31-12-{year}", f"31-12-{year - 1}"
    cur, pri = current, prior

    def n(value):
        return f"({fmt(-value)})" if value < 0 else fmt(value)

    return [
        dict(page=1, method="text", text=f"MONDIAL BONY SERVICE SPA\nBilancio di esercizio al 31/12/{year}\nCodice Fiscale {FISCAL}\n"
                                         "Gli importi presenti sono espressi in Euro\nPag. 1"),
        dict(page=2, method="text", text=f"Bilancio al 31/12/{year} MONDIAL BONY SERVICE SPA\nSede in NAPOLI - VIA X\n"
                                         f"STATO PATRIMONIALE ATTIVO {d1} {d2}\n"
                                         f"10. Cassa e disponibilita liquide {n(cur['cash'])} {n(pri['cash'])}\n"
                                         f"TOTALE ATTIVO {n(cur['assets'])} {n(pri['assets'])}\n"
                                         f"STATO PATRIMONIALE PASSIVO {d1} {d2}\n"
                                         f"170. Utile (Perdita) d'esercizio {n(cur['profit'])} {n(pri['profit'])}\n"
                                         f"TOTALE PASSIVO E PATRIMONIO NETTO {n(cur['assets'])} {n(pri['assets'])}\nPag. 2"),
        dict(page=3, method="text", text=f"Bilancio al 31/12/{year} MONDIAL BONY SERVICE SPA\nCONTO ECONOMICO {d1} {d2}\n"
                                         f"40. Commissioni attive {n(cur['revenue'])} {n(pri['revenue'])}\n"
                                         f"a) spese per il personale {n(-cur['staff'])} {n(-pri['staff'])}\n"
                                         "260. UTILE (PERDITA) DELL'ATTIVITA' CORRENTE AL LORDO DELLE\n"
                                         f"{n(cur['profit'] * 2)} {n(pri['profit'] * 2)}\nIMPOSTE\n"
                                         f"270. Imposte sul reddito {n(-cur['profit'])} {n(-pri['profit'])}\n"
                                         f"280. UTILE (PERDITA) DELL'ATTIVITA' CORRENTE AL NETTO DELLE IMPOSTE {n(cur['profit'])} {n(pri['profit'])}\n"
                                         f"300. UTILE (PERDITA) D'ESERCIZIO {n(cur['profit'])} {n(pri['profit'])}\nPag. 3"),
    ]


Y25 = dict(cash=952074, assets=3965121, profit=109732, revenue=5973306, staff=998618)
Y24 = dict(cash=1127325, assets=4228215, profit=203823, revenue=6761318, staff=952025)
Y23 = dict(cash=1724814, assets=4473344, profit=259195, revenue=6455029, staff=935936)


def statement(year, current, prior, **kwargs):
    return bs.parse_pages(pages(year, current, prior, **kwargs), f"bilancio {year}.pdf", f"sha{year}")


def mapping_of(st, profile=None):
    return {k: v["target"] for k, v in bs.suggest_mapping(st, profile).items()}


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        seed_indicator_definitions(session)
        yield session


AIDA_FACTS = {"revenue_latest": 6761.0, "revenue_y-1": 6455.0, "revenue_y-2": 5090.0, "net_income_latest": 204.0,
              "net_income_y-1": 259.0, "employees_latest": 18.0, "employees_y-1": 17.0, "employees_y-2": 20.0,
              "ebit_latest": 6761.0, "ebit_y-1": 6455.0, "ebit_y-2": 5090.0,
              "cash_latest": 1100.0, "total_assets_latest": 4229.0, "total_assets_y-1": 4474.0, "total_assets_y-2": 3511.0,
              "derived_last_accounts_year": 2024, "accounts_close": "2024-12-31"}


def with_aida_2024(db):
    """An AIDA single-company import for fiscal 2024 (thousand EUR), stored as the importer stores it."""
    from aida_import import derive_financial_signals
    from aida_single_report import DATASET_NAME
    company = Company(legal_name="Mondial Bony Service SPA", registration_number="IT" + FISCAL, country="Italy")
    db.add(company)
    db.flush()
    db.add(RawImportRecord(company_id=company.id, dataset_name=DATASET_NAME, raw_row=dict(AIDA_FACTS), mapping_snapshot={}))
    for name, draft in derive_financial_signals(AIDA_FACTS).items():
        if draft["value"] is not None:
            db.add(SignalRecord(company_id=company.id, signal_key=name, source=DATASET_NAME, status="present",
                                numeric_value=draft["value"], is_simulated=False))
    db.commit()


def with_bulk_aida_2024(db):
    """The same company as the bulk AIDA export holds it: blob and signals sourced "AIDA Raw Exports", plus the year signal."""
    from aida_import import DATASET_NAME as BULK, derive_financial_signals
    company = Company(legal_name="Mondial Bony Service SPA", registration_number="IT" + FISCAL, country="Italy", headcount=17,
                      headcount_source_tier="T1", segment="SME")
    db.add(company)
    db.flush()
    db.add(RawImportRecord(company_id=company.id, dataset_name=BULK, raw_row=dict(AIDA_FACTS), mapping_snapshot={}))
    for name, draft in derive_financial_signals(AIDA_FACTS).items():
        if draft["value"] is not None:
            db.add(SignalRecord(company_id=company.id, signal_key=name, source=BULK, status="present", numeric_value=draft["value"],
                                is_simulated=False))
    db.add(SignalRecord(company_id=company.id, signal_key="last_accounts_year", source=BULK, status="present", numeric_value=2024.0,
                        is_simulated=False))
    db.commit()
    return company


def test_numbers_dates_and_missing_brackets():
    assert bs.parse_number("1.127.325") == 1127325
    assert bs.parse_number("1,127,325") == 1127325
    assert bs.parse_number("(15,938") == -15938
    assert bs.parse_number("600.000,00") == 600000
    assert bs.parse_number("-") == 0
    assert bs.parse_date("12/31/2024", "en") == "2024-12-31"
    assert bs.parse_date("31-12-2025", "it") == "2025-12-31"
    assert bs.parse_date("December 31, 2024") == "2024-12-31"


def test_statement_lines_dates_and_checks():
    st = statement(2025, Y25, Y24)
    assert st["company"]["registration_number"] == "IT" + FISCAL
    assert st["period_end"] == "2025-12-31" and st["unit"] == "EUR"
    assert st["dates"] == ["2025-12-31", "2024-12-31"]
    by_item = {l["item"]: l for l in st["lines"] if l["item"]}
    assert by_item["260"]["values"]["2024-12-31"] == 407646  # label on one line, numbers on the next, tail ignored
    assert "IMPOSTE" in by_item["260"]["label"]
    assert [c["ok"] for c in st["checks"]] and all(c["ok"] for c in st["checks"])
    staff = next(l for l in st["lines"] if "personale" in l["label"])
    assert staff["values"]["2025-12-31"] == -998618


def test_failed_totals_are_reported():
    broken = pages(2025, Y25, Y24)
    broken[1]["text"] = broken[1]["text"].replace("TOTALE ATTIVO 3.965.121", "TOTALE ATTIVO 3.965.000")
    st = bs.parse_pages(broken)
    assert any(not c["ok"] for c in st["checks"]) and any("Check failed" in w for w in st["warnings"])


def test_line_ids_do_not_depend_on_the_year_so_profiles_carry_over():
    a, b = statement(2025, Y25, Y24), statement(2024, Y24, Y23)
    assert [l["id"] for l in a["lines"]] == [l["id"] for l in b["lines"]]
    profile = {a["lines"][0]["id"]: "indicator:custom"}
    assert bs.suggest_mapping(b, profile)[a["lines"][0]["id"]]["origin"] == "profile"


def test_rule_suggestions_flag_the_uncertain_revenue_line():
    suggestions = bs.suggest_mapping(statement(2025, Y25, Y24))
    targets = {s["target"]: s for s in suggestions.values() if s["target"]}
    assert set(targets) == {"base:cash", "base:total_assets", "base:net_income", "base:revenue", "base:personnel_costs"}
    assert targets["base:revenue"]["confidence"] < 0.7 and targets["base:revenue"]["note"]
    # The balance-sheet copy of the result does not feed a second fact.
    assert sum(1 for s in suggestions.values() if s["target"] == "base:net_income") == 1


def test_ai_suggestions_are_validated_and_never_applied_silently():
    st = statement(2025, Y25, Y24)
    mapping = bs.suggest_mapping(st)
    open_id = next(l["id"] for l in st["lines"] if l["item"] == "270")
    answer = json.dumps([dict(id=open_id, target="ebitda", confidence=0.4, rationale="guess"),
                         dict(id="made-up", target="revenue", confidence=1), dict(id=open_id, target="nonsense", confidence=1)])
    out = bs.ai_suggestions(st, mapping, chat_fn=lambda messages: answer)
    assert list(out) == [open_id] and out[open_id]["origin"] == "ai" and out[open_id]["confidence"] == 0.4
    assert mapping[open_id]["target"] == ""


def test_pages_without_text_use_ocr_and_are_labelled():
    class Doc:
        pass
    text = pages(2025, Y25, Y24)[1]["text"]
    st = bs.parse_pages([dict(page=1, method="ocr", text=text)])
    assert any("OCR" in w for w in st["warnings"])


def test_rollover_conflict_default_and_operator_override(db):
    with_aida_2024(db)
    st = statement(2025, Y25, Y24)
    plan = fl.plan_statement(db, st, mapping_of(st))
    merged = plan["merged"]
    assert merged["anchor"] == 2025 and merged["previous_anchor"] == 2024
    assert merged["labels"]["latest"] == "FY2025" and merged["labels"]["y-1"] == "FY2024"
    conflict = next(c for c in merged["conflicts"] if c["key"] == "cash|2024")
    assert conflict["chosen"] == fl.statement_dataset("2025-12-31")  # the balance sheet prevails by default
    assert merged["facts"]["total_assets_y-1"] == pytest.approx(4228.215)
    assert merged["facts"]["cash_y-1"] == pytest.approx(1127.325)
    # Corroborated, not conflicting: AIDA prints whole thousands (net result 204, total assets "4.229" = 4,229 k), and the filed
    # euro figures (203,823; 4,228,215) lie within one thousand of them.
    assert not any(c["key"] in {"net_income|2024", "total_assets|2024"} for c in merged["conflicts"])
    assert merged["series"]["total_assets"][2024]["corroborated_by"] == ["AIDA Single Company Report"]
    flipped = fl.plan_statement(db, st, mapping_of(st), decisions={"cash|2024": "AIDA Single Company Report"})
    assert flipped["merged"]["facts"]["cash_y-1"] == 1100.0


def test_import_rolls_labels_and_signals_and_keeps_missing_facts_missing(db):
    with_aida_2024(db)
    company = db.query(Company).one()
    before = {s.signal_key: s for s in db.query(SignalRecord).filter_by(company_id=company.id)}
    assert before["revenue_trend"].status == "present"
    st = statement(2025, Y25, Y24)
    result = fl.import_statement(db, st, mapping_of(st), profile_name="Std")
    assert result["anchor"] == 2025
    sig = {s.signal_key: s for s in db.query(SignalRecord).filter_by(company_id=company.id)}
    assert sig["total_assets"].numeric_value == pytest.approx(3965.121) and sig["total_assets"].source == fl.LEDGER_SOURCE
    # 2025 has no headcount or EBIT: results that used the 2024 figures as Latest are withdrawn, not carried over.
    for name in ("employee_growth", "ebit_margin", "revenue_per_employee"):
        assert sig[name].status == "not_yet_checked" and sig[name].numeric_value is None
    ledger = db.query(RawImportRecord).filter_by(company_id=company.id, dataset_name=fl.LEDGER_DATASET).one().raw_row
    assert ledger["derived_last_accounts_year"] == 2025 and "employees_latest" not in ledger
    assert ledger["employees_y-1"] == 18 and "employees" in ledger["_missing_at_anchor"]
    payload = json.loads(sig["total_assets"].raw_payload_ref)
    assert payload["raw_fields"][0]["file"] == "bilancio 2025.pdf" and payload["raw_fields"][0]["page"] == 2
    assert db.query(RawImportRecord).filter_by(company_id=company.id, dataset_name=fl.statement_dataset("2025-12-31")).count() == 1


def test_operator_can_keep_the_existing_signal_value(db):
    with_aida_2024(db)
    st = statement(2025, Y25, Y24)
    plan = fl.plan_statement(db, st, mapping_of(st))
    assert any(c["signal"] == "total_assets" and c["decide"] for c in plan["changes"])
    fl.import_statement(db, st, mapping_of(st), signal_replace={"total_assets": False})
    kept = db.query(SignalRecord).filter_by(signal_key="total_assets").one()
    assert kept.numeric_value == 4229 and kept.source == "AIDA Single Company Report"


def test_older_statement_after_a_newer_one_never_moves_the_anchor_back(db):
    with_aida_2024(db)
    new, old = statement(2025, Y25, Y24), statement(2024, Y24, Y23)
    fl.import_statement(db, new, mapping_of(new))
    result = fl.import_statement(db, old, mapping_of(old))
    assert result["anchor"] == 2025
    ledger = db.query(RawImportRecord).filter_by(dataset_name=fl.LEDGER_DATASET).one().raw_row
    assert ledger["total_assets_latest"] == pytest.approx(3965.121) and ledger["total_assets_y-2"] == pytest.approx(4473.344)
    again = fl.plan_statement(db, old, mapping_of(old))
    assert again["replaced_statement"]
    with pytest.raises(ValueError, match="already has a statement"):
        fl.import_statement(db, old, mapping_of(old))
    fl.import_statement(db, old, mapping_of(old), replace_statement=True)


def test_older_aida_report_uploaded_afterwards_keeps_the_merged_timeline(db):
    st = statement(2025, Y25, Y24)
    fl.import_statement(db, st, mapping_of(st))
    import_report(db, parse_report(report_file(), "aida.xlsx"))
    ledger = db.query(RawImportRecord).filter_by(dataset_name=fl.LEDGER_DATASET).one().raw_row
    assert ledger["derived_last_accounts_year"] == 2025
    assert db.query(SignalRecord).filter_by(signal_key="total_assets").one().numeric_value == pytest.approx(3965.121)


def test_unit_must_be_known_and_a_failed_import_changes_nothing(db):
    st = statement(2025, Y25, Y24)
    st["unit"] = None
    with pytest.raises(ValueError, match="euro or in thousands"):
        fl.import_statement(db, st, mapping_of(st))
    assert db.query(Company).count() == 0 and db.query(RawImportRecord).count() == 0
    thousands = fl.build_record({**st, "unit": "thousand EUR"}, mapping_of(st), None)
    assert next(o for o in thousands["observations"] if o["base"] == "total_assets" and o["year"] == 2025)["value"] == 3965121


def test_costs_are_stored_positive_and_direct_indicators_use_the_printed_unit(db):
    st = statement(2025, Y25, Y24)
    mapping = mapping_of(st)
    tax = next(l["id"] for l in st["lines"] if l["item"] == "270")
    mapping[tax] = "indicator:revenue_trend"
    record = fl.build_record(st, mapping, None)
    staff = next(o for o in record["observations"] if o["base"] == "personnel_costs" and o["year"] == 2025)
    assert staff["value"] == pytest.approx(998.618) and "brackets" in staff["evidence"]["sign"]
    assert record["direct"][0]["value"] == -109732 and record["direct"][0]["date"] == "2025-12-31"


def test_new_company_created_from_the_statement_and_profile_saved(db):
    st = statement(2025, Y25, Y24)
    mapping = mapping_of(st)
    tax = next(l["id"] for l in st["lines"] if l["item"] == "270")
    mapping[tax] = fl.NEW_INDICATOR
    fl.import_statement(db, st, mapping, new_labels={tax: "Income tax charge"}, profile_name="Intermediary")
    assert db.query(Company).one().registration_number == "IT" + FISCAL
    from models import ColumnMappingProfile
    saved = json.loads(db.query(ColumnMappingProfile).filter_by(dataset_name=fl.profile_key("Intermediary")).one().mapping_json)
    assert saved[tax] == "indicator:income_tax_charge"
    assert db.query(SignalRecord).filter_by(signal_key="income_tax_charge").one().numeric_value == -109732


def test_report_defaults_connect_every_year_but_trends_keep_the_three_year_window(db):
    import aida_report_mapping as m
    report = parse_report(report_file(), "aida.xlsx")
    defaults = m.suggest_report_mapping(report)
    periods = {v.rsplit("_", 1)[1] for v in defaults.values() if v and v.startswith("variable:")}
    assert {"latest", "y-1", "y-2", "y-10"} <= periods
    prepared = m.prepare_connections(db, report, defaults)
    assert prepared["drafts"]["revenue_trend"]["inputs"] == {"revenue_y-2": 5090.0, "revenue_latest": 6761.0} \
        or set(prepared["drafts"]["revenue_trend"]["inputs"]) <= {"revenue_y-2", "revenue_latest"}
    assert prepared["facts"]["revenue_y-10"] == 4089.0


def test_upload_view_reads_a_pdf_and_imports_it(tmp_path):
    """The Data Admin tab end to end on a throwaway database, with the PDF page text patched in."""
    import textwrap
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    root = str(Path(__file__).resolve().parents[1])
    db_path = tmp_path / "ui.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        seed_indicator_definitions(session)
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {root!r})
        sys.path.insert(0, {str(Path(__file__).parent)!r})
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        import balance_sheet_pdf as bs
        import test_balance_sheet_import as t
        import streamlit as st
        real_extract, real_uploader = bs.extract_pages, st.file_uploader
        bs.extract_pages = lambda content, ocr=None: t.pages(2025, t.Y25, t.Y24)
        engine = create_engine({f"sqlite:///{db_path}"!r}, connect_args={{"check_same_thread": False}})
        from views.balance_sheet_upload import render_upload
        class Upload:
            name = "bilancio 2025.pdf"
            def getvalue(self): return b"%PDF-fake"
        st.file_uploader = lambda *a, **k: Upload()
        try:
            render_upload(sessionmaker(bind=engine)())
        finally:  # AppTest shares the process with later tests
            bs.extract_pages, st.file_uploader = real_extract, real_uploader
    """)
    app = AppTest.from_string(script, default_timeout=120).run()
    assert not app.exception, [e.value for e in app.exception]
    assert "2025-12-31" in [m.value for m in app.metric]
    app.button(key="bs_confirm").click().run()
    assert not app.exception, [e.value for e in app.exception]
    assert any("Latest year is now FY2025" in s.value for s in app.success), [e.value for e in app.error]
    with sessionmaker(bind=engine)() as session:
        assert session.query(SignalRecord).filter_by(signal_key="total_assets").one().numeric_value == pytest.approx(3965.121)


@pytest.fixture(scope="module")
def dossier_db(tmp_path_factory):
    """A company with an AIDA bulk blob (ownership, 2024 figures) and a 2025 balance sheet imported on top."""
    from datetime import datetime
    import test_company_dossier as tcd
    db_path = tmp_path_factory.mktemp("bsdossier") / "d.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        seed_indicator_definitions(session)
        session.add(Company(id="c1", legal_name="Mondial Bony Service SPA", registration_number="IT" + FISCAL, country="Italy", segment="SME"))
        session.commit()
        blob = dict(tcd.AIDA_BLOB, total_assets_latest=4229.0, revenue_latest=6761.0, cash_latest=1100.0)
        session.add(RawImportRecord(company_id="c1", dataset_name="AIDA Raw Exports", raw_row=blob, source_filename="aida.xls"))
        session.commit()
        st_ = statement(2025, Y25, Y24)
        fl.import_statement(session, st_, mapping_of(st_))
    return db_path


def _dossier(dossier_db, tab):
    from streamlit.testing.v1 import AppTest
    import test_company_dossier as tcd
    app = AppTest.from_string(tcd._page_script(dossier_db, 'st.session_state["dossier_open_id"] = "c1"'), default_timeout=120)
    app.run()
    app.session_state["dossier_tab"] = tab
    app.run()
    assert not app.exception, [e.value for e in app.exception]
    return app


def test_dossier_financials_show_the_merged_history_with_sources_and_the_statement(dossier_db):
    import test_company_dossier as tcd
    app = _dossier(dossier_db, "financials")
    text = tcd._texts(app)
    assert "merged annual history" in text and "Latest = FY2025" in text
    assert "Full annual history" in text and "Balance sheet 2025-12-31" in text
    # EBITDA, headcount: shown as missing for 2025, not carried over
    assert any("Not reported for FY2025" in w.value and "ebitda" in w.value for w in app.warning)


def test_dossier_data_tab_renders_statement_and_merged_records_not_raw_json(dossier_db):
    import test_company_dossier as tcd
    app = _dossier(dossier_db, "data")
    text = tcd._texts(app)
    assert "Balance Sheet PDF 2025-12-31" in text and "Merged Annual Financials" in text


def test_signal_audit_finds_the_record_behind_a_merged_signal(dossier_db):
    from views.company_detail import _raw_records_for_source
    with sessionmaker(bind=create_engine(f"sqlite:///{dossier_db}"))() as session:
        company = session.get(Company, "c1")
        signal = session.query(SignalRecord).filter_by(company_id="c1", signal_key="total_assets").one()
        assert signal.source == fl.LEDGER_SOURCE
        assert [r.dataset_name for r in _raw_records_for_source(session, company, signal.source)] == [fl.LEDGER_DATASET]


def test_ownership_still_comes_from_the_aida_blob_and_valuation_prefers_the_newest_year(dossier_db):
    import test_company_dossier as tcd
    assert "Shareholders" in tcd._texts(_dossier(dossier_db, "people"))
    from valuation import Financials, best_financials
    old = Financials("AIDA Raw Exports", 1000.0, {"revenue": [1, 2, 3], "ebitda": [1, 2, 3]}, fiscal_year=2024)
    new = Financials(fl.LEDGER_DATASET, 1000.0, {"revenue": [1, 2, None]}, fiscal_year=2025)
    assert best_financials([old, new]).dataset == fl.LEDGER_DATASET
    assert best_financials([old, Financials("x", 1000.0, {"revenue": [1]}, fiscal_year=None)]).dataset == "AIDA Raw Exports"


def fake_model():
    """A model answer in the format the extraction prompt asks for, limited to the pages it was shown (one page per call)."""
    facts = [
        {"page": 3, "statement": "income", "item": "40", "label": "Commissioni attive", "printed": ["5.973.306", "6.761.318"]},
        {"page": 3, "statement": "income", "item": "999", "label": "Invented income line", "printed": ["5.973.306", "2"]},
        {"page": 3, "statement": "income", "item": "300", "label": "UTILE (PERDITA) D'ESERCIZIO", "printed": ["203.823", "109.732"]},
        {"page": 1, "statement": "other", "item": None, "label": "Numero medio dei dipendenti", "printed": ["18", "17"]},
    ]

    def answer(messages):
        shown = {int(n) for n in re.findall(r"=== PAGE (\d+) ===", messages[-1]["content"])}
        return json.dumps({"company": {"legal_name": "MONDIAL BONY SERVICE SPA", "share_capital": "600.000",
                                       "legal_form": "invented text not on any page"},
                           "facts": [f for f in facts if f["page"] in shown]})
    return answer


def pages_with_headcount():
    ps = pages(2025, Y25, Y24)
    ps[0]["text"] += "\nNumero medio dei dipendenti 18 17\nForma Giuridica Societa per azioni\nCapitale Sociale Euro 600.000,00 i.v."
    return ps


def test_ai_reading_adds_lines_the_rules_missed_and_drops_what_is_not_printed():
    ps = pages_with_headcount()
    st = bs.parse_pages(ps)
    bs.enrich_with_ai(st, ps, fake_model())
    stats = st["extraction"]
    assert stats["agreed"] >= 1 and stats["ai_only"] == 1 and stats["ungrounded"] >= 1 and stats["mismatch"] == 1
    headcount = next(l for l in st["lines"] if l["statement"] == "other")
    assert headcount["values"] == {"2025-12-31": 18.0, "2024-12-31": 17.0} and headcount["origin"] == "ai"
    assert st["company"]["share_capital"] == "600.000" and "legal_form" not in st["company"]
    assert any("dropped" in w for w in st["warnings"])


def test_without_a_model_the_rules_still_read_and_say_so():
    st = bs.parse_statement(b"%PDF-fake", "x.pdf") if False else None
    ps = pages(2025, Y25, Y24)
    report = bs.parse_pages(ps)
    assert report["lines"] and "extraction" not in report


def test_extracted_data_feeds_company_fields_headcount_and_the_one_history(db):
    with_aida_2024(db)
    ps = pages_with_headcount()
    st = bs.parse_pages(ps)
    bs.enrich_with_ai(st, ps, fake_model())
    suggestions = bs.suggest_mapping(st)
    headcount = next(l for l in st["lines"] if l["statement"] == "other")
    assert suggestions[headcount["id"]]["target"] == "base:employees"
    mapping = {k: v["target"] for k, v in suggestions.items()}
    mapping[headcount["id"]] = "base:employees"
    st["lines"].append(dict(id="details|legal_form|1", statement="details", item=None, label="Legal form", page=1,
                            values={"2025-12-31": "S.p.A."}, raw={"2025-12-31": "S.p.A."}, flags=[], origin="ai"))
    mapping["details|legal_form|1"] = "company:legal_form"
    plan = fl.plan_statement(db, st, mapping)
    assert plan["company_changes"] == [dict(field="legal_form", existing=None, statement="S.p.A.")]
    fl.import_statement(db, st, mapping)
    company = db.query(Company).one()
    assert company.legal_form == "S.p.A."
    ledger = db.query(RawImportRecord).filter_by(dataset_name=fl.LEDGER_DATASET).one().raw_row
    assert ledger["employees_latest"] == 18 and ledger["employees_y-1"] == 17  # headcount is not divided by 1000
    mapping["details|legal_form|1"] = "base:total_assets"
    with pytest.raises(ValueError, match="is text"):
        fl.plan_statement(db, st, mapping)


def test_flexible_data_import_accepts_a_pdf_and_routes_it_to_the_document_flow(tmp_path):
    import textwrap
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    root = str(Path(__file__).resolve().parents[1])
    db_path = tmp_path / "flex.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        seed_indicator_definitions(session)
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {root!r})
        sys.path.insert(0, {str(Path(__file__).parent)!r})
        import streamlit as st
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        import balance_sheet_pdf as bs
        import test_balance_sheet_import as t
        real_extract, real_uploader = bs.extract_pages, st.file_uploader
        bs.extract_pages = lambda content, ocr=None: t.pages(2025, t.Y25, t.Y24)
        class Upload:
            name = "bilancio 2025.pdf"
            size = 9
            def getvalue(self): return b"%PDF-fake"
        st.file_uploader = lambda *a, **k: Upload() if k.get("key") == "flex_uploader" else real_uploader(*a, **k)
        from views.data_admin import render_data_admin_page
        engine = create_engine({f"sqlite:///{db_path}"!r}, connect_args={{"check_same_thread": False}})
        try:
            render_data_admin_page(sessionmaker(bind=engine)())
        finally:
            bs.extract_pages, st.file_uploader = real_extract, real_uploader
    """)
    app = AppTest.from_string(script, default_timeout=120).run()
    assert not app.exception, [e.value for e in app.exception]
    assert "2025-12-31" in [m.value for m in app.metric]
    assert "Balance sheet upload (PDF)" not in [t.label for t in app.tabs]
    app.button(key="bs_confirm").click().run()
    assert any("Latest year is now FY2025" in s.value for s in app.success), [e.value for e in app.error]


def test_compact_rows_and_cut_off_answers_are_read():
    full = json.dumps({"company": {"legal_name": "X"}, "facts": [[2, "assets", "10", "Cassa", "952.074", "1.127.325"],
                                                                   [2, "assets", "", "TOTALE ATTIVO", "3.965.121", "4.228.215"]]})
    data, cut = bs._parse_answer(full)
    assert not cut and [bs._row_to_fact(r)["label"] for r in data["facts"]] == ["Cassa", "TOTALE ATTIVO"]
    data, cut = bs._parse_answer(full[:full.rindex('[2, "assets", ""')] + '[2, "assets", "", "TOTALE AT')
    assert cut and len(data["facts"]) == 1 and data["facts"][0][3] == "Cassa"


def test_printed_numbers_must_stand_on_the_page_as_their_own_token():
    text = "10. Cassa 952.074 1.127.325\nTotale (36.530) -"
    assert bs._printed_on_page("952.074", text) and bs._printed_on_page("(36.530)", text) and bs._printed_on_page("-", text)
    assert not bs._printed_on_page("52.074", text) and not bs._printed_on_page("1", text) and not bs._printed_on_page("2", text)


def test_rate_limited_provider_is_waited_for_then_the_next_one_answers(monkeypatch):
    calls = []

    class Response:
        def __init__(self, status, body=None, headers=None):
            self.status_code, self._body, self.headers = status, body, headers or {}

        def json(self):
            return self._body

    def fake_post(url, **kwargs):
        calls.append((url, kwargs["json"]["model"], kwargs["json"].get("reasoning_effort")))
        if "groq" in url:
            return Response(429, headers={"retry-after": "0"})
        if "cerebras" in url:
            return Response(402)
        return Response(200, {"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(bs, "_llm_endpoints", lambda kind: [("k", "https://api.groq.com/openai/v1", "openai/gpt-oss-120b"),
                                                            ("k", "https://api.cerebras.ai/v1", "gpt-oss-120b"),
                                                            ("k", "https://api.mistral.ai/v1", "mistral-large-latest")])
    assert bs.chat([{"role": "user", "content": "hi"}]) == "ok"
    assert [c[1] for c in calls] == ["openai/gpt-oss-120b", "openai/gpt-oss-120b", "gpt-oss-120b", "mistral-large-latest"]
    assert calls[0][2] == "low" and calls[3][2] is None  # reasoning kept short for gpt-oss only


def test_every_configured_assistant_model_joins_the_chain_and_gemini_can_read_scans(monkeypatch):
    import config
    monkeypatch.setattr(config, "crawler_llm_settings", lambda: {"api_key": "a", "base_url": "https://g/v1", "model": "gpt-oss-120b"})
    monkeypatch.setattr(config, "assistant_llm_settings", lambda: {
        "api_key": "b", "base_url": "https://mistral/v1", "model": "mistral-large-latest",
        "fallback_api_key": "c", "fallback_base_url": "https://gemini/v1", "fallback_model": "gemini-2.0-flash"})
    assert [m for _, _, m in bs._llm_endpoints("text")] == ["gpt-oss-120b", "mistral-large-latest", "gemini-2.0-flash"]
    assert [m for _, _, m in bs._llm_endpoints("vision") if "gemini" in m] == ["gemini-2.0-flash"]


def test_year_signal_follows_the_merged_anchor_but_a_later_registry_year_is_kept(db):
    with_bulk_aida_2024(db)
    st = statement(2025, Y25, Y24)
    plan = fl.plan_statement(db, st, mapping_of(st))
    assert any(c["signal"] == "last_accounts_year" and c["existing"] == 2024 and c["new"] == 2025 for c in plan["changes"])
    fl.import_statement(db, st, mapping_of(st))
    year = db.query(SignalRecord).filter_by(signal_key="last_accounts_year").one()
    assert year.numeric_value == 2025 and year.source == fl.LEDGER_SOURCE
    # A registry that already knows a later closing year is never pulled back to the statements' newest year.
    year.numeric_value, year.source = 2026.0, "AIDA Raw Exports"
    db.commit()
    older = statement(2024, Y24, Y23)
    assert not any(c["signal"] == "last_accounts_year" for c in fl.plan_statement(db, older, mapping_of(older))["changes"])


def test_bulk_aida_results_built_on_a_latest_the_history_no_longer_has_are_withdrawn(db):
    with_bulk_aida_2024(db)
    before = {s.signal_key: s for s in db.query(SignalRecord)}
    assert before["ebit_margin"].status == "present" and before["number_of_employees"].status == "present"
    st = statement(2025, Y25, Y24)
    plan = fl.plan_statement(db, st, mapping_of(st))
    stale = {c["signal"]: c for c in plan["changes"] if c["obsolete"]}
    assert {"ebit_margin", "number_of_employees", "revenue_per_employee"} <= set(stale)
    assert all(c["decide"] and c["existing_source"] == "AIDA Raw Exports" for c in stale.values())  # the operator can keep them
    fl.import_statement(db, st, mapping_of(st), signal_replace={"number_of_employees": False})
    after = {s.signal_key: s for s in db.query(SignalRecord)}
    assert after["ebit_margin"].status == "not_yet_checked" and after["ebit_margin"].numeric_value is None
    assert after["revenue_per_employee"].status == "not_yet_checked"
    assert after["number_of_employees"].status == "present" and after["number_of_employees"].source == "AIDA Raw Exports"
    # Results of the bulk export that do not depend on a relative year are left alone.
    assert after["last_accounts_year"].numeric_value == 2025


def test_headcount_from_the_statement_follows_the_employee_signal_onto_the_company(db):
    company = with_bulk_aida_2024(db)
    ps = pages_with_headcount()
    st = bs.parse_pages(ps)
    bs.enrich_with_ai(st, ps, fake_model())
    mapping = mapping_of(st)
    fl.import_statement(db, st, mapping)
    db.refresh(company)
    assert company.headcount == 18 and company.headcount_source_tier == "T1" and company.segment == "SME"
    assert db.query(SignalRecord).filter_by(signal_key="number_of_employees").one().numeric_value == 18


def test_keeping_the_existing_employee_signal_keeps_the_company_headcount(db):
    company = with_bulk_aida_2024(db)
    company.headcount = 21
    db.query(SignalRecord).filter_by(signal_key="number_of_employees").one().numeric_value = 21  # differs from the statement's 18
    db.commit()
    ps = pages_with_headcount()
    st = bs.parse_pages(ps)
    bs.enrich_with_ai(st, ps, fake_model())
    assert any(c["signal"] == "number_of_employees" and c["decide"] for c in fl.plan_statement(db, st, mapping_of(st))["changes"])
    fl.import_statement(db, st, mapping_of(st), signal_replace={"number_of_employees": False})
    db.refresh(company)
    assert company.headcount == 21 and db.query(SignalRecord).filter_by(signal_key="number_of_employees").one().numeric_value == 21


def test_incorporation_date_connection_reads_a_printed_date_and_rejects_text(db):
    ps = pages_with_headcount()
    st = bs.parse_pages(ps)
    st["lines"].append(dict(id="details|founded|1", statement="details", item=None, label="Data di costituzione", page=1,
                            values={"2025-12-31": "12/05/1998"}, raw={"2025-12-31": "12/05/1998"}, flags=[], origin="ai"))
    mapping = mapping_of(st)
    mapping["details|founded|1"] = "company:incorporation_date"
    plan = fl.plan_statement(db, st, mapping)
    assert plan["company_changes"][0]["statement"] == "1998-05-12"
    fl.import_statement(db, st, mapping)
    assert str(db.query(Company).one().incorporation_date)[:10] == "1998-05-12"
    st["lines"][-1]["values"]["2025-12-31"] = "maybe spring"
    with pytest.raises(ValueError, match="not a readable date"):
        fl.plan_statement(db, st, mapping)


@pytest.fixture(scope="module")
def dossier_bulk_db(tmp_path_factory):
    """A bulk-AIDA company (FY2024 blob and signals, year signal 2024) with an AI-read FY2025 statement imported on top."""
    import test_company_dossier as tcd
    from aida_import import derive_financial_signals
    db_path = tmp_path_factory.mktemp("bsbulk") / "d.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        seed_indicator_definitions(session)
        session.add(Company(id="c1", legal_name="Mondial Bony Service SPA", registration_number="IT" + FISCAL, country="Italy",
                            segment="SME", headcount=90, headcount_source_tier="T1"))
        session.commit()
        blob = dict(tcd.AIDA_BLOB)
        session.add(RawImportRecord(company_id="c1", dataset_name="AIDA Raw Exports", raw_row=blob, source_filename="aida.xls"))
        for name, draft in derive_financial_signals(blob).items():
            if draft["value"] is not None:
                session.add(SignalRecord(company_id="c1", signal_key=name, source="AIDA Raw Exports", status="present",
                                         numeric_value=draft["value"], is_simulated=False))
        session.add(SignalRecord(company_id="c1", signal_key="last_accounts_year", source="AIDA Raw Exports", status="present",
                                 numeric_value=2024.0, is_simulated=False))
        session.commit()
        ps = pages_with_headcount()
        st_ = bs.parse_pages(ps)
        bs.enrich_with_ai(st_, ps, fake_model())
        fl.import_statement(session, st_, mapping_of(st_))
    return db_path


def test_dossier_does_not_call_the_accounts_stale_once_a_newer_statement_is_imported(dossier_bulk_db):
    import test_company_dossier as tcd
    app = _dossier(dossier_bulk_db, "overview")
    text = tcd._texts(app)
    assert "is not in AIDA yet" not in text and "Accounts FY2024" not in text
    assert "18 staff" in text  # the header follows the statement's headcount
    with sessionmaker(bind=create_engine(f"sqlite:///{dossier_bulk_db}"))() as session:
        assert session.query(SignalRecord).filter_by(company_id="c1", signal_key="last_accounts_year").one().numeric_value == 2025


def test_identity_block_shows_what_the_statement_printed_about_the_company(dossier_bulk_db):
    import test_company_dossier as tcd
    text = tcd._texts(_dossier(dossier_bulk_db, "overview"))
    assert "Printed in the uploaded statement of 2025-12-31" in text and "Share capital: 600.000" in text
    assert "Societa per azioni" not in text.split("Printed in the uploaded statement")[1].split("\n")[0]  # connected details are not repeated


def test_growth_headline_reads_the_merged_history_not_the_older_aida_blob(dossier_bulk_db):
    import test_company_dossier as tcd
    app = _dossier(dossier_bulk_db, "financials")
    text = tcd._texts(app)
    assert "source: Merged Annual Financials" in text and "source: AIDA Raw Exports" not in text.split("Is it growing?")[1].split("Current levels")[0]


def test_trend_headline_uses_the_window_of_the_trend_and_needs_the_latest_year():
    from types import SimpleNamespace
    from views.company_detail import _trend_headline
    merged = SimpleNamespace(dataset_name=fl.LEDGER_DATASET, mapping_snapshot={},
                             raw_row={"revenue_latest": 80.0, "revenue_y-1": 90.0, "revenue_y-2": 100.0, "revenue_y-3": 10.0})
    old = SimpleNamespace(dataset_name="AIDA Raw Exports", mapping_snapshot={}, raw_row={"revenue_latest": 1.0, "revenue_y-2": 2.0})
    sig = SimpleNamespace(status="present", numeric_value=-20.0, source=fl.LEDGER_DATASET)
    info = _trend_headline("revenue_trend", sig, [old, merged])
    assert info["dataset"] == fl.LEDGER_DATASET and info["base"] == 100.0 and info["latest"] == 80.0 and round(info["pct"]) == -20
    no_latest = SimpleNamespace(dataset_name=fl.LEDGER_DATASET, mapping_snapshot={}, raw_row={"ebit_y-1": 5.0, "ebit_y-2": 4.0})
    assert not _trend_headline("ebit_trend", None, [old, no_latest]).get("has_raw")


def test_removing_a_statement_re_merges_the_rest_and_brings_the_older_year_back(db):
    with_bulk_aida_2024(db)
    st = statement(2025, Y25, Y24)
    fl.import_statement(db, st, mapping_of(st))
    company = db.query(Company).one()
    merged = fl.remove_statement(db, company, fl.statement_dataset("2025-12-31"))
    assert merged["anchor"] == 2024
    sig = {s.signal_key: s for s in db.query(SignalRecord)}
    assert sig["last_accounts_year"].numeric_value == 2024 and sig["total_assets"].numeric_value == 4229
    assert db.query(RawImportRecord).filter_by(dataset_name=fl.statement_dataset("2025-12-31")).count() == 0
    ledger = db.query(RawImportRecord).filter_by(dataset_name=fl.LEDGER_DATASET).one().raw_row
    assert ledger["derived_last_accounts_year"] == 2024 and ledger["total_assets_latest"] == 4229
    assert db.query(Company).count() == 1  # the company and its other sources stay


def test_removing_the_only_statement_withdraws_what_rested_on_it(db):
    st = statement(2025, Y25, Y24)
    fl.import_statement(db, st, mapping_of(st))
    company = db.query(Company).one()
    fl.remove_statement(db, company, fl.statement_dataset("2025-12-31"))
    assert db.query(RawImportRecord).filter_by(company_id=company.id).count() == 0
    assert db.query(SignalRecord).filter_by(company_id=company.id, status="present").count() == 0
    with pytest.raises(ValueError, match="no such statement"):
        fl.remove_statement(db, company, fl.statement_dataset("2025-12-31"))
    with pytest.raises(ValueError, match="Only an uploaded balance sheet"):
        fl.remove_statement(db, company, fl.LEDGER_DATASET)


def test_dossier_offers_to_remove_a_statement_and_re_merges(dossier_bulk_db, tmp_path):
    import shutil
    import test_company_dossier as tcd
    from streamlit.testing.v1 import AppTest
    copy = tmp_path / "copy.db"
    shutil.copy(dossier_bulk_db, copy)
    app = AppTest.from_string(tcd._page_script(copy, 'st.session_state["dossier_open_id"] = "c1"'), default_timeout=120)
    app.run()
    app.session_state["dossier_tab"] = "financials"
    app.run()
    assert not app.exception, [e.value for e in app.exception]
    box = next(c for c in app.checkbox if c.key.startswith("bs_rm_sure_dos_"))
    button = next(b for b in app.button if b.key.startswith("bs_rm_dos_"))
    assert button.disabled
    box.check().run()
    next(b for b in app.button if b.key.startswith("bs_rm_dos_")).click().run()
    assert not app.exception, [e.value for e in app.exception]
    with sessionmaker(bind=create_engine(f"sqlite:///{copy}"))() as session:
        assert session.query(RawImportRecord).filter(RawImportRecord.dataset_name.like(fl.DATASET_PREFIX + "%")).count() == 0
        assert session.query(SignalRecord).filter_by(signal_key="last_accounts_year").one().numeric_value == 2024


def oic_pages(year=2025, euro=True):
    """An OIC (industrial company) statement as pdfplumber returns it: unnumbered section headings, "6)" items, costs printed positive."""
    d1, d2 = f"31/12/{year}", f"31/12/{year - 1}"
    k = (lambda n: n) if euro else (lambda n: n // 1000)

    def two(a, b):
        return f"{_it(k(a))} {_it(k(b))}"

    return [
        dict(page=1, method="text", text="ROSSI MACCHINE S.R.L.\nSede legale in MILANO, VIA ROMA 1\nCapitale sociale Euro 1.000.000,00 i.v.\n"
                                         "Codice fiscale e n. iscrizione Registro Imprese 01234567890\n"
                                         f"Bilancio di esercizio al {d1}\n"
                                         + ("Importi espressi in unita di euro" if euro else "Importi espressi in migliaia di euro")),
        dict(page=2, method="text", text=f"STATO PATRIMONIALE ATTIVO {d1} {d2}\nB) IMMOBILIZZAZIONI\nI - Immobilizzazioni immateriali\n"
                                         f"Totale immobilizzazioni immateriali {two(120000, 100000)}\n"
                                         f"Totale immobilizzazioni {two(2400000, 2300000)}\nC) ATTIVO CIRCOLANTE\nIV - Disponibilita liquide\n"
                                         f"Totale disponibilita liquide {two(800000, 700000)}\nTotale attivo circolante {two(5200000, 4900000)}\n"
                                         f"TOTALE ATTIVO {two(7600000, 7200000)}\n"
                                         f"STATO PATRIMONIALE PASSIVO {d1} {d2}\nA) PATRIMONIO NETTO\n"
                                         f"IX - Utile (perdita) dell'esercizio {two(1116900, 970900)}\nTotale patrimonio netto {two(3100000, 2900000)}\n"
                                         f"D) DEBITI\nTotale debiti {two(4200000, 4000000)}\nTOTALE PASSIVO {two(7600000, 7200000)}"),
        dict(page=3, method="text", text=f"CONTO ECONOMICO {d1} {d2}\nA) VALORE DELLA PRODUZIONE\n"
                                         f"1) ricavi delle vendite e delle prestazioni {two(12500000, 11800000)}\n"
                                         f"5) altri ricavi e proventi {two(90000, 80000)}\nTotale valore della produzione {two(12590000, 11880000)}\n"
                                         "B) COSTI DELLA PRODUZIONE\n"
                                         f"6) per materie prime, sussidiarie, di consumo e di merci {two(6100000, 5800000)}\n"
                                         f"7) per servizi {two(2200000, 2100000)}\n9) per il personale:\na) salari e stipendi {two(2300000, 2200000)}\n"
                                         f"Totale costi per il personale {two(2300000, 2200000)}\n10) ammortamenti e svalutazioni:\n"
                                         f"Totale ammortamenti e svalutazioni {two(410000, 400000)}\n"
                                         f"Totale costi della produzione {two(11010000, 10500000)}\n"
                                         f"Differenza tra valore e costi della produzione (A - B) {two(1580000, 1380000)}\n"
                                         f"21) Utile (perdita) dell'esercizio {two(1116900, 970900)}"),
    ]


def _it(n):
    return f"{n:,}".replace(",", ".")


def test_oic_section_headings_stay_out_of_row_labels_so_the_rules_find_the_key_lines():
    st = bs.parse_pages(oic_pages())
    labels = [l["label"] for l in st["lines"]]
    assert not any(re.search(r"IMMOBILIZZAZIONI\b.*Totale|ATTIVO CIRCOLANTE|PATRIMONIO NETTO IX|COSTI DELLA PRODUZIONE 6|per il personale: a\)", x) for x in labels), labels
    targets = {s["target"] for s in bs.suggest_mapping(st).values() if s["target"]}
    assert targets == {f"base:{b}" for b in ("revenue", "production_value", "materials", "personnel_costs", "production_costs", "ebit", "net_income",
                                             "cash", "total_assets", "equity", "total_debt", "intangibles")}
    revenue = next(l for l in st["lines"] if l["item"] == "1")
    assert revenue["values"] == {"2025-12-31": 12500000.0, "2024-12-31": 11800000.0}
    assert st["unit"] == "EUR" and bs.parse_pages(oic_pages(euro=False))["unit"] == "thousand EUR"


def test_oic_arithmetic_checks_cover_assets_result_and_the_operating_difference():
    st = bs.parse_pages(oic_pages())
    names = [c["name"] for c in st["checks"]]
    assert any(n.startswith("Assets equal liabilities and equity") for n in names)  # "TOTALE PASSIVO" is the OIC total
    assert any(n.startswith("Value of production minus production costs") for n in names)
    assert all(c["ok"] for c in st["checks"]), st["checks"]
    broken = oic_pages()
    broken[2]["text"] = broken[2]["text"].replace("1.580.000", "1.590.000")
    failed = bs.parse_pages(broken)
    assert any(not c["ok"] and c["name"].startswith("Value of production minus") for c in failed["checks"])


def test_oic_statement_imports_into_thousands_and_feeds_the_signals(db):
    st = bs.parse_pages(oic_pages(), "oic.pdf", "sha")
    fl.import_statement(db, st, mapping_of(st))
    ledger = db.query(RawImportRecord).filter_by(dataset_name=fl.LEDGER_DATASET).one().raw_row
    assert ledger["revenue_latest"] == 12500.0 and ledger["ebit_latest"] == 1580.0 and ledger["total_debt_latest"] == 4200.0
    assert ledger["personnel_costs_latest"] == 2300.0 and ledger["materials_latest"] == 6100.0 and ledger["cash_y-1"] == 700.0
    sig = {s.signal_key: s for s in db.query(SignalRecord)}
    assert sig["ebit_margin"].numeric_value == pytest.approx(1580 / 12500 * 100) and sig["labour_cost"].numeric_value == pytest.approx(2300 / 12500 * 100)


def test_bulk_aida_reimport_re_merges_a_company_that_has_statement_history(db):
    import aida_import as A
    company = with_bulk_aida_2024(db)
    company.external_ref_id = "IT1"
    db.commit()
    st = statement(2025, Y25, Y24)
    fl.import_statement(db, st, mapping_of(st))
    ledger = lambda: db.query(RawImportRecord).filter_by(dataset_name=fl.LEDGER_DATASET).one().raw_row  # noqa: E731
    assert ledger()["ebit_y-1"] == 6761.0                         # AIDA's FY2024 figure, now history
    blob = dict(AIDA_FACTS, **{"ebit_latest": 5000.0})            # AIDA revised its FY2024 operating result
    report = A.apply_records(db, {"IT1": {"signals": A.derive_financial_signals(blob), "blob": blob, "stale_accounts": False}}, apply=True)
    assert report["re_merged"] == 1
    db.expire_all()
    assert ledger()["ebit_y-1"] == 5000.0 and ledger()["derived_last_accounts_year"] == 2025
    other = Company(legal_name="Other", registration_number="R-2", country="Italy", external_ref_id="IT2")
    db.add(other)
    db.commit()
    again = A.apply_records(db, {"IT2": {"signals": A.derive_financial_signals(blob), "blob": blob, "stale_accounts": False}}, apply=True)
    assert again["re_merged"] == 0                                # nobody without statement history is touched


def test_a_statement_without_its_own_headcount_leaves_the_companys_headcount_alone(db):
    company = with_bulk_aida_2024(db)
    company.headcount, company.headcount_source_tier = 40, "T2"   # set by hand, differs from AIDA's 18
    db.commit()
    st = statement(2024, Y24, Y23)                                # same year as AIDA, no employee figure on the statement
    fl.import_statement(db, st, mapping_of(st))
    db.refresh(company)
    assert company.headcount == 40 and company.headcount_source_tier == "T2"


def test_an_uppercase_label_with_its_figures_on_the_next_line_is_still_a_row_not_a_heading():
    ps = [dict(page=1, method="text", text="BILANCIO AL 31/12/2025\nCodice fiscale 01234567890\nImporti espressi in unita di euro"),
          dict(page=2, method="text", text="STATO PATRIMONIALE PASSIVO 31/12/2025 31/12/2024\nA) PATRIMONIO NETTO\n"
                                           "A) TOTALE PATRIMONIO NETTO\n3.100.000 2.900.000\nD) DEBITI\nTotale debiti 4.200.000 4.000.000")]
    st = bs.parse_pages(ps)
    equity = next(l for l in st["lines"] if "PATRIMONIO NETTO" in l["label"])
    assert equity["values"] == {"2025-12-31": 3100000.0, "2024-12-31": 2900000.0}
    assert {l["label"] for l in st["lines"]} == {equity["label"], "Totale debiti"}


def test_ai_rows_meet_the_rule_row_with_the_same_label_even_when_oic_sub_item_numbers_repeat():
    ps = [dict(page=1, method="text", text="BILANCIO AL 31/12/2025\nCodice fiscale 01234567890\nImporti espressi in unita di euro"),
          dict(page=2, method="text", text="STATO PATRIMONIALE ATTIVO 31/12/2025 31/12/2024\nI - Immobilizzazioni immateriali\n"
                                           "1) costi di impianto e di ampliamento 10 20\nII - Immobilizzazioni materiali\n"
                                           "1) terreni e fabbricati 500 600\nTOTALE ATTIVO 510 620")]
    st = bs.parse_pages(ps)
    assert [l["item"] for l in st["lines"]][:2] == ["1", "1"]

    def model(messages):
        return json.dumps({"company": {}, "facts": [{"page": 2, "statement": "assets", "item": "1", "label": "terreni e fabbricati",
                                                       "printed": ["500", "600"]}]})
    bs.enrich_with_ai(st, ps, model)
    land = next(l for l in st["lines"] if "terreni" in l["label"])
    plant = next(l for l in st["lines"] if "impianto" in l["label"])
    assert land["origin"] == "ai+rules" and plant["origin"] == "rules" and st["extraction"]["mismatch"] == 0


def perfect_model(pages):
    """A model that reads every table row of every page correctly: label as printed (item number apart), the two printed figures."""
    def answer(messages):
        shown = {int(n) for n in re.findall(r"=== PAGE (\d+) ===", messages[-1]["content"])}
        facts = []
        for page in pages:
            if page["page"] not in shown:
                continue
            statement = "assets"
            for line in page["text"].splitlines():
                low = line.casefold()
                statement = "liabilities" if "passivo" in low and "stato patrimoniale" in low else "income" if "conto economico" in low else statement
                tokens = line.split()
                if len(tokens) > 3 and all(re.fullmatch(r"[\d.]+", t) for t in tokens[-2:]) and not re.search(r"\d{2}/\d{2}/\d{4}", line):
                    label = " ".join(tokens[:-2])
                    item = re.match(r"(\d+)\)", label)
                    facts.append({"page": page["page"], "statement": statement, "item": item[1] if item else None,
                                  "label": label, "printed": tokens[-2:]})
        return json.dumps({"company": {}, "facts": facts})
    return answer


def test_a_correct_model_reading_of_an_oic_statement_agrees_with_every_rule_row_and_adds_nothing():
    ps = oic_pages()
    st = bs.parse_pages(ps)
    rows = len(st["lines"])
    bs.enrich_with_ai(st, ps, perfect_model(ps))
    stats = st["extraction"]
    assert stats["agreed"] == rows and stats["ai_only"] == 0 and stats["mismatch"] == 0, stats
    assert len(st["lines"]) == rows and all(l["origin"] == "ai+rules" for l in st["lines"])


def test_a_page_the_ocr_model_cannot_read_is_reported_and_does_not_lose_the_rest(monkeypatch):
    import types
    pages_ = [types.SimpleNamespace(extract_text=lambda: "x" * 200), types.SimpleNamespace(extract_text=lambda: "")]

    class Pdf:
        pages = pages_
        def __enter__(self): return self
        def __exit__(self, *a): return False
    import pdfplumber
    monkeypatch.setattr(pdfplumber, "open", lambda _: Pdf())
    monkeypatch.setattr(bs, "render_page_png", lambda content, i: b"png")

    def broken(png):
        raise RuntimeError("gemini-2.0-flash: HTTP 404")
    out = bs.extract_pages(b"%PDF", ocr=broken)
    assert [p["method"] for p in out] == ["text", "empty"] and "404" in out[1]["error"]
    report = bs.parse_pages(out)
    assert any("OCR model failed on 1" in w and "404" in w for w in report["warnings"])


class FakePage:
    """A pdfplumber page reduced to its words: label words from x=25, figures right-aligned on column edges (each char 5.5 wide)."""

    def __init__(self, rows):
        self.words, top = [], 20.0
        for label, cells in rows:
            x = 25.0
            for token in label.split():
                self.words.append(dict(text=token, x0=x, x1=x + 5.5 * len(token), top=top))
                x += 5.5 * len(token) + 2.5
            for text, edge in cells:
                width = 5.5 * len(text)
                if text == "6|00.000":  # a number the font split with no gap: two words that touch
                    self.words += [dict(text="6", x0=edge - 33, x1=edge - 27.5, top=top), dict(text="00.000", x0=edge - 27.5, x1=edge, top=top)]
                else:
                    self.words.append(dict(text=text, x0=edge - width, x1=edge, top=top))
            top += 12.0

    def extract_words(self, **kwargs):
        return self.words


INTERIM_ROWS = [
    ("STATO PATRIMONIALE PASSIVO", []),
    ("10. Passività finanziarie valutate al costo ammortizzato", [("104.836", 561.6)]),
    ("20. Passività finanziarie di negoziazione", [("-", 548.0)]),
    ("60. Passività fiscali", [("15.407", 561.6)]),
    ("80. Altre passività", [("2.324.967", 561.6)]),
    ("90. Trattamento di fine rapporto del personale", [("383.068", 561.6)]),
    ("100. Fondi per rischi ed oneri", [("250.000", 561.6)]),
    ("110. Capitale", [("6|00.000", 561.6)]),
    ("120. Azioni proprie (-)", [("-", 548.0)]),
    ("150. Riserve", [("210.801", 561.6)]),
    ("160. Riserve da valutazione", [("91.971", 561.6)]),
    ("170. Utile (Perdita) d'esercizio", [("32.709", 561.6)]),
    ("TOTALE PASSIVO E PATRIMONIO NETTO", [("4.013.759", 561.6), ("-", 570.0), ("24.161", 699.3)]),
    ("CONTO ECONOMICO", []),
    ("20. Interessi passivi e oneri assimilati", [("-", 470.0), ("7.771", 561.6)]),
    ("40. Commissioni attive", [("2.651.101", 561.6)]),
    ("50. Commissioni passive", [("-", 470.0), ("1.937.913", 561.6)]),
    ("300. UTILE (PERDITA) D'ESERCIZIO", [("32.709", 561.6)]),
]


def test_position_aware_text_glues_split_digits_reads_signs_and_ignores_a_side_column():
    text, ncols = bs.geometry_text(FakePage(INTERIM_ROWS))
    rows = {line.split(" ")[0]: line for line in text.splitlines()}
    assert ncols == 1
    assert "110. Capitale 600.000" in text                         # "6" + "00.000" touching
    assert "20. Interessi passivi e oneri assimilati -7.771" in text  # a dash far left of the figure is its sign
    assert "50. Commissioni passive -1.937.913" in text
    assert "20. Passività finanziarie di negoziazione -" in text and "-0" not in text  # a dash inside the column is nil
    assert text.count("TOTALE PASSIVO E PATRIMONIO NETTO 4.013.759") == 1 and "24.161" not in text  # the side column is not a figure
    assert rows["110."] == "110. Capitale 600.000"


def test_two_aligned_columns_keep_a_nil_dash_in_the_first_column():
    rows = [("STATO PATRIMONIALE ATTIVO", [])] + [(f"{i}0. Voce {i}", [(f"{i}00.000", 400.0), (f"{i}10.000", 500.0)]) for i in range(1, 5)] \
        + [("50. Voce nulla", [("-", 392.0), ("1.127.325", 500.0)])]
    text, ncols = bs.geometry_text(FakePage(rows))
    assert ncols == 2 and "50. Voce nulla - 1.127.325" in text


def test_an_interim_statement_with_one_column_and_no_equity_total_is_read_and_blocked_as_a_fiscal_year(db):
    text, ncols = bs.geometry_text(FakePage(INTERIM_ROWS))
    pages = [dict(page=1, method="text", text="MONDIAL BONY SERVICE SPA\nPartita IVA 06038441215 - Codice Fiscale 06038441215\n"
                                              "Fascicolo dal 01/01/2026 al 30/06/2026\n" + text, ncols=ncols)]
    st = bs.parse_pages(pages, "interim.pdf", "sha")
    assert st["period_end"] == "2026-06-30" and st["columns"] == ["2026-06-30"]
    assert st["partial_period"] == dict(start="2026-01-01", end="2026-06-30", days=181)
    equity = next(l for l in st["lines"] if "calcolato" in l["label"])
    assert equity["values"] == {"2026-06-30": 935481.0}           # 600.000 + 210.801 + 91.971 + 32.709, own shares deducted
    assert bs.suggest_mapping(st)[equity["id"]]["target"] == "base:equity"
    assert next(l for l in st["lines"] if "Interessi passivi" in l["label"])["values"] == {"2026-06-30": -7771.0}
    check = next(c for c in st["checks"] if c["name"].startswith("Liabilities (items 10 to 100) plus equity"))
    assert check["ok"], check                                        # the calculated equity is proven by the liabilities total
    mapping = {k: v["target"] for k, v in bs.suggest_mapping(st).items()}
    with pytest.raises(ValueError, match="not a full fiscal year"):
        fl.plan_statement(db, st, mapping, unit="EUR")
    st["accept_partial"] = True
    plan = fl.plan_statement(db, st, mapping, unit="EUR")
    assert plan["merged"]["anchor"] == 2026 and plan["record"]["observations"]
    fl.import_statement(db, st, mapping, unit="EUR")
    assert db.query(RawImportRecord).filter_by(dataset_name=fl.LEDGER_DATASET).one().raw_row["equity_latest"] == 935.481


def test_the_plain_text_is_used_when_the_rebuilt_text_reads_fewer_lines(monkeypatch):
    plain_pages = pages(2025, Y25, Y24)
    rebuilt = [dict(p, text="garbage", plain=p["text"], ncols=3) for p in plain_pages]
    monkeypatch.setattr(bs, "extract_pages", lambda content, ocr=None: rebuilt)
    st = bs.parse_statement(b"%PDF-fake", "x.pdf")
    assert len(st["lines"]) >= 5 and st["period_end"] == "2025-12-31"
