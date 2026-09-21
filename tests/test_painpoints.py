"""
Tests for pain-point detection (painpoints.py): the evidence rules, the seed catalog's integrity, and
that an edited threshold survives a re-seed.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import painpoints as P
from indicators import INDICATOR_SEED
from models import Base, PainPointDefinition
from scoring import build_signal_map

INDICATOR_DEFS = {r["key"]: dict(r) for r in INDICATOR_SEED}
NOW = datetime.utcnow()


def sig(key, value, status="present", simulated=False, fetched=None, source="Test Source"):
    """One SignalRecord-shaped dict. is_simulated defaults to False (a real observation)."""
    return {"signal_key": key, "numeric_value": value, "status": status, "is_simulated": simulated,
            "fetched_at": fetched or NOW, "source": source, "confidence": 1.0, "text_value": None}


def pp(*rules, key="t", **extra):
    return {"key": key, "label": "Test", "category": "Financial", "description": "d", "rules": list(rules), **extra}


def evaluate(pdef, *signals, defs=None):
    defs = defs or INDICATOR_DEFS
    return P.evaluate_pain_point(pdef, build_signal_map(list(signals), defs), defs)


LOWER = P._r("interest_coverage_ratio", "lower", 3, 1.5, 1, "×")          # 3 -> 0, 1.5 -> full
HIGHER = P._r("leverage_ratio", "higher", 4, 8, 1, "×")                     # 4 -> 0, 8 -> full


# ---- intensity ----------------------------------------------------------------------------------

def test_intensity_is_zero_at_warn_and_full_at_severe_and_linear_between():
    assert P._intensity(3, LOWER) == 0.0
    assert P._intensity(1.5, LOWER) == 1.0
    assert P._intensity(2.25, LOWER) == pytest.approx(0.5)
    assert P._intensity(10, LOWER) == 0.0        # healthier than warn never goes negative
    assert P._intensity(0.1, LOWER) == 1.0       # worse than severe clamps at 1
    assert P._intensity(4, HIGHER) == 0.0
    assert P._intensity(6, HIGHER) == pytest.approx(0.5)
    assert P._intensity(20, HIGHER) == 1.0


# ---- what counts as evidence -----------------------------------------------------------------------

def test_a_fired_driver_carries_its_evidence():
    r = evaluate(pp(LOWER), sig("interest_coverage_ratio", 2.25, source="AIDA"))
    d = r["drivers"][0]
    assert d["assessed"] and d["state"] == "fired" and d["intensity"] == pytest.approx(0.5)
    assert d["value"] == 2.25 and d["source"] == "AIDA" and "3×" in d["threshold_text"]
    assert r["status"] == "moderate" and r["score"] == 50.0
    assert "2.25×" in r["headline"]


def test_a_healthy_value_is_checked_and_clear_not_unassessed():
    r = evaluate(pp(LOWER), sig("interest_coverage_ratio", 12))
    assert r["drivers"][0]["state"] == "clear" and r["status"] == "clear" and r["score"] == 0.0


def test_simulated_values_are_never_evidence():
    # The whole point of "data-backed": scoring.py still scores a placeholder, a pain point must not.
    r = evaluate(pp(LOWER), sig("interest_coverage_ratio", 0.5, simulated=True))
    assert r["status"] == "insufficient_data"
    assert r["drivers"][0]["unassessed_reason"] == "simulated"


def test_a_signal_with_no_provenance_flag_is_not_trusted():
    raw = {"signal_key": "interest_coverage_ratio", "numeric_value": 0.5, "status": "present", "fetched_at": NOW}
    assert evaluate(pp(LOWER), raw)["status"] == "insufficient_data"


def test_not_checked_is_a_gap_never_a_clear():
    r = evaluate(pp(LOWER, HIGHER))          # no signals at all
    assert r["status"] == "insufficient_data" and r["score"] is None and r["coverage"] == 0.0
    assert {d["unassessed_reason"] for d in r["drivers"]} == {"not_yet_checked"}
    r2 = evaluate(pp(LOWER), sig("interest_coverage_ratio", None, status="not_yet_checked"))
    assert r2["status"] == "insufficient_data"


def test_an_indicator_missing_from_the_catalog_is_reported_as_a_named_gap():
    # A proposal that is still only a plan (the press-distress bucket does not exist yet).
    r = evaluate(pp(P._r("reported_distress_news", "higher", 1, 4, 3, " mentions")), sig("reported_distress_news", 5))
    d = r["drivers"][0]
    assert not d["assessed"] and d["unassessed_reason"] == "not_in_catalog"
    assert "insolvency" in d["unassessed_text"] and d["producer"]["proposed"] is True


def test_it_starts_working_the_moment_the_indicator_exists():
    key = "reported_distress_news"
    defs = dict(INDICATOR_DEFS, **{key: {"key": key, "label": "Distress mentions", "axis": "context",
                                         "freshness_days": 365, "source_system": "Google News RSS", "automation_tier": "T2", "phase": 4}})
    r = evaluate(pp(P._r(key, "higher", 1, 4, 3, " mentions")), sig(key, 6), defs=defs)
    assert r["status"] == "severe"


def test_implausible_values_are_excluded_not_scored():
    rule = P._r("revenue_trend", "lower", 3, -20, 1, "%", valid_range=(-999, 999))
    r = evaluate(pp(rule), sig("revenue_trend", -407058))     # near-zero base year artefact
    assert r["status"] == "insufficient_data" and r["drivers"][0]["unassessed_reason"] == "implausible"


def test_negative_leverage_counts_as_full_strength():
    rule = P._r("leverage_ratio", "higher", 4, 8, 1, "×", negative_is_severe=True)
    r = evaluate(pp(rule), sig("leverage_ratio", -3.2))
    assert r["drivers"][0]["intensity"] == 1.0 and r["status"] == "severe"


def test_a_stale_value_still_counts_but_is_flagged():
    old = NOW - timedelta(days=400)     # leverage_ratio's freshness window is 365 days
    r = evaluate(pp(HIGHER), sig("leverage_ratio", 8, fetched=old))
    assert r["drivers"][0]["assessed"] and r["drivers"][0]["is_stale"] is True


def test_checked_and_found_nothing_follows_the_rules_absent_means():
    absent = sig("press_launch_mentions", None, status="absent")
    base = dict(indicator="press_launch_mentions", worse_when="lower", warn=1, severe=0, weight=1, unit="", active=True)
    ignore = evaluate(pp(dict(base, absent_means="ignore")), absent)
    zero = evaluate(pp(dict(base, absent_means="zero")), absent)
    pain = evaluate(pp(dict(base, absent_means="pain")), absent)
    assert ignore["status"] == "insufficient_data" and ignore["drivers"][0]["unassessed_reason"] == "absent_not_informative"
    assert zero["status"] == "severe" and zero["drivers"][0]["value"] == 0.0
    assert pain["status"] == "severe"


def test_an_absent_signal_that_still_carries_a_value_uses_it():
    # e.g. online_market_presence is recorded 'absent' with a measured 0/5
    rule = P._r("online_market_presence", "lower", 2.5, 1, 1, " / 5")
    r = evaluate(pp(rule), sig("online_market_presence", 0.0, status="absent"))
    assert r["status"] == "severe"


# ---- aggregation --------------------------------------------------------------------------------------

def test_score_is_the_weighted_mean_over_checked_drivers_only():
    a = P._r("interest_coverage_ratio", "lower", 3, 1.5, 3, "×")      # value 1.5 -> intensity 1
    b = P._r("leverage_ratio", "higher", 4, 8, 1, "×")                # value 4   -> intensity 0
    c = P._r("debt_level", "higher", 0, 1, 6, "")                     # never checked: must not dilute
    r = evaluate(pp(a, b, c), sig("interest_coverage_ratio", 1.5), sig("leverage_ratio", 4))
    assert r["score"] == pytest.approx(75.0)                          # (3*1 + 1*0) / (3+1)
    assert r["coverage"] == pytest.approx(4 / 10)


def test_thin_evidence_caps_how_severe_a_flag_may_claim_to_be():
    lonely = P._r("interest_coverage_ratio", "lower", 3, 1.5, 1, "×")
    heavy = P._r("debt_level", "higher", 0, 1, 9, "")                 # 10% coverage
    r = evaluate(pp(lonely, heavy), sig("interest_coverage_ratio", 1.0))
    assert r["score"] == 100.0 and r["status"] == "emerging" and r["capped"] is True

    mid = P._r("debt_level", "higher", 0, 1, 1.5, "")                 # 40% coverage -> at most moderate
    r2 = evaluate(pp(lonely, mid), sig("interest_coverage_ratio", 1.0))
    assert r2["coverage"] == pytest.approx(0.4) and r2["status"] == "moderate" and r2["capped"] is True

    r3 = evaluate(pp(lonely, P._r("leverage_ratio", "higher", 4, 8, 1, "×")),
                  sig("interest_coverage_ratio", 1.0), sig("leverage_ratio", 9))
    assert r3["status"] == "severe" and r3["capped"] is False and r3["confidence"] == "well_evidenced"


def test_a_single_driver_pain_point_is_fully_covered_by_its_one_driver():
    r = evaluate(pp(P._r("product_quality_trend", "lower", -0.2, -1.0, 1, " rating pts")), sig("product_quality_trend", -1.2))
    assert r["coverage"] == 1.0 and r["status"] == "severe" and not r["capped"]


def test_inactive_rules_are_ignored_entirely():
    off = dict(LOWER, active=False)
    r = evaluate(pp(off, HIGHER), sig("leverage_ratio", 4))
    assert r["drivers_total"] == 1 and r["coverage"] == 1.0


@pytest.mark.parametrize("score,band", [(0, "clear"), (14.9, "clear"), (15, "emerging"), (39.9, "emerging"),
                                        (40, "moderate"), (69.9, "moderate"), (70, "severe"), (100, "severe")])
def test_status_bands(score, band):
    assert P.status_for_score(score) == band


def test_rank_detected_orders_strongest_first_and_drops_the_rest():
    results = [{"status": "clear", "score": 5}, {"status": "moderate", "score": 45}, {"status": "severe", "score": 71},
               {"status": "insufficient_data", "score": None}, {"status": "moderate", "score": 60}]
    assert [(r["status"], r["score"]) for r in P.rank_detected(results)] == [("severe", 71), ("moderate", 60), ("moderate", 45)]


# ---- against the real seed catalog ---------------------------------------------------------------------

def test_debt_strain_end_to_end_with_the_real_seed():
    pain_defs = P.PAIN_POINT_SEED
    signals = [sig("interest_coverage_ratio", 1.0), sig("leverage_ratio", 9.0), sig("revenue_trend", -25)]
    results = {r["key"]: r for r in P.evaluate_pain_points(build_signal_map(signals, INDICATOR_DEFS), INDICATOR_DEFS, pain_defs)}
    assert results["debt_service_strain"]["status"] == "severe"
    assert results["debt_service_strain"]["confidence"] == "well_evidenced"
    assert results["growth_stall"]["status"] == "severe"
    assert results["workforce_strain"]["status"] == "insufficient_data"       # nothing checked there: not "clear"


def test_seed_keys_are_unique_and_every_rule_validates():
    keys = [p["key"] for p in P.PAIN_POINT_SEED]
    assert len(keys) == len(set(keys))
    for p in P.PAIN_POINT_SEED:
        assert P.validate_rules(p["rules"]) == [], p["key"]
        assert p["description"] and p["pilot_angle"] and p["rules"]


def test_every_rule_names_a_real_or_explicitly_proposed_indicator():
    for p in P.PAIN_POINT_SEED:
        for r in p["rules"]:
            assert r["indicator"] in INDICATOR_DEFS or r["indicator"] in P.PROPOSED_INDICATORS, (p["key"], r["indicator"])


def test_proposals_only_feed_pain_points_that_exist():
    keys = {p["key"] for p in P.PAIN_POINT_SEED}
    for name, prop in P.PROPOSED_INDICATORS.items():
        assert set(prop["feeds"]) <= keys, name
        assert prop["status"] in ("ready_to_build", "needs_decision", "needs_source")


def test_every_need_axis_indicator_drives_a_pain_point_or_is_exempt_with_a_reason():
    used = {r["indicator"] for p in P.PAIN_POINT_SEED for r in p["rules"]}
    need = {k for k, d in INDICATOR_DEFS.items() if d["axis"] in ("need", "both")}
    unaccounted = need - used - set(P.EXEMPT_NEED_INDICATORS)
    assert not unaccounted, f"need-axis indicators invisible to pain-point detection: {sorted(unaccounted)}"
    assert all(reason.strip() for reason in P.EXEMPT_NEED_INDICATORS.values())
    assert not (set(P.EXEMPT_NEED_INDICATORS) & used), "an exempted indicator is also used by a rule"
    assert set(P.EXEMPT_NEED_INDICATORS) <= set(INDICATOR_DEFS)


def test_validate_rules_catches_inverted_thresholds():
    bad_higher = dict(HIGHER, warn=8, severe=4)
    bad_lower = dict(LOWER, warn=1, severe=3)
    assert P.validate_rules([bad_higher]) and P.validate_rules([bad_lower])
    assert P.validate_rules([dict(LOWER, weight=-1)])


# ---- portfolio helpers -----------------------------------------------------------------------------------

def test_summary_and_gaps_across_companies():
    defs = [pp(LOWER, HIGHER, key="strain")]
    good = build_signal_map([sig("interest_coverage_ratio", 1.0), sig("leverage_ratio", 9)], INDICATOR_DEFS)
    empty = build_signal_map([], INDICATOR_DEFS)
    half = build_signal_map([sig("interest_coverage_ratio", 20)], INDICATOR_DEFS)
    by_company = {cid: P.evaluate_pain_points(m, INDICATOR_DEFS, defs) for cid, m in [("a", good), ("b", empty), ("c", half)]}

    summary = P.summarize_portfolio(by_company, defs)[0]
    assert (summary["severe"], summary["clear"], summary["insufficient_data"]) == (1, 1, 1)
    assert summary["assessed"] == 2 and summary["total"] == 3

    gaps = P.collect_gaps(by_company)["gaps"]
    by_ind = {g["indicator"]: g for g in gaps}
    assert by_ind["leverage_ratio"]["companies_missing"] == 2              # b and c
    assert by_ind["interest_coverage_ratio"]["companies_missing"] == 1     # b only
    assert gaps[0]["indicator"] == "leverage_ratio"                        # missing more often -> larger impact


def test_implausible_values_are_reported_as_a_data_quality_issue_not_a_gap():
    rule = P._r("revenue_trend", "lower", 3, -20, 1, "%", valid_range=(-999, 999))
    defs = [pp(rule, key="g")]
    m = build_signal_map([sig("revenue_trend", 5000)], INDICATOR_DEFS)
    out = P.collect_gaps({"a": P.evaluate_pain_points(m, INDICATOR_DEFS, defs)})
    assert out["gaps"] == [] and out["quality"][0]["indicator"] == "revenue_trend"


# ---- persistence ------------------------------------------------------------------------------------------

@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def test_seed_inserts_once_and_never_overwrites_an_edit(db):
    assert P.seed_pain_point_definitions(db) == len(P.PAIN_POINT_SEED)
    assert P.seed_pain_point_definitions(db) == 0

    row = db.query(PainPointDefinition).filter_by(key="growth_stall").one()
    rules = [dict(r) for r in row.rules]
    rules[0]["warn"] = 10.0
    row.rules = rules                       # a fresh list, as the editor does
    db.commit()

    P.seed_pain_point_definitions(db)       # a restart
    fetched = {d["key"]: d for d in P.fetch_pain_point_defs(db)}
    assert fetched["growth_stall"]["rules"][0]["warn"] == 10.0
    assert fetched["growth_stall"]["rules"][0].get("valid_range") == [-999, 999]      # untouched fields survive

    # ...and the edited threshold actually changes the outcome
    r = evaluate(fetched["growth_stall"], sig("revenue_trend", 5))
    assert r["score"] > 0     # 5% growth is "pain" now that the warn line moved to 10%


def test_inactive_pain_points_are_not_fetched_by_default(db):
    P.seed_pain_point_definitions(db)
    db.query(PainPointDefinition).filter_by(key="quality_decline").update({"is_active": False})
    db.commit()
    assert "quality_decline" not in {d["key"] for d in P.fetch_pain_point_defs(db)}
    assert "quality_decline" in {d["key"] for d in P.fetch_pain_point_defs(db, include_inactive=True)}
