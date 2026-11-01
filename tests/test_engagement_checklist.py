"""Engagement checklist: baselines come only from the printed statement figures, progress is saved per topic.

Expected texts are typed here from the invented statement figures in test_statement_segments, never read back from the code."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import engagement_checklist as ec
import statement_segments as ss
from models import Base, Company
from test_statement_segments import A23, A24, english


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def merged():
    return ss.merge([english(2023, *A23), english(2024, *A24)])


def test_topics_are_unique_and_each_baseline_key_is_known():
    ids = [t[0] for t in ec.TOPICS]
    assert len(ids) == len(set(ids)) == 17
    known = {"mt_volumes", "mt_pricing", "mt_unit_revenue", "pos", "agents", "ops_per_employee", None}
    assert {t[3] for t in ec.TOPICS} <= known


def test_baselines_quote_the_printed_figures():
    b = ec.baselines(merged(), employees=5)
    assert "10 outgoing operations" in b["mt_volumes"][0] and "average ticket 100 EUR" in b["mt_volumes"][0]
    assert "operations +25.0% and amount +25.0% on FY2023" in b["mt_volumes"][0]
    assert b["mt_pricing"][0].startswith("Commissions received / amount: FY2022 33.33%, FY2023 32.50%, FY2024 30.00%")
    assert "1.50 EUR commission per money transfer" not in b["mt_unit_revenue"][0] and "30.00 EUR commission per money transfer" in b["mt_unit_revenue"][0]
    assert "2.00 EUR per POS operation" in b["mt_unit_revenue"][0]
    assert "commissions 0.0 k EUR (11.8% of commissions)" in b["pos"][0]  # 40 of 340
    assert "Receivables from agents 0.9 k EUR at FY2024 (0.8 k EUR at FY2023)" in b["agents"][0] and "protection fund 0.5 k EUR money transfer, 0.1 k EUR acquiring" in b["agents"][0]
    assert "30 operations in FY2024 over about 5 employees is 6 operations per employee" in b["ops_per_employee"][0]
    assert "FY2024" in b["pos"][1]


def test_missing_inputs_give_no_baseline_instead_of_a_guess():
    assert ec.baselines(None) == {}
    b = ec.baselines(merged())  # no employee count on file
    assert "ops_per_employee" not in b
    only_mt = ss.merge([english(2024, *A24)])
    only_mt["rows"] = [r for r in only_mt["rows"] if r["segment"] == "money_transfer"]
    only_mt["agents"], only_mt["fund"] = {}, {}
    got = ec.baselines(only_mt, 3)
    assert "pos" not in got and "agents" not in got and "per POS operation" not in got["mt_unit_revenue"][0]


def test_save_keeps_other_topics_and_rejects_unknown_input(db):
    company = Company(legal_name="Invented Payments SpA", registration_number="1", country="Italy")
    db.add(company)
    db.commit()
    assert ec.load(db, company.id) == {} and ec.progress({}) == (0, 17)
    ec.save(db, company.id, "pos_role", "Asked", "Anna", "call Tuesday", "Gio")
    ec.save(db, company.id, "agent_model", "Received", " Legal ", "", "")
    items = ec.load(db, company.id)
    assert items["pos_role"]["status"] == "Asked" and items["pos_role"]["updated_by"] == "Gio"
    assert items["agent_model"]["holder"] == "Legal" and items["agent_model"]["updated_by"] == "unknown"
    ec.save(db, company.id, "pos_role", "Received", "Anna", "done", "Gio")
    assert ec.progress(ec.load(db, company.id)) == (2, 17)
    with pytest.raises(ValueError):
        ec.save(db, company.id, "nope", "Asked", "", "", "")
    with pytest.raises(ValueError):
        ec.save(db, company.id, "pos_role", "Maybe", "", "", "")


def _app():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    import test_statement_segments as t
    from balance_sheet_pdf import DATASET_PREFIX
    from models import Base, Company, RawImportRecord
    from views.engagement_checklist import render_engagement
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    company = Company(legal_name="Invented Payments SpA", registration_number="1", country="Italy")
    session.add(company)
    session.flush()
    for year, args in ((2023, t.A23), (2024, t.A24)):
        s = t.english(year, *args)
        session.add(RawImportRecord(company_id=company.id, dataset_name=DATASET_PREFIX + s["period_end"], raw_row={"_statement": s}))
    session.commit()
    render_engagement(session, company)


def test_section_renders_saves_and_shows_the_statement_baseline():
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_function(_app, default_timeout=30).run()
    assert not at.exception
    assert len(at.expander) == 17
    assert any("Already in the statements:** FY2024: 10 outgoing operations" in m.value for m in at.markdown)
    assert any("nothing usable for this question" in m.value for m in at.markdown)
    at.selectbox(key="eng_s_pos_role").select("Asked")
    at.button(key="eng_b_pos_role").click().run()
    assert not at.exception
    assert at.selectbox(key="eng_s_pos_role").value == "Asked"
