"""Plain-text rendering of a score result. Every report prints the weight-set label."""

from __future__ import annotations

from typing import Any, Dict, List

from .schema import AXES


def format_report(result: Dict[str, Any]) -> str:
    ws = result["weight_set"]
    lines: List[str] = []
    banner = f"WEIGHT SET: {ws['id']}  [{ws['label']}]" if ws["status"] == "placeholder" else f"Weight set: {ws['id']} ({ws['status']})"
    lines.append(banner)
    lines.append(f"Redundancy grouping: {result['grouping']['id']} (hash {result['grouping']['hash']})")
    if result.get("company_id"):
        lines.append(f"Company: {result['company_id']}")
    lines.append("")
    for axis in AXES:
        s = result["scores"][axis]
        score = "n/a (no scorable observation)" if s["score"] is None else f"{s['score']:.1f}"
        cov = "n/a" if s["coverage_weight_pct"] is None else f"{s['coverage_weight_pct']:.1f}%"
        cc = s["coverage_count"]
        lines.append(f"{axis}: {score}   coverage {cov} by weight ({cc['scored']}/{cc['applicable']} applicable variables scored; "
                     f"{s['unknown']} unknown, {s['not_applicable']} not applicable)")
    lines.append("(NEED and READINESS are separate scores and are never combined.)")
    q = result["qualification_flag"]
    lines.append("")
    lines.append("QUALIFICATION FLAG: " + ("YES - route to extra qualification before proposing a pilot" if q["flagged"] else "no"))
    for r in q["reasons"]:
        lines.append(f"  - {r.get('variable_key') or ', '.join(r.get('variables', []))}: {r['reason']}")
    if any(v == "unknown" for v in q["gates"].values()):
        lines.append("  gates not yet evaluated: " + ", ".join(k for k, v in q["gates"].items() if v == "unknown"))
    c = result["readiness_caveat"]
    if c["flagged"]:
        lines.append("READINESS CAVEAT: " + ", ".join(v["variable_key"] for v in c["variables"])
                     + " (NEED signal that may limit practical readiness)")
    d = result["distress"]
    lines.append(f"Distress cap: {d['status']}" + (f" (candidates: {', '.join(d['candidates'])})" if d["candidates"] else ""))
    p = result["systems_posture"]
    lines.append("Systems posture (rows 63 x 72): " + (f"{p['cell']} - NEED tier {p['need_tier']}" if p["cell"] else f"undetermined ({p.get('reason', 'inputs missing')})"))
    for n in result["interpretation_notes"]:
        lines.append(f"Note: {n}")
    if result["applicability_undetermined"]:
        lines.append("Applicability undetermined for: " + ", ".join(result["applicability_undetermined"]))
    for w in result["warnings"]:
        lines.append(f"Warning: {w}")
    return "\n".join(lines)
