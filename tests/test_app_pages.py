"""Every page of the real app renders on a database that holds a company built from a PDF statement.

The page list comes from the app's own navigation radio, so a page added later is covered without touching this file.
Several views (pipeline health, indicator weights, crawler setup) have no test of their own; this is their net.
"""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import database
import financial_ledger as fl
from test_balance_sheet_import import mapping_of, oic_pages
import balance_sheet_pdf as bs

APP = str(Path(__file__).resolve().parents[1] / "app.py")


@pytest.fixture()
def app_with_pdf_company(monkeypatch):
    """The app reads the process-wide database (tests/conftest.py points it at a throwaway SQLite file), so the company is
    put there and taken out again; nothing else in the suite sees it."""
    import config
    from models import Company, RawImportRecord, SignalRecord
    assert config.SQLALCHEMY_DATABASE_URI.startswith("sqlite:///")
    database.init_db()
    session = database.get_db_session()
    company_id = None
    try:
        statement = bs.parse_pages(oic_pages(), "oic.pdf", "sha")
        company_id = fl.import_statement(session, statement, mapping_of(statement))["company_id"]
        monkeypatch.setenv("APP_ACCESS_PASSWORD", "a-test-password-of-sufficient-length")
        app = AppTest.from_file(APP, default_timeout=180)
        app.session_state["_vienna_access_granted"] = True
        yield app.run()
    finally:
        session.rollback()
        if company_id:
            session.query(SignalRecord).filter_by(company_id=company_id).delete()
            session.query(RawImportRecord).filter_by(company_id=company_id).delete()
            session.query(Company).filter_by(id=company_id).delete()
            session.commit()
        session.close()


def test_every_page_renders_with_a_company_that_came_from_a_pdf(app_with_pdf_company):
    app = app_with_pdf_company
    assert not app.exception, [e.value for e in app.exception]
    pages = app.sidebar.radio(key="nav_page").options
    assert len(pages) >= 9
    for page in pages:
        app.sidebar.radio(key="nav_page").set_value(page).run()
        assert not app.exception, (page, [e.value for e in app.exception])


def test_company_intelligence_names_the_pdf_company_and_its_merged_history(app_with_pdf_company):
    app = app_with_pdf_company
    app.sidebar.radio(key="nav_page").set_value("🏢 Company Intelligence").run()
    app.session_state["dossier_tab"] = "financials"
    app.run()
    assert not app.exception, [e.value for e in app.exception]
    text = "\n".join(str(getattr(e, "value", "") or getattr(e, "label", "")) for kind in ("subheader", "markdown", "caption", "metric")
                     for e in app.get(kind))
    assert "Rossi Macchine S.R.L." in text and "merged annual history" in text and "Latest = FY2025" in text
