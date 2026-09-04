"""
Unit Tests for Project Vienna: Normalization, the weighted scoring engine,
tri-state status, segment/prime-target rules, and the indicator catalog itself.
"""

import pytest
from datetime import datetime, timedelta
from utils import normalize_handelsregister_nr, is_signal_stale, get_signal_display_status
from scoring import (
    calculate_company_scores, normalize_indicator_value,
    is_prime_target, rank_companies, PRIME_NEED_MIN, PRIME_READINESS_BAND,
)
from indicators import INDICATOR_SEED

def test_handelsregister_normalization():
    assert normalize_handelsregister_nr("HRB 104928") == "HRB-104928"
    assert normalize_handelsregister_nr("HRB-104928") == "HRB-104928"
    assert normalize_handelsregister_nr("Amtsgericht München HRB 883012") == "HRB-883012"
    assert normalize_handelsregister_nr("HRA 310459") == "HRA-310459"

def test_signal_freshness():
    now = datetime.utcnow()
    fresh_date = now - timedelta(days=5)
    stale_date = now - timedelta(days=20)  # freshness window of 14 days in this test

    assert is_signal_stale(14, fresh_date) is False
    assert is_signal_stale(14, stale_date) is True

    assert get_signal_display_status("present", 14, fresh_date) == "present"
    assert get_signal_display_status("present", 14, stale_date) == "stale"
    assert get_signal_display_status("absent", 14, stale_date) == "absent"
    assert get_signal_display_status("not_yet_checked", 14, now) == "not_yet_checked"

def test_normalize_indicator_value_linear():
    defn = {"raw_min": 0, "raw_max": 20, "curve_type": "linear", "invert": False}
    assert normalize_indicator_value(0, defn) == 0.0
    assert normalize_indicator_value(20, defn) == 100.0
    assert normalize_indicator_value(10, defn) == 50.0
    assert normalize_indicator_value(None, defn) == 0.0
    # out-of-range values clamp rather than extrapolate
    assert normalize_indicator_value(-5, defn) == 0.0
    assert normalize_indicator_value(25, defn) == 100.0

def test_normalize_indicator_value_inverted():
    # e.g. interest coverage ratio: a LOW raw value should score HIGH (more need).
    defn = {"raw_min": 0, "raw_max": 15, "curve_type": "linear", "invert": True}
    assert normalize_indicator_value(0, defn) == 100.0
    assert normalize_indicator_value(15, defn) == 0.0
    assert normalize_indicator_value(7.5, defn) == 50.0

def test_normalize_indicator_value_band_curve():
    # e.g. Total Assets: a sweet spot, not monotonic — too little AND too much both score low.
    defn = {"raw_min": 10, "raw_max": 20, "curve_type": "band", "invert": False}
    assert normalize_indicator_value(15, defn) == 100.0   # inside the band
    assert normalize_indicator_value(10, defn) == 100.0   # at the edge
    assert normalize_indicator_value(20, defn) == 100.0
    assert normalize_indicator_value(5, defn) == 50.0      # half a band-width below
    assert normalize_indicator_value(0, defn) == 0.0       # a full band-width below
    assert normalize_indicator_value(25, defn) == 50.0     # half a band-width above
    assert normalize_indicator_value(40, defn) == 0.0      # clamped, not negative

def test_normalize_indicator_value_missing_bounds_falls_back_to_raw():
    defn = {"raw_min": None, "raw_max": None}
    assert normalize_indicator_value(42.0, defn) == 42.0
    assert normalize_indicator_value(150.0, defn) == 100.0  # still clamped

def _fake_indicator_defs():
    return {
        "need_a": {"axis": "need", "weight": 3.0, "invert": False, "raw_min": 0, "raw_max": 100,
                   "curve_type": "linear", "is_gate": False, "gate_penalty_multiplier": 1.0, "freshness_days": 365},
        "need_b": {"axis": "need", "weight": 2.0, "invert": False, "raw_min": 0, "raw_max": 10,
                   "curve_type": "linear", "is_gate": False, "gate_penalty_multiplier": 1.0, "freshness_days": 365},
        "ready_a": {"axis": "readiness", "weight": 2.0, "invert": False, "raw_min": 0, "raw_max": 10,
                    "curve_type": "linear", "is_gate": False, "gate_penalty_multiplier": 1.0, "freshness_days": 365},
        "ready_b": {"axis": "readiness", "weight": 2.0, "invert": False, "raw_min": 1, "raw_max": 5,
                    "curve_type": "linear", "is_gate": False, "gate_penalty_multiplier": 1.0, "freshness_days": 365},
        "both_a": {"axis": "both", "weight": 1.0, "invert": False, "raw_min": 0, "raw_max": 1,
                   "curve_type": "linear", "is_gate": False, "gate_penalty_multiplier": 1.0, "freshness_days": 365},
        "ctx_a": {"axis": "context", "weight": 5.0, "invert": False, "raw_min": 0, "raw_max": 100,
                  "curve_type": "linear", "is_gate": False, "gate_penalty_multiplier": 1.0, "freshness_days": 365},
    }

def test_scoring_engine_weighted_axes():
    defs = _fake_indicator_defs()
    now = datetime.utcnow()
    signals = [
        {"signal_key": "need_a", "status": "present", "numeric_value": 60.0, "fetched_at": now},   # -> 60.0, w3
        # need_b intentionally absent from the list -> not_yet_checked, excluded
        {"signal_key": "ready_a", "status": "present", "numeric_value": 8.0, "fetched_at": now},    # -> 80.0, w2
        {"signal_key": "ready_b", "status": "absent", "numeric_value": None, "fetched_at": now},    # checked, 0 contribution, w2
        {"signal_key": "both_a", "status": "present", "numeric_value": 1.0, "fetched_at": now},     # -> 100.0, w1, counts in BOTH axes
        {"signal_key": "ctx_a", "status": "present", "numeric_value": 999.0, "fetched_at": now},    # must be fully excluded
    ]

    scores = calculate_company_scores(signals, defs)

    # need: (60*3 + 100*1) / (3+1) = 280/4 = 70.0
    assert scores["need_score"] == 70.0
    # readiness: (80*2 + 0*2 + 100*1) / (2+2+1) = 260/5 = 52.0
    assert scores["readiness_score"] == 52.0

    # need weighted completeness: checked weight 4 of total weight 6 (need_a+need_b+both_a)
    assert scores["need_completeness_pct"] == round(4 / 6 * 100, 1)
    # readiness weighted completeness: checked weight 5 of total weight 5 (all readiness+both checked)
    assert scores["readiness_completeness_pct"] == 100.0

    assert scores["signals_checked"] == 5  # need_a, ready_a, ready_b, both_a counted once per axis it's in...
    # both_a is counted once in need_checked and once in readiness_checked (2 total), plus need_a, ready_a, ready_b
    assert scores["signals_total"] == 6  # need(3) + readiness(3), both_a counted in each

def test_scoring_engine_gate_penalty_applies_when_unfavorable():
    defs = {
        "gate_a": {"axis": "readiness", "weight": 3.0, "invert": False, "raw_min": 0, "raw_max": 1,
                   "curve_type": "linear", "is_gate": True, "gate_penalty_multiplier": 0.5, "freshness_days": 365},
        "ready_x": {"axis": "readiness", "weight": 1.0, "invert": False, "raw_min": 0, "raw_max": 100,
                    "curve_type": "linear", "is_gate": False, "gate_penalty_multiplier": 1.0, "freshness_days": 365},
    }
    now = datetime.utcnow()
    signals = [
        {"signal_key": "gate_a", "status": "absent", "numeric_value": None, "fetched_at": now},   # confirmed unfavorable
        {"signal_key": "ready_x", "status": "present", "numeric_value": 100.0, "fetched_at": now},
    ]
    scores = calculate_company_scores(signals, defs)
    # pre-gate: (0*3 + 100*1) / (3+1) = 25.0, then halved by the gate penalty -> 12.5
    assert scores["readiness_score"] == 12.5

def test_redundancy_dampening_applies_within_group():
    # Section 2.5: same-group variables must not each count at full weight.
    # Dampening schedule confirmed with the business side: full weight to the
    # highest-weighted member of the group, half to the next, a quarter to
    # the next, ranked by each variable's *own* configured weight.
    defs = {
        "cost_a": {"axis": "need", "weight": 4.0, "redundancy_group": "COST", "invert": False,
                   "raw_min": 0, "raw_max": 100, "curve_type": "linear", "is_gate": False,
                   "gate_penalty_multiplier": 1.0, "freshness_days": 365},
        "cost_b": {"axis": "need", "weight": 2.0, "redundancy_group": "COST", "invert": False,
                   "raw_min": 0, "raw_max": 100, "curve_type": "linear", "is_gate": False,
                   "gate_penalty_multiplier": 1.0, "freshness_days": 365},
        "cost_c": {"axis": "need", "weight": 3.0, "redundancy_group": "COST", "invert": False,
                   "raw_min": 0, "raw_max": 100, "curve_type": "linear", "is_gate": False,
                   "gate_penalty_multiplier": 1.0, "freshness_days": 365},
        "standalone": {"axis": "need", "weight": 5.0, "redundancy_group": None, "invert": False,
                       "raw_min": 0, "raw_max": 100, "curve_type": "linear", "is_gate": False,
                       "gate_penalty_multiplier": 1.0, "freshness_days": 365},
    }
    now = datetime.utcnow()
    signals = [
        {"signal_key": "cost_a", "status": "present", "numeric_value": 80.0, "fetched_at": now},   # rank 0 (w=4, highest) -> eff 4.0
        {"signal_key": "cost_b", "status": "present", "numeric_value": 60.0, "fetched_at": now},   # rank 2 (w=2, lowest)  -> eff 0.5
        {"signal_key": "cost_c", "status": "present", "numeric_value": 40.0, "fetched_at": now},   # rank 1 (w=3, middle)  -> eff 1.5
        {"signal_key": "standalone", "status": "present", "numeric_value": 50.0, "fetched_at": now},  # ungrouped -> full 5.0
    ]
    scores = calculate_company_scores(signals, defs)
    # weighted_sum = 80*4.0 + 40*1.5 + 60*0.5 + 50*5.0 = 320 + 60 + 30 + 250 = 660
    # weight_checked = weight_total = 4.0 + 1.5 + 0.5 + 5.0 = 11.0
    expected = round(660.0 / 11.0, 1)
    assert scores["need_score"] == expected
    assert scores["need_completeness_pct"] == 100.0


def test_redundancy_dampening_reranks_when_weights_change():
    # The dampening rank is derived from each variable's *current* weight on
    # every call, not a stored rank — editing weight on the Indicator Weights
    # page must re-rank the group automatically.
    defs = {
        "cost_a": {"axis": "need", "weight": 1.0, "redundancy_group": "COST", "invert": False,  # now the LOWEST weight
                   "raw_min": 0, "raw_max": 100, "curve_type": "linear", "is_gate": False,
                   "gate_penalty_multiplier": 1.0, "freshness_days": 365},
        "cost_b": {"axis": "need", "weight": 5.0, "redundancy_group": "COST", "invert": False,  # now the HIGHEST weight
                   "raw_min": 0, "raw_max": 100, "curve_type": "linear", "is_gate": False,
                   "gate_penalty_multiplier": 1.0, "freshness_days": 365},
    }
    now = datetime.utcnow()
    signals = [
        {"signal_key": "cost_a", "status": "present", "numeric_value": 100.0, "fetched_at": now},  # now rank 1 -> eff 0.5
        {"signal_key": "cost_b", "status": "present", "numeric_value": 0.0, "fetched_at": now},     # now rank 0 -> eff 5.0
    ]
    scores = calculate_company_scores(signals, defs)
    # weighted_sum = 100*0.5 + 0*5.0 = 50; weight_total = 5.5
    assert scores["need_score"] == round(50.0 / 5.5, 1)


def test_is_prime_target_matches_moderate_to_high_band():
    # Section 4 of the Brief: high-need + MODERATE-TO-HIGH readiness, not a
    # single high+high corner.
    assert is_prime_target(need_score=70.0, readiness_score=60.0) is True
    assert is_prime_target(need_score=PRIME_NEED_MIN, readiness_score=PRIME_READINESS_BAND[0]) is True
    assert is_prime_target(need_score=PRIME_NEED_MIN, readiness_score=PRIME_READINESS_BAND[1]) is True

    # Need too low disqualifies even with ideal readiness.
    assert is_prime_target(need_score=30.0, readiness_score=60.0) is False
    # Readiness below the band (low readiness) disqualifies.
    assert is_prime_target(need_score=80.0, readiness_score=20.0) is False
    # Readiness ABOVE the band (very high, already innovating heavily) also
    # disqualifies — this is the exact case the old top-right-corner framing got wrong.
    assert is_prime_target(need_score=90.0, readiness_score=95.0) is False

def test_rank_companies_sorts_desc_by_need_then_readiness():
    companies = [
        {"legal_name": "Low", "need_score": 20.0, "readiness_score": 90.0},
        {"legal_name": "HighNeedLowReady", "need_score": 80.0, "readiness_score": 10.0},
        {"legal_name": "HighNeedHighReady", "need_score": 80.0, "readiness_score": 70.0},
    ]
    ranked = rank_companies(companies)
    assert [c["legal_name"] for c in ranked] == ["HighNeedHighReady", "HighNeedLowReady", "Low"]


# --------------------------------------------------------------------------
# Catalog integrity — the 75-row hand-authored indicator seed is easy to get
# subtly wrong (duplicate key, inverted bounds, an out-of-range weight); these
# guard the structural invariants scoring.py assumes, not the specific values.
# --------------------------------------------------------------------------

def test_indicator_catalog_has_no_duplicate_keys():
    keys = [row["key"] for row in INDICATOR_SEED]
    assert len(keys) == len(set(keys))

def test_indicator_catalog_axes_are_valid():
    for row in INDICATOR_SEED:
        assert row["axis"] in ("need", "readiness", "both", "context")

def test_indicator_catalog_weights_in_range():
    for row in INDICATOR_SEED:
        assert 0.0 <= row["weight"] <= 5.0, row["key"]

def test_indicator_catalog_scored_rows_have_valid_bounds():
    for row in INDICATOR_SEED:
        if row["axis"] == "context":
            continue
        assert row.get("raw_min") is not None and row.get("raw_max") is not None, row["key"]
        assert row["raw_min"] != row["raw_max"], row["key"]

def test_indicator_catalog_context_rows_carry_zero_weight():
    for row in INDICATOR_SEED:
        if row["axis"] == "context":
            assert row["weight"] == 0.0, row["key"]

def test_indicator_catalog_gate_rows_have_valid_penalty():
    for row in INDICATOR_SEED:
        if row.get("is_gate"):
            assert 0.0 <= row.get("gate_penalty_multiplier", 1.0) <= 1.0, row["key"]

def test_indicator_catalog_row_count_matches_reconciled_total():
    # 75 pre-existing rows + 9 new rows added when reconciling against the
    # 78-variable gg_indicators.json ground truth (69 of which already had a
    # match here; see indicators.py's module docstring).
    assert len(INDICATOR_SEED) == 84

def test_indicator_catalog_automation_tier_is_valid():
    for row in INDICATOR_SEED:
        tier = row.get("automation_tier")
        assert tier in ("T1", "T2", "T3"), row["key"]

def test_indicator_catalog_context_rows_carry_zero_weight_including_new_rows():
    # Same invariant as test_indicator_catalog_context_rows_carry_zero_weight,
    # re-asserted after reconciliation added two more context rows (Vendor
    # Contract Renewal Timing, Sector Pilot Precedent) that the source
    # spreadsheet itself lists at a nonzero weight despite their own
    # indicator_logic arguing they're sequencing inputs, not scored signals.
    context_keys = {row["key"] for row in INDICATOR_SEED if row["axis"] == "context"}
    assert {"vendor_contract_renewal_timing", "sector_pilot_precedent"} <= context_keys
    for row in INDICATOR_SEED:
        if row["axis"] == "context":
            assert row["weight"] == 0.0, row["key"]
