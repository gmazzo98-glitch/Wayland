"""
Imports the "Risultati (2)" cost-breakdown sheet — see aida_cost_breakdown_import.py for what it
reads and why a separate module, not a patch to aida_import.py, was the right fit.

    python scripts/import_cost_breakdown.py --file "path/to/export.xlsx"           # DRY RUN
    python scripts/import_cost_breakdown.py --file "path/to/export.xlsx" --apply   # write

Matches rows to existing companies by registration_number (VAT) only — never creates a company,
never guesses a match. Idempotent: re-running reports "0 updated" once applied.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aida_cost_breakdown_import import DATASET_NAME, apply_records, build_records, read_cost_breakdown_sheet  # noqa: E402
from database import get_db_session                                                                           # noqa: E402

SPOT_CHECK_VAT = {"IT13338030151": "THYSSENKRUPP NUCERA ITALY SRL", "IT07567120014": "DURR SYSTEMS ITALY S.P.A.",
                   "IT03024640272": "CESARO MAC. IMPORT S.R.L."}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", required=True, help="path to the workbook containing the 'Risultati (2)' sheet")
    ap.add_argument("--apply", action="store_true", help="write (default is a dry run)")
    args = ap.parse_args()

    df = read_cost_breakdown_sheet(args.file)
    db = get_db_session()
    records, unmatched = build_records(db, df)

    print("APPLY — writing." if args.apply else "DRY RUN — nothing is written.")
    print(f"Read {len(df)} rows from \"Risultati (2)\".")
    print(f"  matched to companies in the database: {len(records)}   (unmatched: {len(unmatched)})")
    if unmatched:
        print(f"  first few unmatched registration numbers: {unmatched[:10]}")

    with_service_costs = sum(1 for r in records.values() if r["signals"])
    print(f"  rows with enough data for service_costs (services + revenue both present): {with_service_costs}")

    print("\nSpot check:")
    for vat, name in SPOT_CHECK_VAT.items():
        rec = records.get(vat)
        if rec is None:
            print(f"  {vat} ({name}): not matched")
            continue
        sc = rec["signals"].get("service_costs")
        print(f"  {vat} ({name}): service_costs = {sc['value'] if sc else 'n/a'}%")

    report = apply_records(db, records, apply=args.apply)
    print(f"\nservice_costs signal: {report['inserted']} inserted, {report['updated']} updated, "
          f"{report['unchanged']} unchanged, {len(report['conflicts'])} left alone (held by another producer).")
    for c in report["conflicts"][:10]:
        print(f"    conflict: {c['legal_name']} already has service_costs from {c['held_by']!r}")

    if not args.apply:
        print("\nNothing written. Re-run with --apply.")
    else:
        backup_dir = ROOT / "scratch"
        backup_dir.mkdir(exist_ok=True)
        path = backup_dir / f"cost_breakdown_import_{datetime.now():%Y%m%d_%H%M%S}.json"
        path.write_text(json.dumps({"created": datetime.now().isoformat(), "dataset": DATASET_NAME,
                                    "unmatched": unmatched, "conflicts": report["conflicts"]}, default=str), encoding="utf-8")
        print(f"Report written: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
