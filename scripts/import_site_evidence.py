"""
Imports the results folder of Scraper/tools/site_evidence (the local-model reader of companies' own
websites) into Vienna as signals. See scrapers/site_evidence_reader.py for what is written and why.

    python scripts/import_site_evidence.py --dir C:\\Users\\<you>\\SiteEvidenceOutput           # DRY RUN
    python scripts/import_site_evidence.py --dir C:\\Users\\<you>\\SiteEvidenceOutput --apply   # write

Idempotent: re-running after an import reports 0 new. Never creates a company, never overwrites a real
'present' or 'absent' signal held by another source (those are listed as conflicts for a human), and a
company with nothing found writes nothing (absence on one's own site is not evidence of absence).
Every row it replaces is saved first in scratch/site_evidence_import_<timestamp>.json.
"""

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from database import get_db_session                                                      # noqa: E402
from scrapers.site_evidence_reader import apply_plan, build_plan, read_results           # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", required=True, help="the site_evidence output folder (results.jsonl, state.json)")
    ap.add_argument("--apply", action="store_true", help="write (default is a dry run)")
    args = ap.parse_args()

    hits_by_company, state = read_results(Path(args.dir))
    statuses = Counter(rec.get("status") for rec in state.values())
    print("APPLY - writing." if args.apply else "DRY RUN - nothing is written.")
    print(f"Folder: {args.dir}")
    print(f"Companies processed by the tool: {len(state)}   by status: {dict(statuses)}")
    print(f"Companies with at least one confirmed quote: {len(hits_by_company)}")

    db = get_db_session()
    plan = build_plan(db, hits_by_company)
    by_key = Counter(w["signal_key"] for w in plan["writes"])
    print(f"\nSignals to write: {len(plan['writes'])}   by indicator: {dict(by_key)}")
    for w in plan["writes"][:40]:
        old = w["replaces"]
        note = "" if old is None else f"  (replaces {old['status']}/{'simulated' if old['is_simulated'] else 'real'} from {old['source']})"
        print(f"  + {w['legal_name'][:38]:38} {w['signal_key']:30} {w['signal']['value']:g}  conf {w['signal']['confidence']}{note}")
        print(f"      {w['signal']['summary'][:200]}")
    if plan["conflicts"]:
        print(f"\nLeft alone ({len(plan['conflicts'])}): another producer already holds the row")
        for c in plan["conflicts"][:20]:
            print(f"  = {c['legal_name'][:38]:38} {c['signal_key']:30} {c['reason']}")
    if plan["unmatched"]:
        print(f"\nNot matched to a company in the database ({len(plan['unmatched'])}): {plan['unmatched'][:10]}")

    if not args.apply:
        print("\nNothing written. Re-run with --apply.")
        return 0

    backup = [{"company_id": w["company_id"], "signal_key": w["signal_key"], "replaced": w["replaces"]}
              for w in plan["writes"] if w["replaces"]]
    backup_dir = ROOT / "scratch"
    backup_dir.mkdir(exist_ok=True)
    path = backup_dir / f"site_evidence_import_{datetime.now():%Y%m%d_%H%M%S}.json"
    path.write_text(json.dumps({"created": datetime.now().isoformat(), "folder": args.dir, "replaced": backup,
                                "conflicts": [{k: c[k] for k in ("registration_number", "signal_key", "reason")}
                                              for c in plan["conflicts"]]}, default=str), encoding="utf-8")
    done = apply_plan(db, plan, hits_by_company, state)
    print(f"\nWrote {done['signals_written']} signals for {done['companies']} companies. Backup/report: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
