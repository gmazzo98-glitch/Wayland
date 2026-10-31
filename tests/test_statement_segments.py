"""Business-line figures read from the notes of saved payment-institution statements (statement_segments.py).

The expected numbers are typed here from the printed tables (invented company), never read back from the code under test."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import statement_segments as ss
from balance_sheet_pdf import DATASET_PREFIX
from models import Base, Company, RawImportRecord


def english(year, cur, prev, income=None, label="Fee and commission income"):
    """cur/prev: (mt_amount, mt_ops, mt_commission, acq_amount, acq_ops, acq_commission, acq_recoveries), comma thousands."""
    n = lambda v: f"{v:,}"  # noqa: E731
    table = (
        "E. PAYMENT SERVICES\nE.8 - Operating volumes, number, and revenues of payment transactions\n"
        "Type of transaction Current period Previous period\nAmount of\ntransactions\nNumber of\ntransactions\n"
        "Money transfer \ntransactions\n"
        f"- incoming 0 0 0 0 0 0 0 0\n- outgoing {n(cur[0])} {n(cur[1])} {n(cur[2])} 0 {n(prev[0])} {n(prev[1])} {n(prev[2])} 0\n"
        f"Acquiring service {n(cur[3])} {n(cur[4])} {n(cur[5])} {n(cur[6])} {n(prev[3])} {n(prev[4])} {n(prev[5])} {n(prev[6])}\n"
        "SECTION 3 - INFORMATION ON RISKS\n")
    pages = {"1": f"INCOME STATEMENT 12/31/{year} 12/31/{year - 1}\n40. {label} {n(income[0])} {n(income[1])}\n" if income else "x",
             "2": table,
             "3": f"Receivables from agents 900 800\nPayables to agents 40 30\nThe amounts set aside in the Protection Fund at the end of the "
                  f"financial year amounted to €500 for the money transfer service and €60 for the acquiring service, for a total of €560."}
    return dict(period_end=f"{year}-12-31", filename=f"fs{year}.pdf", page_text=pages)


A24 = ((1000, 10, 300, 200, 20, 40, 5), (800, 8, 260, 100, 10, 25, 3))
A23 = ((800, 8, 260, 100, 10, 25, 3), (600, 6, 200, 40, 4, 15, 1))


def test_extract_reads_rows_years_and_directions():
    e = ss.extract(english(2024, *A24, income=(340, 285)))
    rows = {(r["segment"], r["direction"]): r for r in e["rows"]}
    assert set(rows) == {("money_transfer", "incoming"), ("money_transfer", "outgoing"), ("acquiring", None)}
    assert rows[("money_transfer", "outgoing")]["years"][2024] == dict(amount=1000, operations=10, commission=300, expense_recoveries=0)
    assert rows[("money_transfer", "outgoing")]["years"][2023]["commission"] == 260
    assert rows[("acquiring", None)]["years"][2024]["expense_recoveries"] == 5
    assert rows[("acquiring", None)]["years"][2023]["amount"] == 100
    assert e["agents"]["agent_receivables"] == {2024: 900, 2023: 800}
    assert e["fund"] == {2024: dict(money_transfer=500, acquiring=60)}
    assert [c["ok"] for c in e["checks"]] == [True, True]  # 300+40 and 260+25


def test_commission_cross_check_flags_a_difference_and_reads_the_older_label():
    assert [c["ok"] for c in ss.extract(english(2024, *A24, income=(999, 285), label="Commission income"))["checks"]] == [False, True]


def test_italian_number_format_and_no_table():
    s = english(2024, *A24)
    s["page_text"]["2"] = s["page_text"]["2"].replace("1,000", "1.000")
    s["page_text"]["2"] = "E.8 Volumi operativi\nRimesse di denaro\n- in uscita 1.000 10 300 0 800 8 260 0\nServizio di acquiring 200 20 40 5 100 10 25 3\nSEZIONE 3"
    rows = {(r["segment"], r["direction"]): r for r in ss.extract(s)["rows"]}
    assert rows[("money_transfer", "outgoing")]["years"][2024]["amount"] == 1000
    assert rows[("acquiring", None)]["years"][2023]["commission"] == 25
    assert ss.extract(dict(period_end="2024-12-31", page_text={"1": "STATO PATRIMONIALE"}))["rows"] == []
    assert ss.extract(dict(page_text=s["page_text"]))["rows"] == []  # no year, nothing placed


def test_merge_history_across_statements_and_metrics():
    m = ss.merge([english(2023, *A23), english(2024, *A24)])
    assert m["years"] == [2022, 2023, 2024] and m["restated"] == []
    assert [r["segment"] for r in m["rows"]] == ["money_transfer", "acquiring"]  # the all-zero incoming row is left out
    mt, acq = m["rows"]
    assert mt["years"][2022]["operations"] == 6 and mt["source_year"][2022] == 2023 and mt["source_year"][2023] == 2023
    got = ss.metrics(mt, 2024)
    assert got["average_ticket"] == 100 and got["take_rate"] == pytest.approx(0.3) and got["commission_per_operation"] == 30
    assert got["amount_growth"] == pytest.approx(0.25) and got["operations_growth"] == pytest.approx(0.25)
    assert ss.metrics(acq, 2022)["amount_growth"] is None  # no 2021 printed: stays missing
    assert ss.shares(m, 2024) == {"money_transfer": pytest.approx(300 / 340), "acquiring": pytest.approx(40 / 340)}
    assert m["agents"]["agent_receivables"][2024] == 900 and m["fund"][2024]["acquiring"] == 60


def test_restated_comparative_is_listed_and_the_closing_statement_wins():
    changed = ((800, 8, 270, 100, 10, 25, 3), A24[1])  # FY2023 comparative printed with another commission in the FY2024 statement
    m = ss.merge([english(2023, *A23), english(2024, A24[0], changed[0])])
    mt = m["rows"][0]
    assert mt["years"][2023]["commission"] == 260 and mt["source_year"][2023] == 2023
    assert any(r["year"] == 2023 and "own figures" in r["note"] for r in m["restated"])


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def test_company_segments_reads_saved_statements_and_ignores_others(db):
    company = Company(legal_name="Invented Payments SpA", registration_number="1", country="Italy")
    db.add(company)
    db.flush()
    for year, args in ((2023, A23), (2024, A24)):
        s = english(year, *args)
        db.add(RawImportRecord(company_id=company.id, dataset_name=DATASET_PREFIX + s["period_end"], source_filename=s["filename"],
                               raw_row={"_statement": s}))
    db.add(RawImportRecord(company_id=company.id, dataset_name="Merged Annual Financials", raw_row={"x": 1}))
    db.commit()
    merged = ss.company_segments(db, company.id)
    assert merged["years"] == [2022, 2023, 2024]
    other = Company(legal_name="Plain SpA", registration_number="2", country="Italy")
    db.add(other)
    db.commit()
    assert ss.company_segments(db, other.id) is None


def _business_lines_app():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import test_statement_segments as t
    from balance_sheet_pdf import DATASET_PREFIX
    from models import Base, Company, RawImportRecord
    from views.business_lines import render_business_lines
    from sqlalchemy.pool import StaticPool
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    company = Company(legal_name="Invented Payments SpA", registration_number="1", country="Italy")
    session.add(company)
    session.flush()
    for year, args in ((2023, t.A23), (2024, t.A24)):
        s = t.english(year, *args, income=(340, 285) if year == 2024 else None)
        session.add(RawImportRecord(company_id=company.id, dataset_name=DATASET_PREFIX + s["period_end"], raw_row={"_statement": s}))
    session.commit()
    render_business_lines(session, company)


def test_business_lines_section_renders_the_printed_figures():
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_function(_business_lines_app, default_timeout=30).run()
    assert not at.exception
    assert [m.label for m in at.metric] == ["Money transfer (outgoing) FY2024", "Acquiring (POS) FY2024"]
    assert [m.value for m in at.metric] == ["0.3 k EUR", "0.0 k EUR"]  # invented 300 and 40 euro, shown in thousands
    first = at.dataframe[0].value
    assert list(first["Year"]) == ["FY2024", "FY2023", "FY2022 (comparative)", "FY2024", "FY2023", "FY2022 (comparative)"]
    assert any("lines add to 340" in c.value for c in at.caption)
