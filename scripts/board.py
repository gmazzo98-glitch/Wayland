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
  preflight [ID]                  can this session actually do the work? (write, git, network, own worktree). Run it
                                  BEFORE claiming; if it fails, claim nothing and report its output to the user
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
import shutil
import subprocess
import sys
import threading
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

    def paths(self) -> Dict[str, Path]:
        """toplevel / git_dir / common_dir as absolute paths. git_dir == common_dir means the shared main checkout."""
        top = Path(self.git("rev-parse", "--show-toplevel")[1])
        git_dir = Path(self.git("rev-parse", "--absolute-git-dir")[1])
        common = Path(self.git("rev-parse", "--git-common-dir")[1])
        if not common.is_absolute():
            common = (self.repo / common)
        return {"top": top.resolve(), "git_dir": git_dir.resolve(), "common_dir": common.resolve()}

    def _push_board(self, doc: Dict[str, Any], base: str, message: str) -> bool:
        text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
        blob = self.git("hash-object", "-w", "--stdin", data=text)[1]
        # The scratch index lives inside the git directory, not in %TEMP%: writing there is already required to
        # create objects, whereas a sandbox may well forbid the system temp folder.
        index = self.paths()["git_dir"] / f"dx_board_index_{os.getpid()}_{threading.get_ident()}"
        try:
            env = {"GIT_INDEX_FILE": str(index)}
            self.git("read-tree", base, env=env)
            self.git("update-index", "--add", "--cacheinfo", f"100644,{blob},{BOARD_PATH}", env=env)
            tree = self.git("write-tree", env=env)[1]
        finally:
            try:
                index.unlink()
            except OSError:
                pass
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


def slug(title: str, limit: int = 32) -> str:
    """Branch-name slug that stops at a word boundary: 'Fact readers A: identity, ...' -> 'fact-readers-a-identity'."""
    keep = "".join(c.lower() if c.isalnum() else "-" for c in title)
    out = ""
    for word in (p for p in keep.split("-") if p):
        joined = f"{out}-{word}" if out else word
        if len(joined) > limit:
            break
        out = joined
    return out or "ticket"


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


# ------------------------------------------------------------------------------------------------ preflight

class Check:
    def __init__(self, name: str, ok: bool, detail: str = "", fix: str = "", required: bool = True):
        self.name, self.ok, self.detail, self.fix, self.required = name, ok, detail, fix, required


def _probe_write(directory: Path) -> Tuple[bool, str]:
    """Really create and delete a file: os.access() lies under sandboxes."""
    probe = directory / f".dx_preflight_{os.getpid()}_{threading.get_ident()}"
    try:
        probe.write_text("x")
        probe.unlink()
        return True, ""
    except OSError as e:
        return False, f"{type(e).__name__}: {e}"


SANDBOX_FIX = ("Your session is not allowed to write there. Ask GG to start this agent with write access to the repository "
               "(including its .git folder) and network access to GitHub, or with full access; in Codex that is the "
               "workspace-write writable roots or danger-full-access, in Claude Code a permission mode that allows edits and "
               "git. Alternatively start the session in the project folder itself (not an app-managed worktree elsewhere) and "
               "create your worktree under .claude/worktrees/.")


def check_local_writes(board: "Board") -> List[Check]:
    try:
        p = board.paths()
    except BoardError as e:
        return [Check("inside a git repository", False, str(e), "Run this from inside the Project Vienna repository.")]
    out = []
    ok, why = _probe_write(p["top"])
    out.append(Check("can write files in the working tree", ok, why or str(p["top"]), SANDBOX_FIX))
    for label, d in (("this worktree's git directory", p["git_dir"]), ("the shared git directory", p["common_dir"])):
        ok, why = _probe_write(d)
        out.append(Check(f"can write inside {label}", ok, why or str(d), SANDBOX_FIX))
    rc, _, err = board.git("hash-object", "-w", "--stdin", data=f"preflight {os.getpid()} {time.time()}", check=False)
    out.append(Check("can create git objects (commits)", rc == 0, err, SANDBOX_FIX))
    return out


def run_preflight(board: "Board", ticket: Optional[str] = None) -> List[Check]:
    """Everything an agent needs to be able to finish a ticket, tested BEFORE it claims one. A claim followed by
    'I cannot write here' blocks a ticket for nothing; this turns that into a clear refusal up front."""
    checks = check_local_writes(board)
    if not checks or checks[0].name == "inside a git repository":
        return checks
    p = board.paths()
    linked = p["git_dir"] != p["common_dir"]
    checks.insert(0, Check("working in your own worktree, not the shared main checkout", linked, str(p["top"]),
                           "Create your own worktree and run this again from it: "
                           "git worktree add .claude/worktrees/diag-<agent> --detach origin/master"))
    rc, out, err = board.git("ls-remote", "--exit-code", board.remote, board.branch, check=False)
    reach = rc == 0
    checks.append(Check(f"can reach {board.remote} (fetch)", reach, (err or out)[:200],
                        "No network or no GitHub access from this session. Ask GG to enable network access for the agent."))
    if reach:
        sha = out.split()[0] if out else ""
        rc, _, err = board.git("push", "--dry-run", board.remote, f"{sha}:refs/heads/diag-preflight-dry-run", check=False)
        checks.append(Check(f"can push to {board.remote} (dry run)", rc == 0, err[-200:],
                            "Git credentials are not available to this session. Ask GG to check `git push` works from this machine."))
        try:
            doc, _ = board.read()
            checks.append(Check("the board is on origin/master", True, f"{len(doc['tickets'])} tickets"))
        except BoardError as e:
            checks.append(Check("the board is on origin/master", False, str(e), "Merge/fetch origin/master; ask GG if the board was pushed."))
    import importlib.util as ilu
    missing = [m for m in ("pytest", "sqlalchemy") if ilu.find_spec(m) is None]
    checks.append(Check("python test tooling (pytest, sqlalchemy)", not missing, "missing: " + ", ".join(missing) if missing else "",
                        "pip install -r requirements.txt"))
    main_root = p["common_dir"].parent
    env_here = (p["top"] / ".env").exists()
    env_main = (main_root / ".env").exists()
    checks.append(Check(".env available for read-only database access (only tickets with live_db_read need it)", env_here or env_main,
                        "in this worktree" if env_here else (f"found in the main checkout: copy {main_root / '.env'}" if env_main else "not found"),
                        "Not needed unless your ticket lists live_db_read.", required=False))
    if ticket:
        # Check the ticket's declared needs that can be tested from here.
        try:
            doc, _ = board.read()
            needs = doc["tickets"][ticket].get("needs", [])
        except (BoardError, KeyError):
            needs = []
        if "scraper_repo" in needs:
            scr = os.environ.get("SCRAPER_CRAWLERS_DIR") or str(main_root.parent / "Scraper" / "crawlers")
            checks.append(Check("the Scraper repository is reachable (ticket needs scraper_repo)", Path(scr).exists(), scr,
                                "Set SCRAPER_CRAWLERS_DIR to the Scraper checkout's crawlers folder; the session needs write access there too."))
        if "local_model" in needs:
            checks.append(Check("Ollama present (ticket needs local_model)", shutil.which("ollama") is not None, "",
                                "This ticket needs the local model: run it on the PC that has Ollama, or release it."))
        for n in needs:
            if n in ("web", "llm", "live_db_read", "live_db_write", "gg_input", "gg_approval"):
                checks.append(Check(f"ticket needs {n}", True, "cannot be tested from here: confirm yourself before you start", "", required=False))
    return checks


def render_preflight(checks: List[Check]) -> Tuple[str, bool]:
    lines = []
    failed = [c for c in checks if c.required and not c.ok]
    for c in checks:
        tag = "ok  " if c.ok else ("FAIL" if c.required else "note")
        lines.append(f"  [{tag}] {c.name}" + (f"  ({c.detail})" if c.detail else ""))
        if not c.ok and c.fix:
            lines.append(f"         fix: {c.fix}")
    if failed:
        lines += ["", "RESULT: FAIL. Do NOT claim a ticket. Stop and tell the user exactly which checks failed (the lines above), and what",
                  "access they need to give this session. Do not work around a refused permission and do not keep asking for approvals."]
    else:
        lines += ["", "RESULT: OK. You can claim a ticket."]
    return "PREFLIGHT\n" + "\n".join(lines), not failed


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
    ap.add_argument("command", choices=["status", "preflight", "next", "claim", "heartbeat", "release", "block", "reopen", "done", "add"])
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
    need_agent = a.command not in ("status", "preflight")
    if need_agent and not a.agent:
        print("error: give --agent NAME (or set DIAG_AGENT); use a unique name per agent", file=sys.stderr)
        return 2
    try:
        if a.command == "status":
            doc, _ = board.read()
            print(json.dumps(doc, indent=2) if a.json else render_status(doc))
        elif a.command == "preflight":
            text, ok = render_preflight(run_preflight(board, a.ticket))
            print(text)
            return 0 if ok else 1
        elif a.command == "next":
            doc, _ = board.read()
            print(render_next(doc, a.agent))
        elif a.command == "claim":
            if a.jitter:
                time.sleep(random.uniform(0, a.jitter))
            bad = [c for c in check_local_writes(board) if not c.ok]
            if bad:       # never hold a ticket from a session that cannot write: it would only block everyone else
                raise BoardError("not claiming: " + "; ".join(f"{c.name} failed ({c.detail})" for c in bad)
                                 + ". Run `python scripts/board.py preflight` and report its output to the user.")
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
