"""
Computes `materials_cost_ratio_trend` for every company already imported by aida_import.py, without
re-running the full 5-export pipeline — the 3-year materials and revenue series it needs are already
sitting in each company's existing RawImportRecord blob (dataset AIDA Raw Exports), so this just reads
that back and derives the one new signal, the same way scripts/import_aida.py derives everything else.

    python scripts/backfill_materials_cost_ratio_trend.py            # DRY RUN: report only
    python scripts/backfill_materials_cost_ratio_trend.py --apply    # write

Idempotent and conflict-safe: reuses aida_import's own _aida_owned rule, so a value entered by hand or
written by another producer is left alone and reported as a conflict, never overwritten.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aida_import import DATASET_NAME, _aida_owned, _differs, materials_cost_ratio_trend  # noqa: E402
from database import get_db_session                                                      # noqa: E402
from models import RawImportRecord, SignalRecord                                         # noqa: E402

SIGNAL_KEY = "materials_cost_ratio_trend"
BASIS = ("percentage points: ('Materie prime e consumo' ÷ revenue) at the latest year "
         "minus the earliest of the last three years")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write (default is a dry run)")
    args = ap.parse_args()

    db = get_db_session()
    raw_rows = db.query(RawImportRecord).filter_by(dataset_name=DATASET_NAME).all()
    print("APPLY — writing." if args.apply else "DRY RUN — nothing is written.")
    print(f"Read {len(raw_rows)} companies' {DATASET_NAME!r} raw rows.")

    existing_sigs = {
        s.company_id: s for s in db.query(SignalRecord).filter_by(signal_key=SIGNAL_KEY)
        .filter(SignalRecord.company_id.in_([r.company_id for r in raw_rows])).all()
    }

    present = conflicts = unchanged = written = no_input = 0
    fetched_at = datetime.utcnow()
    for raw in raw_rows:
        blob = raw.raw_row or {}
        v = materials_cost_ratio_trend(
            blob.get("materials_latest"), blob.get("materials_y-2"),
            blob.get("revenue_latest"), blob.get("revenue_y-2"),
        )
        if v is None:
            no_input += 1
            continue
        present += 1

        sig = existing_sigs.get(raw.company_id)
        if sig and not _aida_owned(sig):
            conflicts += 1
            continue
        if sig and not _differs(sig.numeric_value, v):
            unchanged += 1
            continue

        written += 1
        if not args.apply:
            continue
        if not sig:
            sig = SignalRecord(company_id=raw.company_id, signal_key=SIGNAL_KEY, source=DATASET_NAME)
            db.add(sig)
        sig.numeric_value = round(v, 4)
        sig.text_value = None
        sig.status = "present"
        sig.confidence = 1.0
        sig.is_simulated = False
        sig.source = DATASET_NAME
        sig.fetched_at = fetched_at
        sig.raw_payload_ref = json.dumps({
            "dataset": DATASET_NAME, "signal_key": SIGNAL_KEY, "simulated": False, "basis": BASIS,
            "evidence": {"method": BASIS, "inputs": {
                "materials_latest": blob.get("materials_latest"), "materials_base": blob.get("materials_y-2"),
                "revenue_latest": blob.get("revenue_latest"), "revenue_base": blob.get("revenue_y-2"),
            }, "raw_fields": []},
            "note": "Backfilled from the existing AIDA Raw Exports blob by scripts/backfill_materials_cost_ratio_trend.py "
                    "— the 5-export re-import derives this signal directly from now on.",
        }, default=str)

    if args.apply:
        db.commit()

    print(f"  computable (have both years of materials + revenue): {present}   (no input: {no_input})")
    print(f"  written / unchanged / left alone (conflict)         : {written} / {unchanged} / {conflicts}")
    if not args.apply:
        print("\nNothing written. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
