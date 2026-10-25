"""
Two-axis scoring over the extended schema (sections 3.2-3.7 of the integration brief).

NEED and READINESS are two separate scores. This module never produces a combined number
and does not implement the VIENNA classifier (deferred).

Observation states (extending the SignalRecord states present / absent / not_yet_checked / stale):
  observed        measured from a source                      -> scored
  inferred        derived / estimated                         -> scored (counted separately)
  unknown         not collected, not found, or below the confidence bar
                                                              -> NEVER scored as bad; excluded from the
                                                                 score and counted in coverage
  not_applicable  cannot apply to this company                -> removed from the denominator entirely

Roles: Scored variables enter the weighted axis score. Gate variables only set
qualification_flag (the scores stay unchanged). Moderators are never scored; optional,
off-by-default rules in settings/moderators.json adjust interpretation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from .registry import IndicatorRegistry, is_number
from .schema import AXES, SCHEMA_VERSION, SchemaError
from .weights import WeightSet, default_weight_set, effective_weights

STATES = ("observed", "inferred", "unknown", "not_applicable")
SCORING_STATES = ("observed", "inferred")

# Same evidence-eligibility bars as the live engine (scoring.py): confidence is a gate on
# whether an observation counts, never a multiplier on the score.
MIN_SCORABLE_CONFIDENCE = 0.60
MIN_GATE_CONFIDENCE = 0.75


class ObservationError(SchemaError):
    """An observation does not fit the schema (unknown variable, unknown label, bad strength)."""


@dataclass
class Observation:
    """One company x one variable. Give a `label` (a named status from status_mappings.json) OR a
    graded `strength` in [0,1] = how fully the Sheet1 Status text is present."""
    variable_key: str
    state: str = "unknown"
    label: Optional[str] = None
    strength: Optional[float] = None
    source: Optional[str] = None
    confidence: Optional[float] = None
    is_simulated: bool = False
    note: Optional[str] = None

    def __post_init__(self) -> None:
        if self.state not in STATES:
            raise ObservationError(f"{self.variable_key}: state {self.state!r} not in {STATES}")
        if self.state in SCORING_STATES:
            if (self.label is None) == (self.strength is None):
                raise ObservationError(f"{self.variable_key}: a {self.state} observation needs exactly one of label / strength")
            if self.strength is not None and not (is_number(self.strength) and 0.0 <= self.strength <= 1.0):
                raise ObservationError(f"{self.variable_key}: strength must be within [0, 1], got {self.strength!r}")

    # convenience constructors
    @classmethod
    def trigger(cls, key: str, state: str = "observed", **kw: Any) -> "Observation":
        return cls(key, state, label="trigger", **kw)

    @classmethod
    def not_trigger(cls, key: str, state: str = "observed", **kw: Any) -> "Observation":
        return cls(key, state, label="not_trigger", **kw)

    @classmethod
    def with_label(cls, key: str, label: str, state: str = "observed", **kw: Any) -> "Observation":
        return cls(key, state, label=label, **kw)

    @classmethod
    def with_strength(cls, key: str, strength: float, state: str = "observed", **kw: Any) -> "Observation":
        return cls(key, state, strength=strength, **kw)

    @classmethod
    def unknown(cls, key: str, note: Optional[str] = None) -> "Observation":
        return cls(key, "unknown", note=note)

    @classmethod
    def not_applicable(cls, key: str, note: Optional[str] = None) -> "Observation":
        return cls(key, "not_applicable", note=note)


@dataclass
class CompanyContext:
    ateco_code: Optional[str] = None
    attributes: Dict[str, Any] = field(default_factory=dict)


@dataclass
class _Resolved:
    state: str
    axis_values: Dict[str, Optional[float]] = field(default_factory=dict)
    trigger_strength: Optional[float] = None
    label: Optional[str] = None
    source: Optional[str] = None
    confidence: Optional[float] = None
    reason: Optional[str] = None


def _axis_values_for(reg: IndicatorRegistry, key: str, obs: Observation, warnings: List[str]):
    """-> (per-axis values 0..1 or None, trigger_strength 0..1) for a scoring-state observation."""
    m = reg.mapping(key)
    axes_cfg = m.get("axes", {})
    states = m.get("states", {})
    trigger_labels = m.get("trigger_labels", ["trigger"])
    values: Dict[str, Optional[float]] = {}

    if obs.label is not None:
        lab = obs.label
        if lab in states:
            raw = states[lab]
            for axis, v in raw.items():
                if v == "TO_CALIBRATE":
                    warnings.append(f"{key}: state {lab!r} has no calibrated value yet; treated as unknown")
                    return None, None
                values[axis] = None if v is None else float(v)
        elif lab in ("trigger", "not_trigger") and (axes_cfg or reg.role(key) != "Scored"):
            for axis, a in axes_cfg.items():
                values[axis] = float(a[lab])
        else:
            known = sorted(set(states) | ({"trigger", "not_trigger"} if axes_cfg else set()))
            raise ObservationError(f"{key}: unrecognised label {lab!r}; known labels: {known}")
        trig = 1.0 if (lab == "trigger" or lab in trigger_labels) else 0.0
        return values, trig

    if m.get("labels_only") or (not axes_cfg and reg.role(key) == "Scored"):
        raise ObservationError(f"{key}: this variable only accepts named labels {sorted(states)}, not a graded strength")
    s = float(obs.strength)
    for axis, a in axes_cfg.items():
        values[axis] = a["not_trigger"] + s * (a["trigger"] - a["not_trigger"])
    return values, s


def _resolve(reg: IndicatorRegistry, key: str, obs: Optional[Observation], ctx: CompanyContext,
             warnings: List[str], undetermined: List[str]) -> _Resolved:
    ap = reg.applicability_of(key, ctx)
    if ap["status"] == "not_applicable":
        return _Resolved("not_applicable", reason=ap["reason"])
    if ap["status"] == "undetermined":
        undetermined.append(key)
    if obs is None:
        return _Resolved("unknown", reason="no observation")
    if obs.state == "not_applicable":
        return _Resolved("not_applicable", reason=obs.note or "observation marked not_applicable", source=obs.source)
    if obs.state == "unknown":
        return _Resolved("unknown", reason=obs.note or "unknown", source=obs.source)

    if obs.is_simulated:
        return _Resolved("unknown", reason="simulated source result", source=obs.source)
    bar = MIN_GATE_CONFIDENCE if reg.role(key) == "Gate" else MIN_SCORABLE_CONFIDENCE
    if obs.confidence is not None and obs.confidence < bar:
        return _Resolved("unknown", reason=f"source confidence {obs.confidence} below {bar}", source=obs.source,
                         confidence=obs.confidence)
    values, trig = _axis_values_for(reg, key, obs, warnings)
    if values is None:
        return _Resolved("unknown", reason="state value is TO_CALIBRATE", source=obs.source)
    return _Resolved(obs.state, axis_values=values, trigger_strength=trig, label=obs.label,
                     source=obs.source, confidence=obs.confidence)


# --------------------------------------------------------------------------- moderators

def _moderator_effects(reg: IndicatorRegistry, resolved: Dict[str, _Resolved]):
    mult: Dict[str, Dict[str, float]] = {a: {} for a in AXES}
    applied: List[Dict[str, Any]] = []
    notes: List[str] = []
    for rule in reg.moderators:
        if not rule.get("enabled"):
            continue
        r = resolved.get(rule["moderator_key"])
        if r is None or r.state not in SCORING_STATES or r.label != rule["when_label"]:
            continue
        eff = rule["effect"]
        if eff["type"] == "weight_multiplier":
            for t in eff["targets"]:
                mult[eff["axis"]][t] = mult[eff["axis"]].get(t, 1.0) * float(eff["factor"])
            applied.append({"rule": rule["id"], "type": "weight_multiplier", "axis": eff["axis"],
                            "targets": eff["targets"], "factor": eff["factor"]})
        else:
            notes.append(eff["text"])
            applied.append({"rule": rule["id"], "type": "note"})
    return mult, applied, notes


# --------------------------------------------------------------------------- posture (rows 63 x 72)

def _posture(reg: IndicatorRegistry, resolved: Dict[str, _Resolved]) -> Dict[str, Any]:
    cfg = reg.interactions["systems_posture"]
    age_key, int_key = cfg["age_variable"], cfg["integration_variable"]
    out: Dict[str, Any] = {"age_variable": age_key, "integration_variable": int_key,
                           "cell": None, "need_tier": None, "age": None, "integration": None}

    def classify(key: str, true_labels: List[str], false_labels: List[str]) -> Optional[bool]:
        r = resolved[key]
        if r.state not in SCORING_STATES:
            return None
        if r.label is not None:
            if r.label in true_labels:
                return True
            if r.label in false_labels:
                return False
            return None
        s = r.trigger_strength
        if s in (0.0, 1.0):
            return bool(s)
        cut = cfg.get("numeric_cutoff")
        if is_number(cut):
            return s >= cut
        out.setdefault("reason", "graded strength strictly between 0 and 1 needs posture.numeric_cutoff (TO_CALIBRATE)")
        return None

    old = classify(age_key, cfg["age_old_labels"], cfg["age_new_labels"])
    siloed = classify(int_key, cfg["integration_siloed_labels"], cfg["integration_integrated_labels"])
    out["age"] = None if old is None else ("old" if old else "new")
    out["integration"] = None if siloed is None else ("siloed" if siloed else "integrated")
    if old is None or siloed is None:
        out.setdefault("reason", "both the age (row 63) and the integration (row 72) observations are needed")
        return out
    cell = f"{'old' if old else 'new'}_{'siloed' if siloed else 'integrated'}"
    out["cell"] = cell
    out["need_tier"] = cfg["cells"][cell]["need_tier"]
    return out


# --------------------------------------------------------------------------- scoring

def _index_observations(reg: IndicatorRegistry, observations: Iterable[Observation]) -> Dict[str, Observation]:
    out: Dict[str, Observation] = {}
    for o in observations:
        if o.variable_key not in reg.by_key:
            raise ObservationError(f"observation for unknown variable {o.variable_key!r}")
        if o.variable_key in out:
            raise ObservationError(f"two observations for {o.variable_key!r}")
        out[o.variable_key] = o
    return out


def score_company(reg: IndicatorRegistry, observations: Iterable[Observation],
                  context: Optional[CompanyContext] = None, weight_set: Optional[WeightSet] = None,
                  company_id: Optional[str] = None) -> Dict[str, Any]:
    ctx = context or CompanyContext()
    ws = weight_set or default_weight_set(reg)
    obs_by_key = _index_observations(reg, observations)
    warnings: List[str] = []
    undetermined: List[str] = []

    resolved = {k: _resolve(reg, k, obs_by_key.get(k), ctx, warnings, undetermined) for k in reg.keys}
    mult, moderators_applied, notes = _moderator_effects(reg, resolved)

    scores: Dict[str, Any] = {}
    for axis in AXES:
        keys = [k for k in reg.scored_keys if axis in reg.axes_of(k)]
        applicable = [k for k in keys if resolved[k].state != "not_applicable"]
        w = effective_weights(ws, reg, axis, applicable, mult[axis])
        w_total = sum(w.values())
        num = den = 0.0
        n_obs = n_inf = n_unknown = n_unmapped = 0
        detail = []
        for k in applicable:
            r = resolved[k]
            v = r.axis_values.get(axis) if r.state in SCORING_STATES else None
            counted = v is not None
            if r.state in SCORING_STATES and not counted:
                n_unmapped += 1
            elif r.state == "unknown":
                n_unknown += 1
            if counted:
                num += w[k] * v
                den += w[k]
                if r.state == "observed":
                    n_obs += 1
                else:
                    n_inf += 1
            detail.append({
                "variable_key": k, "state": r.state, "label": r.label, "axis_value": v,
                "weight": round(w[k], 6), "contribution": round(w[k] * v, 6) if counted else 0.0,
                "counts_toward_score": counted, "source": r.source, "reason": r.reason,
            })
        score = round(100.0 * num / den, 1) if den > 0 else None
        scores[axis] = {
            "score": score,
            "coverage_weight_pct": round(100.0 * den / w_total, 1) if w_total > 0 else None,
            "coverage_count": {"scored": n_obs + n_inf, "applicable": len(applicable)},
            "observed": n_obs, "inferred": n_inf, "unknown": n_unknown,
            "observed_but_not_mapped_to_this_axis": n_unmapped,
            "not_applicable": len(keys) - len(applicable),
            "detail": detail,
        }

    # ---- gates: flag, never score
    gates: Dict[str, str] = {}
    reasons: List[Dict[str, Any]] = []
    for k in reg.gate_keys:
        r, g = resolved[k], reg.roles_cfg["gates"][k]
        if r.state == "not_applicable":
            gates[k] = "not_applicable"
        elif r.state in SCORING_STATES:
            if r.trigger_strength is not None and r.trigger_strength >= g["fail_strength_at_least"]:
                gates[k] = "fail"
                reasons.append({"source": "gate", "variable_key": k, "sheet_row": reg.by_key[k].sheet_row, "reason": g["reason"]})
            else:
                gates[k] = "pass"
        else:
            gates[k] = "unknown"
    qualification = {"flagged": bool(reasons), "reasons": reasons, "gates": gates}

    # ---- readiness caveat flag (rows 50, 51, 72)
    caveat_vars = []
    for k in reg.caveat_keys():
        r = resolved[k]
        if r.state in SCORING_STATES and r.trigger_strength and r.trigger_strength > 0:
            caveat_vars.append({"variable_key": k, "sheet_row": reg.by_key[k].sheet_row, "trigger_strength": r.trigger_strength})
    caveat = {"flagged": bool(caveat_vars), "variables": caveat_vars}

    # ---- distress cap (uncalibrated unless distress.json is filled in)
    distress = _distress(reg, resolved, scores, qualification)

    result = {
        "schema_version": SCHEMA_VERSION,
        "company_id": company_id,
        "weight_set": ws.summary(),
        "grouping": {"id": reg.grouping_id, "hash": reg.grouping_hash},
        "scores": scores,
        "qualification_flag": qualification,
        "readiness_caveat": caveat,
        "distress": distress,
        "systems_posture": _posture(reg, resolved),
        "moderators_applied": moderators_applied,
        "interpretation_notes": notes,
        "moderator_context": {k: {"state": resolved[k].state, "label": resolved[k].label} for k in reg.moderator_keys},
        "applicability_undetermined": sorted(set(undetermined)),
        "warnings": warnings,
    }
    return result


def _distress(reg: IndicatorRegistry, resolved: Dict[str, _Resolved], scores: Dict[str, Any],
              qualification: Dict[str, Any]) -> Dict[str, Any]:
    cfg = reg.distress
    watch = list(cfg["financial_need_variables"])
    if cfg.get("apply_to_caveat_flagged_variables"):
        watch += [k for k in reg.caveat_keys() if k not in watch]
    candidates = {k: resolved[k].trigger_strength for k in watch
                  if k in resolved and resolved[k].state in SCORING_STATES and resolved[k].trigger_strength}
    thr = cfg["trigger"]["variable_trigger_strength_at_least"]
    minv = cfg["trigger"]["min_variables_triggered"]
    out: Dict[str, Any] = {"status": "not_calibrated", "candidates": candidates, "triggered": [], "applied": False}
    if not (is_number(thr) and is_number(minv)):
        return out
    triggered = sorted(k for k, s in candidates.items() if s >= thr)
    out.update(status="evaluated", triggered=triggered)
    if len(triggered) < minv:
        return out
    action = cfg["action"]
    if action["type"] == "qualification_flag":
        qualification["flagged"] = True
        qualification["reasons"].append({"source": "distress", "variables": triggered,
                                         "reason": "Financial distress may crowd out pilot budget: qualify budget before proposing a pilot."})
        out["applied"] = True
    elif action["type"] == "readiness_cap":
        cap = action.get("readiness_cap_value")
        if not is_number(cap):
            out["status"] = "not_calibrated"
            return out
        r = scores["READINESS"]
        if r["score"] is not None and r["score"] > cap:
            r["score_uncapped"] = r["score"]
            r["score"] = float(cap)
            r["capped_by"] = "distress"
        out["applied"] = True
    return out


def compare_weight_sets(reg: IndicatorRegistry, observations: Iterable[Observation],
                        weight_sets: List[WeightSet], context: Optional[CompanyContext] = None) -> Dict[str, Any]:
    """Two or more weight sets on the same observations, side by side. Scores are never combined."""
    obs = list(observations)
    results = {ws.weight_set_id: score_company(reg, obs, context, ws) for ws in weight_sets}
    table = {axis: {i: r["scores"][axis]["score"] for i, r in results.items()} for axis in AXES}
    return {"results": results, "scores_by_axis": table}
