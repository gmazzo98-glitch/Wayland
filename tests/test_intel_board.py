"""
The intel check-in / check-out board: the shipped docs/intel/board.json is consistent with the roadmap and the golden
file, and scripts/intel_board.py drives the shared engine against ITS OWN file, atomically, without touching the
diagnosis board.
"""

import importlib.util
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("intel_board", ROOT / "scripts" / "intel_board.py")
ib = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ib)

BOARD = json.loads((ROOT / "docs" / "intel" / "board.json").read_text(encoding="utf-8"))
GOLDEN = json.loads((ROOT / "docs" / "intel" / "golden" / "asco_filtri.json").read_text(encoding="utf-8"))
ROADMAP = (ROOT / "docs" / "INTEL_ROADMAP.md").read_text(encoding="utf-8")
PROMPT = (ROOT / "docs" / "INTEL_AGENT_PROMPT.md").read_text(encoding="utf-8")
NEEDS = {"live_db_read", "live_db_write", "web", "llm", "scraper_repo", "local_model", "gg_input", "gg_approval", "worker"}


def test_shipped_board_is_well_formed():
    tickets = BOARD["tickets"]
    assert BOARD["program"] == "intel" and len(tickets) >= 30
    for tid, t in tickets.items():
        assert tid.startswith("I") and tid[1:].isdigit(), tid
        assert t["status"] in ("OPEN", "CLAIMED", "DONE", "BLOCKED"), tid
        assert t["priority"] in ("P0", "P1", "P2", "P3") and t["size"] in ("S", "M", "L", "XL"), tid
        assert f"docs/intel/results/{tid}.md" in t["files"], f"{tid} must declare its results note"
        assert set(t["needs"]) <= NEEDS, (tid, set(t["needs"]) - NEEDS)
        for dep in t["depends_on"]:
            assert dep in tickets and dep != tid, (tid, dep)
        if t["status"] == "DONE":
            assert (ROOT / t["result"]).exists(), f"{tid} is DONE but {t['result']} is missing"


def test_no_dependency_cycles():
    tickets, state = BOARD["tickets"], {}

    def visit(n):
        assert state.get(n) != 1, f"dependency cycle through {n}"
        if state.get(n) == 2:
            return
        state[n] = 1
        for d in tickets[n]["depends_on"]:
            visit(d)
        state[n] = 2

    for n in tickets:
        visit(n)


def test_every_golden_fact_is_owned_by_a_real_ticket_and_listed_on_it():
    tickets = BOARD["tickets"]
    for f in GOLDEN["facts"]:
        for tid in f.get("closed_by", []):
            assert tid in tickets, (f["id"], tid)
            assert f["id"] in tickets[tid].get("golden", []), (f["id"], tid)


def test_the_roadmap_describes_every_ticket_and_the_prompt_names_the_protocol():
    for tid in BOARD["tickets"]:
        assert f"### {tid} " in ROADMAP, f"docs/INTEL_ROADMAP.md has no '### {tid} ' section"
    for must in ("scripts/intel_board.py", "preflight", "docs/INTEL_ROADMAP.md", "INTEL_AGENT", "intel_gap.py"):
        assert must in PROMPT, must


def test_parallel_ready_tickets_never_share_write_scope():
    """Two tickets an agent could claim at the same time must not be able to edit the same file."""
    board = ib.board
    ready = [t for t in BOARD["tickets"].values() if t["status"] == "OPEN" and not t["depends_on"]]
    for i, a in enumerate(ready):
        for b in ready[i + 1:]:
            hit = board.scopes_overlap(a["files"], b["files"])
            assert not hit, (a["title"], b["title"], hit)


# ---- the shim -------------------------------------------------------------------------------------

def test_configure_points_the_engine_at_the_intel_board_only():
    ib.configure()
    assert ib.board.BOARD_PATH == "docs/intel/board.json"
    other = importlib.util.spec_from_file_location("diag_board", ROOT / "scripts" / "board.py")
    diag = importlib.util.module_from_spec(other)
    other.loader.exec_module(diag)
    assert diag.BOARD_PATH == "docs/diagnosis/board.json"   # the diagnosis engine instance is untouched


def test_new_tickets_get_i_ids_and_the_banner_points_at_the_roadmap():
    now = datetime(2026, 10, 27, tzinfo=timezone.utc)
    doc = {"tickets": {"I01": dict(BOARD["tickets"]["I01"]), "I92": dict(BOARD["tickets"]["I92"])}}
    tid = ib.intel_op_add(doc, "tester", now, "A new idea", files=["intel/x.py"], depends_on=["I01"])
    assert tid == "I93" and doc["tickets"][tid]["status"] == "OPEN"
    with pytest.raises(ib.board.BoardError):
        ib.intel_op_add(doc, "tester", now, "bad", depends_on=["I77"])
    banner = ib.intel_claim_banner(BOARD, "I10", "claude-7f3a")
    assert "docs/INTEL_ROADMAP.md" in banner and "intel/I10-" in banner and "--require-ticket I10" in banner


# ---- git transport against a throwaway bare repository --------------------------------------------

def _run(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def remote_and_clone(tmp_path):
    bare = tmp_path / "origin.git"
    _run(tmp_path, "init", "--bare", "-b", "master", str(bare))
    seed = tmp_path / "seed"
    _run(tmp_path, "clone", str(bare), str(seed))
    _run(seed, "config", "user.email", "t@t")
    _run(seed, "config", "user.name", "t")
    (seed / "docs" / "intel").mkdir(parents=True)
    board_doc = json.loads(json.dumps(BOARD))
    (seed / "docs" / "intel" / "board.json").write_text(json.dumps(board_doc, indent=2) + "\n", encoding="utf-8")
    (seed / "README.md").write_text("x\n")
    _run(seed, "add", "-A")
    _run(seed, "commit", "-m", "seed")
    _run(seed, "push", "origin", "HEAD:master")
    clone = tmp_path / "clone"
    _run(tmp_path, "clone", str(bare), str(clone))
    _run(clone, "config", "user.email", "a@t")
    _run(clone, "config", "user.name", "a")
    return bare, clone


def test_claiming_through_the_shim_writes_only_the_intel_board(remote_and_clone, capsys):
    bare, clone = remote_and_clone
    code = ib.main(["claim", "I12", "--agent", "claude-test", "--repo", str(clone)])
    assert code == 0
    shown = subprocess.run(["git", "show", "master:docs/intel/board.json"], cwd=bare, capture_output=True, text=True, check=True)
    t = json.loads(shown.stdout)["tickets"]["I12"]
    assert t["status"] == "CLAIMED" and t["claimed_by"] == "claude-test"
    assert "intel/I12-" in capsys.readouterr().out
    # a second agent is refused the same ticket
    assert ib.main(["claim", "I12", "--agent", "other-agent", "--repo", str(clone)]) != 0
