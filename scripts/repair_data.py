"""
Repairs the data problems found in the 2026-09-22 audit (see data_repairs.py for what each one is).

    python scripts/repair_data.py                       # DRY RUN: report only, nothing is written
    python scripts/repair_data.py --apply               # backup first, then apply

Runs against whatever DATABASE_URL points to — with the live Supabase project configured, --apply
changes live data. Before it writes anything it saves a backup (every old value it is about to
overwrite, plus the affected catalog rows) to scratch/repair_backup_<timestamp>.json, and it stops
if that file can't be written. Every step is idempotent: run it again and it reports "nothing to do".

--data-dir is where the ORIGINAL AIDA exports live (WAYLAND_FINANCIAL_PL*.xls and
WAYLAND_FINANCIAL_CAPACITY*.xls); the curated Main file's shifted `cogs` columns and swapped
`leverage_ratio` history are re-read from them. Default: ../Data next to this repo.
"""

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from database import get_db_session                    # noqa: E402
from data_repairs import (                             # noqa: E402
    apply_blob_corrections, load_aida_corrections, recompute_derived_signals, reset_founding_year_product_age,
)
from indicators import CATALOG_MIGRATIONS, apply_catalog_migrations   # noqa: E402
from models import IndicatorDefinition, PainPointDefinition           # noqa: E402
from painpoints import PAIN_POINT_MIGRATIONS, apply_pain_point_migrations  # noqa: E402


def _json_default(o):
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    return str(o)


def _summ(name: str, changes: list, extra: str = "") -> None:
    print(f"  {name:<44s} {len(changes):5d} change(s) {extra}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write the changes (default is a dry run)")
    ap.add_argument("--data-dir", default=str(ROOT.parent / "Data"), help="folder with the original AIDA .xls exports")
    args = ap.parse_args()

    # Deliberately NOT init_db(): that applies the catalog migrations itself, which would make the dry run
    # write. The tables already exist; this script applies each step explicitly, after the backup.
    db = get_db_session()

    try:
        corrections = load_aida_corrections(Path(args.data_dir))
    except (FileNotFoundError, StopIteration) as e:
        print(f"Cannot read the original AIDA exports from {args.data_dir}: {e}")
        return 2

    print("DRY RUN — reporting only." if not args.apply else "APPLY — backing up, then writing.")
    reports = {
        "derived_signals": recompute_derived_signals(db),
        "aida_blob_corrections": apply_blob_corrections(db, corrections),
        "product_age_founding_year": reset_founding_year_product_age(db),
        "catalog_migrations": apply_catalog_migrations(db, dry_run=True),
        "pain_point_migrations": apply_pain_point_migrations(db, dry_run=True),
    }
    d, b, p = reports["derived_signals"], reports["aida_blob_corrections"], reports["product_age_founding_year"]
    _summ("margin_compression / ebitda_trend recomputed", d["changes"], f"(of {d['checked']} checked, {d['unchanged']} already right)")
    _summ("AIDA raw-row columns realigned (cogs, leverage)", b["changes"], f"(of {b['checked']} rows, {b['unchanged']} already right)")
    _summ("  -> `cogs` signal refreshed", b["signal_changes"])
    _summ("product_age reset (was a founding year)", p["changes"])
    _summ("indicator catalog fields corrected", reports["catalog_migrations"]["applied"],
          f"({len(reports['catalog_migrations']['skipped'])} left alone: edited by hand)")
    _summ("pain-point rules corrected", reports["pain_point_migrations"]["applied"],
          f"({len(reports['pain_point_migrations']['skipped'])} left alone: edited by hand)")

    if not args.apply:
        print("\nNothing written. Re-run with --apply to backup and apply.")
        return 0

    # Backup FIRST: every old value about to be overwritten, plus the catalog rows involved.
    backup_dir = ROOT / "scratch"
    backup_dir.mkdir(exist_ok=True)
    backup_path = backup_dir / f"repair_backup_{datetime.now():%Y%m%d_%H%M%S}.json"
    catalog_keys = {m["key"] for m in CATALOG_MIGRATIONS}
    pain_keys = {m["key"] for m in PAIN_POINT_MIGRATIONS}
    backup = {
        "created": datetime.now().isoformat(),
        "note": "The guarded catalog/pain-point migrations also run inside init_db(), so a running app applies them on its "
                "own the moment new code is saved — before this backup. Their PREVIOUS values are recorded below in "
                "catalog_migrations / pain_point_migrations (the `old` side of each change), and `reports` holds every old "
                "signal and raw-row value this run overwrites.",
        "catalog_migrations": CATALOG_MIGRATIONS, "pain_point_migrations": PAIN_POINT_MIGRATIONS,
        "reports": reports,
        "indicator_definitions": [r.to_dict() for r in db.query(IndicatorDefinition).filter(IndicatorDefinition.key.in_(catalog_keys)).all()],
        "pain_point_definitions": [r.to_dict() for r in db.query(PainPointDefinition).filter(PainPointDefinition.key.in_(pain_keys)).all()],
    }
    backup_path.write_text(json.dumps(backup, default=_json_default, indent=1), encoding="utf-8")
    if not backup_path.exists() or backup_path.stat().st_size == 0:
        print("Could not write the backup — aborting without changing anything.")
        return 3
    print(f"\nBackup written: {backup_path} ({backup_path.stat().st_size / 1e6:.1f} MB)")

    done = {
        "catalog": apply_catalog_migrations(db),
        "derived": recompute_derived_signals(db, apply=True),
        "blob": apply_blob_corrections(db, corrections, apply=True),
        "product_age": reset_founding_year_product_age(db, apply=True),
        "pain_points": apply_pain_point_migrations(db),
    }
    print("Applied:",
          f"{len(done['catalog']['applied'])} catalog fields,",
          f"{len(done['derived']['changes'])} derived signals,",
          f"{len(done['blob']['changes'])} raw-row columns,",
          f"{len(done['product_age']['changes'])} product_age resets,",
          f"{len(done['pain_points']['applied'])} pain-point rules.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
