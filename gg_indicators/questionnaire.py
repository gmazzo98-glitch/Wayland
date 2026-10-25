"""
First-contact questionnaire section, generated from the Proxy text of every variable whose
Acquisition mode is "First contact only" (rows 11, 12, 32, 65, 70, 72, 73, 75 today). These are
loaded from the questionnaire, never scraped. Question wording is built from the sheet's own
Proxy text; nothing is invented.
"""

from __future__ import annotations

from typing import Any, Dict, List

from .registry import IndicatorRegistry

FIRST_CONTACT = "First contact only"
NOT_SHARED = {"code": "not_shared", "text": "Not known / not willing to share", "records_as": "unknown",
              "note": "Never scored as a low or bad value."}
NOT_APPLICABLE = {"code": "not_applicable", "text": "Does not apply to this company", "records_as": "not_applicable"}


def _sentence(text: str) -> str:
    t = " ".join((text or "").split()).rstrip(".")
    return t[:1].upper() + t[1:] + "." if t else ""


def _has_applicability_rule(reg: IndicatorRegistry, key: str) -> bool:
    return (any(r["variable_key"] == key for r in reg.applicability.get("ateco_rules", []))
            or any(key in r["variable_keys"] for r in reg.applicability.get("attribute_rules", [])))


def _options(reg: IndicatorRegistry, key: str) -> List[Dict[str, Any]]:
    m = reg.mapping(key)
    opts: List[Dict[str, Any]] = []
    named = [(lab, vals) for lab, vals in m["states"].items() if vals]
    if named or m.get("labels_only"):
        for lab, vals in named:
            opts.append({"code": lab, "text": lab.replace("_", " "), "records_as": "observed", "label": lab,
                         "axis_values": vals})
    else:
        opts.append({"code": "trigger", "text": m["trigger_status"], "records_as": "observed", "label": "trigger"})
        opts.append({"code": "not_trigger", "text": f"Not '{m['trigger_status']}'", "records_as": "observed", "label": "not_trigger"})
    opts.append(dict(NOT_SHARED))
    if _has_applicability_rule(reg, key):
        opts.append(dict(NOT_APPLICABLE))
    return opts


def build_first_contact_section(reg: IndicatorRegistry) -> Dict[str, Any]:
    items = []
    for s in reg.specs:
        if s.acquisition_mode != FIRST_CONTACT:
            continue
        items.append({
            "variable_key": s.key,
            "sheet_row": s.sheet_row,
            "role": reg.role(s.key),
            "axis": s.tag.axis,
            "question": f"{s.label}: {_sentence(s.proxy)}",
            "proxy_text": s.proxy,
            "sheet_status_text": s.status_text,
            "answer_options": _options(reg, s.key),
            "applicability_note": next((r.get("assumption") or r.get("rationale") for r in
                                        [*reg.applicability.get("ateco_rules", []), *reg.applicability.get("attribute_rules", [])]
                                        if s.key == r.get("variable_key") or s.key in r.get("variable_keys", [])), None),
            "source_it": reg.source_it[s.key]["sources"],
        })
    return {
        "section": "First-contact questionnaire (generated from the Proxy column)",
        "loaded_from": "first_contact_questionnaire",
        "recording_rule": "A blank or declined answer is recorded as unknown, never as a low value. 'Does not apply' is recorded as not_applicable.",
        "items": items,
    }


def render_markdown(section: Dict[str, Any]) -> str:
    out = [f"# {section['section']}", "", section["recording_rule"], ""]
    for it in section["items"]:
        out += [f"## {it['question']}", "",
                f"- Variable: `{it['variable_key']}` (Sheet1 row {it['sheet_row']}, {it['role']}, {it['axis']})",
                f"- Sheet1 status being tested: {it['sheet_status_text']}"]
        if it["applicability_note"]:
            out.append(f"- Applicability: {it['applicability_note']}")
        out.append("- Answers:")
        out += [f"  - [ ] {o['text']} -> {o['records_as']}" for o in it["answer_options"]]
        out.append("")
    return "\n".join(out)
