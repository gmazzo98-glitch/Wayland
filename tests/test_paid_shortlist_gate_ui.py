"""
Streamlit-rendering smoke test for the "Remove from Shortlist" action added to
views/paid_shortlist_gate.py. Uses streamlit.testing.v1.AppTest the same way
tests/test_access_control.py and tests/test_company_detail_parent_card.py already do, so this
never needs the full app's password gate or a browser - and critically, never touches the live
DB's ~953-company gate-eligibility loop (slow enough over a real network round trip that it's
impractical to click through live for a quick check; this seeds exactly the 2 companies it needs).
"""

import textwrap
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from streamlit.testing.v1 import AppTest

from indicators import seed_indicator_definitions
from models import Base, Company

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)


def _make_db_file(tmp_path):
    db_path = (tmp_path / "ui.db").as_posix()
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        seed_indicator_definitions(db)
    return db_path, factory


def _run_render(db_path: str):
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {_PROJECT_ROOT!r})
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from views.paid_shortlist_gate import render_paid_shortlist_gate_page

        engine = create_engine({f"sqlite:///{db_path}"!r}, connect_args={{"check_same_thread": False}})
        db = sessionmaker(bind=engine)()
        render_paid_shortlist_gate_page(db)
    """)
    return AppTest.from_string(script).run(timeout=60)


def test_removing_one_company_leaves_the_other_shortlisted(tmp_path):
    db_path, factory = _make_db_file(tmp_path)
    with factory() as db:
        db.add(Company(id="c1", legal_name="Remove Me SRL", registration_number="IT00000000011",
                       country="Italy", segment="SME", shortlist_status="shortlisted",
                       need_score=50.0, readiness_score=50.0))
        db.add(Company(id="c2", legal_name="Keep Me SRL", registration_number="IT00000000012",
                       country="Italy", segment="SME", shortlist_status="shortlisted",
                       need_score=50.0, readiness_score=50.0))
        db.commit()

    app = _run_render(db_path)
    ms = next(w for w in app.multiselect if w.label == "Select companies to remove")
    assert "Remove Me SRL (IT00000000011)" in ms.options
    assert "Keep Me SRL (IT00000000012)" in ms.options

    remove_btn = next(b for b in app.button if b.label == "Remove selected from shortlist")
    assert remove_btn.disabled, "must start disabled — nothing picked yet"

    app = ms.set_value(["Remove Me SRL (IT00000000011)"]).run(timeout=60)
    remove_btn = next(b for b in app.button if b.label == "Remove selected from shortlist")
    assert not remove_btn.disabled
    remove_btn.click().run(timeout=60)

    with factory() as db:
        assert db.query(Company).filter_by(id="c1").first().shortlist_status == "candidate"
        assert db.query(Company).filter_by(id="c2").first().shortlist_status == "shortlisted"


def test_removing_is_scoped_to_only_what_was_selected_not_everyone_shortlisted(tmp_path):
    """Regression guard: a multiselect that starts pre-populated by a stray earlier interaction
    (e.g. a "Select all" click landing on the wrong widget) must never silently demote every
    shortlisted company - this pins the button to act on exactly the picked set, nothing else."""
    db_path, factory = _make_db_file(tmp_path)
    with factory() as db:
        for i in range(5):
            db.add(Company(id=f"c{i}", legal_name=f"Company {i} SRL", registration_number=f"IT0000000{i:04d}",
                           country="Italy", segment="SME", shortlist_status="shortlisted",
                           need_score=50.0, readiness_score=50.0))
        db.commit()

    app = _run_render(db_path)
    ms = next(w for w in app.multiselect if w.label == "Select companies to remove")
    app = ms.set_value(["Company 2 SRL (IT00000000002)"]).run(timeout=60)
    remove_btn = next(b for b in app.button if b.label == "Remove selected from shortlist")
    remove_btn.click().run(timeout=60)

    with factory() as db:
        statuses = {db.query(Company).filter_by(id=f"c{i}").first().shortlist_status for i in range(5)}
        removed = db.query(Company).filter_by(id="c2").first().shortlist_status
    assert removed == "candidate"
    assert statuses == {"candidate", "shortlisted"}  # exactly one changed, not all five
