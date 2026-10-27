"""
Check-in / check-out board for the deep-intelligence program (docs/INTEL_ROADMAP.md).

This is scripts/board.py (the atomic compare-and-swap-on-origin/master engine built for the diagnosis program)
pointed at a second, independent board file, docs/intel/board.json. Nothing in the diagnosis board changes.
Everything in board.py's docstring applies: every change is a push to origin/master, so two agents can never
win the same ticket; check-out also locks the ticket's declared write scope; names starting with '@' are
exclusive resources.

Differences from the diagnosis board:
  * agent name comes from --agent or $INTEL_AGENT (falls back to $DIAG_AGENT);
  * new tickets are numbered I<nn> (the engine hard-codes D<nn>);
  * the claim banner points at docs/INTEL_ROADMAP.md and names the branch intel/<ID>-<slug>.

Commands (same as board.py): preflight [ID] | status | next | claim [ID] | heartbeat ID | release ID |
block ID --reason T | reopen ID | done ID --result PATH | add --title T ...
Run `python scripts/intel_board.py preflight` before claiming anything.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any, Dict

_spec = importlib.util.spec_from_file_location("vienna_board_engine", Path(__file__).with_name("board.py"))
board = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("vienna_board_engine", board)
_spec.loader.exec_module(board)

BOARD_PATH = "docs/intel/board.json"
TICKET_PREFIX = "I"
SPEC = "docs/INTEL_ROADMAP.md"


def intel_op_add(doc, agent, now, title, files=(), depends_on=(), wave=3, priority="P2", size="M", needs=(), why=""):
    ids = [int(i[len(TICKET_PREFIX):]) for i in doc["tickets"]
           if i.startswith(TICKET_PREFIX) and i[len(TICKET_PREFIX):].isdigit()]
    tid = f"{TICKET_PREFIX}{(max(ids) + 1 if ids else 1):02d}"
    deps = list(depends_on)
    for d in deps:
        if d not in doc["tickets"]:
            raise board.BoardError(f"unknown dependency {d}")
    doc["tickets"][tid] = {
        "title": title, "wave": wave, "priority": priority, "size": size, "depends_on": deps,
        "files": list(files), "needs": list(needs), "status": "OPEN", "claimed_by": None, "claimed_at": None,
        "heartbeat_at": None, "proposed_by": agent, "why": why,
        "history": [{"at": board._iso(now), "by": agent, "event": "proposed"}]}
    return tid


def intel_claim_banner(doc: Dict[str, Any], tid: str, agent: str) -> str:
    t = doc["tickets"][tid]
    return "\n".join([
        f"CHECKED OUT {tid} - {t['title']}   (agent {agent})",
        f"  write scope: {', '.join(t.get('files', [])) or '(none declared)'}",
        f"  needs      : {', '.join(t.get('needs', [])) or '-'}",
        f"  depends on : {', '.join(t.get('depends_on', [])) or '-'}",
        f"  branch     : intel/{tid}-{board.slug(t['title'])}",
        "",
        f"Next: read the ticket in {SPEC} (section 'Ticket details'), work in your own git worktree/branch, stay inside",
        "the write scope, run `python scripts/intel_board.py heartbeat " + tid + "` at least hourly, prove progress with",
        "`python scripts/intel_gap.py ... --require-ticket " + tid + "`, and check in with `done` when merged.",
    ])


def configure() -> None:
    """Point the shared engine at the intel board. Idempotent."""
    board.BOARD_PATH = BOARD_PATH
    board.op_add = intel_op_add
    board.claim_banner = intel_claim_banner
    if os.environ.get("INTEL_AGENT"):
        os.environ["DIAG_AGENT"] = os.environ["INTEL_AGENT"]


def main(argv=None) -> int:
    configure()
    return board.main(argv)


if __name__ == "__main__":
    sys.exit(main())
