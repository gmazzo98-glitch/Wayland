"""
Unit Tests for Project Vienna: Normalization, Scoring Engine, Tri-State Status,
and the segment/prime-target rules from the Technical Brief.
"""

import pytest
from datetime import datetime, timedelta
from utils import normalize_handelsregister_nr, is_signal_stale, get_signal_display_status
from scoring import (
    calculate_company_scores, normalize_signal_value,
    is_prime_target, rank_companies, PRIME_NEED_MIN, PRIME_READINESS_BAND,
)

def test_handelsregister_normalization():
    assert normalize_handelsregister_nr("HRB 104928") == "HRB-104928"
    assert normalize_handelsregister_nr("HRB-104928") == "HRB-104928"
    assert normalize_handelsregister_nr("Amtsgericht München HRB 883012") == "HRB-883012"
    assert normalize_handelsregister_nr("HRA 310459") == "HRA-310459"

def test_signal_freshness():
    now = datetime.utcnow()
    fresh_date = now - timedelta(days=5)
    stale_job_date = now - timedelta(days=20) # job_posting_velocity freshness window is 14 days

    assert is_signal_stale("job_posting_velocity", fresh_date) is False
    assert is_signal_stale("job_posting_velocity", stale_job_date) is True

    assert get_signal_display_status("present", "job_posting_velocity", fresh_date) == "present"
    assert get_signal_display_status("present", "job_posting_velocity", stale_job_date) == "stale"
    assert get_signal_display_status("absent", "job_posting_velocity", stale_job_date) == "absent"
    assert get_signal_display_status("not_yet_checked", "job_posting_velocity", now) == "not_yet_checked"

def test_scoring_engine_two_axis():
    signals = [
        # Need signals (5 total in the catalog: margin_compression, interest_coverage_ratio,
        # sector_export_exposure, job_posting_velocity, tech_stack_intensity)
        {"signal_key": "margin_compression", "status": "present", "numeric_value": 15.0, "fetched_at": datetime.utcnow()}, # 60.0
        {"signal_key": "sector_export_exposure", "status": "present", "numeric_value": 0.8, "fetched_at": datetime.utcnow()}, # 80.0
        {"signal_key": "job_posting_velocity", "status": "absent", "numeric_value": None, "fetched_at": datetime.utcnow()}, # 0.0 (checked absent)
        # interest_coverage_ratio and tech_stack_intensity are not_yet_checked (3 of 5 need signals checked)

        # Readiness signals (8 total in the catalog)
        {"signal_key": "patent_count", "status": "present", "numeric_value": 3.0, "fetched_at": datetime.utcnow()}, # ~20.0
        {"signal_key": "trademark_count", "status": "present", "numeric_value": 2.0, "fetched_at": datetime.utcnow()}, # 20.0
        # 6 other readiness signals not_yet_checked (2 of 8 readiness signals checked)
    ]

    scores = calculate_company_scores(signals)

    assert scores["need_completeness_pct"] == 60.0
    assert scores["readiness_completeness_pct"] == 25.0

    assert scores["signals_checked"] == 5
    assert scores["signals_total"] == 13
    assert round(scores["total_completeness_pct"], 1) == 38.5

def test_normalize_signal_value():
    assert normalize_signal_value("patent_count", 0.0) == 0.0
    assert normalize_signal_value("patent_count", 15.0) == 99.9
    assert normalize_signal_value("kununu_rating", 5.0) == 100.0
    assert normalize_signal_value("kununu_rating", 2.5) == 50.0

def test_normalize_signal_value_interest_coverage_is_inverted():
    # A LOW interest coverage ratio means high financial pressure -> high NEED score.
    assert normalize_signal_value("interest_coverage_ratio", 0.0) == 100.0
    assert normalize_signal_value("interest_coverage_ratio", 15.0) == 0.0
    assert normalize_signal_value("interest_coverage_ratio", 7.5) == 50.0

def test_normalize_signal_value_new_readiness_signals():
    assert normalize_signal_value("patent_ipc_diversity", 5.0) == 100.0
    assert normalize_signal_value("management_diversity", 10.0) == 100.0
    assert normalize_signal_value("management_diversity", 0.0) == 0.0

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
