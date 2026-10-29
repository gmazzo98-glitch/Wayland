"""
Company notes: collaboration inside the dossier, and the notes the AI is allowed to read.

Everything that touches `CompanyNote` goes through here (the view, the assistant tool and the AI brief all call
these functions), so the rule "the AI sees a note only after someone sent it, always labelled as an analyst note"
lives in one place.
"""

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from models import CompanyNote, CompanyNotePresence

KINDS = {
    "note": ("📝", "Note"),
    "insight": ("💡", "Insight"),
    "proposal": ("🔧", "Proposed change"),
    "question": ("❓", "Question"),
    "decision": ("✅", "Decision"),
}
MAX_BODY = 4000
PRESENT_WITHIN = timedelta(seconds=20)
AI_LIMIT = 25            # notes handed to the model per company, newest first
AI_CHARS = 700           # characters of one note handed to the model
AI_SOURCE = "analyst note (human opinion, not verified data)"


class NoteConflict(Exception):
    """The note was changed by someone else after the editor opened it."""


def clean_author(name: Optional[str]) -> str:
    return " ".join((name or "").split())[:80]


def _clean_body(body: Optional[str]) -> str:
    text = (body or "").strip()
    if not text:
        raise ValueError("A note cannot be empty.")
    if len(text) > MAX_BODY:
        raise ValueError(f"A note is limited to {MAX_BODY} characters (this one has {len(text)}).")
    return text


def add_note(db: Session, company_id: str, author: str, body: str, kind: str = "note",
             parent_id: Optional[str] = None, send_to_ai: bool = False) -> CompanyNote:
    author = clean_author(author)
    if not author:
        raise ValueError("Enter your name first, so the others know who wrote it.")
    if kind not in KINDS:
        raise ValueError(f"Unknown note kind {kind!r}.")
    if parent_id:
        parent = db.query(CompanyNote).filter_by(id=parent_id, company_id=company_id).first()
        if parent is None or parent.deleted_at is not None:
            raise ValueError("The note you are replying to no longer exists.")
        parent_id = parent.parent_id or parent.id          # one level of replies, kept flat
    now = datetime.utcnow()
    note = CompanyNote(company_id=company_id, parent_id=parent_id, author=author, kind=kind, body=_clean_body(body),
                       created_at=now, updated_at=now)
    if send_to_ai:
        note.use_in_ai, note.ai_sent_by, note.ai_sent_at = True, author, now
    db.add(note)
    db.commit()
    return note


def edit_note(db: Session, note_id: str, editor: str, body: str, seen_updated_at: Optional[datetime] = None) -> CompanyNote:
    note = db.query(CompanyNote).filter_by(id=note_id).first()
    if note is None or note.deleted_at is not None:
        raise ValueError("This note no longer exists.")
    if seen_updated_at is not None and note.updated_at and note.updated_at > seen_updated_at:
        raise NoteConflict(f"{note.edited_by or note.author} changed this note while you were editing it.")
    note.body = _clean_body(body)
    note.edited_by = clean_author(editor) or None
    note.updated_at = datetime.utcnow()
    db.commit()
    return note


def delete_note(db: Session, note_id: str) -> None:
    """Soft delete the note and its replies; a deleted note is never shown or sent to the AI."""
    now = datetime.utcnow()
    for note in db.query(CompanyNote).filter((CompanyNote.id == note_id) | (CompanyNote.parent_id == note_id)).all():
        note.deleted_at, note.use_in_ai, note.updated_at = now, False, now
    db.commit()


def set_ai(db: Session, note_id: str, on: bool, by: str) -> None:
    note = db.query(CompanyNote).filter_by(id=note_id).first()
    if note is None or note.deleted_at is not None:
        raise ValueError("This note no longer exists.")
    if bool(note.use_in_ai) == bool(on):
        return
    note.use_in_ai = bool(on)
    note.ai_sent_by, note.ai_sent_at = (clean_author(by) or None, datetime.utcnow()) if on else (None, None)
    note.updated_at = datetime.utcnow()
    db.commit()


def set_pinned(db: Session, note_id: str, pinned: bool) -> None:
    note = db.query(CompanyNote).filter_by(id=note_id).first()
    if note and note.deleted_at is None:
        note.pinned, note.updated_at = bool(pinned), datetime.utcnow()
        db.commit()


def list_notes(db: Session, company_id: str) -> list[CompanyNote]:
    return (db.query(CompanyNote).filter(CompanyNote.company_id == company_id, CompanyNote.deleted_at.is_(None))
            .order_by(CompanyNote.created_at).all())


def threads(notes: list[CompanyNote]) -> list[tuple[CompanyNote, list[CompanyNote]]]:
    """[(note, replies)] with pinned threads first, then the thread whose latest activity is newest."""
    replies: dict[str, list[CompanyNote]] = {}
    roots = []
    ids = {n.id for n in notes}
    for n in notes:
        if n.parent_id and n.parent_id in ids:
            replies.setdefault(n.parent_id, []).append(n)
        else:
            roots.append(n)

    def activity(n):
        return max([n.created_at] + [r.created_at for r in replies.get(n.id, [])])

    roots.sort(key=lambda n: (bool(n.pinned), activity(n)), reverse=True)
    return [(n, replies.get(n.id, [])) for n in roots]


# ---------------------------------------------------------------------------------------------------- presence

def touch_presence(db: Session, company_id: str, author: str) -> list[str]:
    """Record that `author` has this company's notes open; return the names of the others who do."""
    author = clean_author(author)
    now = datetime.utcnow()
    if author:
        row = db.query(CompanyNotePresence).filter_by(company_id=company_id, author=author).first()
        if row is None:
            row = CompanyNotePresence(company_id=company_id, author=author)
            db.add(row)
        row.seen_at = now
        db.commit()
    rows = db.query(CompanyNotePresence).filter(CompanyNotePresence.company_id == company_id,
                                                CompanyNotePresence.seen_at >= now - PRESENT_WITHIN).all()
    return sorted(r.author for r in rows if r.author != author)


# --------------------------------------------------------------------------------------------------------- AI

def ai_notes(db: Session, company_id: str) -> list[dict]:
    """The notes someone sent to the AI, newest first, as plain dicts that always say what they are."""
    rows = (db.query(CompanyNote).filter(CompanyNote.company_id == company_id, CompanyNote.use_in_ai.is_(True),
                                         CompanyNote.deleted_at.is_(None))
            .order_by(CompanyNote.created_at.desc()).limit(AI_LIMIT).all())
    return [{"source": AI_SOURCE, "kind": KINDS.get(n.kind, ("", n.kind))[1],
             "written_by": n.author, "written_on": n.created_at.date().isoformat(),
             "edited": bool(n.edited_by), "text": n.body[:AI_CHARS]} for n in rows]
