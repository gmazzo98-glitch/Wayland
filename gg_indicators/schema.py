"""
Loader for Indicators.xlsx -> the extended variable schema / data contract.

Sheet1 is the human-owned catalogue and is only ever READ (the workbook is opened
read-only and nothing here writes back to it). The `Scoring Schema` sheet supplies the
manual columns (Role, Acquisition mode, Redundancy group, Origin; the two weight columns
are deliberately empty and are never defaulted). Axis and polarity are re-derived here
from the bracket tag by the same rules the sheet's own formulas use, and cross-checked
against the sheet's cached formula results when Excel saved them.

This EXTENDS the existing catalog (indicators.INDICATOR_SEED / models.IndicatorDefinition):
every sheet row that already has a catalog signal key keeps that key as its variable_key
(see settings/key_overrides.json), and `catalog_diff` lists where the sheet and the
existing catalog disagree. Nothing here touches the database or the live scoring path.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

PKG_DIR = Path(__file__).resolve().parent
SETTINGS_DIR = PKG_DIR / "settings"
DEFAULT_WORKBOOK = PKG_DIR / "source" / "Indicators.xlsx"

SCHEMA_VERSION = "2026-10-gg-indicators-v1"

AXES = ("NEED", "READINESS")
ROLES = ("Scored", "Gate", "Moderator")
ACQUISITION_MODES = (
    "Registry or filing",
    "Web or job board scrape",
    "Manual desk research",
    "First contact only",
)


class SchemaError(ValueError):
    """The workbook or a config file does not have the shape the loader relies on."""


# --------------------------------------------------------------------------- tag parsing

_TAG_RE = re.compile(r"^\s*\[([^\]]*)\]")
_CAVEAT_RE = re.compile(r"readiness\s+caveat", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedTag:
    raw: str
    axis: str            # NEED | READINESS | BOTH | CONTEXT
    polarity: str        # Standard | Negative
    readiness_caveat: bool  # a NEED variable that also warns about practical readiness
    modifiers: tuple     # other words in the tag (GATE, BASELINE, GG-FIT, MODERATOR, caveat ...), informational


def extract_tag(indicator_text: str) -> str:
    """The text between the leading '[' and the first ']' (the sheet's MID/FIND formula)."""
    m = _TAG_RE.match(indicator_text or "")
    if not m:
        raise SchemaError(f"Indicator text does not start with a bracketed axis tag: {(indicator_text or '')[:60]!r}")
    return m.group(1)


def parse_tag(raw_tag: str) -> ParsedTag:
    """
    Axis: contains CONTEXT -> CONTEXT; else BOTH -> BOTH; else uppercase READINESS -> READINESS;
    else NEED. The tests are case sensitive ON PURPOSE (exactly the Scoring Schema formulas):
    '[NEED, readiness caveat]' is a NEED variable with a caveat, not a READINESS one.
    Polarity: Negative when the tag contains NEGATIVE, else Standard. The separator inside the
    tag (comma, spaced hyphen, slash) never matters because every test is a substring test.
    """
    if "CONTEXT" in raw_tag:
        axis = "CONTEXT"
    elif "BOTH" in raw_tag:
        axis = "BOTH"
    elif "READINESS" in raw_tag:
        axis = "READINESS"
    else:
        axis = "NEED"
    polarity = "Negative" if "NEGATIVE" in raw_tag else "Standard"
    caveat = axis == "NEED" and bool(_CAVEAT_RE.search(raw_tag))
    words = [w.strip() for w in re.split(r"[,/]|\s-\s", raw_tag) if w.strip()]
    structural = {"NEED", "READINESS", "BOTH", "CONTEXT", "NEGATIVE"}
    modifiers = tuple(w for w in words if w not in structural and not (caveat and _CAVEAT_RE.fullmatch(w)))
    return ParsedTag(raw=raw_tag, axis=axis, polarity=polarity, readiness_caveat=caveat, modifiers=modifiers)


def axes_for(axis: str) -> List[str]:
    """Which scored axes a tag axis feeds. CONTEXT feeds none."""
    return {"NEED": ["NEED"], "READINESS": ["READINESS"], "BOTH": ["NEED", "READINESS"], "CONTEXT": []}[axis]


# --------------------------------------------------------------------------- variable keys

def derive_variable_key(label: str) -> str:
    """snake_case of the Variable text. Only the fallback: key_overrides.json wins (see resolve_key)."""
    s = re.sub(r"\([^)]*\)", " ", label or "")
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()
    if not s:
        raise SchemaError(f"Cannot derive a key from label {label!r}")
    return s


def _norm_label(label: str) -> str:
    return re.sub(r"\s+", " ", (label or "").strip()).lower()


def load_json(name: str, directory: Path = SETTINGS_DIR) -> Dict[str, Any]:
    path = directory / name
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SchemaError(f"Missing config file {path}") from exc


def resolve_key(label: str, sheet_row: int, table: Dict[str, Any]) -> Dict[str, Any]:
    """
    Stable key lookup. 1) exact label match in the persisted table; 2) same sheet row (a renamed
    label keeps its key); 3) a derived key, flagged so the caller can persist it. Renaming a label
    in the sheet therefore never changes a key.
    """
    entries = table["keys"]
    nl = _norm_label(label)
    for e in entries:
        if _norm_label(e["label"]) == nl:
            return {"key": e["key"], "key_source": "override_label"}
    for e in entries:
        if e["row"] == sheet_row:
            return {"key": e["key"], "key_source": "override_row_label_renamed",
                    "previous_label": e["label"]}
    return {"key": derive_variable_key(label), "key_source": "derived_unpersisted"}


# --------------------------------------------------------------------------- workbook

@dataclass
class VariableSpec:
    sheet_row: int
    label: str
    status_text: str
    proxy: str
    indicator_text: str
    comment: str
    source_text: str
    tag: ParsedTag
    role: str
    acquisition_mode: str
    redundancy_group_sheet: Optional[str]
    origin: Optional[str]
    sheet_weights: Dict[str, Optional[float]]
    key: str = ""
    key_source: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def axes(self) -> List[str]:
        return axes_for(self.tag.axis)


def _cell(v: Any) -> str:
    return "" if v is None else str(v).strip()


def read_workbook(path: Path = DEFAULT_WORKBOOK, key_table: Optional[Dict[str, Any]] = None) -> List[VariableSpec]:
    """Reads Sheet1 + Scoring Schema (never writes). Rows are matched by position, as the sheet's formulas do."""
    import openpyxl

    key_table = key_table or load_json("key_overrides.json")
    wb_f = openpyxl.load_workbook(path, read_only=False, data_only=False)
    wb_v = openpyxl.load_workbook(path, read_only=False, data_only=True)
    try:
        for name in ("Sheet1", "Scoring Schema"):
            if name not in wb_f.sheetnames:
                raise SchemaError(f"{path.name} has no sheet {name!r} (found {wb_f.sheetnames})")
        s1, ss = wb_f["Sheet1"], wb_f["Scoring Schema"]
        ssv = wb_v["Scoring Schema"]

        header = [_cell(c.value) for c in s1[1]][:6]
        if header != ["Variable", "Status", "Proxy", "Indicator", "Comment", "Source"]:
            raise SchemaError(f"Unexpected Sheet1 header: {header}")
        ss_header = [_cell(c.value) for c in ss[1]][:11]
        expected = ["Sheet1 row", "Variable", "Axis tag (from Sheet1)", "Axis", "Polarity", "Role",
                    "Acquisition mode", "Redundancy group (proposed)", "Weight on NEED axis",
                    "Weight on READINESS axis", "Origin"]
        if ss_header != expected:
            raise SchemaError(f"Unexpected Scoring Schema header: {ss_header}")

        specs: List[VariableSpec] = []
        r = 2
        while _cell(s1.cell(r, 1).value):
            label = _cell(s1.cell(r, 1).value)
            ind = _cell(s1.cell(r, 4).value)
            tag = parse_tag(extract_tag(ind))

            def sc(col: int) -> Any:
                return ss.cell(r, col).value

            role, mode = _cell(sc(6)), _cell(sc(7))
            if role not in ROLES:
                raise SchemaError(f"Row {r} ({label}): Role {role!r} is not one of {ROLES}")
            if mode not in ACQUISITION_MODES:
                raise SchemaError(f"Row {r} ({label}): Acquisition mode {mode!r} is not one of {ACQUISITION_MODES}")
            if _cell(ss.cell(r, 2).value) not in ("", f"=Sheet1!A{r}"):
                raise SchemaError(f"Scoring Schema row {r} no longer lines up with Sheet1 row {r}")

            # Cross-check our parse against the sheet's own cached formula results (when Excel saved them).
            cached_axis, cached_pol = ssv.cell(r, 4).value, ssv.cell(r, 5).value
            if cached_axis is not None and cached_axis != tag.axis:
                raise SchemaError(f"Row {r} ({label}): parsed axis {tag.axis} != sheet formula result {cached_axis}")
            if cached_pol is not None and cached_pol != tag.polarity:
                raise SchemaError(f"Row {r} ({label}): parsed polarity {tag.polarity} != sheet formula result {cached_pol}")

            def num(v):
                return None if v in (None, "") else float(v)

            spec = VariableSpec(
                sheet_row=r, label=label, status_text=_cell(s1.cell(r, 2).value),
                proxy=_cell(s1.cell(r, 3).value), indicator_text=ind,
                comment=_cell(s1.cell(r, 5).value), source_text=_cell(s1.cell(r, 6).value),
                tag=tag, role=role, acquisition_mode=mode,
                redundancy_group_sheet=_cell(sc(8)) or None, origin=_cell(sc(11)) or None,
                sheet_weights={"NEED": num(sc(9)), "READINESS": num(sc(10))},
            )
            res = resolve_key(label, r, key_table)
            spec.key, spec.key_source = res["key"], res["key_source"]
            if "previous_label" in res:
                spec.extra["previous_label"] = res["previous_label"]
            specs.append(spec)
            r += 1

        keys = [s.key for s in specs]
        dupes = sorted({k for k in keys if keys.count(k) > 1})
        if dupes:
            raise SchemaError(f"Duplicate variable_key(s): {dupes}")
        return specs
    finally:
        wb_f.close()
        wb_v.close()


# --------------------------------------------------------------------------- catalog bridge

def tier_for_mode(mode: str, table: Optional[Dict[str, Any]] = None) -> str:
    table = table or load_json("acquisition_modes.json")
    return table["modes"][mode]["automation_tier"]


_CATALOG_AXIS = {"need": "NEED", "readiness": "READINESS", "both": "BOTH", "context": "CONTEXT"}


def catalog_diff(spec: VariableSpec, seed_by_key: Dict[str, Dict[str, Any]], tier: str) -> Dict[str, Any]:
    """Where the sheet row and the existing INDICATOR_SEED row disagree (reported, never auto-resolved)."""
    seed = seed_by_key.get(spec.key)
    if seed is None:
        return {"in_catalog_seed": False, "differences": []}
    diffs: List[str] = []
    cat_axis = _CATALOG_AXIS.get(seed.get("axis"), seed.get("axis"))
    if cat_axis != spec.tag.axis:
        diffs.append(f"axis: sheet {spec.tag.axis} vs catalog {cat_axis}")
    if bool(seed.get("is_gate")) != (spec.role == "Gate"):
        diffs.append(f"role: sheet {spec.role} vs catalog is_gate={bool(seed.get('is_gate'))}")
    if spec.role == "Moderator" and cat_axis != "CONTEXT":
        diffs.append(f"role: sheet Moderator but catalog scores it on axis {cat_axis}")
    if spec.role == "Scored" and cat_axis == "CONTEXT":
        diffs.append("role: sheet Scored but catalog treats it as context (unscored)")
    if seed.get("automation_tier") and seed["automation_tier"] != tier:
        diffs.append(f"automation_tier: sheet mode {spec.acquisition_mode!r} -> {tier} vs catalog {seed['automation_tier']}")
    cat_group = seed.get("redundancy_group")
    if (spec.redundancy_group_sheet or None) != (cat_group or None):
        diffs.append(f"redundancy_group: sheet {spec.redundancy_group_sheet!r} vs catalog {cat_group!r}")
    return {"in_catalog_seed": True, "catalog_axis": cat_axis, "catalog_weight": seed.get("weight"),
            "catalog_invert": bool(seed.get("invert")), "catalog_is_gate": bool(seed.get("is_gate")),
            "catalog_gate_penalty_multiplier": seed.get("gate_penalty_multiplier", 1.0) if seed.get("is_gate") else None,
            "differences": diffs}


def own_group(key: str) -> str:
    """Variables with no redundancy group are their own group."""
    return f"SOLO:{key}"
