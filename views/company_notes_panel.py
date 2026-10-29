"""
Company Intelligence > Notes: a shared notebook for one company.

Everyone who opens the company sees the same notes. Each note is its own row, so several people can write at the
same moment without overwriting each other; the list re-reads the database every few seconds (st.fragment with
run_every) and shows who else has the notes open. Streamlit cannot merge two people typing into ONE text box, so
the unit of collaboration is the note and its replies, not a shared paragraph.

A note stays inside the team until someone presses "Send to AI": from then on the assistant, the per-company chat
and the AI brief can read it, always labelled as an analyst note (company_notes.ai_notes).
"""

from sqlalchemy.orm import Session
import streamlit as st

import company_notes as cn

AUTHOR_KEY = "notes_author"
REFRESH_SECONDS = 3
_KIND_IDS = list(cn.KINDS)


def _kind_label(kind: str) -> str:
    icon, label = cn.KINDS.get(kind, ("", kind))
    return f"{icon} {label}"


def _stamp(dt) -> str:
    return dt.strftime("%d %b %Y %H:%M") + " UTC" if dt else ""


def _flash(message: str, ok: bool = False) -> None:
    st.session_state["notes_flash"] = (ok, message)


def _show_flash() -> None:
    ok, message = st.session_state.pop("notes_flash", (None, None))
    if message:
        (st.success if ok else st.error)(message)


# --------------------------------------------------------------------------------------------------- composer

def _post(bind, company_id: str, kind_key: str, body_key: str, ai_key: str) -> None:
    """Button callback: runs before the next script run, which is the only moment the text box may be cleared."""
    send = bool(st.session_state.get(ai_key))
    db = Session(bind)
    try:
        cn.add_note(db, company_id, st.session_state.get(AUTHOR_KEY), st.session_state.get(body_key),
                    kind=st.session_state.get(kind_key) or "note", send_to_ai=send)
    except ValueError as e:
        _flash(str(e))
        return
    finally:
        db.close()
    st.session_state[body_key] = ""
    st.session_state[ai_key] = False
    _flash("Note saved." + (" The AI can now read it." if send else ""), ok=True)


def _composer(bind, company_id: str) -> None:
    kind_key, body_key, ai_key = f"notes_kind_{company_id}", f"notes_body_{company_id}", f"notes_ai_{company_id}"
    with st.container(border=True):
        c1, c2 = st.columns([1, 2])
        with c1:
            st.selectbox("Type", _KIND_IDS, format_func=_kind_label, key=kind_key)
        with c2:
            st.checkbox("🤖 Send to AI as soon as it is saved", key=ai_key,
                        help="Off: only your colleagues can read it. On: the assistant and the AI brief can also use it, "
                             "always labelled as an analyst note.")
        st.text_area("Write a note", key=body_key, height=110, max_chars=cn.MAX_BODY, label_visibility="collapsed",
                     placeholder="What did you learn, what should we consider, what would you change in the assessment? "
                                 "(Ctrl+Enter to apply the text, then press Save.)")
        st.button("Save note", type="primary", key=f"notes_save_{company_id}", on_click=_post,
                  args=(bind, company_id, kind_key, body_key, ai_key))


# ----------------------------------------------------------------------------------------------------- one note

def _note_card(db, note, me: str, *, is_reply: bool = False) -> None:
    head = f"**{note.author}** · {_kind_label(note.kind)} · {_stamp(note.created_at)}"
    if note.edited_by:
        head += f" · edited by {note.edited_by}"
    st.markdown(("↳ " if is_reply else "") + head)
    st.markdown(note.body)

    badges = []
    if note.pinned:
        badges.append("📌 pinned")
    if note.use_in_ai:
        badges.append(f"🤖 sent to AI by {note.ai_sent_by or '?'} on {_stamp(note.ai_sent_at)}")
    if badges:
        st.caption(" · ".join(badges))

    cols = st.columns([1.3, 1.2, 1, 1, 1] if not is_reply else [1.3, 1, 1, 3])
    k = f"{note.id}"
    with cols[0]:
        if note.use_in_ai:
            if st.button("Withdraw from AI", key=f"ai_off_{k}", help="The AI stops using this note."):
                cn.set_ai(db, note.id, False, me)
                st.rerun(scope="fragment")
        elif st.button("🤖 Send to AI", key=f"ai_on_{k}",
                       help="From now on the assistant and the AI brief can use this note, labelled as an analyst note."):
            if not me:
                _flash("Enter your name first (above), so the AI can say who wrote what was sent.")
            else:
                cn.set_ai(db, note.id, True, me)
            st.rerun(scope="fragment")
    idx = 1
    if not is_reply:
        with cols[idx]:
            with st.popover("↩️ Reply"):
                reply_key = f"reply_{k}"
                st.text_area("Reply", key=reply_key, height=80, label_visibility="collapsed")
                if st.button("Send reply", key=f"reply_send_{k}"):
                    try:
                        cn.add_note(db, note.company_id, me, st.session_state.get(reply_key), kind=note.kind,
                                    parent_id=note.id)
                        st.session_state[reply_key] = ""
                        st.rerun(scope="fragment")
                    except ValueError as e:
                        db.rollback()
                        st.error(str(e))
        idx += 1
        with cols[idx]:
            if st.button("📌 Unpin" if note.pinned else "📌 Pin", key=f"pin_{k}"):
                cn.set_pinned(db, note.id, not note.pinned)
                st.rerun(scope="fragment")
        idx += 1
    else:
        idx = 1
    with cols[idx]:
        with st.popover("✏️ Edit"):
            edit_key = f"edit_{k}"
            st.session_state.setdefault(edit_key, note.body)
            st.session_state.setdefault(f"seen_{k}", note.updated_at)
            st.text_area("Edit note", key=edit_key, height=120, label_visibility="collapsed")
            if st.button("Save changes", key=f"edit_save_{k}"):
                try:
                    cn.edit_note(db, note.id, me, st.session_state[edit_key], st.session_state[f"seen_{k}"])
                    st.session_state.pop(edit_key, None)
                    st.session_state.pop(f"seen_{k}", None)
                    st.rerun(scope="fragment")
                except cn.NoteConflict as e:
                    db.rollback()
                    st.session_state.pop(edit_key, None)
                    st.session_state.pop(f"seen_{k}", None)
                    st.warning(f"{e} Reopen the editor to see the new text.")
                except ValueError as e:
                    db.rollback()
                    st.error(str(e))
    with cols[idx + 1]:
        with st.popover("🗑️ Delete"):
            st.caption("Removes the note" + ("" if is_reply else " and its replies") + " for everyone and stops the AI using it.")
            if st.button("Yes, delete", key=f"del_{k}"):
                cn.delete_note(db, note.id)
                st.rerun(scope="fragment")


def _thread(db, note, replies, me: str) -> None:
    with st.container(border=True):
        _note_card(db, note, me)
        for reply in replies:
            st.divider()
            _note_card(db, reply, me, is_reply=True)


# --------------------------------------------------------------------------------------------------- live list

def _live_list(bind, company_id: str) -> None:
    with Session(bind) as db:        # a fresh session per run, so every run reads what the others just saved
        me = cn.clean_author(st.session_state.get(AUTHOR_KEY))
        others = cn.touch_presence(db, company_id, me)
        _show_flash()
        if me:
            st.caption(("👀 Also here now: " + ", ".join(others)) if others else "👀 Nobody else has these notes open right now.")
        notes = cn.list_notes(db, company_id)
        if not notes:
            st.info("No notes on this company yet. The first one starts the conversation.")
            return
        ai_count = sum(1 for n in notes if n.use_in_ai)
        st.caption(f"{len(notes)} note(s), {ai_count} sent to the AI. Updates appear on their own every {REFRESH_SECONDS} seconds.")
        for note, replies in cn.threads(notes):
            _thread(db, note, replies, me)


def render_notes_section(db: Session, company) -> None:
    st.subheader("📝 Notes & collaboration")
    st.caption("A shared notebook for this company. Colleagues who open it see your notes within seconds. "
               "Notes stay between you until you press **Send to AI**; then the assistant and the AI brief can use them, "
               "always labelled as an analyst note and never as verified data.")
    st.text_input("Your name", key=AUTHOR_KEY, placeholder="e.g. Giorgio",
                  help="Shown on everything you write. The app has one shared password, so this is how the team tells who wrote what.")
    bind = db.get_bind()
    _composer(bind, company.id)
    st.fragment(run_every=REFRESH_SECONDS)(_live_list)(bind, company.id)
