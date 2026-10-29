"""
Company notes: the shared notebook in the dossier and the rule that the AI reads a note only after someone sent it.
"""

import textwrap
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from streamlit.testing.v1 import AppTest

import assistant_llm
import company_brief
import company_notes as cn
from indicators import seed_indicator_definitions
from models import Base, Company, CompanyNote, CompanyNotePresence, RawImportRecord
from painpoints import seed_pain_point_definitions

_ROOT = str(Path(__file__).resolve().parent.parent)


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'notes.db').as_posix()}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    seed_indicator_definitions(session)
    seed_pain_point_definitions(session)
    session.add(Company(id="c1", legal_name="Rossi Macchine S.r.l.", registration_number="IT0001", country="Italy"))
    session.add(Company(id="c2", legal_name="Bianchi Pompe S.p.A.", registration_number="IT0002", country="Italy"))
    session.commit()
    yield session
    session.close()


def test_a_note_needs_text_a_name_and_a_known_kind(db):
    with pytest.raises(ValueError, match="empty"):
        cn.add_note(db, "c1", "Anna", "   ")
    with pytest.raises(ValueError, match="name"):
        cn.add_note(db, "c1", "  ", "hello")
    with pytest.raises(ValueError, match="kind"):
        cn.add_note(db, "c1", "Anna", "hello", kind="gossip")
    with pytest.raises(ValueError, match="limited"):
        cn.add_note(db, "c1", "Anna", "x" * (cn.MAX_BODY + 1))
    assert cn.list_notes(db, "c1") == []


def test_notes_are_private_to_the_team_until_sent_to_the_ai(db):
    note = cn.add_note(db, "c1", "Anna", "The owner wants to retire in two years.", kind="insight")
    assert cn.ai_notes(db, "c1") == []
    cn.set_ai(db, note.id, True, "Anna")
    [seen] = cn.ai_notes(db, "c1")
    assert seen["written_by"] == "Anna" and seen["kind"] == "Insight" and "retire" in seen["text"]
    assert "not verified" in seen["source"]
    cn.set_ai(db, note.id, False, "Anna")
    assert cn.ai_notes(db, "c1") == []


def test_notes_sent_for_one_company_never_reach_another(db):
    cn.add_note(db, "c1", "Anna", "private to Rossi", send_to_ai=True)
    assert cn.ai_notes(db, "c2") == []


def test_deleting_a_note_removes_its_replies_and_withdraws_it_from_the_ai(db):
    root = cn.add_note(db, "c1", "Anna", "Root", send_to_ai=True)
    cn.add_note(db, "c1", "Ben", "Reply", parent_id=root.id)
    cn.delete_note(db, root.id)
    assert cn.list_notes(db, "c1") == [] and cn.ai_notes(db, "c1") == []
    assert db.query(CompanyNote).count() == 2          # soft delete: the trace stays
    with pytest.raises(ValueError):
        cn.set_ai(db, root.id, True, "Anna")


def test_replies_stay_one_level_deep_and_threads_sort_by_pin_then_activity(db):
    a = cn.add_note(db, "c1", "Anna", "first")
    b = cn.add_note(db, "c1", "Ben", "second")
    reply = cn.add_note(db, "c1", "Cara", "re: first", parent_id=a.id)
    nested = cn.add_note(db, "c1", "Dan", "re: re: first", parent_id=reply.id)
    assert nested.parent_id == a.id
    order = [t[0].id for t in cn.threads(cn.list_notes(db, "c1"))]
    assert order == [a.id, b.id]                       # a has the newest activity (the replies)
    cn.set_pinned(db, b.id, True)
    assert [t[0].id for t in cn.threads(cn.list_notes(db, "c1"))] == [b.id, a.id]


def test_editing_a_note_someone_else_changed_is_refused_not_overwritten(db):
    note = cn.add_note(db, "c1", "Anna", "v1")
    opened_at = note.updated_at
    time.sleep(0.01)
    cn.edit_note(db, note.id, "Ben", "v2 by Ben")
    with pytest.raises(cn.NoteConflict):
        cn.edit_note(db, note.id, "Anna", "v2 by Anna", seen_updated_at=opened_at)
    db.refresh(note)
    assert note.body == "v2 by Ben" and note.edited_by == "Ben"


def test_two_writers_at_once_both_keep_their_notes(db):
    cn.add_note(db, "c1", "Anna", "from Anna")
    cn.add_note(db, "c1", "Ben", "from Ben")
    assert {n.author for n in cn.list_notes(db, "c1")} == {"Anna", "Ben"}


def test_presence_lists_the_others_who_have_the_notes_open_recently(db):
    assert cn.touch_presence(db, "c1", "Anna") == []
    assert cn.touch_presence(db, "c1", "Ben") == ["Anna"]
    assert cn.touch_presence(db, "c1", "Anna") == ["Ben"]
    assert cn.touch_presence(db, "c2", "Anna") == []   # another company's notebook
    row = db.query(CompanyNotePresence).filter_by(company_id="c1", author="Anna").one()
    row.seen_at = datetime.utcnow() - cn.PRESENT_WITHIN - timedelta(seconds=5)
    db.commit()
    assert cn.touch_presence(db, "c1", "Ben") == []    # Anna left


def test_the_assistant_tools_return_only_sent_notes_and_label_them(db):
    cn.add_note(db, "c1", "Anna", "kept private")
    cn.add_note(db, "c1", "Ben", "Plant expansion planned for 2027.", kind="proposal", send_to_ai=True)
    out = assistant_llm._execute_tool(db, "get_company_notes", {"company_name": "Rossi Macchine"})
    assert [n["written_by"] for n in out["notes"]] == ["Ben"]
    assert out["notes"][0]["source"] == cn.AI_SOURCE
    profile = assistant_llm._execute_tool(db, "get_company_profile", {"company_name": "Rossi Macchine"})
    assert [n["text"] for n in profile["analyst_notes"]] == ["Plant expansion planned for 2027."]
    none_sent = assistant_llm._execute_tool(db, "get_company_notes", {"company_name": "Bianchi Pompe"})
    assert none_sent["notes"] == [] and "No notes" in none_sent["note"]
    assert any(t["function"]["name"] == "get_company_notes" for t in assistant_llm.TOOLS)
    assert "analyst note" in assistant_llm.SYSTEM_PROMPT


def test_the_ai_brief_gets_notes_as_labelled_evidence_but_never_builds_on_notes_alone(db):
    company = db.query(Company).filter_by(id="c1").one()
    cn.add_note(db, "c1", "Anna", "Owner is open to a minority partner.", send_to_ai=True)
    assert company_brief.collect_evidence(db, company) == []           # opinion alone is not evidence
    db.add(RawImportRecord(company_id="c1", dataset_name="crawler_company_website", source_filename="crawler",
                           raw_row={"product_types": ["pumps"], "homepage_url": "https://rossi.example"}))
    db.commit()
    evidence = company_brief.collect_evidence(db, company)
    kinds = [e["kind"] for e in evidence]
    assert "analyst_note" in kinds and "company_website_product_lines" in kinds
    before = company_brief.evidence_fingerprint(evidence)
    cn.add_note(db, "c1", "Ben", "Second sent note.", send_to_ai=True)
    assert company_brief.evidence_fingerprint(company_brief.collect_evidence(db, company)) != before   # brief goes stale


# ------------------------------------------------------------------------------------------------- the rendered tab

def _app(tmp_path):
    db_path = (tmp_path / "ui.db").as_posix()
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as s:
        s.add(Company(id="c1", legal_name="Rossi Macchine S.r.l.", registration_number="IT0001", country="Italy"))
        s.commit()
        cn.add_note(s, "c1", "Anna", "Existing note from Anna", kind="insight")
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {_ROOT!r})
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from models import Company
        from views.company_notes_panel import render_notes_section
        engine = create_engine({f"sqlite:///{db_path}"!r}, connect_args={{"check_same_thread": False}})
        db = sessionmaker(bind=engine)()
        render_notes_section(db, db.query(Company).first())
    """)
    return AppTest.from_string(script, default_timeout=60), db_path


def test_the_notes_panel_shows_existing_notes_and_saves_a_new_one(tmp_path):
    app, db_path = _app(tmp_path)
    app.run()
    assert not app.exception, [e.value for e in app.exception]
    assert any("Existing note from Anna" in m.value for m in app.markdown)

    # no name yet: the note is refused with a clear message and nothing is saved
    app.text_area(key="notes_body_c1").set_value("Needs a name").run()
    app.button(key="notes_save_c1").click().run()
    assert any("name" in e.value for e in app.error)

    app.text_input(key="notes_author").set_value("Ben").run()
    app.checkbox(key="notes_ai_c1").check().run()
    app.button(key="notes_save_c1").click().run()
    assert not app.exception, [e.value for e in app.exception]
    assert any("Note saved" in s.value and "AI" in s.value for s in app.success)
    assert any("Needs a name" in m.value for m in app.markdown)
    assert app.text_area(key="notes_body_c1").value == ""
    with sessionmaker(bind=create_engine(f"sqlite:///{db_path}"))() as s:
        assert [n["written_by"] for n in cn.ai_notes(s, "c1")] == ["Ben"]
