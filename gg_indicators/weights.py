"""
Versioned weight sets.

Weights are NOT part of the catalogue and not hardcoded: each set is a JSON file in
settings/weight_sets/. Dropping another file there adds a set (no code change) and two sets
can be scored side by side (engine.compare_weight_sets).

Two modes:
  * "equal_per_group_unit": generated from the active redundancy grouping (the labelled
    placeholder). Every dampened unit in an axis weighs the same; a group's members share it.
  * "explicit": {"weights": {"NEED": {key: w}, "READINESS": {key: w}}, "redundancy_dampening":
    "rank_halving" | "none"}. rank_halving reuses scoring._apply_redundancy_dampening (the existing
    mechanism: full weight to the highest-weighted member of a group, half to the next, a quarter...).
    Validation fails loudly unless each axis sums to 1 across Scored variables, and rejects weights
    on Gate or Moderator rows, on CONTEXT rows, on the wrong axis, and negative weights.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .registry import IndicatorRegistry, is_number
from .schema import AXES, SETTINGS_DIR, SchemaError

WEIGHT_SETS_DIR = SETTINGS_DIR / "weight_sets"
PLACEHOLDER_LABEL = "PLACEHOLDER, NOT A DECISION"
SUM_TOLERANCE = 1e-6
STATUSES = ("placeholder", "draft", "approved")
MODES = ("equal_per_group_unit", "explicit")


class WeightSetError(SchemaError):
    """A weight set is invalid. Raised on load, never swallowed."""


@dataclass
class WeightSet:
    weight_set_id: str
    status: str
    mode: str
    label: str
    description: str = ""
    weights: Dict[str, Dict[str, float]] = field(default_factory=dict)
    redundancy_dampening: str = "rank_halving"
    approved_by: Optional[str] = None
    approved_on: Optional[str] = None
    source_file: Optional[str] = None

    @property
    def is_placeholder(self) -> bool:
        return self.status == "placeholder"

    def display_label(self) -> str:
        return PLACEHOLDER_LABEL if self.is_placeholder else self.label

    def summary(self) -> Dict[str, Any]:
        return {"id": self.weight_set_id, "status": self.status, "label": self.display_label(), "mode": self.mode}


def weight_set_from_dict(raw: Dict[str, Any], source: Optional[str] = None) -> WeightSet:
    for req in ("weight_set_id", "status", "mode"):
        if req not in raw:
            raise WeightSetError(f"{source or 'weight set'}: missing {req!r}")
    if raw["status"] not in STATUSES:
        raise WeightSetError(f"{raw['weight_set_id']}: status {raw['status']!r} not in {STATUSES}")
    if raw["mode"] not in MODES:
        raise WeightSetError(f"{raw['weight_set_id']}: mode {raw['mode']!r} not in {MODES}")
    damp = raw.get("redundancy_dampening", "rank_halving")
    if damp not in ("rank_halving", "none"):
        raise WeightSetError(f"{raw['weight_set_id']}: redundancy_dampening {damp!r} not in ('rank_halving', 'none')")
    return WeightSet(
        weight_set_id=raw["weight_set_id"], status=raw["status"], mode=raw["mode"],
        label=raw.get("label") or raw["status"].upper(), description=raw.get("description", ""),
        weights={a: dict(w) for a, w in (raw.get("weights") or {}).items()},
        redundancy_dampening=damp, approved_by=raw.get("approved_by"), approved_on=raw.get("approved_on"),
        source_file=source,
    )


def validate_weight_set(ws: WeightSet, reg: IndicatorRegistry) -> None:
    errs: List[str] = []
    if ws.status == "approved" and not (ws.approved_by and ws.approved_on):
        errs.append("status 'approved' requires approved_by and approved_on")
    if ws.status == "placeholder" and ws.label != PLACEHOLDER_LABEL:
        # the label is forced on output, but a placeholder file that claims otherwise is a mistake worth surfacing
        errs.append(f"a placeholder set must carry the label {PLACEHOLDER_LABEL!r}")

    if ws.mode == "equal_per_group_unit":
        if ws.weights:
            errs.append("mode equal_per_group_unit is generated; it must not carry explicit weights")
        for axis in AXES:
            keys = [k for k in reg.scored_keys if axis in reg.axes_of(k)]
            w = effective_weights(ws, reg, axis, keys)
            if keys and abs(sum(w.values()) - 1.0) > SUM_TOLERANCE:
                errs.append(f"generated {axis} weights sum to {sum(w.values())}, not 1")
    else:
        for axis in AXES:
            axis_w = ws.weights.get(axis)
            if axis_w is None:
                errs.append(f"missing weights for axis {axis}")
                continue
            for key, w in axis_w.items():
                if key not in reg.by_key:
                    errs.append(f"{axis}: unknown variable {key}")
                    continue
                role = reg.role(key)
                if role != "Scored":
                    errs.append(f"{axis}: weight on {role} variable {key} (only Scored variables take weights)")
                    continue
                if axis not in reg.axes_of(key):
                    errs.append(f"{axis}: weight on {key} whose tag axis is {reg.by_key[key].tag.axis}")
                if not is_number(w) or w < 0:
                    errs.append(f"{axis}: weight for {key} must be a non-negative number, got {w!r}")
            nums = [w for k, w in axis_w.items() if is_number(w) and k in reg.by_key and reg.role(k) == "Scored"]
            total = sum(nums)
            if abs(total - 1.0) > SUM_TOLERANCE:
                errs.append(f"{axis} weights sum to {total:.6f}, must be 1 across Scored variables")
    if errs:
        raise WeightSetError(f"Weight set {ws.weight_set_id!r} is invalid:\n  - " + "\n  - ".join(errs))


def load_weight_sets(reg: IndicatorRegistry, directory: Path = WEIGHT_SETS_DIR) -> Dict[str, WeightSet]:
    sets: Dict[str, WeightSet] = {}
    for path in sorted(directory.glob("*.json")):
        ws = weight_set_from_dict(json.loads(path.read_text(encoding="utf-8")), source=path.name)
        validate_weight_set(ws, reg)
        if ws.weight_set_id in sets:
            raise WeightSetError(f"duplicate weight_set_id {ws.weight_set_id!r} ({path.name})")
        sets[ws.weight_set_id] = ws
    if not sets:
        raise WeightSetError(f"no weight sets found in {directory}")
    return sets


def default_weight_set(reg: IndicatorRegistry, directory: Path = WEIGHT_SETS_DIR) -> WeightSet:
    """The approved set if exactly one exists, otherwise the placeholder (which every report labels)."""
    sets = load_weight_sets(reg, directory)
    approved = [s for s in sets.values() if s.status == "approved"]
    if len(approved) == 1:
        return approved[0]
    if len(approved) > 1:
        raise WeightSetError(f"several approved weight sets ({[a.weight_set_id for a in approved]}): pass one explicitly")
    placeholders = [s for s in sets.values() if s.is_placeholder]
    if not placeholders:
        raise WeightSetError("no approved or placeholder weight set to default to: pass one explicitly")
    return placeholders[0]


def effective_weights(ws: WeightSet, reg: IndicatorRegistry, axis: str, keys: List[str],
                      multipliers: Optional[Dict[str, float]] = None) -> Dict[str, float]:
    """
    Weights for one axis over exactly `keys` (the company's APPLICABLE scored variables on that
    axis; not_applicable ones are already gone, which is how a group "carries alone" when a
    member does not apply). Not normalised to 1 here for explicit sets: the score divides by the
    weight of the observed subset anyway.
    """
    multipliers = multipliers or {}
    if not keys:
        return {}
    if ws.mode == "equal_per_group_unit":
        groups: Dict[str, List[str]] = {}
        for k in keys:
            groups.setdefault(reg.group_of(k) or f"SOLO:{k}", []).append(k)
        unit = 1.0 / len(groups)
        w = {k: unit / len(members) for members in groups.values() for k in members}
    else:
        w = {k: float(ws.weights.get(axis, {}).get(k, 0.0)) for k in keys}
    w = {k: v * multipliers.get(k, 1.0) for k, v in w.items()}
    if ws.mode == "explicit" and ws.redundancy_dampening == "rank_halving":
        from scoring import _apply_redundancy_dampening  # the existing mechanism, reused as-is
        defs = {k: {"weight": w[k], "redundancy_group": reg.group_of(k)} for k in keys}
        w = _apply_redundancy_dampening(keys, defs)
    return w
