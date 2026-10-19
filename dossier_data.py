"""
Read-side data layer for the Company Intelligence dossier's NEW layers (everything the old page never showed):
the multi-year financial statement from the AIDA export, derived ratios, rule-based red flags and strengths,
the ownership/group facts, and the per-company data-gap list.

Pure functions over plain dicts (a RawImportRecord blob, a {signal_key: (value, status)} map), so each can be
unit-tested without a database or Streamlit. Nothing here estimates a missing figure: a value that is not in the
source is None, and a ratio needs both inputs.

All money is thousand EUR, exactly as AIDA exports it.
"""

from datetime import date
from typing import Any, Dict, List, Optional, Tuple

PERIODS = ["latest", "y-1", "y-2"]            # newest first, as the AIDA export names them
AIDA_DATASET = "AIDA Raw Exports"

# (key in the blob, label) - the income statement, then the balance sheet, then workforce/other
INCOME_LINES = [
    ("revenue", "Revenue"), ("production_value", "Value of production"), ("gross_margin", "Gross margin"),
    ("ebitda", "EBITDA"), ("ebit", "EBIT"), ("net_income", "Net income"),
    ("production_costs", "Total production costs"), ("personnel_costs", "Personnel costs"),
    ("materials", "Raw materials & consumables"),
]
BALANCE_LINES = [
    ("total_assets", "Total assets"), ("equity", "Equity"),
    ("total_debt", "Total liabilities"),  # AIDA's 'total debt' is total liabilities (see vienna_valuation notes)
    ("cash", "Cash & equivalents"), ("intangibles", "Intangible assets"),
]
OTHER_LINES = [
    ("employees", "Employees"), ("interest_coverage", "Interest coverage (x)"), ("leverage_ratio", "Leverage (assets / equity, x)"),
    ("material_capex", "Capex - tangible"), ("immaterial_capex", "Capex - intangible"),
]


def _f(v) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None  # NaN -> None


def series(blob: dict, key: str) -> Dict[str, Optional[float]]:
    return {p: _f((blob or {}).get(f"{key}_{p}")) for p in PERIODS}


def _div(a: Optional[float], b: Optional[float], scale: float = 100.0) -> Optional[float]:
    if a is None or b is None or b == 0:
        return None
    return a / b * scale


def fiscal_year_labels(blob: dict) -> Dict[str, str]:
    """{'latest': 'FY2024', 'y-1': 'FY2023', ...} when the export says which year the latest accounts closed,
    otherwise neutral labels - never a guessed year."""
    year = _f((blob or {}).get("derived_last_accounts_year"))
    if year is None:
        close = str((blob or {}).get("accounts_close") or "")[:4]
        year = float(close) if close.isdigit() else None
    if year is None:
        return {"latest": "Latest", "y-1": "Year -1", "y-2": "Year -2"}
    y = int(year)
    return {"latest": f"FY{y}", "y-1": f"FY{y - 1}", "y-2": f"FY{y - 2}"}


def financial_statement(blob: Optional[dict]) -> Optional[Dict[str, Any]]:
    """The three-year statement + ratios from one company's AIDA blob, or None when there is no blob."""
    if not blob:
        return None
    lines = {k: series(blob, k) for k, _ in INCOME_LINES + BALANCE_LINES + OTHER_LINES}
    rev, ebitda, ebit, ni = lines["revenue"], lines["ebitda"], lines["ebit"], lines["net_income"]
    ratios: Dict[str, Dict[str, Optional[float]]] = {
        "ebitda_margin": {p: _div(ebitda[p], rev[p]) for p in PERIODS},
        "ebit_margin": {p: _div(ebit[p], rev[p]) for p in PERIODS},
        "net_margin": {p: _div(ni[p], rev[p]) for p in PERIODS},
        "gross_margin_pct": {p: _div(lines["gross_margin"][p], rev[p]) for p in PERIODS},
        "personnel_pct_revenue": {p: _div(lines["personnel_costs"][p], rev[p]) for p in PERIODS},
        "equity_ratio": {p: _div(lines["equity"][p], lines["total_assets"][p]) for p in PERIODS},
        "cash_pct_revenue": {p: _div(lines["cash"][p], rev[p]) for p in PERIODS},
        "revenue_per_employee": {p: _div(rev[p], lines["employees"][p], 1.0) for p in PERIODS},  # k EUR per head
    }
    growth: Dict[str, Optional[float]] = {}
    for i, p in enumerate(PERIODS):
        older = PERIODS[i + 1] if i + 1 < len(PERIODS) else None
        growth[p] = None if older is None or not rev[older] or rev[p] is None or rev[older] <= 0 \
            else (rev[p] - rev[older]) / rev[older] * 100.0
    ratios["revenue_growth"] = growth
    return {"labels": fiscal_year_labels(blob), "lines": lines, "ratios": ratios,
            "has_data": any(v is not None for s in lines.values() for v in s.values())}


# ----------------------------------------------------------------------------------------------- ownership

def ownership(blob: Optional[dict]) -> Dict[str, Any]:
    blob = blob or {}

    def rows(v):
        return [x for x in (v or []) if isinstance(x, dict)]

    sh = rows(blob.get("shareholders"))
    return {
        "shareholders": [{"Name": s.get("name"), "Type": s.get("type"), "Country": s.get("country"),
                          "Direct %": s.get("direct"), "Total %": s.get("total")} for s in sh],
        "controlling": [{"Name": s.get("name"), "Type": s.get("type"), "Level": s.get("level")}
                        for s in rows(blob.get("controlling_shareholders"))],
        "participations": [{"Name": s.get("name"), "Type": s.get("type")} for s in rows(blob.get("participations"))],
        "group_size": _f(blob.get("group_size")), "consolidation": blob.get("consolidation_code"),
        "independence": blob.get("bvd_independence_class"),
        "procedures": [str(p) for p in (blob.get("procedures") or [])],
    }


# --------------------------------------------------------------------------------------------- red flags

def flags(fin: Optional[Dict[str, Any]], signals: Dict[str, Tuple[Optional[float], str]], today: Optional[date] = None
          ) -> List[Dict[str, str]]:
    """Rule-based flags, each {'level': 'risk'|'watch'|'strength', 'text': ...}. Heuristics for triage, not verdicts:
    every threshold is stated in the text so a reader can disagree with it."""
    out: List[Dict[str, str]] = []
    today = today or date.today()

    def present(key):
        v = signals.get(key)
        return v[0] if v and v[1] == "present" and v[0] is not None else None

    distress = present("distress_procedure")
    if distress:
        out.append({"level": "risk", "text": "Formal distress procedure on record (insolvency, composition or liquidation without a closing date)."})
    year = present("last_accounts_year")
    if year is not None and today.year - int(year) >= 2:
        out.append({"level": "risk", "text": f"Latest filed accounts are from {int(year)} - every figure here is {today.year - int(year)} years old."})
    elif year is not None and today.year - int(year) == 1:
        out.append({"level": "watch", "text": f"Latest filed accounts are FY{int(year)} (the newest year most companies have filed)."})

    if fin and fin["has_data"]:
        L, R, lab = fin["lines"], fin["ratios"], fin["labels"]["latest"]
        eq, ta = L["equity"]["latest"], L["total_assets"]["latest"]
        if eq is not None and eq < 0:
            out.append({"level": "risk", "text": f"Negative equity in {lab} ({eq:,.0f}k)."})
        elif R["equity_ratio"]["latest"] is not None and R["equity_ratio"]["latest"] < 10:
            out.append({"level": "watch", "text": f"Thin equity: {R['equity_ratio']['latest']:.0f}% of total assets in {lab} (watch below 10%)."})
        elif R["equity_ratio"]["latest"] is not None and R["equity_ratio"]["latest"] >= 40:
            out.append({"level": "strength", "text": f"Strong capitalisation: equity is {R['equity_ratio']['latest']:.0f}% of total assets."})
        ni = L["net_income"]["latest"]
        if ni is not None and ni < 0:
            out.append({"level": "risk" if (L["net_income"]["y-1"] or 0) < 0 else "watch",
                        "text": f"Net loss in {lab} ({ni:,.0f}k)" + (" - second consecutive loss." if (L["net_income"]["y-1"] or 0) < 0 else ".")})
        g = R["revenue_growth"]["latest"]
        if g is not None and g <= -15:
            out.append({"level": "risk", "text": f"Revenue fell {abs(g):.0f}% in {lab} (flag at -15% or worse)."})
        elif g is not None and g <= -5:
            out.append({"level": "watch", "text": f"Revenue fell {abs(g):.0f}% in {lab}."})
        elif g is not None and g >= 10:
            out.append({"level": "strength", "text": f"Revenue grew {g:.0f}% in {lab}."})
        m = R["ebitda_margin"]["latest"]
        if m is not None and m < 0:
            out.append({"level": "risk", "text": f"Negative EBITDA margin ({m:.1f}%) in {lab}."})
        elif m is not None and m < 3:
            out.append({"level": "watch", "text": f"Very thin EBITDA margin ({m:.1f}%) in {lab}."})
        elif m is not None and m >= 15:
            out.append({"level": "strength", "text": f"Healthy EBITDA margin ({m:.1f}%) in {lab}."})
        ic = L["interest_coverage"]["latest"]
        if ic is not None and ic < 1.5:
            out.append({"level": "risk" if ic < 1 else "watch", "text": f"Interest coverage only {ic:.1f}x in {lab} (earnings barely cover interest below 1.5x)."})
        cash_pct = R["cash_pct_revenue"]["latest"]
        if cash_pct is not None and cash_pct < 2:
            out.append({"level": "watch", "text": f"Cash is only {cash_pct:.1f}% of annual revenue ({lab}) - under a week of sales."})
        e0, e2 = L["employees"]["latest"], L["employees"]["y-2"]
        if e0 is not None and e2 and e2 > 0 and (e0 - e2) / e2 <= -0.15:
            out.append({"level": "watch", "text": f"Headcount fell {abs((e0 - e2) / e2 * 100):.0f}% over two years ({e2:.0f} -> {e0:.0f})."})
    order = {"risk": 0, "watch": 1, "strength": 2}
    return sorted(out, key=lambda f: order[f["level"]])


# ----------------------------------------------------------------------------------------------- data gaps

def gap_rows(indicator_defs: dict, sig_dict: dict, plan: Optional[dict] = None, limit: Optional[int] = None) -> List[dict]:
    """Scored indicators this company has no value for, heaviest first, each with where the data could come from
    (scripts/indicator_gaps.py's GAP_PLAN). A context row never counts: it carries no weight."""
    plan = plan or {}
    rows = []
    for key, d in indicator_defs.items():
        if d.get("axis") == "context":
            continue
        s = sig_dict.get(key)
        if s is not None and s.status != "not_yet_checked":
            continue
        cls, where, effort = plan.get(key, ("UNCLASSIFIED", "no source planned yet", ""))
        rows.append({"Indicator": d.get("label") or key, "Axis": d.get("axis"), "Weight": float(d.get("weight") or 0),
                     "Category": d.get("category"), "Source class": cls, "Where the data would come from": where,
                     "Effort": effort})
    rows.sort(key=lambda r: (-r["Weight"], r["Indicator"]))
    return rows[:limit] if limit else rows
