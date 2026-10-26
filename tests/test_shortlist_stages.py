"""The company funnel has one definition (models.SHORTLIST_STAGES) and parked companies stay out of working lists."""

import pytest

from database import ScopedSession, init_db
from models import (Company, SHORTLIST_STAGES, SHORTLIST_STATUSES, is_parked, is_shortlisted, set_shortlist_status,
                    stage_label)
from site_evidence_hub import build_company_list


def test_every_status_has_a_label_and_a_meaning():
    assert set(SHORTLIST_STAGES) == set(SHORTLIST_STATUSES)
    assert all(label and meaning for label, meaning in SHORTLIST_STAGES.values())


def test_shortlisted_and_in_pilot_are_unlocked_candidate_and_parked_are_not():
    assert [s for s in SHORTLIST_STATUSES if is_shortlisted(s)] == ["shortlisted", "in_pilot"]
    assert is_parked("rejected") and not is_parked("candidate")
    assert stage_label("rejected") == "Parked"


def test_set_status_stamps_shortlisted_at_once_and_rejects_unknown_values():
    c = Company(legal_name="A", registration_number="r1")
    set_shortlist_status(c, "shortlisted")
    stamped = c.shortlisted_at
    assert stamped is not None
    set_shortlist_status(c, "in_pilot")
    assert c.shortlisted_at == stamped
    with pytest.raises(ValueError):
        set_shortlist_status(c, "maybe")


def test_parked_companies_are_left_out_of_the_crawl_list():
    init_db()
    db = ScopedSession()
    try:
        for reg, status in (("p-1", "candidate"), ("p-2", "shortlisted"), ("p-3", "rejected")):
            db.add(Company(legal_name=reg, registration_number=reg, website_url=f"https://{reg}.example",
                           shortlist_status=status))
        db.commit()
        csv_text, kept, _ = build_company_list(db)
        assert "p-1" in csv_text and "p-2" in csv_text and "p-3" not in csv_text
        assert csv_text.index("p-2") < csv_text.index("p-1")  # shortlist first
    finally:
        db.query(Company).filter(Company.registration_number.in_(["p-1", "p-2", "p-3"])).delete(synchronize_session=False)
        db.commit()
        db.close()


# ---- changing stages from the UI ------------------------------------------------------------------------------
from tests.test_company_dossier import _page_app, _seed  # noqa: E402  (same seeded Rossi/Bianchi database)
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402


def _stage_of(db_path, company_id):
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    with sessionmaker(bind=engine)() as db:
        return db.query(Company).filter_by(id=company_id).first().shortlist_status


def test_apply_stage_counts_only_real_changes(tmp_path):
    from views.stage_controls import apply_stage
    db_path = _seed(tmp_path)
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    with sessionmaker(bind=engine)() as db:
        assert apply_stage(db, ["c1", "c2", "c2"], "shortlisted") == 1  # c1 already was; c2 listed twice counts once
        assert apply_stage(db, [], "rejected") == 0
    assert _stage_of(db_path, "c2") == "shortlisted"


def test_dossier_header_changes_the_stage_immediately(tmp_path):
    db_path = _seed(tmp_path)
    app = _page_app(db_path, 'import streamlit as st\nst.session_state["dossier_open_id"] = "c1"\n'
                             'from views.company_dossier import render_company_detail_page\nrender_company_detail_page(db)').run()
    assert not app.exception, [e.value for e in app.exception]
    next(w for w in app.selectbox if w.key == "dos_hdr_stage_c1_shortlisted").select("in_pilot").run()
    assert _stage_of(db_path, "c1") == "in_pilot"


def test_bulk_control_sets_all_picked_companies(tmp_path):
    db_path = _seed(tmp_path)
    app = _page_app(db_path, "from views.stage_controls import render_stage_bulk\n"
                             "render_stage_bulk(db, {'Rossi': 'c1', 'Bianchi': 'c2'}, key='t', expanded=True)").run()
    assert not app.exception, [e.value for e in app.exception]
    next(w for w in app.checkbox if w.key == "t_all").check().run()
    next(w for w in app.selectbox if w.key == "t_stage").select("rejected").run()
    next(b for b in app.button if b.key == "t_apply").click().run()
    assert {_stage_of(db_path, "c1"), _stage_of(db_path, "c2")} == {"rejected"}


# ---- the per-stage lists ----------------------------------------------------------------------------------------
import pandas as pd  # noqa: E402
from views.stage_lists import list_counts, list_frame  # noqa: E402


def test_list_frame_filters_by_stage_searches_and_ranks_by_need():
    df = pd.DataFrame({
        "id": ["a", "b", "c", "d"], "legal_name": ["Alfa Srl", "Beta Spa", "Alfieri Spa", "Gamma"],
        "registration_number": ["IT1", "IT2", "IT3", "IT4"], "need_score": [10.0, 80.0, 50.0, 90.0],
        "readiness_score": [1.0, 1.0, 1.0, 1.0], "stage_key": ["shortlisted", "shortlisted", "shortlisted", "candidate"]})
    assert list_counts(df) == {"candidate": 1, "shortlisted": 3, "in_pilot": 0, "rejected": 0}
    assert list(list_frame(df, "shortlisted")["id"]) == ["b", "c", "a"]
    assert list(list_frame(df, "shortlisted", "alf")["id"]) == ["c", "a"]
    assert list_frame(df, "in_pilot").empty


def test_lists_page_shows_the_stage_and_removes_ticked_companies_from_it(tmp_path):
    db_path = _seed(tmp_path)
    app = _page_app(db_path, "from views.stage_lists import render_stage_lists\nrender_stage_lists(db)").run()
    assert not app.exception, [e.value for e in app.exception]
    assert app.session_state["lists_stage"] == "shortlisted"  # c1 (Rossi) is the seeded shortlisted company
    app.session_state["matrix_crawl_selection"] = {"c1"}
    app = app.run()
    next(b for b in app.button if b.key == "list_leave_shortlisted").click().run()
    assert _stage_of(db_path, "c1") == "candidate"
