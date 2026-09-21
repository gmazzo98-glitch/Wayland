"""
Imports the ORIGINAL AIDA exports (the WAYLAND_*.xls files) into signals — see aida_import.py for what it derives
and why it does not go through the curated "Main" spreadsheet.

    python scripts/import_aida.py                          # DRY RUN: parse, derive, report; writes nothing
    python scripts/import_aida.py --apply                  # backup, then write
    python scripts/import_aida.py --apply --management     # ...and compute the board/management indicators for every company

Runs against whatever DATABASE_URL points to (the live Supabase project when .env is loaded). Before writing it
saves every signal it is about to overwrite to scratch/aida_import_backup_<timestamp>.json and stops if that file
can't be written. Idempotent: a second run reports "0 updated". A value entered by hand, or written by another
producer, is never overwritten — it is listed as a conflict.

--management runs company_service.sync_management_composition_signals / sync_succession_signal over every company.
Those are the same computations the company page runs on open, but that page-open trigger is the only place they
ever ran, so 946 of 953 companies had a full board roster and no management indicators. On the live database this
step takes several minutes (about twenty round trips per company).
"""

import argparse
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aida_import import (                                    # noqa: E402
    DATASET_NAME, apply_records, build_records, load_exports, management_signal_keys, management_snapshot, run_management_pass,
)
from database import get_db_session                          # noqa: E402


def _json_default(o):
    return o.isoformat() if isinstance(o, (datetime, date)) else str(o)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write (default is a dry run)")
    ap.add_argument("--management", action="store_true", help="also compute the management/board indicators for every company")
    ap.add_argument("--data-dir", default=str(ROOT.parent / "Data"), help="folder with the original AIDA .xls exports")
    args = ap.parse_args()

    try:
        exports = load_exports(Path(args.data_dir))
    except FileNotFoundError as e:
        print(f"Cannot read the AIDA exports: {e}")
        return 2
    records = build_records(exports)
    db = get_db_session()

    print("APPLY — backing up, then writing." if args.apply else "DRY RUN — nothing is written.")
    print(f"Read {len(records)} companies from the exports.")
    report = apply_records(db, records)
    print(f"  matched to companies in the database : {report['companies']}   (not in the database: {len(report['unmatched'])})")
    print(f"  signals to insert / update / unchanged: {report['inserted']} / {report['updated']} / {report['unchanged']}")
    print(f"  left alone (held by another producer)  : {len(report['conflicts'])}")
    print(f"  companies whose latest accounts are older than Sept 2024: {report['stale_accounts']}")
    print("  populated per indicator:")
    for key, s in sorted(report["by_key"].items()):
        print(f"      {key:28s} present {s['present']:4d}   empty {s['not_yet_checked']:4d}   conflicts {s['conflict']}")

    if not args.apply:
        print("\nNothing written. Re-run with --apply.")
        return 0

    backup_dir = ROOT / "scratch"
    backup_dir.mkdir(exist_ok=True)
    path = backup_dir / f"aida_import_backup_{datetime.now():%Y%m%d_%H%M%S}.json"
    backup = {"created": datetime.now().isoformat(), "dataset": DATASET_NAME, "signals_overwritten": report["backup"],
              "conflicts_left_alone": report["conflicts"], "unmatched": report["unmatched"]}
    if args.management:
        backup["management_signals_before"] = management_snapshot(db)
    path.write_text(json.dumps(backup, default=_json_default), encoding="utf-8")
    if not path.exists() or path.stat().st_size == 0:
        print("Could not write the backup — aborting without changing anything.")
        return 3
    print(f"\nBackup written: {path} ({path.stat().st_size / 1e6:.1f} MB)")

    done = apply_records(db, records, apply=True)
    print(f"Written: {done['inserted']} inserted, {done['updated']} updated, {done['unchanged']} unchanged; "
          f"{done['companies']} raw rows stored as dataset {DATASET_NAME!r}.")

    if args.management:
        started = time.time()
        res = run_management_pass(db, progress=lambda i, n: print(f"  management pass: {i}/{n} companies ({time.time() - started:.0f}s)", flush=True))
        print(f"Management pass over {res['companies']} companies. Written per indicator:")
        for key in management_signal_keys():
            print(f"      {key:28s} {res['written'].get(key, 0):4d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
