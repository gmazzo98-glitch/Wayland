"""
Check-in / check-out board for the diagnosis program (docs/DIAGNOSIS_TICKETS.md).

Several AI agents, launched at the same moment from one shared prompt, each need to take a different piece of work
without colliding. The state lives in ONE file, docs/diagnosis/board.json, and is changed ONLY through this script:

  * Atomic by construction. Every change is a compare-and-swap on origin/master: the script reads the board from
    origin/master, applies the change, builds a commit on top of that exact revision with git plumbing, and pushes
    it. If someone else pushed in between, git rejects the push and the script starts again from the new state, so
    two agents can never both win the same ticket.
  * Never touches your working tree, index or checked-out branch. It works from any branch or git worktree, so it
    cannot disturb another agent's files (and your own branch must never stage docs/diagnosis/board.json).
  * Check-out also locks FILES: each ticket declares the paths it may write; a claim is refused while another
    claimed ticket's paths overlap. Names starting with '@' (e.g. @scraper-repo, @bundle, @live-db-write) are
    exclusive resources, mutually exclusive between tickets that list the same name.

Commands (--agent NAME or $DIAG_AGENT; use a unique name per agent, e.g. claude-7f3a):
  status [--json]                 board overview, who holds what, stale claims, held locks
  next                            tickets this agent could claim right now, and why others cannot be
  claim [ID] [--take-over]        check out a ticket (no ID = pick one for you, spread across agents)
  heartbeat ID                    "still working" (do this at least hourly; a claim silent for 8 h can be taken over)
  release ID [--note TEXT]        give a ticket back to the pool
  block ID --reason TEXT          stop work, explain why (a human or another ticket must unblock it)
  reopen ID                       BLOCKED -> OPEN
  done ID --result PATH           check in: your work is on origin/master and PATH (your results note) exists there
  add --title T [--files ...]     propose a new ticket (it starts OPEN, proposed_by you)
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

BOARD_PATH = "docs/diagnosis/board.json"
STALE_HOURS = 8
PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
STATUSES = ("OPEN", "CLAIMED", "DONE", "BLOCKED")


class BoardError(Exception):
    """A refusal the caller should read and act on (not a bug)."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


# ------------------------------------------------------------------------------------------------ file locks

def _split(pattern: str) -> Tuple[str, bool]:
    """(static prefix, has_wildcard). 'a/b/*.py' -> ('a/b/', True); 'a/b/c.py' -> ('a/b/c.py', False)."""
    p = pattern.replace("\\", "/").lstrip("./")
    for i, ch in enumerate(p):
        if ch in "*?[":
            return p[:i], True
    return p, False


def patterns_overlap(a: str, b: str) -> bool:
    """Conservative: two write scopes overlap if they could ever name the same file."""
    if a.startswith("@") or b.startswith("@"):
        return a == b
    pa, wa = _split(a)
    pb, wb = _split(b)
    if not wa and not wb:
        return pa == pb
    if wa and wb:
        return pa.startswith(pb) or pb.startswith(pa)
    lit, pre = (pa, pb) if wb else (pb, pa)       # one literal path, one wildcard prefix
    return lit.startswith(pre)


def scopes_overlap(a: List[str], b: List[str]) -> List[Tuple[str, str]]:
    return [(x, y) for x in a for y in b if patterns_overlap(x, y)]


# ------------------------------------------------------------------------------------------------ pure board logic

def is_stale(ticket: Dict[str, Any], now: datetime) -> bool:
    if ticket.get("status") != "CLAIMED":
        return False
    beat = _parse(ticket.get("heartbeat_at") or ticket.get("claimed_at"))
    return bool(beat and now - beat > timedelta(hours=STALE_HOURS))


def blockers(doc: Dict[str, Any], tid: str, now: datetime, take_over: bool = False) -> List[str]:
    """Reasons ticket `tid` cannot be claimed right now (empty = it can)."""
    tickets = doc["tickets"]
    t = tickets.get(tid)
    if t is None:
        return [f"no such ticket {tid}"]
    why: List[str] = []
    if t["status"] == "DONE":
        return ["already DONE"]
    if t["status"] == "BLOCKED":
        return [f"BLOCKED: {t.get('blocked_reason') or 'see notes'}"]
    if t["status"] == "CLAIMED":
        if take_over and is_stale(t, now):
            pass
        elif is_stale(t, now):
            return [f"claimed by {t['claimed_by']} but silent for over {STALE_HOURS} h: use --take-over"]
        else:
            return [f"claimed by {t['claimed_by']}"]
    for dep in t.get("depends_on", []):
        d = tickets.get(dep)
        if d is None:
            why.append(f"depends on unknown ticket {dep}")
        elif d["status"] != "DONE":
            why.append(f"waits for {dep} ({d['status']})")
    for oid, o in tickets.items():
        if oid == tid or o["status"] != "CLAIMED":
            continue
        hit = scopes_overlap(t.get("files", []), o.get("files", []))
        if hit:
            x, y = hit[0]
            why.append(f"file lock: {oid} ({o['claimed_by']}) holds {y} which overlaps {x}")
    return why


def eligible(doc: Dict[str, Any], now: datetime, take_over: bool = False) -> List[str]:
    out = [tid for tid, t in doc["tickets"].items()
           if t["status"] in ("OPEN", "CLAIMED") and not blockers(doc, tid, now, take_over)]
    return sorted(out, key=lambda i: (PRIORITY_ORDER.get(doc["tickets"][i].get("priority", "P2"), 9),
                                      doc["tickets"][i].get("wave", 9), i))


def held_by(doc: Dict[str, Any], agent: str) -> List[str]:
    return [i for i, t in doc["tickets"].items() if t["status"] == "CLAIMED" and t.get("claimed_by") == agent]


def _log(t: Dict[str, Any], now: datetime, agent: str, event: str, note: str = "") -> None:
    t.setdefault("history", []).append({"at": _iso(now), "by": agent, "event": event, **({"note": note} if note else {})})


def op_claim(doc, agent, tid, now, take_over=False, allow_multiple=False, rng: Optional[random.Random] = None):
    held = held_by(doc, agent)
    if held and not allow_multiple:
        raise BoardError(f"{agent} already holds {', '.join(held)}: finish it (done), or release it, before claiming another")
    if tid is None:
        ranked = eligible(doc, now, take_over)
        if not ranked:
            raise BoardError("no ticket is claimable right now (run `next` to see why each one is not)")
        # Priority and wave always win. Among equally ranked tickets pick at random, so agents launched in the same
        # second spread out instead of all racing for the first one (the push still decides who really gets it).
        top_key = lambda i: (PRIORITY_ORDER.get(doc["tickets"][i].get("priority", "P2"), 9), doc["tickets"][i].get("wave", 9))
        best = [i for i in ranked if top_key(i) == top_key(ranked[0])]
        tid = (rng or random).choice(best[:5])
    why = blockers(doc, tid, now, take_over)
    if why:
        raise BoardError(f"cannot claim {tid}: " + "; ".join(why))
    t = doc["tickets"][tid]
    prev = t.get("claimed_by")
    t.update(status="CLAIMED", claimed_by=agent, claimed_at=_iso(now), heartbeat_at=_iso(now))
    _log(t, now, agent, "take_over" if prev else "claim", f"from {prev}" if prev else "")
    return tid


def _own(doc, tid, agent, force=False):
    t = doc["tickets"].get(tid)
    if t is None:
        raise BoardError(f"no such ticket {tid}")
    if t["status"] != "CLAIMED":
        raise BoardError(f"{tid} is {t['status']}, not CLAIMED")
    if t.get("claimed_by") != agent and not force:
        raise BoardError(f"{tid} is held by {t.get('claimed_by')}, not {agent}")
    return t


def op_heartbeat(doc, agent, tid, now):
    _own(doc, tid, agent)["heartbeat_at"] = _iso(now)


def op_release(doc, agent, tid, now, note="", force=False):
    t = _own(doc, tid, agent, force)
    _log(t, now, agent, "release", note)
    t.update(status="OPEN", claimed_by=None, claimed_at=None, heartbeat_at=None)


def op_block(doc, agent, tid, now, reason):
    if not reason:
        raise BoardError("--reason is required")
    t = _own(doc, tid, agent)
    _log(t, now, agent, "block", reason)
    t.update(status="BLOCKED", blocked_reason=reason, claimed_by=None, claimed_at=None, heartbeat_at=None)


def op_reopen(doc, agent, tid, now):
    t = doc["tickets"].get(tid)
    if t is None or t["status"] != "BLOCKED":
        raise BoardError(f"{tid} is not BLOCKED")
    _log(t, now, agent, "reopen")
    t.update(status="OPEN", blocked_reason=None)


def op_done(doc, agent, tid, now, result):
    t = _own(doc, tid, agent)
    _log(t, now, agent, "done", result)
    t.update(status="DONE", done_at=_iso(now), result=result, heartbeat_at=_iso(now))


def op_add(doc, agent, now, title, files=(), depends_on=(), wave=3, priority="P2", size="M", needs=(), why=""):
    ids = [int(i[1:]) for i in doc["tickets"] if i[:1] == "D" and i[1:].isdigit()]
    tid = f"D{(max(ids) + 1 if ids else 1):02d}"
    deps = list(depends_on)
    for d in deps:
        if d not in doc["tickets"]:
            raise BoardError(f"unknown dependency {d}")
    doc["tickets"][tid] = {"title": title, "wave": wave, "priority": priority, "size": size, "depends_on": deps,
                           "files": list(files), "needs": list(needs), "status": "OPEN", "claimed_by": None,
                           "claimed_at": None, "heartbeat_at": None, "proposed_by": agent, "why": why,
                           "history": [{"at": _iso(now), "by": agent, "event": "proposed"}]}
    return tid


# ------------------------------------------------------------------------------------------------ git transport

class Board:
    """Reads the board from origin/master and changes it by compare-and-swap pushes. `remote`/`branch` are
    parameters so tests can use a throwaway bare repository."""

    def __init__(self, repo: Path, remote: str = "origin", branch: str = "master", sleep: Callable[[float], None] = time.sleep):
        self.repo = Path(repo)
        self.remote = remote
        self.branch = branch
        self.sleep = sleep

    # -- git
    def git(self, *args: str, env: Optional[dict] = None, data: Optional[str] = None, check: bool = True):
        p = subprocess.run(["git", *args], cwd=self.repo, capture_output=True, input=data.encode("utf-8") if data is not None else None,
                           env={**os.environ, **(env or {})})
        out = p.stdout.decode("utf-8", "replace")
        err = p.stderr.decode("utf-8", "replace")
        if check and p.returncode != 0:
            raise BoardError(f"git {' '.join(args)} failed: {err.strip() or out.strip()}")
        return p.returncode, out.strip(), err.strip()

    def fetch(self) -> str:
        self.git("fetch", self.remote, self.branch)
        return self.git("rev-parse", f"{self.remote}/{self.branch}")[1]

    def read(self) -> Tuple[Dict[str, Any], str]:
        base = self.fetch()
        rc, text, err = self.git("show", f"{base}:{BOARD_PATH}", check=False)
        if rc != 0:
            raise BoardError(f"{BOARD_PATH} is not on {self.remote}/{self.branch} yet ({err})")
        return json.loads(text), base

    def _identity(self) -> dict:
        name = self.git("config", "user.name", check=False)[1] or "diagnosis-board"
        mail = self.git("config", "user.email", check=False)[1] or "board@localhost"
        return {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": mail, "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": mail}

    def _push_board(self, doc: Dict[str, Any], base: str, message: str) -> bool:
        text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
        blob = self.git("hash-object", "-w", "--stdin", data=text)[1]
        with tempfile.TemporaryDirectory() as td:
            env = {"GIT_INDEX_FILE": os.path.join(td, "index")}
            self.git("read-tree", base, env=env)
            self.git("update-index", "--add", "--cacheinfo", f"100644,{blob},{BOARD_PATH}", env=env)
            tree = self.git("write-tree", env=env)[1]
        commit = self.git("commit-tree", tree, "-p", base, "-m", message, env=self._identity())[1]
        rc, _, err = self.git("push", self.remote, f"{commit}:refs/heads/{self.branch}", check=False)
        if rc == 0:
            return True
        if any(s in err for s in ("rejected", "non-fast-forward", "fetch first", "stale info")):
            return False       # somebody else pushed first: re-read and try again
        raise BoardError(f"push failed: {err}")

    def mutate(self, change: Callable[[Dict[str, Any], datetime], Any], message: str, attempts: int = 12):
        """Compare-and-swap loop. `change(doc, now)` edits the document (or raises BoardError, which aborts)."""
        for n in range(attempts):
            doc, base = self.read()
            result = change(doc, _utcnow())
            doc["updated_at"] = _iso(_utcnow())
            if self._push_board(doc, base, message):
                return result
            self.sleep(random.uniform(0.2, 1.0) * (1 + n))
        raise BoardError("the board kept changing under us (12 attempts); wait a moment and retry")


# ------------------------------------------------------------------------------------------------ presentation

def _age(ts: Optional[str], now: datetime) -> str:
    dt = _parse(ts)
    if not dt:
        return "-"
    mins = int((now - dt).total_seconds() // 60)
    return f"{mins}m" if mins < 120 else f"{mins // 60}h"


def render_status(doc: Dict[str, Any], now: Optional[datetime] = None) -> str:
    now = now or _utcnow()
    rows = []
    for tid, t in sorted(doc["tickets"].items(), key=lambda kv: (kv[1].get("wave", 9), kv[0])):
        owner = t.get("claimed_by") or ""
        if is_stale(t, now):
            owner += " (STALE)"
        rows.append((tid, f"W{t.get('wave', '?')}", t.get("priority", ""), t["status"], owner, _age(t.get("heartbeat_at"), now) if t["status"] == "CLAIMED" else "", t["title"][:62]))
    head = ("ID", "Wave", "Pri", "Status", "Held by", "Beat", "Title")
    widths = [max(len(str(r[i])) for r in rows + [head]) for i in range(len(head))]
    line = lambda r: "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(r))
    counts = {s: sum(1 for t in doc["tickets"].values() if t["status"] == s) for s in STATUSES}
    locks = [f"  {t['claimed_by']:<22} {tid}: " + ", ".join(t.get("files", [])) for tid, t in doc["tickets"].items() if t["status"] == "CLAIMED"]
    out = [line(head), line(["-" * w for w in widths])] + [line(r) for r in rows]
    out.append("")
    out.append("  ".join(f"{s}={n}" for s, n in counts.items()))
    if locks:
        out += ["", "Held file locks:"] + locks
    return "\n".join(out)


def render_next(doc: Dict[str, Any], agent: str, now: Optional[datetime] = None) -> str:
    now = now or _utcnow()
    ok = eligible(doc, now)
    lines = [f"Claimable now ({len(ok)}):"]
    for tid in ok:
        t = doc["tickets"][tid]
        lines.append(f"  {tid} [{t.get('priority')}, W{t.get('wave')}, {t.get('size')}] {t['title']}")
    lines.append("")
    lines.append("Not claimable:")
    for tid, t in sorted(doc["tickets"].items()):
        if tid in ok or t["status"] == "DONE":
            continue
        lines.append(f"  {tid}: " + "; ".join(blockers(doc, tid, now)))
    held = held_by(doc, agent)
    if held:
        lines.insert(0, f"{agent} already holds {', '.join(held)}.")
    return "\n".join(lines)


def slug(title: str) -> str:
    keep = "".join(c.lower() if c.isalnum() else "-" for c in title)
    return "-".join(p for p in keep.split("-") if p)[:40]


def claim_banner(doc: Dict[str, Any], tid: str, agent: str) -> str:
    t = doc["tickets"][tid]
    return "\n".join([
        f"CHECKED OUT {tid} - {t['title']}   (agent {agent})",
        f"  write scope: {', '.join(t.get('files', [])) or '(none declared)'}",
        f"  needs      : {', '.join(t.get('needs', [])) or '-'}",
        f"  depends on : {', '.join(t.get('depends_on', [])) or '-'}",
        f"  branch     : diag/{tid}-{slug(t['title'])}",
        "",
        "Next: read the ticket in docs/DIAGNOSIS_TICKETS.md, work in your own git worktree/branch, stay inside the write",
        "scope, run `python scripts/board.py heartbeat " + tid + "` at least hourly, and check in with `done` when merged.",
    ])


# ------------------------------------------------------------------------------------------------ CLI

def _verify_landed(board: Board, result: str) -> None:
    base = board.fetch()
    rc, _, _ = board.git("cat-file", "-e", f"{base}:{result}", check=False)
    if rc != 0:
        raise BoardError(f"results note {result} is not on {board.remote}/{board.branch}: commit and push it first")
    head = board.git("rev-parse", "HEAD")[1]
    rc, _, _ = board.git("merge-base", "--is-ancestor", head, base, check=False)
    if rc != 0:
        raise BoardError("your current HEAD is not on origin/master yet: merge origin/master into your branch, test, and "
                         "`git push origin HEAD:master` first (or pass --no-verify if the work was pushed from another checkout)")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["status", "next", "claim", "heartbeat", "release", "block", "reopen", "done", "add"])
    ap.add_argument("ticket", nargs="?")
    ap.add_argument("--agent", default=os.environ.get("DIAG_AGENT"))
    ap.add_argument("--repo", default=".")
    ap.add_argument("--remote", default="origin")
    ap.add_argument("--branch", default="master")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--take-over", action="store_true")
    ap.add_argument("--allow-multiple", action="store_true")
    ap.add_argument("--jitter", type=float, default=0.0, help="sleep a random 0..N seconds first (spreads simultaneous launches)")
    ap.add_argument("--note", default="")
    ap.add_argument("--reason", default="")
    ap.add_argument("--result", default="")
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--title", default="")
    ap.add_argument("--files", nargs="*", default=[])
    ap.add_argument("--depends-on", nargs="*", default=[])
    ap.add_argument("--wave", type=int, default=3)
    ap.add_argument("--priority", default="P2")
    ap.add_argument("--size", default="M")
    ap.add_argument("--needs", nargs="*", default=[])
    ap.add_argument("--why", default="")
    a = ap.parse_args(argv)

    board = Board(Path(a.repo), a.remote, a.branch)
    need_agent = a.command not in ("status",)
    if need_agent and not a.agent:
        print("error: give --agent NAME (or set DIAG_AGENT); use a unique name per agent", file=sys.stderr)
        return 2
    try:
        if a.command == "status":
            doc, _ = board.read()
            print(json.dumps(doc, indent=2) if a.json else render_status(doc))
        elif a.command == "next":
            doc, _ = board.read()
            print(render_next(doc, a.agent))
        elif a.command == "claim":
            if a.jitter:
                time.sleep(random.uniform(0, a.jitter))
            tid = board.mutate(lambda d, now: op_claim(d, a.agent, a.ticket, now, a.take_over, a.allow_multiple),
                               f"board: {a.agent} checks out {a.ticket or 'a ticket'}")
            doc, _ = board.read()
            print(claim_banner(doc, tid, a.agent))
        elif a.command == "heartbeat":
            board.mutate(lambda d, now: op_heartbeat(d, a.agent, a.ticket, now), f"board: {a.agent} heartbeat {a.ticket}")
            print(f"heartbeat recorded for {a.ticket}")
        elif a.command == "release":
            board.mutate(lambda d, now: op_release(d, a.agent, a.ticket, now, a.note, a.force), f"board: {a.agent} releases {a.ticket}")
            print(f"{a.ticket} is OPEN again")
        elif a.command == "block":
            board.mutate(lambda d, now: op_block(d, a.agent, a.ticket, now, a.reason), f"board: {a.ticket} BLOCKED")
            print(f"{a.ticket} is BLOCKED: {a.reason}")
        elif a.command == "reopen":
            board.mutate(lambda d, now: op_reopen(d, a.agent, a.ticket, now), f"board: {a.ticket} reopened")
            print(f"{a.ticket} is OPEN again")
        elif a.command == "done":
            if not a.result:
                raise BoardError("--result PATH is required (your results note, e.g. docs/diagnosis/results/D01.md)")
            if not a.no_verify:
                _verify_landed(board, a.result)
            board.mutate(lambda d, now: op_done(d, a.agent, a.ticket, now, a.result), f"board: {a.ticket} DONE ({a.agent})")
            print(f"{a.ticket} checked in as DONE. Run `python scripts/board.py next` to see what is claimable now.")
        elif a.command == "add":
            if not a.title:
                raise BoardError("--title is required")
            tid = board.mutate(lambda d, now: op_add(d, a.agent, now, a.title, a.files, a.depends_on, a.wave, a.priority,
                                                     a.size, a.needs, a.why), f"board: {a.agent} proposes a ticket")
            print(f"added {tid} (OPEN). Describe it in docs/DIAGNOSIS_TICKETS.md under '### {tid}' in your own branch.")
    except BoardError as e:
        print(f"board: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
