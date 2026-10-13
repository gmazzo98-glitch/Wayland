"""
Streamlit-rendering smoke test for the "Parent / Group Relationship" card added to
_render_crawled_intelligence (see docs/CRAWLER_ROADMAP.md T24's third follow-up). Uses
streamlit.testing.v1.AppTest the same way tests/test_access_control.py already does, so this
never needs the full app's password gate (access_control.require_app_access) or browser.
"""

import textwrap
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from streamlit.testing.v1 import AppTest

from models import Base, Company, RawImportRecord

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)


def _make_db_file(tmp_path):
    db_path = (tmp_path / "ui.db").as_posix()
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return db_path, sessionmaker(bind=engine)


def _run_render(db_path: str, company_id: str):
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {_PROJECT_ROOT!r})
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from models import Company
        from views.company_detail import _render_crawled_intelligence

        engine = create_engine({f"sqlite:///{db_path}"!r}, connect_args={{"check_same_thread": False}})
        db = sessionmaker(bind=engine)()
        company = db.query(Company).filter_by(id={company_id!r}).first()
        _render_crawled_intelligence(db, company)
    """)
    return AppTest.from_string(script).run(timeout=20)


def test_parent_card_shows_scores_and_a_jump_button_when_linked_to_a_real_db_parent(tmp_path):
    db_path, factory = _make_db_file(tmp_path)
    with factory() as db:
        db.add(Company(id="parent1", legal_name="BigCo Holding S.p.A.", registration_number="IT00000000099",
                        country="Italy", segment="Midcap", need_score=42.0, readiness_score=58.0))
        db.add(Company(id="child1", legal_name="Child SRL", registration_number="IT00000000098",
                        country="Italy", segment="SME", parent_company_id="parent1"))
        db.commit()

    app = _run_render(db_path, "child1")

    assert any("BigCo Holding" in md.value for md in app.markdown)
    assert any("42" in m.value for m in app.metric)  # parent's NEED score
    assert any("Open BigCo Holding" in b.label for b in app.button)


def test_parent_card_jump_button_sets_the_company_selector_session_state(tmp_path):
    """The jump button's whole job is pre-selecting the parent in the OTHER tab's company
    picker (views/company_detail.py's `detail_company_select`, keyed by an exact
    "Name (RegNumber) — flag Country [Segment]" label) and clearing any country filter that
    might hide it, then rerunning - this confirms it actually writes those two keys."""
    db_path, factory = _make_db_file(tmp_path)
    with factory() as db:
        db.add(Company(id="parent1", legal_name="BigCo Holding S.p.A.", registration_number="IT00000000099",
                        country="Italy", segment="Midcap", need_score=42.0, readiness_score=58.0))
        db.add(Company(id="child1", legal_name="Child SRL", registration_number="IT00000000098",
                        country="Italy", segment="SME", parent_company_id="parent1"))
        db.commit()

    app = _run_render(db_path, "child1")
    app.button[0].click().run(timeout=20)

    assert app.session_state["detail_country_filter"] == "All Countries"
    assert app.session_state["detail_company_select"] == "BigCo Holding S.p.A. (IT00000000099) — 🇮🇹 Italy [Midcap]"


def test_parent_card_shows_an_unmatched_mention_without_a_jump_button(tmp_path):
    db_path, factory = _make_db_file(tmp_path)
    with factory() as db:
        db.add(Company(id="child1", legal_name="Child SRL", registration_number="IT00000000098",
                        country="Italy", segment="SME"))
        db.add(RawImportRecord(
            company_id="child1", dataset_name="crawler_website_discovery",
            source_filename="crawler:crawler_website_discovery",
            raw_row={"website_url": None, "confidence": None,
                     "parent": {"name": "Some Big Group", "matched_company_id": None, "matched_legal_name": None},
                     "candidates": []},
        ))
        db.commit()

    app = _run_render(db_path, "child1")

    assert any("Some Big Group" in md.value for md in app.markdown)
    assert any("not found as a separate company" in md.value for md in app.markdown)
    assert len(app.button) == 0  # nothing to jump to


def test_no_parent_card_when_no_relationship_was_ever_found(tmp_path):
    db_path, factory = _make_db_file(tmp_path)
    with factory() as db:
        db.add(Company(id="child1", legal_name="Child SRL", registration_number="IT00000000098",
                        country="Italy", segment="SME"))
        db.commit()

    app = _run_render(db_path, "child1")

    assert "Parent / Group Relationship" not in app.get("expander")[0].label if app.get("expander") else True
    assert not any("Parent / Group Relationship" in md.value for md in app.markdown)
