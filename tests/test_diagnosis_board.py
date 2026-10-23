"""
The check-in / check-out board (scripts/board.py): pure rules, plus the git transport against a throwaway bare
repository, including two agents claiming the same ticket at the same moment.
"""

import importlib.util
import json
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("board", Path(__file__).resolve().parent.parent / "scripts" / "board.py")
board = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(board)

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


def T(**kw):
    base = {"title": "t", "wave": 1, "priority": "P1", "size": "M", "depends_on": [], "files": [], "needs": [],
            "status": "OPEN", "claimed_by": None, "claimed_at": None, "heartbeat_at": None}
    base.update(kw)
    return base


def doc_of(**tickets):
    return {"schema": 1, "tickets": tickets}


# ------------------------------------------------------------------------------------------------ pure rules

def test_patterns_overlap():
    o = board.patterns_overlap
    assert o("diagnosis/readers/*.py", "diagnosis/readers/financial.py")
    assert o("diagnosis/**", "diagnosis/readers/financial.py")
    assert o("diagnosis/readers/**", "diagnosis/readers/people/**")
    assert not o("diagnosis/readers/financial.py", "diagnosis/readers/financial_extra.py")
    assert not o("diagnosis/readers/**", "diagnosis/hypotheses/**")
    assert not o("docs/diagnosis/results/D01.md", "docs/diagnosis/results/D010.md")
    assert o("@bundle", "@bundle") and not o("@bundle", "@scraper-repo")
    assert not o("@bundle", "worker_dist/**")        # named resources only collide with the same name


def test_dependency_and_lock_blockers():
    d = doc_of(
        A=T(files=["diagnosis/peers.py"], status="CLAIMED", claimed_by="x", claimed_at="2026-10-23T11:00:00Z", heartbeat_at="2026-10-23T11:00:00Z"),
        B=T(files=["diagnosis/peers.py"]),                       # same file as A
        C=T(depends_on=["A"], files=["x.py"]),                   # waits for A
        E=T(files=["other.py"]),
    )
    assert any("file lock" in w for w in board.blockers(d, "B", NOW))
    assert any("waits for A" in w for w in board.blockers(d, "C", NOW))
    assert board.blockers(d, "E", NOW) == []
    assert board.eligible(d, NOW) == ["E"]


def test_claim_picks_by_priority_then_wave_and_one_ticket_per_agent():
    d = doc_of(A=T(priority="P2"), B=T(priority="P0", wave=2), C=T(priority="P0", wave=1, files=["c.py"]))
    assert board.eligible(d, NOW)[0] == "C"
    tid = board.op_claim(d, "agent-1", None, NOW)
    assert tid in ("C",)                                           # only one best-ranked ticket: no randomness to hide behind
    with pytest.raises(board.BoardError, match="already holds"):
        board.op_claim(d, "agent-1", "A", NOW)
    assert board.op_claim(d, "agent-2", "A", NOW) == "A"


def test_stale_claim_can_only_be_taken_over_explicitly():
    d = doc_of(A=T(status="CLAIMED", claimed_by="old", claimed_at="2026-10-22T00:00:00Z", heartbeat_at="2026-10-22T00:00:00Z"))
    assert board.is_stale(d["tickets"]["A"], NOW)
    with pytest.raises(board.BoardError, match="take-over"):
        board.op_claim(d, "new", "A", NOW)
    assert board.op_claim(d, "new", "A", NOW, take_over=True) == "A"
    assert d["tickets"]["A"]["claimed_by"] == "new"
    assert d["tickets"]["A"]["history"][-1]["event"] == "take_over"


def test_fresh_claim_cannot_be_taken_over():
    d = doc_of(A=T(status="CLAIMED", claimed_by="old", claimed_at="2026-10-23T11:30:00Z", heartbeat_at="2026-10-23T11:30:00Z"))
    with pytest.raises(board.BoardError, match="claimed by old"):
        board.op_claim(d, "new", "A", NOW, take_over=True)


def test_release_block_reopen_done_ownership():
    d = doc_of(A=T())
    board.op_claim(d, "me", "A", NOW)
    with pytest.raises(board.BoardError, match="held by me"):
        board.op_release(d, "other", "A", NOW)
    board.op_heartbeat(d, "me", "A", NOW + timedelta(hours=1))
    board.op_block(d, "me", "A", NOW, "needs GG input")
    assert d["tickets"]["A"]["status"] == "BLOCKED" and board.blockers(d, "A", NOW)
    board.op_reopen(d, "me", "A", NOW)
    board.op_claim(d, "me", "A", NOW)
    board.op_done(d, "me", "A", NOW, "docs/diagnosis/results/A.md")
    assert d["tickets"]["A"]["status"] == "DONE"
    assert board.blockers(d, "A", NOW) == ["already DONE"]


def test_add_assigns_next_id_and_checks_dependencies():
    d = doc_of(D01=T(), D07=T())
    assert board.op_add(d, "me", NOW, "new thing", files=["a.py"], depends_on=["D01"]) == "D08"
    with pytest.raises(board.BoardError):
        board.op_add(d, "me", NOW, "bad", depends_on=["D99"])


def test_branch_slug_stops_at_a_word_boundary():
    assert board.slug("Fact readers A: identity, financials, people, ownership") == "fact-readers-a-identity"
    assert board.slug("!!!") == "ticket"
    assert len(board.slug("x" * 80)) <= 32 or board.slug("x" * 80) == "ticket"


def test_status_renders():
    d = doc_of(D01=T(title="Readers A", status="CLAIMED", claimed_by="agent-x", claimed_at="2026-10-23T11:00:00Z", heartbeat_at="2026-10-23T11:00:00Z", files=["diagnosis/readers/a.py"]),
               D02=T(title="Readers B", depends_on=["D01"]))
    out = board.render_status(d, NOW)
    assert "agent-x" in out and "Held file locks" in out
    assert "waits for D01" in board.render_next(d, "me", NOW)


# ------------------------------------------------------------------------------------------------ git transport

def _run(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def remote_and_clones(tmp_path):
    bare = tmp_path / "origin.git"
    _run(tmp_path, "init", "--bare", "-b", "master", str(bare))
    seed = tmp_path / "seed"
    _run(tmp_path, "clone", str(bare), str(seed))
    _run(seed, "config", "user.email", "t@t"); _run(seed, "config", "user.name", "t")
    (seed / "docs" / "diagnosis").mkdir(parents=True)
    (seed / "docs" / "diagnosis" / "board.json").write_text(json.dumps(doc_of(
        D01=T(title="Readers A", files=["diagnosis/readers/a.py"]),
        D02=T(title="Readers B", files=["diagnosis/readers/b.py"]),
        D03=T(title="After D01", depends_on=["D01"], files=["diagnosis/c.py"]),
        D04=T(title="Clash with D01", files=["diagnosis/readers/a.py"]),
    ), indent=2) + "\n")
    (seed / "README.md").write_text("x\n")
    _run(seed, "add", "-A"); _run(seed, "commit", "-m", "seed"); _run(seed, "push", "origin", "HEAD:master")
    clones = []
    for n in ("a", "b"):
        c = tmp_path / f"clone_{n}"
        _run(tmp_path, "clone", str(bare), str(c))
        _run(c, "config", "user.email", f"{n}@t"); _run(c, "config", "user.name", n)
        clones.append(c)
    return bare, clones


def _read_remote(bare: Path):
    out = subprocess.run(["git", "show", f"master:{board.BOARD_PATH}"], cwd=bare, capture_output=True, check=True)
    return json.loads(out.stdout.decode("utf-8"))


def test_claim_through_git_does_not_touch_the_working_tree(remote_and_clones):
    bare, (ca, cb) = remote_and_clones
    b = board.Board(ca)
    before = (ca / board.BOARD_PATH).read_text()
    tid = b.mutate(lambda d, now: board.op_claim(d, "agent-a", "D01", now), "claim")
    assert tid == "D01"
    assert (ca / board.BOARD_PATH).read_text() == before            # working copy untouched
    remote = _read_remote(bare)
    assert remote["tickets"]["D01"]["claimed_by"] == "agent-a"
    status = subprocess.run(["git", "status", "--porcelain"], cwd=ca, capture_output=True).stdout.decode()
    assert status.strip() == ""                                      # nothing staged or modified


def test_second_agent_sees_the_first_agents_lock(remote_and_clones):
    bare, (ca, cb) = remote_and_clones
    board.Board(ca).mutate(lambda d, now: board.op_claim(d, "agent-a", "D01", now), "claim")
    with pytest.raises(board.BoardError, match="claimed by agent-a"):
        board.Board(cb).mutate(lambda d, now: board.op_claim(d, "agent-b", "D01", now), "claim")
    with pytest.raises(board.BoardError, match="file lock"):
        board.Board(cb).mutate(lambda d, now: board.op_claim(d, "agent-b", "D04", now), "claim")
    assert board.Board(cb).mutate(lambda d, now: board.op_claim(d, "agent-b", "D02", now), "claim") == "D02"
    remote = _read_remote(bare)
    assert {t: remote["tickets"][t]["claimed_by"] for t in ("D01", "D02")} == {"D01": "agent-a", "D02": "agent-b"}


def test_simultaneous_claims_of_one_ticket_have_exactly_one_winner(remote_and_clones):
    bare, clones = remote_and_clones
    results = {}

    def go(name, clone):
        try:
            results[name] = board.Board(clone, sleep=lambda s: None).mutate(
                lambda d, now: board.op_claim(d, name, "D01", now), f"claim {name}")
        except board.BoardError as e:
            results[name] = f"refused: {e}"

    threads = [threading.Thread(target=go, args=(n, c)) for n, c in zip(("agent-a", "agent-b"), clones)]
    for t in threads: t.start()
    for t in threads: t.join()
    winners = [n for n, r in results.items() if r == "D01"]
    assert len(winners) == 1, results
    assert _read_remote(bare)["tickets"]["D01"]["claimed_by"] == winners[0]


def test_done_requires_work_to_have_landed(remote_and_clones):
    bare, (ca, cb) = remote_and_clones
    b = board.Board(ca)
    b.mutate(lambda d, now: board.op_claim(d, "agent-a", "D01", now), "claim")
    with pytest.raises(board.BoardError, match="not on"):
        board._verify_landed(b, "docs/diagnosis/results/D01.md")      # results note not pushed yet
    (ca / "docs" / "diagnosis" / "results").mkdir(parents=True)
    (ca / "docs" / "diagnosis" / "results" / "D01.md").write_text("done\n")
    _run(ca, "add", "docs/diagnosis/results/D01.md"); _run(ca, "commit", "-m", "D01 results")
    with pytest.raises(board.BoardError, match="not on"):
        board._verify_landed(b, "docs/diagnosis/results/D01.md")      # committed locally but not pushed
    # the claim above moved origin/master, so the real flow applies: merge origin/master (never rebase), then push
    _run(ca, "pull", "--no-rebase", "--no-edit", "origin", "master")
    _run(ca, "push", "origin", "HEAD:master")
    board._verify_landed(b, "docs/diagnosis/results/D01.md")          # now it has landed
    b.mutate(lambda d, now: board.op_done(d, "agent-a", "D01", now, "docs/diagnosis/results/D01.md"), "done")
    assert _read_remote(bare)["tickets"]["D01"]["status"] == "DONE"
    # D03 depended on D01, which is DONE: now claimable
    assert board.Board(cb).mutate(lambda d, now: board.op_claim(d, "agent-b", "D03", now), "claim") == "D03"


def test_cli_status_and_claim(remote_and_clones, capsys):
    bare, (ca, cb) = remote_and_clones
    assert board.main(["claim", "D02", "--agent", "agent-a", "--repo", str(ca)]) == 0
    assert "CHECKED OUT D02" in capsys.readouterr().out
    assert board.main(["claim", "D02", "--agent", "agent-b", "--repo", str(cb)]) == 1
    assert board.main(["status", "--repo", str(cb)]) == 0
    assert "agent-a" in capsys.readouterr().out
    assert board.main(["claim", "--repo", str(cb)]) == 2             # no agent name given


# ------------------------------------------------------------------------------------------------ the real board

def test_shipped_board_is_consistent_with_the_ticket_docs_and_fact_keys():
    """The committed board.json, docs/DIAGNOSIS_TICKETS.md and diagnosis.factkeys must agree: every ticket has a
    spec section, every dependency exists, no dependency cycle, and every fact-key owner is a real ticket."""
    from diagnosis.factkeys import FACT_KEYS
    doc = json.loads((ROOT / board.BOARD_PATH).read_text(encoding="utf-8"))
    tickets = doc["tickets"]
    text = (ROOT / "docs" / "DIAGNOSIS_TICKETS.md").read_text(encoding="utf-8")
    for tid, t in tickets.items():
        assert f"### {tid} " in text or f"### {tid}\n" in text, f"{tid} has no section in docs/DIAGNOSIS_TICKETS.md"
        assert t["status"] in board.STATUSES
        assert t.get("files"), f"{tid} declares no write scope"
        for dep in t["depends_on"]:
            assert dep in tickets, f"{tid} depends on unknown {dep}"
    # headings in the doc that are not on the board
    import re
    for tid in re.findall(r"^### ((?:D|R)\d\d)\b", text, flags=re.M):
        assert tid in tickets, f"{tid} is in the ticket doc but not on the board"
    # cycle check
    seen, stack = set(), []

    def visit(n):
        assert n not in stack, f"dependency cycle through {n}"
        if n in seen:
            return
        stack.append(n)
        for d in tickets[n]["depends_on"]:
            visit(d)
        stack.pop(); seen.add(n)

    for n in tickets:
        visit(n)
    for k in FACT_KEYS.values():
        assert k.owner in tickets, f"fact key {k.key} is owned by {k.owner}, which is not a ticket"
    # wave-1 tickets that can start together must not overlap each other's write scope
    wave1 = [i for i, t in tickets.items() if t["wave"] == 1]
    for i, a in enumerate(wave1):
        for b in wave1[i + 1:]:
            hit = board.scopes_overlap(tickets[a]["files"], tickets[b]["files"])
            assert not hit, f"wave-1 tickets {a} and {b} overlap on {hit[0]}: they could not run together"
