"""
READ_FIRST_AGENT_STATUS_AND_OPEN_POINTS.md must stay parseable, and scripts/open_points.py must only change
the point it is asked to change. Pure text: no database. Mutating tests work on a tmp copy.
"""

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("open_points", ROOT / "scripts" / "open_points.py")
op = importlib.util.module_from_spec(_spec)
sys.modules["open_points"] = op  # dataclasses looks the module up by name
_spec.loader.exec_module(op)

REAL = ROOT / "READ_FIRST_AGENT_STATUS_AND_OPEN_POINTS.md"
NOW = "2026-10-26T10:00:00Z"


@pytest.fixture()
def f(tmp_path):
    p = tmp_path / "status.md"
    shutil.copy(REAL, p)
    return p


def run(f, *argv):
    return op.main(["--file", str(f), "--now", NOW, *argv])


def doc(f):
    return op.Doc(f)


def mutate(f, old, new, count=1):
    text = f.read_text(encoding="utf-8")
    assert old in text, old
    f.write_text(text.replace(old, new, count), encoding="utf-8")


# ----------------------------------------------------------------------------- the committed file

def test_committed_file_validates():
    d = op.Doc(REAL)
    assert op.validate_text(d) == []


def test_read_first_line_is_at_the_top_of_claude_agents_and_readme():
    assert op.validate_first_lines(ROOT) == []
    for name in ("CLAUDE.md", "AGENTS.md", "README.md"):
        first = (ROOT / name).read_text(encoding="utf-8").split("\n", 1)[0].strip()
        assert first == op.READ_FIRST_LINE


def test_sections_and_seed_points_exist():
    d = op.Doc(REAL)
    assert list(d.section_starts()) == ["A", "B", "C", "D", "E"]
    ids = [p.id for p in d.points()]
    # The 13 seed points stay, numbered without gaps; later points (added by agents) only extend the list.
    assert ids == [f"OP-{i:03d}" for i in range(1, len(ids) + 1)] and len(ids) >= 13
    text = REAL.read_text(encoding="utf-8")
    assert "Stay side by side until weights are approved | Giorgio | " in text and "2026-10-26" in text
    sec_b = text.split("## B.")[1].split("## C.")[0]
    assert len([x for x in sec_b.splitlines() if x.strip()]) < 40   # section B stays short (current state only)


# ----------------------------------------------------------------------------- validation catches corruption

def test_first_line_loss_is_caught(tmp_path):
    for name in op.FIRST_LINE_FILES:
        shutil.copy(ROOT / name, tmp_path / name)
    assert op.validate_first_lines(tmp_path) == []
    p = tmp_path / "AGENTS.md"
    p.write_text("# Agents\n" + p.read_text(encoding="utf-8"), encoding="utf-8")
    assert any("AGENTS.md" in e for e in op.validate_first_lines(tmp_path))
    (tmp_path / "CLAUDE.md").unlink()
    assert any("CLAUDE.md is missing" in e for e in op.validate_first_lines(tmp_path))


def test_duplicate_id_is_caught(f):
    mutate(f, "### OP-002:", "### OP-001:")
    assert any("OP-001 is duplicated" in e for e in op.validate_text(doc(f)))


def test_invalid_status_is_caught(f):
    mutate(f, "* status: OPEN", "* status: WAITING")
    assert any("invalid status" in e for e in op.validate_text(doc(f)))


def test_checked_out_needs_owner_and_lease(f):
    mutate(f, "### OP-006: All TO_MAP entries in source_it\n* status: OPEN", "### OP-006: All TO_MAP entries in source_it\n* status: CHECKED_OUT")
    errs = op.validate_text(doc(f))
    assert any("OP-006: CHECKED_OUT but owner is none" in e for e in errs)
    assert any("OP-006: CHECKED_OUT but lease_until is none" in e for e in errs)


def test_done_must_live_in_the_closed_log(f):
    mutate(f, "### OP-010: Fix the 2 failing legacy tests\n* status: OPEN", "### OP-010: Fix the 2 failing legacy tests\n* status: DONE")
    assert any("OP-010: DONE but not in the Closed log" in e for e in op.validate_text(doc(f)))


def test_unknown_blocker_is_caught(f):
    mutate(f, "* blocked_by: OP-001, OP-002, OP-003, OP-013", "* blocked_by: OP-001, OP-099")
    assert any("OP-099 does not exist" in e for e in op.validate_text(doc(f)))


def test_bad_needs_and_timestamp_are_caught(f):
    mutate(f, "* needs: agent work", "* needs: magic")
    assert any("invalid needs" in e for e in op.validate_text(doc(f)))


# ----------------------------------------------------------------------------- the helper edits only what it should

def sections(f):
    d = doc(f)
    s = d.section_starts()
    t = "\n".join(d.lines)
    return {"A": t[t.index("## A."):t.index("## B.")], "B": t[t.index("## B."):t.index("## C.")],
            "D": t[t.index("## D."):t.index("## E.")]}


def test_checkout_claims_a_point_and_touches_nothing_else(f):
    before_pts = {p.id: op.Doc.render(p) for p in doc(f).points()}
    before_sections = sections(f)
    assert run(f, "checkout", "OP-010", "--agent", "claude-test1") == 0
    p = doc(f).find("OP-010")
    assert p.get("status") == "CHECKED_OUT" and p.get("owner") == "claude-test1"
    assert p.get("checked_out_at") == "2026-10-26T10:00:00Z" and p.get("lease_until") == "2026-10-28T10:00:00Z"
    assert p.notes[0].startswith("2026-10-26: checked out by claude-test1")
    after_pts = {q.id: op.Doc.render(q) for q in doc(f).points()}
    assert {k for k in after_pts if after_pts[k] != before_pts[k]} == {"OP-010"}
    assert sections(f) == before_sections
    assert op.validate_text(doc(f)) == []


def test_checkout_refuses_decisions_blocked_points_and_double_claims(f):
    assert run(f, "checkout", "OP-001", "--agent", "a") == 2          # NEEDS_DECISION: Giorgio only (rule 9)
    assert run(f, "checkout", "OP-009", "--agent", "a") == 2          # blocked
    assert run(f, "checkout", "OP-006", "--agent", "a") == 0
    assert run(f, "checkout", "OP-006", "--agent", "b") == 2          # already held
    assert run(f, "checkout", "OP-404", "--agent", "b") == 2          # no such point
    assert doc(f).find("OP-006").get("owner") == "a"


def test_stale_lease_can_be_reclaimed_with_a_note_but_a_live_one_cannot(f):
    run(f, "checkout", "OP-010", "--agent", "old-agent")
    assert op.main(["--file", str(f), "--now", "2026-10-27T10:00:00Z", "checkout", "OP-010", "--agent", "new", "--reclaim"]) == 2
    assert op.main(["--file", str(f), "--now", "2026-10-29T10:00:00Z", "checkout", "OP-010", "--agent", "new", "--reclaim"]) == 0
    p = doc(f).find("OP-010")
    assert p.get("owner") == "new"
    assert any("reclaimed from old-agent" in n for n in p.notes)


def test_extend_moves_the_lease(f):
    run(f, "checkout", "OP-010", "--agent", "a")
    assert op.main(["--file", str(f), "--now", "2026-10-27T10:00:00Z", "extend", "OP-010"]) == 0
    assert doc(f).find("OP-010").get("lease_until") == "2026-10-29T10:00:00Z"


def test_checkin_done_moves_the_point_to_the_closed_log_with_the_commit(f):
    run(f, "checkout", "OP-010", "--agent", "a")
    before_b = sections(f)["B"]
    assert run(f, "checkin", "OP-010", "--status", "DONE", "--note", "fixed", "--commit", "abc1234") == 0
    d = doc(f)
    p = d.find("OP-010")
    assert p.section == "E" and p.get("status") == "DONE" and p.get("owner") == "none"
    assert "abc1234" in p.notes[0]
    assert "_none yet_" not in "\n".join(d.lines)
    assert op.validate_text(d) == [] and sections(f)["B"] == before_b
    assert [q.id for q in d.points() if q.section == "C"].count("OP-010") == 0
    assert run(f, "checkin", "OP-010", "--status", "OPEN", "--note", "x") == 2     # not checked out any more


def test_done_needs_a_commit_hash(f):
    run(f, "checkout", "OP-010", "--agent", "a")
    assert run(f, "checkin", "OP-010", "--status", "DONE", "--note", "fixed") == 2
    assert doc(f).find("OP-010").get("status") == "CHECKED_OUT"


def test_checkin_not_done_releases_the_point_and_blocks_correctly(f):
    run(f, "checkout", "OP-006", "--agent", "a")
    assert run(f, "checkin", "OP-006", "--status", "OPEN", "--note", "got halfway: AIDA list missing") == 0
    p = doc(f).find("OP-006")
    assert p.get("status") == "OPEN" and p.get("owner") == "none" and p.get("lease_until") == "none"
    assert "halfway" in p.notes[0]
    run(f, "checkout", "OP-006", "--agent", "a")
    assert run(f, "checkin", "OP-006", "--status", "BLOCKED", "--note", "waiting", "--blocked-by", "OP-005") == 0
    assert doc(f).find("OP-006").blockers() == ["OP-005"]
    assert run(f, "checkin", "OP-006", "--status", "BLOCKED", "--note", "x") == 2


def test_blockers_must_be_done_before_checkout_and_reopen(f):
    # make OP-009 agent-work with a single blocker that is then closed
    mutate(f, "* blocked_by: OP-008, OP-001, OP-002, OP-003, OP-004, OP-007", "* blocked_by: OP-010")
    assert run(f, "reopen", "OP-009", "--note", "try") == 2
    run(f, "checkout", "OP-010", "--agent", "a")
    run(f, "checkin", "OP-010", "--status", "DONE", "--note", "ok", "--commit", "deadbee")
    assert run(f, "reopen", "OP-009", "--note", "blocker done") == 0
    assert run(f, "checkout", "OP-009", "--agent", "b") == 0


def test_note_works_on_decision_points_without_resolving_them(f):
    assert run(f, "note", "OP-001", "--text", "recommendation: keep as context") == 0
    p = doc(f).find("OP-001")
    assert p.get("status") == "NEEDS_DECISION" and p.notes[0] == "2026-10-26: recommendation: keep as context"


def test_add_uses_the_next_free_number_and_validates(f):
    first = len(doc(f).points()) + 1
    assert run(f, "add", "--title", "New problem", "--needs", "agent work", "--context", "x.py",
               "--done-when", "x.py is fixed") == 0
    p = doc(f).find(f"OP-{first:03d}")
    assert p.get("status") == "OPEN" and p.section == "C"
    assert run(f, "add", "--title", "Needs Giorgio", "--needs", "decision from Giorgio", "--context", "y", "--done-when", "z") == 0
    assert doc(f).find(f"OP-{first + 1:03d}").get("status") == "NEEDS_DECISION"
    assert run(f, "add", "--title", "Bad", "--needs", "agent work", "--context", "y", "--done-when", "z", "--blocked-by", "OP-777") == 2
    assert op.validate_text(doc(f)) == []


def test_crlf_files_keep_their_line_endings(tmp_path):
    p = tmp_path / "s.md"
    p.write_bytes(REAL.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    assert run(p, "checkout", "OP-010", "--agent", "a") == 0
    raw = p.read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")


def test_validate_command_exit_codes(f):
    assert op.main(["--file", str(f), "validate"]) == 0
    mutate(f, "* status: OPEN", "* status: NOPE")
    assert op.main(["--file", str(f), "validate"]) == 1
