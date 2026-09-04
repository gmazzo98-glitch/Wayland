"""
Calibration Loop — Section 6 of the Indicator Prompt.

None of indicators.py's weights are validated yet: GG hasn't run enough
pilots for that. This script is the plumbing Section 6 asks be built anyway,
so calibration is possible later without a schema change — not a predictive
model. With zero to single-digit pilot outcomes there is no valid statistical
basis for one; that stays a manual, human-reviewed step until there are
roughly 10-15 scored pilots (at which point a regularized/L1 logistic
regression becomes appropriate, per Section 6 — not before, and not here).

What this does: for every SCORED PilotOutcome (outcome_success is not null),
re-normalizes each snapshotted signal's raw value using the indicator's
CURRENT bounds/invert/curve on IndicatorDefinition (only the raw value and
status are frozen in the snapshot, not the normalization — so a later bounds
edit is reflected the next time this runs), then reports, per variable, the
mean normalized value on the success side vs. the fail side and the gap
between them. Sorted by |gap| descending: the variables that moved most
between the two groups so far, nothing more.

Run directly (`python calibration.py`) or import build_calibration_report()
for a Streamlit page later, if the business side wants one before there's
enough data to make one worthwhile.
"""

import json
from collections import defaultdict
from typing import Any, Dict, List

from database import get_db_session
from models import PilotOutcome
from indicators import fetch_indicator_defs
from scoring import normalize_indicator_value


def _load_scored_outcomes(db) -> List[PilotOutcome]:
    return (
        db.query(PilotOutcome)
        .filter(PilotOutcome.outcome_success.isnot(None))
        .filter(PilotOutcome.signal_snapshot_json.isnot(None))
        .all()
    )


def _normalized_snapshot_values(outcomes: List[PilotOutcome], indicator_defs: Dict[str, Dict[str, Any]]) -> Dict[str, List[float]]:
    """signal_key -> list of normalized (0-100) values across the given outcomes."""
    values_by_key: Dict[str, List[float]] = defaultdict(list)
    for outcome in outcomes:
        snapshot = json.loads(outcome.signal_snapshot_json or "{}")
        for key, entry in snapshot.items():
            defn = indicator_defs.get(key)
            if not defn or defn.get("axis") == "context":
                continue
            status = entry.get("status")
            if status not in ("present", "stale", "absent"):
                continue
            # Mirrors scoring._evaluate_axis: absent contributes 0, never a
            # normalize() call on a None value.
            normalized = 0.0 if status == "absent" else normalize_indicator_value(entry.get("value"), defn)
            values_by_key[key].append(normalized)
    return values_by_key


def build_calibration_report(db, indicator_defs: Dict[str, Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Returns:
      outcomes_used / successes / failures: counts of scored PilotOutcome rows found.
      insufficient_data: True if there isn't at least one success AND one
        failure to compare — expected pre-launch, not an error.
      rows: [{key, label, axis, n_success, n_fail, mean_success, mean_fail, gap}],
        sorted by |gap| descending. Only variables present on BOTH sides are
        included — a variable only ever seen among successes (or only among
        failures) has nothing to contrast against yet.
    """
    indicator_defs = indicator_defs or fetch_indicator_defs(db)
    outcomes = _load_scored_outcomes(db)
    successes = [o for o in outcomes if o.outcome_success is True]
    failures = [o for o in outcomes if o.outcome_success is False]

    result = {
        "outcomes_used": len(outcomes),
        "successes": len(successes),
        "failures": len(failures),
        "insufficient_data": not successes or not failures,
        "rows": [],
    }
    if result["insufficient_data"]:
        return result

    success_values = _normalized_snapshot_values(successes, indicator_defs)
    fail_values = _normalized_snapshot_values(failures, indicator_defs)

    rows = []
    for key in set(success_values) | set(fail_values):
        succ_vals, fail_vals = success_values.get(key, []), fail_values.get(key, [])
        if not succ_vals or not fail_vals:
            continue
        mean_success = sum(succ_vals) / len(succ_vals)
        mean_fail = sum(fail_vals) / len(fail_vals)
        defn = indicator_defs.get(key, {})
        rows.append({
            "key": key,
            "label": defn.get("label", key),
            "axis": defn.get("axis"),
            "n_success": len(succ_vals),
            "n_fail": len(fail_vals),
            "mean_success": round(mean_success, 1),
            "mean_fail": round(mean_fail, 1),
            "gap": round(mean_success - mean_fail, 1),
        })
    rows.sort(key=lambda r: abs(r["gap"]), reverse=True)
    result["rows"] = rows
    return result


def print_calibration_report(report: Dict[str, Any]) -> None:
    print(f"Scored pilot outcomes: {report['outcomes_used']} ({report['successes']} success / {report['failures']} fail)")
    if report["insufficient_data"]:
        print("Not enough data: need at least one successful AND one unsuccessful scored pilot to compare anything.")
        print("Expected pre-launch — Section 6 says don't fit a model until ~10-15 scored pilots exist anyway.")
        return

    print(f"{'Variable':<48} {'Axis':<10} {'Mean(success)':>13} {'Mean(fail)':>11} {'Gap':>8}  n(s)/n(f)")
    for row in report["rows"]:
        print(
            f"{row['label']:<48} {row['axis']:<10} {row['mean_success']:>13.1f} "
            f"{row['mean_fail']:>11.1f} {row['gap']:>+8.1f}  {row['n_success']}/{row['n_fail']}"
        )
    print()
    print(
        f"Directional only — {report['successes']} success / {report['failures']} fail is far below the "
        "~10-15 scored pilots Section 6 sets as the bar for even a regularized (L1/Lasso) model. Read this "
        "as 'which variables moved most between the two groups so far', not as a validated weight change."
    )


if __name__ == "__main__":
    db = get_db_session()
    try:
        print_calibration_report(build_calibration_report(db))
    finally:
        db.close()
