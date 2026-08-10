"""
Two-Axis Scoring Engine (Need vs. Readiness)
Strictly adheres to Section 4 of GG_Dashboard_Technical_Brief.docx

Need Axis: Signals indicating pressure or capacity gap (could innovate).
Readiness Axis: Signals indicating active R&D/IP activity (is innovating).
"""

from typing import Dict, List, Any
from config import SIGNAL_METADATA
from utils import get_signal_display_status

def normalize_signal_value(signal_key: str, raw_value: float) -> float:
    """
    Normalizes raw signal numeric values into a 0.0 to 100.0 score scale.
    """
    if raw_value is None:
        return 0.0

    if signal_key == "margin_compression":
        # Margin compression percentage (e.g. 5% to 25%) mapped to 0-100 score
        return min(100.0, max(0.0, raw_value * 4.0))
    elif signal_key == "sector_export_exposure":
        # Export ratio 0.0 to 1.0 mapped to 0-100
        return min(100.0, max(0.0, raw_value * 100.0))
    elif signal_key == "job_posting_velocity":
        # Number of active job listings (e.g. 0 to 20)
        return min(100.0, max(0.0, raw_value * 5.0))
    elif signal_key == "tech_stack_intensity":
        # Tech intensity score (0 to 10 scale) mapped to 0-100
        return min(100.0, max(0.0, raw_value * 10.0))
    elif signal_key == "rd_expense_ratio":
        # R&D spend as % of revenue (0% to 15%)
        return min(100.0, max(0.0, raw_value * 6.66))
    elif signal_key == "interest_coverage_ratio":
        # Inverted: a LOW ratio means the company can barely cover interest from
        # operating profit — that's financial pressure, i.e. high NEED. Scale 0x-15x.
        return min(100.0, max(0.0, (1.0 - (raw_value / 15.0)) * 100.0))
    elif signal_key == "patent_count":
        # Patent count (0 to 15 patents)
        return min(100.0, max(0.0, raw_value * 6.66))
    elif signal_key == "patent_ipc_diversity":
        # Distinct IPC class prefixes (0 to 5)
        return min(100.0, max(0.0, raw_value * 20.0))
    elif signal_key == "management_diversity":
        # Leadership-mention proxy score (0 to 10, see scrapers/management_diversity.py)
        return min(100.0, max(0.0, raw_value * 10.0))
    elif signal_key == "trademark_count":
        # Trademark count (0 to 10)
        return min(100.0, max(0.0, raw_value * 10.0))
    elif signal_key == "public_grant_count":
        # Public grants received (0 to 5)
        return min(100.0, max(0.0, raw_value * 20.0))
    elif signal_key == "kununu_rating":
        # Rating 1.0 to 5.0 scale
        return min(100.0, max(0.0, (raw_value / 5.0) * 100.0))
    elif signal_key == "partnership_news_count":
        # News partnership count (0 to 10)
        return min(100.0, max(0.0, raw_value * 10.0))
    
    return min(100.0, max(0.0, float(raw_value)))

def calculate_company_scores(company_signals: List[Any]) -> Dict[str, Any]:
    """
    Computes Need score, Readiness score, and Completeness percentages for a company.
    
    company_signals: List of SignalRecord objects or dictionaries for a company.
    """
    signal_map = {}
    for sig in company_signals:
        key = getattr(sig, "signal_key", None) or sig.get("signal_key")
        status = getattr(sig, "status", None) or sig.get("status")
        val = getattr(sig, "numeric_value", None) if hasattr(sig, "numeric_value") else sig.get("numeric_value")
        fetched_at = getattr(sig, "fetched_at", None) if hasattr(sig, "fetched_at") else sig.get("fetched_at")
        
        display_status = get_signal_display_status(status, key, fetched_at)
        signal_map[key] = {
            "status": display_status,
            "raw_status": status,
            "value": val
        }
    
    need_signals = [k for k, meta in SIGNAL_METADATA.items() if meta["axis"] == "need"]
    readiness_signals = [k for k, meta in SIGNAL_METADATA.items() if meta["axis"] == "readiness"]

    def evaluate_axis(axis_keys: List[str]):
        checked_count = 0
        score_sum = 0.0
        
        for key in axis_keys:
            data = signal_map.get(key, {"status": "not_yet_checked", "value": None})
            st = data["status"]
            
            if st in ("present", "stale"):
                checked_count += 1
                normalized_val = normalize_signal_value(key, data["value"])
                score_sum += normalized_val
            elif st == "absent":
                # Actively checked, 0 value
                checked_count += 1
                # 0 added to score sum
            # 'not_yet_checked' is excluded from checked_count & denominator
            
        axis_score = (score_sum / checked_count) if checked_count > 0 else 0.0
        completeness_pct = (checked_count / len(axis_keys)) * 100.0 if len(axis_keys) > 0 else 0.0
        
        return axis_score, completeness_pct, checked_count, len(axis_keys)

    need_score, need_comp_pct, need_checked, need_total = evaluate_axis(need_signals)
    readiness_score, readiness_comp_pct, readiness_checked, readiness_total = evaluate_axis(readiness_signals)

    total_checked = need_checked + readiness_checked
    total_signals = need_total + readiness_total
    total_comp_pct = (total_checked / total_signals) * 100.0 if total_signals > 0 else 0.0

    return {
        "need_score": round(need_score, 1),
        "readiness_score": round(readiness_score, 1),
        "need_completeness_pct": round(need_comp_pct, 1),
        "readiness_completeness_pct": round(readiness_comp_pct, 1),
        "total_completeness_pct": round(total_comp_pct, 1),
        "signals_checked": total_checked,
        "signals_total": total_signals
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
