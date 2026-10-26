"""
Reads and edits READ_FIRST_AGENT_STATUS_AND_OPEN_POINTS.md so agents never hand-edit the open points.

Only the point you name is changed (plus, on DONE, moving that one point from section C to the Closed
log in section E). Sections A, B and D and every other point are left byte for byte as they were.
The script does not run git: commit and push the file yourself ("claim OP-xxx" / "checkin OP-xxx").

Commands:
  list [--status S]                          every point: id, status, needs, owner, title
  show OP-xxx                                one point in full
  checkout OP-xxx --agent NAME [--reclaim]   claim an OPEN, agent-work point whose blockers are all DONE
                                             (48 h lease; --reclaim takes over a CHECKED_OUT point whose lease expired)
  extend OP-xxx [--hours N]                  push lease_until out (default 48 h from now)
  checkin OP-xxx --status DONE|OPEN|BLOCKED|NEEDS_DECISION --note TEXT [--commit HASH] [--blocked-by OP-..]
                                             [--needs "agent work"|"decision from Giorgio"|"external data"]
  reopen OP-xxx --note TEXT                  BLOCKED agent-work point whose blockers are all DONE -> OPEN
  note OP-xxx --text TEXT                    add a dated note to any point (also allowed on NEEDS_DECISION: a
                                             recommendation, never a resolution)
  add --title T --needs N --context C --done-when D [--blocked-by OP-..] [--note TEXT]
                                             open a new point with the next free number
  validate                                   check the file and the read-first line in CLAUDE.md, AGENTS.md, README.md
Add --file PATH to work on another copy (tests do), and --now ISO_TIMESTAMP to fake the clock.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
STATUS_FILE = ROOT / "READ_FIRST_AGENT_STATUS_AND_OPEN_POINTS.md"
READ_FIRST_LINE = ("Before doing anything, read READ_FIRST_AGENT_STATUS_AND_OPEN_POINTS.md. "
                   "Before you finish, update it.")
FIRST_LINE_FILES = ("CLAUDE.md", "AGENTS.md", "README.md")

STATUSES = ("OPEN", "CHECKED_OUT", "NEEDS_DECISION", "BLOCKED", "DONE")
NEEDS = ("agent work", "decision from Giorgio", "external data")
FIELDS = ("status", "needs", "owner", "checked_out_at", "lease_until", "blocked_by", "context", "done_when", "notes")
LEASE_HOURS = 48
SECTION_ORDER = ["A", "B", "C", "D", "E"]

SECTION_RE = re.compile(r"^## ([A-E])\. ")
POINT_RE = re.compile(r"^### (OP-\d{3,}): (.+?)\s*$")
FIELD_RE = re.compile(r"^\* (status|needs|owner|checked_out_at|lease_until|blocked_by|context|done_when|notes):[ ]?(.*)$")
NOTE_RE = re.compile(r"^  - (.*)$")


class PointsError(Exception):
    pass


@dataclass
class Point:
    id: str
    title: str
    section: str
    start: int
    end: int
    fields: Dict[str, str] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)

    def get(self, name: str) -> str:
        return self.fields.get(name, "")

    def blockers(self) -> List[str]:
        v = self.get("blocked_by").strip()
        return [] if v in ("", "none") else [x.strip() for x in v.split(",") if x.strip()]


# --------------------------------------------------------------------------- file io

class Doc:
    def __init__(self, path: Path):
        self.path = path
        raw = path.read_bytes().decode("utf-8")
        self.eol = "\r\n" if "\r\n" in raw else "\n"
        self.lines: List[str] = raw.replace("\r\n", "\n").split("\n")

    def save(self) -> None:
        self.path.write_bytes(self.eol.join(self.lines).encode("utf-8"))

    # ---- parsing
    def section_starts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for i, line in enumerate(self.lines):
            m = SECTION_RE.match(line)
            if m and m.group(1) not in out:
                out[m.group(1)] = i
        return out

    def points(self) -> List[Point]:
        starts = self.section_starts()
        bounds = sorted((i, s) for s, i in starts.items())

        def section_at(idx: int) -> str:
            cur = ""
            for i, s in bounds:
                if i <= idx:
                    cur = s
            return cur

        pts: List[Point] = []
        i = 0
        n = len(self.lines)
        while i < n:
            m = POINT_RE.match(self.lines[i])
            if not m:
                i += 1
                continue
            j = i + 1
            while j < n and not self.lines[j].startswith("## ") and not self.lines[j].startswith("### "):
                j += 1
            p = Point(id=m.group(1), title=m.group(2), section=section_at(i), start=i, end=j)
            in_notes = False
            for line in self.lines[i + 1:j]:
                if not line.strip():
                    continue
                fm = FIELD_RE.match(line)
                if fm:
                    name, val = fm.group(1), fm.group(2).strip()
                    if name in p.fields:
                        p.problems.append(f"field '{name}' appears twice")
                    p.fields[name] = val
                    in_notes = name == "notes"
                    continue
                nm = NOTE_RE.match(line)
                if nm and in_notes:
                    p.notes.append(nm.group(1))
                    continue
                p.problems.append(f"unparseable line: {line.strip()[:60]!r}")
            pts.append(p)
            i = j
        return pts

    def find(self, op: str) -> Point:
        for p in self.points():
            if p.id == op:
                return p
        raise PointsError(f"{op} does not exist")

    # ---- editing
    @staticmethod
    def render(p: Point) -> List[str]:
        out = [f"### {p.id}: {p.title}"]
        for name in FIELDS:
            if name == "notes":
                out.append("* notes:")
                out += [f"  - {n}" for n in p.notes]
            else:
                out.append(f"* {name}: {p.fields.get(name, 'none')}")
        out.append("")
        return out

    def replace(self, p: Point) -> None:
        self.lines[p.start:p.end] = self.render(p)

    def remove(self, p: Point) -> None:
        del self.lines[p.start:p.end]

    def insert_before_section(self, section_letter: str, block: List[str]) -> None:
        idx = self.section_starts()[section_letter]
        self.lines[idx:idx] = block

    def append_to_closed_log(self, block: List[str]) -> None:
        for i, line in enumerate(self.lines):
            if line.strip() == "_none yet_":
                del self.lines[i]
                break
        while self.lines and self.lines[-1] == "":
            self.lines.pop()
        self.lines += [""] + block[:-1]  # render() ends with a blank line; the file's own trailing newline replaces it
        self.lines.append("")


# --------------------------------------------------------------------------- time

def now_utc(override: Optional[str] = None) -> datetime:
    if override:
        return parse_ts(override)
    return datetime.now(timezone.utc).replace(microsecond=0)


def parse_ts(s: str) -> datetime:
    dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fmt_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def dated(now: datetime, text: str) -> str:
    return f"{now.date().isoformat()}: {text}"


# --------------------------------------------------------------------------- validation

def validate_text(doc: Doc) -> List[str]:
    errs: List[str] = []
    starts = doc.section_starts()
    if [s for s in SECTION_ORDER if s in starts] != SECTION_ORDER or [s for _, s in sorted((i, s) for s, i in starts.items())] != SECTION_ORDER:
        errs.append("sections A, B, C, D, E must all be present, in that order")
    pts = doc.points()
    seen: Dict[str, int] = {}
    for p in pts:
        seen[p.id] = seen.get(p.id, 0) + 1
    errs += [f"{k} is duplicated ({v} times)" for k, v in seen.items() if v > 1]
    by_id = {p.id: p for p in pts}
    for p in pts:
        tag = p.id
        errs += [f"{tag}: {x}" for x in p.problems]
        for name in FIELDS:
            if name not in p.fields:
                errs.append(f"{tag}: missing field '{name}'")
        st = p.get("status")
        if st not in STATUSES:
            errs.append(f"{tag}: invalid status {st!r}")
        if p.get("needs") not in NEEDS:
            errs.append(f"{tag}: invalid needs {p.get('needs')!r} (one of {NEEDS})")
        if p.section not in ("C", "E"):
            errs.append(f"{tag}: points belong in section C or E, found in {p.section or 'no section'}")
        if st == "CHECKED_OUT":
            for name in ("owner", "checked_out_at", "lease_until"):
                if p.get(name) in ("", "none"):
                    errs.append(f"{tag}: CHECKED_OUT but {name} is none")
        elif st in STATUSES and any(p.get(n) not in ("", "none") for n in ("owner", "lease_until", "checked_out_at")):
            errs.append(f"{tag}: status {st} but owner/checked_out_at/lease_until are not 'none'")
        for name in ("checked_out_at", "lease_until"):
            v = p.get(name)
            if v not in ("", "none"):
                try:
                    parse_ts(v)
                except ValueError:
                    errs.append(f"{tag}: {name} {v!r} is not an ISO timestamp")
        if st == "DONE" and p.section != "E":
            errs.append(f"{tag}: DONE but not in the Closed log (section E)")
        if p.section == "E" and st != "DONE":
            errs.append(f"{tag}: in the Closed log but status is {st}")
        for b in p.blockers():
            if b not in by_id:
                errs.append(f"{tag}: blocked_by {b} does not exist")
            elif b == p.id:
                errs.append(f"{tag}: blocked by itself")
        if st == "NEEDS_DECISION" and p.get("needs") != "decision from Giorgio":
            errs.append(f"{tag}: NEEDS_DECISION must have needs 'decision from Giorgio'")
    return errs


def validate_first_lines(root: Path = ROOT) -> List[str]:
    errs = []
    for name in FIRST_LINE_FILES:
        path = root / name
        if not path.exists():
            errs.append(f"{name} is missing")
            continue
        first = path.read_text(encoding="utf-8").lstrip("﻿").split("\n", 1)[0].strip()
        if first != READ_FIRST_LINE:
            errs.append(f"{name}: first line must be exactly: {READ_FIRST_LINE!r}")
    return errs


# --------------------------------------------------------------------------- commands

def done_ids(doc: Doc) -> set:
    return {p.id for p in doc.points() if p.get("status") == "DONE"}


def cmd_list(doc: Doc, args) -> None:
    rows = [p for p in doc.points() if not args.status or p.get("status") == args.status]
    w = max([len(p.get("status")) for p in rows] + [6])
    for p in rows:
        owner = p.get("owner") if p.get("owner") != "none" else "-"
        print(f"{p.id}  {p.get('status'):<{w}}  {p.get('needs'):<22}  {owner:<18}  {p.title}")


def cmd_show(doc: Doc, args) -> None:
    print("\n".join(doc.render(doc.find(args.op))).rstrip())


def cmd_checkout(doc: Doc, args) -> None:
    p, now = doc.find(args.op), now_utc(args.now)
    if p.section != "C":
        raise PointsError(f"{p.id} is in the Closed log")
    st = p.get("status")
    if st == "CHECKED_OUT":
        if not args.reclaim:
            raise PointsError(f"{p.id} is checked out by {p.get('owner')} until {p.get('lease_until')} (use --reclaim only after the lease expired)")
        if parse_ts(p.get("lease_until")) > now:
            raise PointsError(f"{p.id}: lease runs until {p.get('lease_until')}, it cannot be reclaimed yet")
        p.notes.insert(0, dated(now, f"reclaimed from {p.get('owner')}: lease_until {p.get('lease_until')} had passed with no update on this point"))
    elif st != "OPEN":
        raise PointsError(f"{p.id} has status {st}; only OPEN points can be checked out")
    if p.get("needs") != "agent work":
        raise PointsError(f"{p.id} needs '{p.get('needs')}', not agent work")
    open_blockers = [b for b in p.blockers() if b not in done_ids(doc)]
    if open_blockers:
        raise PointsError(f"{p.id} is blocked by {', '.join(open_blockers)} which are not DONE")
    p.fields.update(status="CHECKED_OUT", owner=args.agent, checked_out_at=fmt_ts(now),
                    lease_until=fmt_ts(now + timedelta(hours=LEASE_HOURS)))
    p.notes.insert(0, dated(now, f"checked out by {args.agent}"))
    doc.replace(p)
    doc.save()
    print(f"{p.id} checked out by {args.agent} until {p.fields['lease_until']}")
    print(f'next: git add {STATUS_FILE.name} && git commit -m "claim {p.id}" && git push   (this file only, before starting the work)')


def cmd_extend(doc: Doc, args) -> None:
    p, now = doc.find(args.op), now_utc(args.now)
    if p.get("status") != "CHECKED_OUT":
        raise PointsError(f"{p.id} is not checked out")
    p.fields["lease_until"] = fmt_ts(now + timedelta(hours=args.hours))
    p.notes.insert(0, dated(now, f"lease extended to {p.fields['lease_until']}"))
    doc.replace(p)
    doc.save()
    print(f"{p.id} lease_until {p.fields['lease_until']}")


def check_ids_exist(doc: Doc, ids: List[str]) -> None:
    known = {p.id for p in doc.points()}
    missing = [i for i in ids if i not in known]
    if missing:
        raise PointsError(f"unknown point id(s): {', '.join(missing)}")


def cmd_checkin(doc: Doc, args) -> None:
    p, now = doc.find(args.op), now_utc(args.now)
    if p.get("status") != "CHECKED_OUT":
        raise PointsError(f"{p.id} is not checked out (status {p.get('status')})")
    if args.agent and args.agent != p.get("owner"):
        raise PointsError(f"{p.id} is held by {p.get('owner')}, not {args.agent}")
    if args.status == "CHECKED_OUT":
        raise PointsError("use extend to keep a point checked out")
    blockers = [b.strip() for b in (args.blocked_by or "").split(",") if b.strip()]
    check_ids_exist(doc, blockers)
    if args.status == "DONE" and not args.commit:
        raise PointsError("DONE needs --commit HASH (the commit of the work)")
    if args.status == "BLOCKED" and not (blockers or args.needs):
        raise PointsError("BLOCKED needs --blocked-by OP-.. or --needs 'external data'")
    p.fields.update(status=args.status, owner="none", checked_out_at="none", lease_until="none")
    if blockers or args.status in ("OPEN", "DONE"):
        p.fields["blocked_by"] = ", ".join(blockers) if blockers else "none"
    if args.status == "NEEDS_DECISION":
        p.fields["needs"] = "decision from Giorgio"
    elif args.needs:
        p.fields["needs"] = args.needs
    line = args.note + (f" (commit {args.commit})" if args.commit else "")
    p.notes.insert(0, dated(now, line))
    if args.status == "DONE":
        doc.remove(p)
        doc.append_to_closed_log(doc.render(p))
    else:
        doc.replace(p)
    bad = validate_text(doc)
    if bad:
        raise PointsError("refusing to save, the result would not validate:\n  - " + "\n  - ".join(bad))
    doc.save()
    print(f"{p.id} checked in as {args.status}")
    print(f'next: git add {STATUS_FILE.name} && git commit -m "checkin {p.id}" && git push')


def cmd_reopen(doc: Doc, args) -> None:
    p, now = doc.find(args.op), now_utc(args.now)
    if p.get("status") != "BLOCKED" or p.get("needs") != "agent work":
        raise PointsError(f"{p.id}: only a BLOCKED point that needs agent work can be reopened")
    open_blockers = [b for b in p.blockers() if b not in done_ids(doc)]
    if open_blockers:
        raise PointsError(f"{p.id} is still blocked by {', '.join(open_blockers)}")
    p.fields["status"] = "OPEN"
    p.notes.insert(0, dated(now, f"reopened: {args.note}"))
    doc.replace(p)
    doc.save()
    print(f"{p.id} is OPEN")


def cmd_note(doc: Doc, args) -> None:
    p, now = doc.find(args.op), now_utc(args.now)
    p.notes.insert(0, dated(now, args.text))
    doc.replace(p)
    doc.save()
    print(f"note added to {p.id}")


def cmd_add(doc: Doc, args) -> None:
    now = now_utc(args.now)
    if args.needs not in NEEDS:
        raise PointsError(f"--needs must be one of {NEEDS}")
    blockers = [b.strip() for b in (args.blocked_by or "").split(",") if b.strip()]
    check_ids_exist(doc, blockers)
    nums = [int(p.id.split("-")[1]) for p in doc.points()]
    op = f"OP-{max(nums + [0]) + 1:03d}"
    open_blockers = [b for b in blockers if b not in done_ids(doc)]
    if args.needs == "decision from Giorgio":
        status = "NEEDS_DECISION"
    elif open_blockers or args.needs == "external data":
        status = "BLOCKED"
    else:
        status = "OPEN"
    p = Point(id=op, title=args.title, section="C", start=0, end=0)
    p.fields = {"status": status, "needs": args.needs, "owner": "none", "checked_out_at": "none", "lease_until": "none",
                "blocked_by": ", ".join(blockers) if blockers else "none", "context": args.context,
                "done_when": args.done_when}
    p.notes = [dated(now, args.note or "added while working on another point")]
    doc.insert_before_section("D", doc.render(p))
    bad = validate_text(doc)
    if bad:
        raise PointsError("refusing to save, the result would not validate:\n  - " + "\n  - ".join(bad))
    doc.save()
    print(f"added {op} ({status})")


def cmd_validate(doc: Doc, args) -> int:
    errs = validate_text(doc)
    if args.file == STATUS_FILE or args.root_files:
        errs += validate_first_lines(ROOT if args.file == STATUS_FILE else Path(args.root_files))
    if errs:
        print("INVALID:\n  - " + "\n  - ".join(errs))
        return 1
    pts = doc.points()
    print(f"OK: {len(pts)} points ({sum(p.section == 'C' for p in pts)} open, {sum(p.section == 'E' for p in pts)} closed)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", type=Path, default=STATUS_FILE)
    ap.add_argument("--now", help="ISO timestamp to use instead of the clock (tests)")
    ap.add_argument("--root-files", help="directory holding CLAUDE.md/AGENTS.md/README.md to check when --file is not the repo file")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("list"); s.add_argument("--status", choices=STATUSES)
    s = sub.add_parser("show"); s.add_argument("op")
    s = sub.add_parser("checkout"); s.add_argument("op"); s.add_argument("--agent", required=True); s.add_argument("--reclaim", action="store_true")
    s = sub.add_parser("extend"); s.add_argument("op"); s.add_argument("--hours", type=int, default=LEASE_HOURS)
    s = sub.add_parser("checkin"); s.add_argument("op"); s.add_argument("--status", required=True, choices=("DONE", "OPEN", "BLOCKED", "NEEDS_DECISION"))
    s.add_argument("--note", required=True); s.add_argument("--commit"); s.add_argument("--blocked-by"); s.add_argument("--needs", choices=NEEDS); s.add_argument("--agent")
    s = sub.add_parser("reopen"); s.add_argument("op"); s.add_argument("--note", required=True)
    s = sub.add_parser("note"); s.add_argument("op"); s.add_argument("--text", required=True)
    s = sub.add_parser("add"); s.add_argument("--title", required=True); s.add_argument("--needs", required=True, choices=NEEDS)
    s.add_argument("--context", required=True); s.add_argument("--done-when", required=True); s.add_argument("--blocked-by"); s.add_argument("--note")
    sub.add_parser("validate")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        doc = Doc(args.file)
        if args.cmd == "validate":
            return cmd_validate(doc, args)
        {"list": cmd_list, "show": cmd_show, "checkout": cmd_checkout, "extend": cmd_extend, "checkin": cmd_checkin,
         "reopen": cmd_reopen, "note": cmd_note, "add": cmd_add}[args.cmd](doc, args)
        return 0
    except PointsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
