"""
Row 74 helper: turns "which certifications does this company hold" into an Observation for
quality_process_maturity, read against the sector norm in
settings/expected_certifications_by_ateco.json (never in absolute terms).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .engine import Observation
from .registry import IndicatorRegistry, is_number

KEY = "quality_process_maturity"


def _norm(name: str) -> str:
    return "".join(ch for ch in (name or "").upper() if ch.isalnum())


def classify_quality_process(reg: IndicatorRegistry, ateco_code: Optional[str],
                             certifications: Optional[List[Dict[str, Any]]],
                             source: Optional[str] = None) -> Tuple[Observation, Dict[str, Any]]:
    """
    certifications: None = not checked (-> unknown); [] = checked and none held (-> gap vs the norm);
    otherwise [{"standard": "IATF 16949", "years_held": 12 (optional)}, ...].
    Returns (Observation, detail) where detail explains the norm used and what is missing.
    """
    norm = reg.expected_certifications(ateco_code)
    detail: Dict[str, Any] = {"norm_id": norm["id"], "expected_any_of": norm["expected_any_of"],
                              "norm_status": norm["status"], "held": [], "missing": []}
    if certifications is None:
        return Observation.unknown(KEY, "certifications not checked"), detail

    held_norm = {_norm(c["standard"]): c for c in certifications}
    detail["held"] = [c["standard"] for c in certifications]

    def holds(std: str) -> bool:
        names = [std] + list(norm["aliases"].get(std, []))
        return any(_norm(n) in held_norm for n in names)

    meets = any(holds(std) for std in norm["expected_any_of"])
    if not meets:
        detail["missing"] = norm["expected_any_of"]
        return Observation.with_label(KEY, "gap_vs_sector_norm", source=source), detail

    th = reg.certifications["thresholds"]
    min_years, min_std = th["min_years_held_for_long_held"], th["min_standards_for_multi_standard"]
    if is_number(min_years) and is_number(min_std):
        long_held = all(is_number(c.get("years_held")) and c["years_held"] >= min_years for c in certifications)
        if long_held and len(certifications) >= min_std:
            return Observation.with_label(KEY, "long_held_multi_standard", source=source), detail
    else:
        detail["note"] = "min_years_held_for_long_held / min_standards_for_multi_standard are TO_CALIBRATE: cannot assign long_held_multi_standard"
    return Observation.with_label(KEY, "certified_meets_norm", source=source), detail
