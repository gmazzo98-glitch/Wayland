"""
IndicatorRegistry: the workbook schema + every config file in settings/, loaded and
cross-validated once. Attributes are plain dicts/lists on purpose so a test (or a what-if
run) can deepcopy the registry and change one setting without touching a file.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .schema import (
    DEFAULT_WORKBOOK, SCHEMA_VERSION, SETTINGS_DIR, ROLES, SchemaError, VariableSpec,
    axes_for, load_json, read_workbook, tier_for_mode,
)

TO_CALIBRATE = "TO_CALIBRATE"
TO_MAP = "TO_MAP"


class ConfigError(SchemaError):
    """A config file contradicts the workbook (or itself)."""


def is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


class IndicatorRegistry:
    def __init__(self, specs: List[VariableSpec], settings: Dict[str, Any], workbook: Optional[Path] = None):
        self.specs = specs
        self.by_key: Dict[str, VariableSpec] = {s.key: s for s in specs}
        self.workbook = workbook
        self.key_table = settings["key_overrides"]
        self.acquisition_modes = settings["acquisition_modes"]
        self.roles_cfg = settings["roles"]
        self.moderators = settings["moderators"]["rules"]
        self.applicability = settings["applicability"]
        self.certifications = settings["expected_certifications_by_ateco"]
        self.distress = settings["distress"]
        self.interactions = settings["interactions"]
        self.status_mappings = settings["status_mappings"]["variables"]
        self.source_it = settings["source_it"]["variables"]
        self.groupings = settings["redundancy_groups"]
        self.validate()

    # ---- construction
    @classmethod
    def load(cls, workbook: Path = DEFAULT_WORKBOOK, settings_dir: Path = SETTINGS_DIR) -> "IndicatorRegistry":
        names = ["key_overrides", "acquisition_modes", "roles", "moderators", "applicability",
                 "expected_certifications_by_ateco", "distress", "interactions", "status_mappings",
                 "source_it", "redundancy_groups"]
        settings = {n: load_json(f"{n}.json", settings_dir) for n in names}
        specs = read_workbook(workbook, settings["key_overrides"])
        return cls(specs, settings, workbook)

    # ---- basic lookups
    @property
    def keys(self) -> List[str]:
        return [s.key for s in self.specs]

    def role(self, key: str) -> str:
        return self.roles_cfg.get("role_overrides", {}).get(key) or self.by_key[key].role

    def axes_of(self, key: str) -> List[str]:
        return axes_for(self.by_key[key].tag.axis)

    def keys_with_role(self, role: str) -> List[str]:
        return [k for k in self.keys if self.role(k) == role]

    @property
    def scored_keys(self) -> List[str]:
        return self.keys_with_role("Scored")

    @property
    def gate_keys(self) -> List[str]:
        return self.keys_with_role("Gate")

    @property
    def moderator_keys(self) -> List[str]:
        return self.keys_with_role("Moderator")

    def caveat_keys(self) -> List[str]:
        return [k for k in self.scored_keys if self.by_key[k].tag.readiness_caveat]

    def tier(self, key: str) -> str:
        return tier_for_mode(self.by_key[key].acquisition_mode, self.acquisition_modes)

    def mapping(self, key: str) -> Dict[str, Any]:
        return self.status_mappings[key]

    # ---- redundancy groups
    @property
    def grouping_id(self) -> str:
        return self.groupings["active_grouping_id"]

    @property
    def grouping(self) -> Dict[str, str]:
        return self.groupings["groupings"][self.grouping_id]["groups"]

    def group_of(self, key: str) -> Optional[str]:
        """None means the variable is its own group."""
        return self.grouping.get(key) or None

    @property
    def grouping_hash(self) -> str:
        blob = json.dumps(self.grouping, sort_keys=True).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()[:12]

    # ---- validation
    def validate(self) -> None:
        errs: List[str] = []
        keys = set(self.keys)

        for k in keys - set(self.status_mappings):
            errs.append(f"status_mappings.json has no entry for {k} (add one; direction must be explicit)")
        for k in set(self.status_mappings) - keys:
            errs.append(f"status_mappings.json entry {k} is not a variable in the workbook")
        for k in keys - set(self.source_it):
            errs.append(f"source_it.json has no entry for {k} (use status TO_MAP if unknown)")

        for key in keys & set(self.status_mappings):
            spec, role, m = self.by_key[key], self.role(key), self.status_mappings[key]
            if role == "Scored":
                for axis in self.axes_of(key):
                    has_axis = axis in m.get("axes", {})
                    has_state = any(axis in st for st in m.get("states", {}).values())
                    if not (has_axis or has_state):
                        errs.append(f"{key}: Scored on {axis} but status_mappings has no {axis} value")
                    if axis in m.get("axes", {}):
                        a = m["axes"][axis]
                        raises = a["trigger"] > a["not_trigger"]
                        if spec.tag.polarity == "Negative" and raises:
                            errs.append(f"{key}: tag is NEGATIVE but the mapping RAISES {axis}")
                        if spec.tag.polarity == "Standard" and not raises and not m.get("states"):
                            errs.append(f"{key}: tag is Standard but the mapping LOWERS {axis}")
                        if spec.tag.polarity == "Negative" and m.get("direction", {}).get(axis) != "lowers":
                            errs.append(f"{key}: Negative polarity needs direction.{axis} == 'lowers' written out")
                for axis in m.get("axes", {}):
                    if axis not in self.axes_of(key):
                        errs.append(f"{key}: status_mappings feeds {axis} but the tag axis is {spec.tag.axis}")
            else:
                if m.get("axes"):
                    errs.append(f"{key}: role {role} must not carry axis values")
            for lab, st in m.get("states", {}).items():
                for axis in st:
                    if axis not in ("NEED", "READINESS"):
                        errs.append(f"{key}: state {lab} names unknown axis {axis}")

        gates = set(self.roles_cfg.get("gates", {}))
        for k in set(self.gate_keys) - gates:
            errs.append(f"{k}: Role is Gate but roles.json has no gate definition")
        for k in gates - set(self.gate_keys):
            errs.append(f"{k}: roles.json defines a gate but the variable's role is {self.role(k) if k in keys else 'missing'}")
        for k, g in self.roles_cfg.get("gates", {}).items():
            if not is_number(g.get("fail_strength_at_least")):
                errs.append(f"{k}: gate fail_strength_at_least must be a number")

        for r in self.moderators:
            if r["moderator_key"] not in keys or self.role(r["moderator_key"]) != "Moderator":
                errs.append(f"moderator rule {r['id']}: {r['moderator_key']} is not a Moderator-role variable")
            eff = r["effect"]
            if eff["type"] == "weight_multiplier":
                for t in eff["targets"]:
                    if t not in keys or t not in self.scored_keys:
                        errs.append(f"moderator rule {r['id']}: target {t} is not a Scored variable")
                if r.get("enabled") and not is_number(eff.get("factor")):
                    errs.append(f"moderator rule {r['id']} is enabled but its factor is {eff.get('factor')!r}, not a number (TO_CALIBRATE)")
            elif eff["type"] != "note":
                errs.append(f"moderator rule {r['id']}: unknown effect type {eff['type']}")

        for rule in self.applicability.get("ateco_rules", []):
            if rule["variable_key"] not in keys:
                errs.append(f"applicability rule for unknown variable {rule['variable_key']}")
        for rule in self.applicability.get("attribute_rules", []):
            for k in rule["variable_keys"]:
                if k not in keys:
                    errs.append(f"applicability attribute rule for unknown variable {k}")

        if self.grouping_id not in self.groupings["groupings"]:
            errs.append(f"active_grouping_id {self.grouping_id!r} is not defined")
        else:
            for k in self.grouping:
                if k not in keys:
                    errs.append(f"redundancy grouping names unknown variable {k}")

        for k in self.interactions["asymmetric_variables"]["variables"]:
            if k not in keys:
                errs.append(f"asymmetric variable {k} is not in the workbook")

        if errs:
            raise ConfigError("Indicator config is inconsistent:\n  - " + "\n  - ".join(errs))

    # ---- applicability
    @staticmethod
    def ateco_digits(code: Optional[str]) -> str:
        return "".join(ch for ch in (code or "") if ch.isdigit())

    def applicability_of(self, key: str, context: "Any") -> Dict[str, Any]:
        """-> {"status": applicable | not_applicable | undetermined, "reason": str|None}"""
        digits = self.ateco_digits(getattr(context, "ateco_code", None))
        attrs = getattr(context, "attributes", None) or {}
        for rule in self.applicability.get("ateco_rules", []):
            if rule["variable_key"] != key:
                continue
            div = digits[:2]
            if len(div) < 2:
                return {"status": "undetermined", "reason": "no ATECO code on the company"}
            if div in rule["not_applicable_divisions"]:
                return {"status": "not_applicable", "reason": f"ATECO division {div} is not applicable for this variable"}
            if div in rule["applicable_divisions"]:
                return {"status": "applicable", "reason": None}
            return {"status": rule.get("unlisted", "undetermined"), "reason": f"ATECO division {div} is not listed in the applicability rule"}
        for rule in self.applicability.get("attribute_rules", []):
            if key in rule["variable_keys"]:
                for attr, bad in rule["not_applicable_if"].items():
                    if attr in attrs and attrs[attr] == bad:
                        return {"status": "not_applicable", "reason": rule["rationale"]}
        return {"status": "applicable", "reason": None}

    # ---- sector norm (row 74)
    def expected_certifications(self, ateco_code: Optional[str]) -> Dict[str, Any]:
        digits = self.ateco_digits(ateco_code)
        best = None
        for norm in self.certifications["sector_norms"]:
            for prefix in norm["ateco_prefixes"]:
                if digits.startswith(prefix) and (best is None or len(prefix) > best[0]):
                    best = (len(prefix), norm)
        if best:
            n = best[1]
            return {"id": n["id"], "expected_any_of": list(n["expected_any_of"]), "aliases": n.get("aliases", {}),
                    "status": n.get("status"), "basis": n.get("basis")}
        b = self.certifications["generic_baseline"]
        return {"id": "generic_baseline", "expected_any_of": list(b["expected_any_of"]), "aliases": {},
                "status": b.get("status"), "basis": b.get("basis")}

    # ---- housekeeping lists for reports
    def todo_markers(self) -> Dict[str, List[str]]:
        """Every TO_CALIBRATE / TO_MAP marker in the loaded config, as 'file: path' strings."""
        found = {TO_CALIBRATE: [], TO_MAP: []}

        def walk(obj: Any, trail: str, src: str) -> None:
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k.startswith("$comment") or k in ("note", "basis", "rationale", "reason_needed", "numeric_cutoff_reason",
                                                         "fail_strength_calibration", "assumption", "description"):
                        continue
                    walk(v, f"{trail}.{k}" if trail else k, src)
            elif isinstance(obj, list):
                for i, v in enumerate(obj):
                    walk(v, f"{trail}[{i}]", src)
            elif isinstance(obj, str):
                for marker in (TO_CALIBRATE, TO_MAP):
                    if obj == marker or obj.startswith(marker + ":") or obj.startswith(marker + " "):
                        found[marker].append(f"{src}: {trail}")
            return None

        walk(self.status_mappings, "", "status_mappings")
        walk(self.certifications, "", "expected_certifications_by_ateco")
        walk(self.moderators, "", "moderators")
        walk(self.distress, "", "distress")
        walk(self.interactions, "", "interactions")
        walk(self.roles_cfg, "", "roles")
        walk(self.source_it, "", "source_it")
        return found
