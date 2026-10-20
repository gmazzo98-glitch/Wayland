"""
Looks at, exports and undoes a run of the site evidence reader that was started from the app (T26), straight
from the database, with no browser and nothing else running.

    python scripts/site_evidence_run.py list                          # the latest runs and their state
    python scripts/site_evidence_run.py show   [RUN_ID]               # what the app shows: state, progress, alerts, last events
    python scripts/site_evidence_run.py audit  [RUN_ID] --out FILE    # the full audit bundle (JSON), to attach to a bug report
    python scripts/site_evidence_run.py rollback [RUN_ID]             # DRY RUN: what undoing the run's signals would do
    python scripts/site_evidence_run.py rollback [RUN_ID] --apply     # undo it (needs --apply)

RUN_ID defaults to the latest run. Rollback uses only the audit trail: signals the run created are removed, the
placeholder rows it replaced are put back exactly as they were, and anything another source changed since is left
alone. Every undo is itself written to the audit trail.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import site_evidence_hub as hub                                      # noqa: E402
from database import get_db_session                                  # noqa: E402
from models import SiteEvidenceRun                                   # noqa: E402


def _run(db, run_id):
    run = db.get(SiteEvidenceRun, run_id) if run_id else hub.latest_run(db)
    if run is None:
        sys.exit("No such run." if run_id else "No run has been started yet.")
    return run


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["list", "show", "audit", "rollback"])
    ap.add_argument("run_id", nargs="?")
    ap.add_argument("--out", help="audit: where to write the bundle")
    ap.add_argument("--apply", action="store_true", help="rollback: really change the database (default is a dry run)")
    args = ap.parse_args()
    db = get_db_session()

    if args.command == "list":
        for r in db.query(SiteEvidenceRun).order_by(SiteEvidenceRun.requested_at.desc()).limit(20):
            print(f"{r.id}  {r.status:9} requested {r.requested_at:%Y-%m-%d %H:%M}  {r.done or 0}/{r.input_total or 0} companies  "
                  f"{r.signals_written} signals written, {r.signals_skipped} left alone")
        return 0

    run = _run(db, args.run_id)
    if args.command == "show":
        v = hub.view_of(db, run)
        p = v["progress"]
        print(f"Run {run.id}\n  {v['label']}  ({v['tone']})\n  {v['detail']}")
        print(f"  {p['done']} of {p['total']} companies, {p['hits_total']} quotes, {run.signals_written} signals written, "
              f"{run.signals_skipped} left alone")
        for a in v["alerts"]:
            print(f"  ! {a['text']}")
        if run.last_error:
            print(f"  error: {run.last_error}")
        print("  latest problems:")
        for e in reversed(hub.events(db, run.id, ["error", "warn"], limit=15)):
            print(f"    {e.ts:%Y-%m-%d %H:%M:%S} {e.level:5} {e.code}: {e.message[:140]}")
        return 0

    if args.command == "audit":
        out = Path(args.out or f"site-evidence-audit-{run.id[:8]}.json")
        out.write_text(hub.audit_bundle_json(db, run.id), encoding="utf-8")
        print(f"Wrote {out} ({out.stat().st_size // 1024} KB)")
        return 0

    plan = hub.rollback_plan(db, run.id)
    counts = {}
    for item in plan:
        counts[item["action"]] = counts.get(item["action"], 0) + 1
    print(f"{'APPLY' if args.apply else 'DRY RUN'}: run {run.id} ({run.status}), {len(plan)} signal(s) touched: {counts}")
    for item in plan:
        print(f"  {item['action']:8} {item['registration_number']} {str(item['name'])[:34]:34} {item['signal_key']:30} {item.get('reason') or ''}")
    if not args.apply:
        print("\nNothing changed. Add --apply to undo.")
        return 0
    print("\nDone:", hub.rollback_apply(db, run.id))
    return 0


if __name__ == "__main__":
    sys.exit(main())
