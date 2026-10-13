"""
Recomputes the live ATECO cost-peer benchmarks (SectorCostBenchmark) and the
materials_cost_ratio_peer_gap signal derived from them — see ateco_cost_benchmarks.py.

    python scripts/recompute_ateco_benchmarks.py            # DRY RUN: report only
    python scripts/recompute_ateco_benchmarks.py --apply    # write

Idempotent: safe to re-run after every import. Writing the benchmarks and writing the
peer-gap signal are two separate steps so a benchmark-only refresh is still possible later.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ateco_cost_benchmarks import compute_materials_cost_ratio_peer_gap, recompute_ateco_cost_benchmarks  # noqa: E402
from database import get_db_session                                                                       # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write (default is a dry run)")
    args = ap.parse_args()

    db = get_db_session()
    print("APPLY — writing." if args.apply else "DRY RUN — nothing is written.")

    bench = recompute_ateco_cost_benchmarks(db, apply=args.apply)
    print(f"Benchmarks: {bench['groups_computed']} (nace_code, signal) groups "
          f"({bench['groups_too_small_for_percentiles']} with fewer than 5 peers, percentiles left null).")

    gap = compute_materials_cost_ratio_peer_gap(db, apply=args.apply)
    print(f"materials_cost_ratio_peer_gap: {gap['written']} written, {gap['unchanged']} unchanged, "
          f"{gap['conflicts']} left alone, {gap['no_benchmark_for_sector']} skipped (no benchmark for that sector yet).")

    if not args.apply:
        print("\nNothing written. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
