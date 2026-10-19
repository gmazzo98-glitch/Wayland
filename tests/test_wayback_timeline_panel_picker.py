"""
The Wayback Timeline company list can be filled from Vienna's own companies (shortlist status,
segment, country, sector, scores) instead of typed in by hand. Rendered through AppTest against a
tiny real SQLite database, like tests/test_paid_shortlist_gate_ui.py.
"""

import csv
import io
import textwrap
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from streamlit.testing.v1 import AppTest

from models import Base, Company

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)


def _make_db(tmp_path):
    db_path = (tmp_path / "wb.db").as_posix()
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.add_all([
            Company(id="a", legal_name="Alpha SRL", registration_number="IT001", country="Italy", segment="SME",
                    website_url="www.alpha.it", shortlist_status="shortlisted", need_score=70.0, readiness_score=40.0),
            Company(id="b", legal_name="Beta SPA", registration_number="IT002", country="Italy", segment="SME",
                    website_url=None, shortlist_status="shortlisted"),
            Company(id="c", legal_name="Gamma GmbH", registration_number="DE003", country="Germany", segment="Midcap",
                    website_url="https://gamma.de", shortlist_status="candidate", need_score=90.0, readiness_score=80.0),
        ])
        db.commit()
    return db_path


def _render(db_path):
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {_PROJECT_ROOT!r})
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from views.wayback_timeline_panel import _render_companies

        engine = create_engine({f"sqlite:///{db_path}"!r}, connect_args={{"check_same_thread": False}})
        db = sessionmaker(bind=engine)()
        _render_companies(db, "worker-1")
    """)
    return AppTest.from_string(script).run(timeout=60)


def _list_ids(app):
    return [r["company_id"] for r in csv.DictReader(io.StringIO(app.session_state["wb_companies_csv"]))]


def _caption_text(app):
    return " ".join(c.value for c in app.caption)


def test_default_filter_offers_only_shortlisted_companies_that_have_a_website(tmp_path):
    app = _render(_make_db(tmp_path))
    assert _list_ids(app) == ["illy", "brembo"]
    text = _caption_text(app)
    assert "1 companies match and can be added" in text
    assert "1 matching without a website" in text


def test_adding_all_shown_appends_with_a_normalized_url_and_keeps_existing_rows(tmp_path):
    app = _render(_make_db(tmp_path))
    next(b for b in app.button if b.label == "Add all 1 shown").click().run(timeout=60)

    rows = list(csv.DictReader(io.StringIO(app.session_state["wb_companies_csv"])))
    assert [r["company_id"] for r in rows] == ["illy", "brembo", "IT001"]
    added = rows[-1]
    assert added["homepage_url"] == "https://www.alpha.it"
    assert added["name"] == "Alpha SRL"
    assert "already in the list" in _caption_text(app)


def test_status_filter_can_be_widened_to_other_columns(tmp_path):
    app = _render(_make_db(tmp_path))
    next(w for w in app.multiselect if w.label == "Shortlist status").set_value(["candidate"]).run(timeout=60)
    assert "1 companies match and can be added" in _caption_text(app)

    ready = next(w for w in app.number_input if w.label == "Min readiness score (0 = ignore)")
    app = ready.set_value(75.0).run(timeout=60)
    assert "1 companies match and can be added" in _caption_text(app)

    ready = next(w for w in app.number_input if w.label == "Min readiness score (0 = ignore)")
    app = ready.set_value(85.0).run(timeout=60)
    assert "0 companies match and can be added" in _caption_text(app)


def test_clear_list_empties_it(tmp_path):
    app = _render(_make_db(tmp_path))
    next(b for b in app.button if b.label == "Clear list").click().run(timeout=60)
    assert _list_ids(app) == []
