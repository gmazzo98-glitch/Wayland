"""
Adapter from the LIVE engine's per-company signal map (scoring.build_signal_map) to the
Observation model here. It does not change scoring.py or the database; it only lets the new
engine run over today's SignalRecords so the two can be compared.

Mapping of the live states (deliberately conservative):
  present / stale          -> observed (strength from the live normalised 0-100 score)
  absent                   -> observed, normalised 0 (same as the live engine) - EXCEPT for the asymmetric
                              variables (rows 73, 75) where absence is unknown
  not_yet_checked          -> unknown
  simulated / low-confidence -> unknown (the live engine also excludes them)

Direction: the live normalised score is oriented to the variable's axis (inversion is already
applied). Our strength is "how fully the Sheet1 status text is present". For Standard polarity
Scored rows that equals normalised/100; for Negative polarity it is 1 - normalised/100; for Gate rows
(a gate has no axis value; live normalised high = healthy/pass) it is 1 - normalised/100.
"""

from __future__ import annotations

from typing import Any, Dict, List

from scoring import normalize_indicator_value

from .engine import Observation
from .registry import IndicatorRegistry


def observations_from_signal_map(reg: IndicatorRegistry, signal_map: Dict[str, Dict[str, Any]],
                                 indicator_defs: Dict[str, Dict[str, Any]]) -> List[Observation]:
    asym = set(reg.interactions["asymmetric_variables"]["variables"])
    out: List[Observation] = []
    for key in reg.keys:
        sig = signal_map.get(key)
        if sig is None or key not in indicator_defs:
            continue  # variable has no live signal (e.g. the four DREAMY-round rows): left as unknown by the engine
        status = sig.get("status")
        common = dict(source=sig.get("source"), confidence=sig.get("confidence"),
                      is_simulated=bool(sig.get("is_simulated")))
        role, polarity = reg.role(key), reg.by_key[key].tag.polarity
        if status in ("present", "stale") and sig.get("value") is not None:
            norm = normalize_indicator_value(sig["value"], indicator_defs[key]) / 100.0
        elif status == "absent":
            if key in asym:
                out.append(Observation.unknown(key, "absence of evidence is unknown for this variable"))
                continue
            norm = 0.0
        else:
            out.append(Observation.unknown(key, f"live status {status}"))
            continue
        if role == "Moderator":
            continue  # moderators need a named label; the live engine only has a number for them
        strength = (1.0 - norm) if (role == "Gate" or polarity == "Negative") else norm
        out.append(Observation.with_strength(key, round(strength, 6), **common))
    return out
