"""A newly imported statement must be recognised as a given fiscal year and put in that year's place in the trend history,
whatever the order of the uploads, gaps, a non-December year end, a restated comparative or an unreadable date.

The expected numbers are computed here from the inputs (an oracle), never read back from the code under test.
"""

import itertools
import json
import re

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import balance_sheet_pdf as bs
import financial_ledger as fl
from indicators import seed_indicator_definitions
from models import Base, Company, RawImportRecord, SignalRecord
from test_balance_sheet_import import FISCAL, mapping_of


def figures(year):
    """The company's figures for one fiscal year, in euro. Invented, strictly increasing, so a misplaced year shows."""
    n = year - 2020
    return dict(cash=500000 + 10000 * n, assets=4000000 + 100000 * n, profit=100000 + 7000 * n, revenue=5000000 + 300000 * n,
                staff=900000 + 20000 * n)


def page_text(close, current, prior, language_year=None):
    """A Bank of Italy style statement closing on `close` (MM-DD) with two columns, current year first."""
    year = current[0]
    d1, d2 = f"{close[-2:]}-{close[:2]}-{year}", f"{close[-2:]}-{close[:2]}-{year - 1}"
    cur, pri = current[1], prior[1]
    n = lambda v: (f"({f'{-v:,}'.replace(',', '.')})" if v < 0 else f"{v:,}".replace(",", "."))  # noqa: E731
    return [
        dict(page=1, method="text", text=f"MONDIAL BONY SERVICE SPA\nBilancio di esercizio al {d1.replace('-', '/')}\nCodice Fiscale {FISCAL}\n"
                                         "Gli importi presenti sono espressi in Euro"),
        dict(page=2, method="text", text=f"STATO PATRIMONIALE ATTIVO {d1} {d2}\n10. Cassa e disponibilita liquide {n(cur['cash'])} {n(pri['cash'])}\n"
                                         f"TOTALE ATTIVO {n(cur['assets'])} {n(pri['assets'])}\nSTATO PATRIMONIALE PASSIVO {d1} {d2}\n"
                                         f"170. Utile (Perdita) d'esercizio {n(cur['profit'])} {n(pri['profit'])}\n"
                                         f"TOTALE PASSIVO E PATRIMONIO NETTO {n(cur['assets'])} {n(pri['assets'])}"),
        dict(page=3, method="text", text=f"CONTO ECONOMICO {d1} {d2}\n40. Commissioni attive {n(cur['revenue'])} {n(pri['revenue'])}\n"
                                         f"a) spese per il personale {n(-cur['staff'])} {n(-pri['staff'])}\n"
                                         f"300. UTILE (PERDITA) D'ESERCIZIO {n(cur['profit'])} {n(pri['profit'])}"),
    ]


def stmt(year, close="12-31", **override):
    cur, pri = figures(year), figures(year - 1)
    cur.update(override.get("current", {}))
    pri.update(override.get("prior", {}))
    return bs.parse_pages(page_text(close, (year, cur), (year - 1, pri)), f"bilancio {year}.pdf", f"sha{year}{close}")


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        seed_indicator_definitions(session)
        yield session


def ledger(db):
    return db.query(RawImportRecord).filter_by(dataset_name=fl.LEDGER_DATASET).one().raw_row


def signals(db):
    return {s.signal_key: s.numeric_value for s in db.query(SignalRecord) if s.status == "present"}


def oracle(years):
    """Expected facts (thousand EUR) and signals for a set of covered fiscal years, straight from the definition: the newest year
    is Latest, the others count down from it by actual year, and a trend compares Latest with two years back, else one year back."""
    anchor = max(years)
    k = lambda y, name: figures(y)[name] / 1000  # noqa: E731
    facts = {}
    for y in years:
        label = "latest" if y == anchor else f"y-{anchor - y}"
        for fact, name in (("revenue", "revenue"), ("total_assets", "assets"), ("cash", "cash"), ("net_income", "profit"),
                           ("personnel_costs", "staff")):
            facts[f"{fact}_{label}"] = k(y, name)
    base = anchor - 2 if anchor - 2 in years else anchor - 1 if anchor - 1 in years else None
    expected = dict(total_assets=k(anchor, "assets"), cash_position=k(anchor, "cash"),
                    net_margin=k(anchor, "profit") / k(anchor, "revenue") * 100, labour_cost=k(anchor, "staff") / k(anchor, "revenue") * 100)
    if base:
        expected["revenue_trend"] = (k(anchor, "revenue") - k(base, "revenue")) / abs(k(base, "revenue")) * 100
    return anchor, facts, expected


def assert_matches_oracle(db, covered):
    anchor, facts, expected = oracle(covered)
    row = ledger(db)
    assert row["derived_last_accounts_year"] == anchor
    for name, value in facts.items():
        assert row[name] == pytest.approx(value), name
    assert not [k for k in row if re.match(r"^(revenue|total_assets|cash|net_income|personnel_costs)_(latest|y-\d+)$", k) and k not in facts], \
        "a year appears that no statement covers"
    sig = signals(db)
    for name, value in expected.items():
        assert sig[name] == pytest.approx(value, abs=1e-3), name  # signals are stored to 4 decimals
    if "revenue_trend" not in expected:
        assert "revenue_trend" not in sig
    assert sig["last_accounts_year"] == anchor


@pytest.mark.parametrize("order", list(itertools.permutations([2023, 2024, 2025])))
def test_any_upload_order_gives_the_same_history_and_the_same_trends(db, order):
    for year in order:
        s = stmt(year)
        fl.import_statement(db, s, mapping_of(s), replace_statement=True)
    assert_matches_oracle(db, {2022, 2023, 2024, 2025})


@pytest.mark.parametrize("years", [[2025], [2024], [2025, 2022], [2022, 2025], [2025, 2023], [2026, 2024, 2021]])
def test_gaps_stay_gaps_and_the_trend_uses_only_the_years_that_exist(db, years):
    for year in years:
        s = stmt(year)
        fl.import_statement(db, s, mapping_of(s))
    assert_matches_oracle(db, {y for year in years for y in (year, year - 1)})


def test_a_trend_never_bridges_a_gap_wider_than_its_window(db):
    s = stmt(2025)
    fl.import_statement(db, s, mapping_of(s))
    old = stmt(2021)  # covers 2021 and 2020: nothing within two years of FY2025
    fl.import_statement(db, old, mapping_of(old))
    row = ledger(db)
    assert row["revenue_y-4"] == pytest.approx(figures(2021)["revenue"] / 1000) and "revenue_y-2" not in row
    assert signals(db)["revenue_trend"] == pytest.approx((figures(2025)["revenue"] - figures(2024)["revenue"]) / figures(2024)["revenue"] * 100, abs=1e-3)


def test_a_june_year_end_is_the_year_it_closes_in_and_meets_aida_by_that_year(db):
    s = stmt(2025, close="06-30")
    assert s["period_end"] == "2025-06-30" and s["dates"] == ["2025-06-30", "2024-06-30"]
    fl.import_statement(db, s, mapping_of(s))
    older = stmt(2023, close="06-30")
    fl.import_statement(db, older, mapping_of(older))
    assert_matches_oracle(db, {2022, 2023, 2024, 2025})
    assert db.query(RawImportRecord).filter_by(dataset_name="Balance Sheet PDF 2025-06-30").count() == 1


def test_a_restated_comparative_is_a_conflict_the_newest_statement_wins_by_default_and_the_operator_can_overrule(db):
    first = stmt(2024)
    fl.import_statement(db, first, mapping_of(first))
    restated = stmt(2025, prior={"revenue": figures(2024)["revenue"] + 250000})  # FY2025's comparative column restates FY2024
    plan = fl.plan_statement(db, restated, mapping_of(restated))
    conflict = next(c for c in plan["merged"]["conflicts"] if c["key"] == "revenue|2024")
    assert conflict["chosen"] == fl.statement_dataset("2025-12-31") and conflict["default"] == conflict["chosen"]
    assert {o["dataset"] for o in conflict["options"]} == {fl.statement_dataset("2024-12-31"), fl.statement_dataset("2025-12-31")}
    fl.import_statement(db, restated, mapping_of(restated))
    assert ledger(db)["revenue_y-1"] == pytest.approx((figures(2024)["revenue"] + 250000) / 1000)
    fl.apply_decisions(db, db.query(Company).one(), {"revenue|2024": fl.statement_dataset("2024-12-31")})
    assert ledger(db)["revenue_y-1"] == pytest.approx(figures(2024)["revenue"] / 1000)
    assert signals(db)["revenue_trend"] == pytest.approx((figures(2025)["revenue"] - figures(2023)["revenue"]) / figures(2023)["revenue"] * 100, abs=1e-3)


def test_the_year_comes_from_the_column_headings_not_from_a_date_elsewhere_in_the_text():
    pages = page_text("12-31", (2025, figures(2025)), (2024, figures(2024)))
    pages[0]["text"] = pages[0]["text"].replace("al 31/12/2025", "al 31/12/2024")  # a cover that names last year
    s = bs.parse_pages(pages)
    assert s["period_end"] == "2025-12-31" and s["dates"] == ["2025-12-31", "2024-12-31"]
    assert any("column headings are used" in w for w in s["warnings"])
    assert next(l for l in s["lines"] if "Cassa" in l["label"])["values"]["2025-12-31"] == figures(2025)["cash"]


def test_an_italian_date_in_words_is_read():
    pages = page_text("12-31", (2025, figures(2025)), (2024, figures(2024)))
    for p in pages:
        p["text"] = re.sub(r"(STATO PATRIMONIALE \w+|CONTO ECONOMICO) \d\d-\d\d-\d{4} \d\d-\d\d-\d{4}", r"\1", p["text"])
    pages[0]["text"] = pages[0]["text"].replace("Bilancio di esercizio al 31/12/2025", "Bilancio di esercizio al 31 dicembre 2025")
    s = bs.parse_pages(pages)
    assert s["period_end"] == "2025-12-31" and s["dates"] == ["2025-12-31", "2024-12-31"]


def test_a_statement_without_a_readable_date_is_dated_by_the_operator_and_lands_in_the_right_years(db):
    pages = page_text("12-31", (2025, figures(2025)), (2024, figures(2024)))
    pages[0]["text"] = pages[0]["text"].replace("Bilancio di esercizio al 31/12/2025\n", "")
    for p in pages:
        p["text"] = re.sub(r"(STATO PATRIMONIALE \w+|CONTO ECONOMICO) \d\d-\d\d-\d{4} \d\d-\d\d-\d{4}", r"\1", p["text"])
    s = bs.parse_pages(pages, "undated.pdf", "sha")
    assert s["period_end"] is None and s["columns"] == ["column 1", "column 2"]
    with pytest.raises(ValueError, match="statement date is missing"):
        fl.plan_statement(db, s, mapping_of(s), unit="EUR")
    s["period_end"] = "2025-12-31"  # a date typed in without dating the columns must not import under placeholder years
    with pytest.raises(ValueError, match="no dates yet"):
        fl.plan_statement(db, s, mapping_of(s), unit="EUR")
    s["period_end"] = None
    bs.apply_period_end(s, "2025-12-31")
    assert s["period_end"] == "2025-12-31" and s["columns"] == ["2025-12-31", "2024-12-31"]
    assert not any("could not be read" in w for w in s["warnings"])
    assert next(l for l in s["lines"] if "Cassa" in l["label"])["values"] == {"2025-12-31": figures(2025)["cash"], "2024-12-31": figures(2024)["cash"]}
    fl.import_statement(db, s, mapping_of(s), unit="EUR")
    assert_matches_oracle(db, {2024, 2025})
    bs.apply_period_end(s, "2025-12-31")  # idempotent
    assert s["columns"] == ["2025-12-31", "2024-12-31"]


def test_year_only_column_headings_place_a_statement_that_prints_the_older_year_first():
    pages = [dict(page=1, method="text", text="ROSSI SRL\nCodice fiscale 01234567890\nImporti espressi in unita di euro"),
             dict(page=2, method="text", text="STATO PATRIMONIALE ATTIVO\nEsercizio 2024 Esercizio 2025\n10. Cassa 111 222\nTOTALE ATTIVO 333 444")]
    s = bs.parse_pages(pages)
    assert s["column_years"] == [2024, 2025] and s["period_end"] is None
    assert "headed 2024 and 2025" in " ".join(s["warnings"])
    bs.apply_period_end(s, "2025-12-31")
    cash = next(l for l in s["lines"] if "Cassa" in l["label"])
    assert cash["values"] == {"2024-12-31": 111.0, "2025-12-31": 222.0}  # the first printed column is the OLDER year
    assert s["period_end"] == "2025-12-31"


def test_the_ai_reading_puts_each_figure_in_the_year_of_its_printed_column_even_when_the_older_year_is_first():
    ps = [dict(page=1, method="text", text="Bilancio al 31/12/2025\nROSSI SRL\nCodice fiscale 01234567890\nImporti espressi in unita di euro"),
          dict(page=2, method="text", text="STATO PATRIMONIALE ATTIVO 31/12/2024 31/12/2025\n10. Cassa 111 222")]
    s = bs.parse_pages(ps)
    assert s["columns"] == ["2024-12-31", "2025-12-31"]
    extra = [dict(page=2, method="text", text=ps[1]["text"] + "\n20. Crediti verso clienti 7 9")]
    bs.enrich_with_ai(s, extra, lambda m: json.dumps({"company": {}, "facts": [
        {"page": 2, "statement": "assets", "item": "20", "label": "Crediti verso clienti", "printed": ["7", "9"]}]}))
    clients = next(l for l in s["lines"] if "Crediti" in l["label"])
    assert clients["values"] == {"2024-12-31": 7.0, "2025-12-31": 9.0}


def test_the_preview_says_where_every_year_lands_and_what_it_does_to_what_is_known(db):
    first = stmt(2024)
    fl.import_statement(db, first, mapping_of(first))
    new = stmt(2025, prior={"revenue": figures(2024)["revenue"] + 250000})
    plan = fl.plan_statement(db, new, mapping_of(new))
    rows = {r["year"]: r for r in plan["placement"]}
    assert set(rows) == {2024, 2025} and rows[2025]["lands_as"] == "latest" and rows[2024]["lands_as"] == "y-1"
    assert rows[2025]["new"] == rows[2025]["figures"] and rows[2025]["confirms"] == rows[2025]["differs"] == 0
    assert rows[2024]["differs"] == 1 and rows[2024]["confirms"] == rows[2024]["figures"] - 1
    far = stmt(2025)
    far["lines"] = [dict(l, values={d.replace("2025", "2031").replace("2024", "2030"): v for d, v in l["values"].items()}) for l in far["lines"]]
    far["period_end"] = "2031-12-31"
    assert any("has not happened yet" in w for w in fl.plan_statement(db, far, mapping_of(far))["warnings"])


def test_the_dossier_names_the_fiscal_years_and_shows_each_figure_in_its_year(db, tmp_path):
    for year in (2023, 2025, 2024):
        s = stmt(year)
        fl.import_statement(db, s, mapping_of(s), replace_statement=True)
    import dossier_data as dd
    blob = ledger(db)
    fin = dd.financial_statement(blob)
    assert fin["labels"] == {"latest": "FY2025", "y-1": "FY2024", "y-2": "FY2023"}
    for period, year in (("latest", 2025), ("y-1", 2024), ("y-2", 2023)):
        assert fin["lines"]["revenue"][period] == pytest.approx(figures(year)["revenue"] / 1000)
        assert fin["lines"]["cash"][period] == pytest.approx(figures(year)["cash"] / 1000)
    assert fin["ratios"]["revenue_growth"]["latest"] == pytest.approx(
        (figures(2025)["revenue"] - figures(2024)["revenue"]) / figures(2024)["revenue"] * 100)
    assert fin["ratios"]["revenue_growth"]["y-1"] == pytest.approx(
        (figures(2024)["revenue"] - figures(2023)["revenue"]) / figures(2023)["revenue"] * 100)


def test_upload_page_asks_for_the_date_of_an_undated_pdf_and_shows_where_each_year_lands(tmp_path):
    import textwrap
    from datetime import date
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    root = str(Path(__file__).resolve().parents[1])
    db_path = tmp_path / "undated.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        seed_indicator_definitions(session)
    script = textwrap.dedent(f"""
        import re, sys
        sys.path.insert(0, {root!r})
        sys.path.insert(0, {str(Path(__file__).parent)!r})
        import streamlit as st
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        import balance_sheet_pdf as bs
        import test_year_placement as t
        pages = t.page_text("12-31", (2025, t.figures(2025)), (2024, t.figures(2024)))
        pages[0]["text"] = pages[0]["text"].replace("Bilancio di esercizio al 31/12/2025\\n", "")
        for p in pages:
            p["text"] = re.sub(r"(STATO PATRIMONIALE \\w+|CONTO ECONOMICO) \\d\\d-\\d\\d-\\d{{4}} \\d\\d-\\d\\d-\\d{{4}}", r"\\1", p["text"])
        real_extract, real_uploader = bs.extract_pages, st.file_uploader
        bs.extract_pages = lambda content, ocr=None: pages
        class Upload:
            name = "undated.pdf"
            def getvalue(self): return b"%PDF-fake"
        st.file_uploader = lambda *a, **k: Upload()
        engine = create_engine({f"sqlite:///{db_path}"!r}, connect_args={{"check_same_thread": False}})
        from views.balance_sheet_upload import render_upload
        try:
            render_upload(sessionmaker(bind=engine)())
        finally:
            bs.extract_pages, st.file_uploader = real_extract, real_uploader
    """)
    app = AppTest.from_string(script, default_timeout=120).run()
    assert not app.exception, [e.value for e in app.exception]
    assert any("Enter the closing date" in i.value for i in app.info)
    assert not [b for b in app.button if b.key == "bs_confirm"]
    app.date_input[0].set_value(date(2025, 12, 31)).run()
    assert not app.exception, [e.value for e in app.exception]
    assert "2025-12-31" in [m.value for m in app.metric]
    placement = next(df for df in app.dataframe if "Fiscal year" in df.value.columns)
    assert list(placement.value["Fiscal year"]) == ["FY2025", "FY2024"] and list(placement.value["Becomes"]) == ["Latest", "Y-1"]
    app.button(key="bs_confirm").click().run()
    assert any("Latest year is now FY2025" in s.value for s in app.success), [e.value for e in app.error]
    with sessionmaker(bind=engine)() as session:
        row = session.query(RawImportRecord).filter_by(dataset_name=fl.LEDGER_DATASET).one().raw_row
        assert row["derived_last_accounts_year"] == 2025 and row["cash_y-1"] == pytest.approx(figures(2024)["cash"] / 1000)


def test_trend_charts_keep_time_order_and_name_the_fiscal_years(tmp_path):
    """st.line_chart sorted "Latest, Y-1, Y-2" alphabetically, which drew time backwards; the dossier now orders by time."""
    import test_company_dossier as tcd
    from streamlit.testing.v1 import AppTest
    path = tmp_path / "trend.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        seed_indicator_definitions(session)
        for year in (2023, 2025, 2024):  # uploaded out of order on purpose
            s = stmt(year)
            fl.import_statement(session, s, mapping_of(s), replace_statement=True)
        company_id = session.query(Company).one().id
    app = AppTest.from_string(tcd._page_script(path, f'st.session_state["dossier_open_id"] = "{company_id}"'), default_timeout=120).run()
    app.session_state["dossier_tab"] = "financials"
    app.run()
    assert not app.exception, [e.value for e in app.exception]
    series = [(list(d["x"]), list(d["y"])) for d in (json.loads(c.proto.spec)["data"][0] for c in app.get("plotly_chart"))]
    revenue = [(x, y) for x, y in series if x == ["FY2022", "FY2023", "FY2024", "FY2025"]
               and y == pytest.approx([figures(y_)["revenue"] / 1000 for y_ in range(2022, 2026)])]
    assert revenue, series  # oldest year first, each point in its own fiscal year, whatever order the files were uploaded in
    assert all(x == sorted(x) for x, _ in series)


def test_axis_labels_are_fiscal_years_when_the_record_is_dated_and_relative_otherwise():
    from views.company_detail import _period_labels
    series = [("Y-2", 1.0), ("Y-1", 2.0), ("Latest", 3.0)]
    assert _period_labels(series, {"derived_last_accounts_year": 2025}) == ["FY2023", "FY2024", "FY2025"]
    assert _period_labels(series, {"accounts_close": "2024-06-30T00:00:00"}) == ["FY2022", "FY2023", "FY2024"]
    assert _period_labels(series, {}) == ["Y-2", "Y-1", "Latest"]
    assert sorted(_period_labels(series, {"derived_last_accounts_year": 2025})) == _period_labels(series, {"derived_last_accounts_year": 2025})


def test_figures_without_a_closing_date_are_reported_as_left_out_not_dropped_silently(db):
    s = stmt(2025)
    fl.import_statement(db, s, mapping_of(s))
    company = db.query(Company).one()
    db.add(RawImportRecord(company_id=company.id, dataset_name="Old spreadsheet", raw_row={"revenue_latest": 1.0, "revenue_y-1": 2.0},
                           mapping_snapshot={}))
    db.add(RawImportRecord(company_id=company.id, dataset_name="Notes", raw_row={"comment": "no figures here"}, mapping_snapshot={}))
    db.commit()
    assert fl.unplaced_datasets(db, company.id) == ["Old spreadsheet"]
    plan = fl.plan_statement(db, stmt(2024), mapping_of(stmt(2024)))
    assert any("Old spreadsheet" in w and "cannot be placed in a fiscal year" in w for w in plan["warnings"])
    assert not any("Notes" in w for w in plan["warnings"])
