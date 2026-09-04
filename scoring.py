"""
Two-Axis Scoring Engine (Need vs. Readiness), weighted by IndicatorDefinition.

Every signal's normalization bounds, axis, invert direction, and weight come
from the IndicatorDefinition table (indicators.py / models.py) — nothing here
is hardcoded per signal_key, so the whole catalog is editable from the
Indicator Weights page without touching this file. See Section 4 of
GG_Dashboard_Technical_Brief.docx for the underlying two-axis philosophy.

Need Axis: Signals indicating pressure or capacity gap (could innovate).
Readiness Axis: Signals indicating active R&D/IP activity, governance, and
organizational capacity to actually run a pilot (is innovating / could absorb one).
'both'-axis signals (e.g. a recent generational handover) count in each axis's
weighted sum independently. 'context'-axis signals are never scored — they're
informational tags/moderators, excluded from both sums.
"""

from typing import Dict, List, Any
from utils import get_signal_display_status


def normalize_indicator_value(value: float, defn: Dict[str, Any]) -> float:
    """
    Maps a raw signal value onto a 0.0-100.0 score using the indicator's own
    raw_min/raw_max bounds.
      - curve_type 'linear' (default): raw_min->0, raw_max->100, then flipped
        if invert=True (a LOW raw value should mean a HIGH score).
      - curve_type 'band': raw_min..raw_max is a sweet spot scoring 100
        anywhere inside it, tapering linearly to 0 one band-width outside
        either edge (e.g. Total Assets — too little or too much both score low).
    Falls back to treating the raw value as already 0-100 if no bounds are set.
    """
    if value is None:
        return 0.0

    raw_min, raw_max = defn.get("raw_min"), defn.get("raw_max")
    if raw_min is None or raw_max is None or raw_min == raw_max:
        return min(100.0, max(0.0, float(value)))

    if defn.get("curve_type") == "band":
        width = raw_max - raw_min
        if width <= 0:
            return 0.0
        if raw_min <= value <= raw_max:
            return 100.0
        if value < raw_min:
            return max(0.0, 100.0 * (1 - (raw_min - value) / width))
        return max(0.0, 100.0 * (1 - (value - raw_max) / width))

    base = (value - raw_min) / (raw_max - raw_min)
    base = min(1.0, max(0.0, base))
    score = base * 100.0
    if defn.get("invert"):
        score = 100.0 - score
    return score


def _apply_redundancy_dampening(keys: List[str], indicator_defs: Dict[str, Dict[str, Any]]) -> Dict[str, float]:
    """
    Redundancy groups (e.g. Materials/Labour/Logistics/Energy/COGS all under
    COST_PRESSURE) measure overlapping underlying constructs — summing every
    member at full weight double-counts one signal. Confirmed with the
    business side: a dampening schedule, not a composite average/max (both
    also allowed by the spec, but this is simplest to reason about and keeps
    every variable's own normalized value visible rather than collapsing the
    group into one representative number).

    Within each group, sort members by their own configured weight
    (descending); the highest-weighted member counts at its full weight, the
    next at half, the next at a quarter, and so on. Re-derived on every call
    from each variable's *current* weight, so re-ranking a group by editing
    weights on the Indicator Weights page (which the business side flagged as
    something they'll keep tuning) changes the dampening automatically —
    there's no separately-stored dampening percentage to fall out of sync.

    `keys` is expected to already be filtered to one axis (as _evaluate_axis
    does) — a group that has members on both axes (e.g. MGMT_PROFILE has a
    NEED row and two READINESS rows) is therefore naturally dampened as two
    smaller, separate groups, one per axis, never mixed across axes.

    Ungrouped variables (redundancy_group is None/blank) are always at their
    full configured weight — grouping of exactly one member is a no-op by
    construction (rank 0 -> multiplier 1.0), but they're kept out of the
    ranking entirely for clarity.
    """
    groups: Dict[Any, List[str]] = {}
    for k in keys:
        rg = indicator_defs[k].get("redundancy_group") or None
        groups.setdefault(rg, []).append(k)

    effective_weight: Dict[str, float] = {}
    for rg, group_keys in groups.items():
        if rg is None:
            for k in group_keys:
                effective_weight[k] = indicator_defs[k].get("weight", 0.0)
            continue
        ordered = sorted(group_keys, key=lambda k: (-indicator_defs[k].get("weight", 0.0), k))
        for rank, k in enumerate(ordered):
            effective_weight[k] = indicator_defs[k].get("weight", 0.0) * (0.5 ** rank)
    return effective_weight


def _evaluate_axis(signal_map: Dict[str, Any], indicator_defs: Dict[str, Dict[str, Any]], axis_name: str):
    keys = [k for k, d in indicator_defs.items() if d["axis"] in (axis_name, "both")]
    eff_weight = _apply_redundancy_dampening(keys, indicator_defs)

    weighted_sum = 0.0
    weight_checked = 0.0
    weight_total = sum(eff_weight.values()) or 0.0
    checked_count = 0
    gate_multiplier = 1.0

    for key in keys:
        defn = indicator_defs[key]
        weight = eff_weight[key]
        data = signal_map.get(key, {"status": "not_yet_checked", "value": None})
        status = data["status"]

        if status in ("present", "stale"):
            checked_count += 1
            weight_checked += weight
            normalized = normalize_indicator_value(data["value"], defn)
            weighted_sum += normalized * weight
            if defn.get("is_gate") and normalized < 50.0:
                gate_multiplier *= defn.get("gate_penalty_multiplier", 1.0)
        elif status == "absent":
            checked_count += 1
            weight_checked += weight
            if defn.get("is_gate"):
                gate_multiplier *= defn.get("gate_penalty_multiplier", 1.0)
            # absent contributes 0 to weighted_sum, same as before

    axis_score = (weighted_sum / weight_checked) if weight_checked > 0 else 0.0
    axis_score = min(100.0, axis_score * gate_multiplier)
    weighted_completeness_pct = (weight_checked / weight_total * 100.0) if weight_total > 0 else 0.0
    count_completeness_pct = (checked_count / len(keys) * 100.0) if keys else 0.0

    return axis_score, weighted_completeness_pct, count_completeness_pct, checked_count, len(keys)


def calculate_company_scores(company_signals: List[Any], indicator_defs: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """
    Computes weighted Need/Readiness scores and completeness for a company.

    company_signals: SignalRecord objects or dicts for one company.
    indicator_defs: {signal_key: definition_dict} — fetch once per page render
        via indicators.fetch_indicator_defs(db), not per company, to avoid
        re-querying the (small, ~75-row) catalog on every loop iteration.
    """
    signal_map = {}
    for sig in company_signals:
        key = getattr(sig, "signal_key", None) or sig.get("signal_key")
        if key not in indicator_defs:
            continue  # signal exists in DB but its definition was deactivated/removed
        status = getattr(sig, "status", None) or sig.get("status")
        val = getattr(sig, "numeric_value", None) if hasattr(sig, "numeric_value") else sig.get("numeric_value")
        fetched_at = getattr(sig, "fetched_at", None) if hasattr(sig, "fetched_at") else sig.get("fetched_at")

        display_status = get_signal_display_status(status, indicator_defs[key].get("freshness_days"), fetched_at)
        signal_map[key] = {"status": display_status, "raw_status": status, "value": val}

    need_score, need_wcomp, need_ccomp, need_checked, need_total = _evaluate_axis(signal_map, indicator_defs, "need")
    readiness_score, ready_wcomp, ready_ccomp, ready_checked, ready_total = _evaluate_axis(signal_map, indicator_defs, "readiness")

    total_checked = need_checked + ready_checked
    total_signals = need_total + ready_total
    total_comp_pct = (total_checked / total_signals * 100.0) if total_signals > 0 else 0.0

    return {
        "need_score": round(need_score, 1),
        "readiness_score": round(readiness_score, 1),
        "need_completeness_pct": round(need_wcomp, 1),
        "readiness_completeness_pct": round(ready_wcomp, 1),
        "need_completeness_count_pct": round(need_ccomp, 1),
        "readiness_completeness_count_pct": round(ready_ccomp, 1),
        "total_completeness_pct": round(total_comp_pct, 1),
        "signals_checked": total_checked,
        "signals_total": total_signals,
    }


# Ideal band per Section 4 of the Technical Brief: "the ideal targets are
# high-need + moderate-to-high-readiness", explicitly called out as distinct
# from a single high+high composite corner (very-high readiness alongside
# very-high need tends to mean "already innovating heavily on its own" — a
# weaker fit for an intermediary pairing in unproven startups).
PRIME_NEED_MIN = 50.0
PRIME_READINESS_BAND = (40.0, 85.0)


def is_prime_target(need_score: float, readiness_score: float) -> bool:
    return need_score >= PRIME_NEED_MIN and PRIME_READINESS_BAND[0] <= readiness_score <= PRIME_READINESS_BAND[1]


def rank_companies(companies_scored: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Sort by need_score desc, then readiness_score desc. Callers must pass one
    segment (Midcap or SME) at a time — Section 5 of the Brief: "Score Midcap
    and SME as distinct segments... don't let the dashboard silently pool them."
    """
    return sorted(companies_scored, key=lambda c: (c["need_score"], c["readiness_score"]), reverse=True)
