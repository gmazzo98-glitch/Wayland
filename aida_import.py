"""
Imports the ORIGINAL AIDA exports (the six WAYLAND_*.xls files) straight into signals, instead of going through
the hand-curated "Main" spreadsheet — which had columns shifted (`cogs`), swapped (`leverage_ratio` history) or
left empty (`total_assets`, `subsidiary_count`, `legal_procedure_flag`, `last_filing_date`) even though the raw
exports carry all of them. See data_repairs.py for the audit that found this.

How it stays correct
  * Columns are found by HEADER NAME ("TOTALE ATTIVO | migl EUR | Anno - 1"), never by position: the export
    orders its three years Ultimo, Anno-2, Anno-1 for some columns and Ultimo, Anno-1, Anno-2 for others, which
    is exactly how the curated file got them swapped.
  * Every value is derived by a small pure function with its inputs and formula recorded on the SignalRecord
    (`raw_payload_ref.evidence`), so any number can be audited on the company page.
  * Nothing is guessed. A ratio with a zero/missing denominator is left not_yet_checked; a shareholder split is
    written only when the disclosed shareholders cover at least 75% of the equity.
  * A signal a human entered by hand, or that any other producer wrote, is never overwritten; it is reported
    as a conflict. Only rows that are still empty, simulated, or already AIDA-owned are written.
  * Any extra financial series present in the exports but not wired to an indicator is kept, untouched, in the
    stored raw row (`x_<name>_latest` / `_y-1` / `_y-2`) — so the next AIDA export with more columns loses
    nothing, and a new indicator can be applied retroactively without re-exporting.

The management/board indicators are computed from the people already imported (`run_management_pass`), because
that computation otherwise only ever ran when someone opened a company page.
"""

import json
import math
import re
import unicodedata
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd
from sqlalchemy.orm import Session

from indicators import SRC_AIDA
from models import Company, RawImportRecord, SignalRecord, SourceHealth

DATASET_NAME = SRC_AIDA           # "AIDA Raw Exports": the blob dataset, the signals' source and the SourceHealth row
MIN_DISCLOSED_OWNERSHIP = 75.0    # % of equity the shareholder list must account for before a share is written

EXPORT_PATTERNS = {
    "PL": "WAYLAND_FINANCIAL_PL*.xls",
    "CAP": "WAYLAND_FINANCIAL_CAPACITY*.xls",
    "SPINE": "WAYLAND_ENTITY_SPINE*.xls",
    "STRUCT": "WAYLAND_STRUCTURE_LEGAL_OWNERSHIP*.xls",
    "SHARE": "WAYLAND_SHAREHOLDERS_CONTROL*.xls",
}

# (our name, export, first part of the AIDA header). Each has three years: latest, y-1, y-2.
SERIES_SPEC = [
    ("revenue", "PL", "Ricavi vendite e prestazioni"),
    ("production_value", "PL", "TOT. VAL. DELLA PRODUZIONE"),
    ("ebitda", "PL", "EBITDA"),
    ("ebit", "PL", "RISULTATO OPERATIVO"),
    ("net_income", "PL", "Utile Netto"),
    ("employees", "PL", "Dipendenti"),
    ("production_costs", "PL", "COSTI DELLA PRODUZIONE"),
    ("personnel_costs", "PL", "Totale costi del personale"),
    ("gross_margin", "PL", "Margine sui consumi"),
    ("materials", "PL", "Materie prime e consumo"),
    ("cash", "CAP", "TOT. DISPON. LIQUIDE"),
    ("total_debt", "CAP", "TOTALE DEBITI"),
    ("leverage_ratio", "CAP", "Rapporto di indebitamento"),
    ("interest_coverage", "CAP", "Grado di copertura degli interessi passivi"),
    ("material_capex", "CAP", "Immobilizzazioni materiali: (Investimenti)"),
    ("immaterial_capex", "CAP", "Immobilizzazioni immateriali: (Investimenti)"),
    ("total_assets", "CAP", "TOTALE ATTIVO"),
    ("intangibles", "CAP", "TOTALE IMMOB. IMMATERIALI"),
    ("equity", "CAP", "TOTALE PATRIMONIO NETTO"),
]
YEAR_SUFFIXES = {"ultimo anno disp.": "latest", "anno - 1": "y-1", "anno - 2": "y-2"}

# How each raw series is described to the mapping snapshot the rest of the app reads (same base names the
# curated import used, so the company page's trend headlines and data_repairs keep working).
MAPPING_SNAPSHOT = {
    "revenue": "indicator:revenue_trend", "ebit": "indicator:ebit_trend", "ebitda": "indicator:ebitda_trend",
    "gross_margin": "indicator:margin_compression", "cash": "indicator:cash_position", "total_debt": "indicator:debt_level",
    "total_assets": "indicator:total_assets", "interest_coverage": "indicator:interest_coverage_ratio",
    "leverage_ratio": "indicator:leverage_ratio", "production_costs": "indicator:cogs",
    "material_capex": "indicator:material_capex", "immaterial_capex": "indicator:immaterial_capex",
}


# ---- reading -------------------------------------------------------------------------------------------

def _fold(s) -> str:
    """Lower-case, accent-free, single-spaced — so 'società' == 'Societa' and a stray newline can't hide a header."""
    s = unicodedata.normalize("NFKD", str(s))
    return re.sub(r"\s+", " ", "".join(c for c in s if not unicodedata.combining(c))).strip().lower()


def normalize_header(col) -> str:
    return re.sub(r"\s*\n\s*", " | ", str(col)).strip()


def find_column(df: pd.DataFrame, *needles: str) -> Optional[str]:
    """First column whose folded header contains every needle (folded)."""
    wanted = [_fold(n) for n in needles]
    for col in df.columns:
        folded = _fold(col)
        if all(w in folded for w in wanted):
            return col
    return None


def series_columns(df: pd.DataFrame, name: str) -> Dict[str, str]:
    """{'latest': col, 'y-1': col, 'y-2': col} for the series whose first header part is `name`."""
    want, out = _fold(name), {}
    for col in df.columns:
        parts = [p.strip() for p in str(col).split("|")]
        if len(parts) >= 2 and _fold(parts[0]) == want:
            suffix = YEAR_SUFFIXES.get(_fold(parts[-1]))
            if suffix:
                out[suffix] = col
    return out


def read_export(path: Path) -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name="Risultati")
    df.columns = [normalize_header(c) for c in df.columns]
    df = df.drop_duplicates("BvD ID number")
    df["BvD ID number"] = df["BvD ID number"].astype(str).str.strip()
    return df.set_index("BvD ID number")


def load_exports(data_dir: Path) -> Dict[str, pd.DataFrame]:
    out = {}
    for key, pattern in EXPORT_PATTERNS.items():
        found = sorted(Path(data_dir).glob(pattern))
        if not found:
            raise FileNotFoundError(f"{pattern} not found in {data_dir}")
        out[key] = read_export(found[-1])
    return out


# ---- small pure helpers -----------------------------------------------------------------------------------

def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _lines(v) -> List[str]:
    return [x.strip() for x in str(v).split("\n")] if v is not None and not (isinstance(v, float) and math.isnan(v)) else []


def _k(x: float) -> str:
    return f"{x:,.0f}k"


def pct_change(latest: Optional[float], base: Optional[float]) -> Optional[float]:
    """Sign-safe % change (divided by the absolute base, so a loss turning smaller reads as an improvement)."""
    if latest is None or base is None or base == 0:
        return None
    return (latest - base) / abs(base) * 100.0


def ratio(num: Optional[float], den: Optional[float], scale: float = 100.0) -> Optional[float]:
    if num is None or den is None or den <= 0:
        return None
    return num / den * scale


def pct_token(token: str) -> Optional[float]:
    """One cell of an AIDA ownership-percentage column. BvD codes: WO wholly owned, MO majority owned (>50%),
    NG negligible, n.d. not available, '-' none; a leading '>' marks a lower bound."""
    t = (token or "").strip()
    if t == "WO":
        return 100.0
    if t == "MO":
        return 50.01
    m = re.match(r"^>?\s*([0-9]+(?:[.,][0-9]+)?)\s*$", t)
    return float(m.group(1).replace(",", ".")) if m else None


_DISTRESS_RE = re.compile(
    r"concordat|misure cautelari|liquidazione|scioglimento|fallimento|procedimento unitario|ristrutturazion|"
    r"amministrazione (straordinaria|controllata|giudiziaria)|composizione (della )?crisi|risanamento|cancellazione",
    re.IGNORECASE)


def is_distress_procedure(text: str) -> bool:
    """A registry 'Procedura/Cessazione' entry that is distress or wind-down. Relocations ('Trasferimento in altra
    provincia'), mergers and the like share the column and are NOT distress."""
    return bool(_DISTRESS_RE.search(text or ""))


def parse_procedures(procedures, starts, closings) -> dict:
    """
    -> {"names": [distinct procedure texts], "distress": [distinct distress-type texts], "closings": [dates],
        "open": int, "flag": 0/1}.
    'Open' is INFERRED: distress entries minus recorded closing dates. The registry does not pair a closing date
    with a specific entry, so this is deliberately approximate and the evidence text says so.
    """
    names = list(dict.fromkeys(_lines(procedures)))
    distress = [n for n in names if n and is_distress_procedure(n)]
    closed = list(dict.fromkeys(x for x in _lines(closings) if x and x.lower() not in ("nan", "-", "n.d.")))
    open_n = max(0, len(distress) - len(closed))
    return {"names": names, "distress": distress, "closings": closed,
            "starts": [x for x in _lines(starts) if x and x.lower() != "nan"], "open": open_n, "flag": 1 if open_n > 0 else 0}


_NO_INFO = "no shareholders information"


def parse_shareholders(names, types, countries, direct, total, bvd_ids=None, immediate_names=None) -> dict:
    """
    The stacked 'Azionisti' cells (one line per shareholder, all columns aligned — verified equal length on every
    row of the real export) -> {"lines": [...], "disclosed": %, "family": %|None, "foreign": %|None, "foreign_countries": [..]}.

    A holder's stake is its DIRECT % when disclosed; when the direct % is 'n.d.' (unknown) its TOTAL % stands in; when it
    is '-' the holder is an INDIRECT owner whose stake is already inside its parent's, so it adds nothing (summing
    TOTAL % would double count: a parent and its own owners are both listed). The exception is
    individuals/families, whose TOTAL % is used: they usually own through a holding, so their direct stake is '-'
    while the total says what they really control — and individuals can't own each other, so the sum can't double count.
    """
    N, T, C, D, P = _lines(names), _lines(types), _lines(countries), _lines(direct), _lines(total)
    B, ISH = _lines(bvd_ids), [x for x in _lines(immediate_names) if x]
    lines = []
    for i in range(max(len(N), len(T), len(C), len(D), len(P))):
        nm = N[i] if i < len(N) else ""
        if _NO_INFO in nm.lower() and i < len(B) and B[i] and B[i].lower() != "nan":
            # The export prints its "no shareholders information" placeholder in the NAME cell for the immediate
            # shareholder (whose name lives in the ISH column) while every other cell of that line is real data:
            # a BvD id, a country, 100% direct. Dropping the line by its name lost the main holder of most companies.
            nm = ISH[0] if ISH else f"(name not in export, BvD {B[i]})"
        d_raw = D[i] if i < len(D) else ""
        d = pct_token(d_raw)
        t = pct_token(P[i]) if i < len(P) else None
        stake = d if d is not None else (t if d_raw.strip().lower() in ("n.d.", "") else None)
        lines.append({"name": nm, "type": T[i] if i < len(T) else "", "country": (C[i] if i < len(C) else "").upper(),
                      "direct": d, "total": t, "stake": stake})
    real = [l for l in lines if l["name"] and _NO_INFO not in l["name"].lower()]
    disclosed = sum(l["stake"] for l in real if l["stake"] is not None)

    def is_individual(l):
        return _fold(l["type"]).startswith("persone fisiche")

    family = sum((l["total"] if l["total"] is not None else (l["direct"] or 0.0)) for l in real if is_individual(l))
    foreign_lines = [l for l in real if l["country"] not in ("", "-", "IT") and l["stake"] is not None]
    foreign = sum(l["stake"] for l in foreign_lines)
    return {"lines": real, "disclosed": disclosed, "family": min(100.0, family), "foreign": min(100.0, foreign),
            "foreign_countries": sorted({l["country"] for l in foreign_lines})}


def independence_ordinal(letter) -> Optional[int]:
    """BvD independence class -> 1 (A, most independent) .. 4 (D). 'U' (unknown) and '-' are None."""
    first = (str(letter or "").strip()[:1]).upper()
    return {"A": 1, "B": 2, "C": 3, "D": 4}.get(first)


# ---- one company's signals ----------------------------------------------------------------------------------------

def _draft(value, text=None, basis="", inputs=None) -> dict:
    ok = value is not None and math.isfinite(float(value))
    return {"value": round(float(value), 4) if ok else None, "status": "present" if ok else "not_yet_checked",
            "text": text if ok else None, "basis": basis, "inputs": inputs or {}}


def derive_financial_signals(n: Dict[str, Optional[float]]) -> Dict[str, dict]:
    """`n` holds the raw series as {base_suffix: number}, e.g. n['revenue_latest']. Returns {indicator_key: draft}."""
    g = n.get
    rev_l, rev_b = g("revenue_latest"), g("revenue_y-2")
    out: Dict[str, dict] = {}

    def add(key, value, text, basis, **inputs):
        out[key] = _draft(value, text, basis, inputs)

    # -- trends: latest vs the furthest-back year, exactly as the curated import did
    for key, base, label in (("revenue_trend", "revenue", "revenue"), ("ebit_trend", "ebit", "EBIT"), ("ebitda_trend", "ebitda", "EBITDA")):
        l, b = g(f"{base}_latest"), g(f"{base}_y-2")
        v = pct_change(l, b)
        add(key, v, f"{label} {_k(b)} → {_k(l)} (earliest → latest year)" if v is not None else None,
            f"% change of {label}, latest vs earliest of the last three years, divided by the absolute base",
            latest=l, base=b)

    gm_l, gm_b = g("gross_margin_latest"), g("gross_margin_y-2")
    pm_l, pm_b = ratio(gm_l, rev_l), ratio(gm_b, rev_b)
    v = None if pm_l is None or pm_b is None or abs(pm_l) > 100 or abs(pm_b) > 100 else max(0.0, pm_b - pm_l)
    add("margin_compression", v, f"gross margin {pm_b:.1f}% → {pm_l:.1f}% of revenue" if v is not None else None,
        "percentage points of revenue: 'Margine sui consumi' ÷ revenue at the earliest and latest year, the fall between them, floored at 0",
        margin_latest=gm_l, margin_base=gm_b, revenue_latest=rev_l, revenue_base=rev_b)

    # -- levels taken as delivered
    add("interest_coverage_ratio", g("interest_coverage_latest"), f"interest cover {g('interest_coverage_latest')}×" if g("interest_coverage_latest") is not None else None,
        "AIDA 'Grado di copertura degli interessi passivi', latest year; 'n.s.' (not significant) is left unchecked")
    add("leverage_ratio", g("leverage_ratio_latest"), "total assets ÷ equity (AIDA 'Rapporto di indebitamento')",
        "AIDA 'Rapporto di indebitamento' = total assets ÷ equity; negative means negative equity")
    add("cash_position", g("cash_latest"), f"cash {_k(g('cash_latest'))}" if g("cash_latest") is not None else None, "AIDA 'TOT. DISPON. LIQUIDE', latest year, k EUR")
    add("debt_level", g("total_debt_latest"), f"total debt {_k(g('total_debt_latest'))}" if g("total_debt_latest") is not None else None, "AIDA 'TOTALE DEBITI', latest year, k EUR")
    add("total_assets", g("total_assets_latest"), f"total assets {_k(g('total_assets_latest'))}" if g("total_assets_latest") is not None else None, "AIDA 'TOTALE ATTIVO', latest year, k EUR")
    add("cogs", g("production_costs_latest"), "total costs of production (not COGS)", "AIDA 'COSTI DELLA PRODUZIONE', latest year, k EUR — total production costs, NOT cost of goods sold")
    add("material_capex", g("material_capex_latest"), None, "AIDA 'Immobilizzazioni materiali: (Investimenti)', latest year, k EUR (outflows are negative)")
    add("immaterial_capex", g("immaterial_capex_latest"), None, "AIDA 'Immobilizzazioni immateriali: (Investimenti)', latest year, k EUR (outflows are negative)")

    # -- ratios that need a denominator
    ebit_l, ni_l, cash_l = g("ebit_latest"), g("net_income_latest"), g("cash_latest")
    emp_l, emp_b = g("employees_latest"), g("employees_y-2")
    pers_l, mat_l, ta_l, intang_l = g("personnel_costs_latest"), g("materials_latest"), g("total_assets_latest"), g("intangibles_latest")

    v = ratio(ebit_l, rev_l)
    add("ebit_margin", v, f"EBIT {_k(ebit_l)} ÷ revenue {_k(rev_l)}" if v is not None else None, "EBIT ÷ revenue, latest year (%)", ebit=ebit_l, revenue=rev_l)
    v = ratio(ni_l, rev_l)
    add("net_margin", v, f"net income {_k(ni_l)} ÷ revenue {_k(rev_l)}" if v is not None else None, "net income ÷ revenue, latest year (%)", net_income=ni_l, revenue=rev_l)
    v = None if cash_l is None or rev_l is None or rev_l <= 0 else cash_l / (rev_l / 12.0)
    add("cash_to_revenue", v, f"cash {_k(cash_l)} ÷ monthly revenue {_k(rev_l / 12)}" if v is not None else None,
        "cash ÷ (revenue ÷ 12), latest year: months of sales held in cash", cash=cash_l, revenue=rev_l)
    v = ratio(intang_l, ta_l)
    add("intangibles_share", v, f"intangibles {_k(intang_l)} ÷ total assets {_k(ta_l)}" if v is not None else None,
        "intangible fixed assets ÷ total assets, latest year (%)", intangibles=intang_l, total_assets=ta_l)
    v = None if rev_l is None or emp_l is None or emp_l <= 0 else rev_l / emp_l
    add("revenue_per_employee", v, f"revenue {_k(rev_l)} ÷ {emp_l:,.0f} employees" if v is not None else None,
        "revenue ÷ employees, latest year (k EUR)", revenue=rev_l, employees=emp_l)
    v = pct_change(emp_l, emp_b)
    add("employee_growth", v, f"{emp_b:,.0f} → {emp_l:,.0f} employees" if v is not None else None,
        "% change in headcount, latest vs earliest of the last three years", latest=emp_l, base=emp_b)
    v = ratio(pers_l, rev_l)
    add("labour_cost", v, f"personnel cost {_k(pers_l)} ÷ revenue {_k(rev_l)}" if v is not None else None,
        "'Totale costi del personale' ÷ revenue, latest year (%)", personnel=pers_l, revenue=rev_l)
    v = ratio(mat_l, rev_l)
    add("materials_cost", v, f"materials {_k(mat_l)} ÷ revenue {_k(rev_l)}" if v is not None else None,
        "'Materie prime e consumo' ÷ revenue, latest year (%)", materials=mat_l, revenue=rev_l)
    v = None if pers_l is None or emp_l is None or emp_l <= 0 else pers_l / emp_l
    add("average_salary", v, f"personnel cost {_k(pers_l)} ÷ {emp_l:,.0f} employees" if v is not None else None,
        "'Totale costi del personale' ÷ employees, latest year (k EUR per employee)", personnel=pers_l, employees=emp_l)
    parts = [x for x in (g("material_capex_latest"), g("immaterial_capex_latest")) if x is not None]
    capex = -sum(parts) if parts else None
    v = ratio(capex, rev_l)
    add("capex_ratio", v, f"capex {_k(capex)} ÷ revenue {_k(rev_l)}" if v is not None else None,
        "-(material + immaterial capex) ÷ revenue, latest year (%); AIDA reports investments as negative outflows, and a missing component counts as none",
        capex=capex, revenue=rev_l)
    return out


def derive_structure_signals(*, participations, procedures: dict, independence, group_size, accounts_close, shareholders: dict) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    n_part = _num(participations)
    out["subsidiary_participations"] = _draft(n_part, f"{int(n_part)} participation(s) on record" if n_part is not None else None,
                                              "AIDA 'Numero di partecipazioni disponibili' (0 = none on record)")

    if procedures["distress"]:
        text = ("; ".join(procedures["distress"]) + f" — {procedures['open']} of {len(procedures['distress'])} without a recorded closing date"
                + (f" (closing dates: {', '.join(procedures['closings'])})" if procedures["closings"] else "") + ". 'Open' is inferred.")
    else:
        text = "no distress-type procedure on record" + (f" (other entries: {'; '.join(procedures['names'])})" if procedures["names"] else "")
    out["distress_procedure"] = _draft(float(procedures["flag"]), text,
                                       "1 when a distress-type 'Procedura/Cessazione' entry has no recorded closing date, else 0",
                                       {"entries": procedures["names"], "closings": procedures["closings"]})

    year = accounts_close.year if accounts_close is not None else None
    out["last_accounts_year"] = _draft(year, f"latest accounts closed {accounts_close:%Y-%m-%d}" if year else None,
                                       "year of AIDA 'Data di chiusura ultimo bilancio'")
    ordinal = independence_ordinal(independence)
    out["bvd_independence"] = _draft(ordinal, f"class {independence} (A = most independent … D)" if ordinal else None,
                                     "BvD independence class as an ordinal: A=1, B=2, C=3, D=4; 'U'/'-' unknown")
    gs = _num(group_size)
    out["group_size"] = _draft(gs, (f"{int(gs)} companies in the group" if gs and gs > 1 else "standalone: no corporate group on record") if gs is not None else None,
                               "AIDA 'N. di società nel gruppo societario' (0 or 1 = standalone)")

    covered = shareholders["disclosed"] >= MIN_DISCLOSED_OWNERSHIP
    top = ", ".join(f"{l['name']} ({l['stake']:.0f}%)" for l in sorted(
        (l for l in shareholders["lines"] if l["stake"] is not None), key=lambda l: -l["stake"])[:3])
    note = f"disclosed holders cover {shareholders['disclosed']:.0f}% of equity; largest: {top or 'n/a'}"
    out["family_ownership_share"] = _draft(shareholders["family"] if covered else None, f"individuals/families {shareholders['family']:.0f}% — {note}",
                                           "sum of the TOTAL % of shareholders typed 'Persone fisiche o famiglie', capped at 100; written only if the disclosed list covers ≥75% of equity")
    out["foreign_ownership_share"] = _draft(shareholders["foreign"] if covered else None,
                                            f"non-Italian holders {shareholders['foreign']:.0f}%"
                                            + (f" ({', '.join(shareholders['foreign_countries'])})" if shareholders["foreign_countries"] else "") + f" — {note}",
                                            "sum of the stakes of shareholders whose country is not IT, capped at 100; written only if the disclosed list covers ≥75% of equity")
    return out


# ---- building the records from the exports ------------------------------------------------------------------------------

def _json_safe(v):
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, (datetime, pd.Timestamp)):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _json_safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_json_safe(x) for x in v]
    return v


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", _fold(s)).strip("_")


def build_records(exports: Dict[str, pd.DataFrame]) -> Dict[str, dict]:
    """{BvD id: {"signals": {key: draft}, "blob": {...}, "flags": {...}}} — nothing here touches the database."""
    spec_names = {_fold(name) for _, _, name in SERIES_SPEC}
    cols: Dict[Tuple[str, str], Dict[str, str]] = {}
    for base, export, name in SERIES_SPEC:
        found = series_columns(exports[export], name)
        missing = {"latest", "y-1", "y-2"} - set(found)
        if missing:
            raise KeyError(f"AIDA export {export!r} has no {name!r} column for {sorted(missing)}")
        cols[(base, export)] = found

    # every other financial series in the P&L / balance-sheet exports rides along untouched in the blob
    extra: Dict[str, Dict[str, str]] = {}
    for export in ("PL", "CAP"):
        for col in exports[export].columns:
            parts = [p.strip() for p in str(col).split("|")]
            if len(parts) >= 2 and _fold(parts[0]) not in spec_names and YEAR_SUFFIXES.get(_fold(parts[-1])):
                extra.setdefault((export, _slug(parts[0])), {})[YEAR_SUFFIXES[_fold(parts[-1])]] = col

    struct, share, spine = exports["STRUCT"], exports["SHARE"], exports["SPINE"]
    c = {
        "participations": find_column(struct, "numero di partecipazioni"),
        "proc": find_column(struct, "procedura/cessazione"), "proc_start": find_column(struct, "data di inizio procedura"),
        "proc_close": find_column(struct, "data di chiusura della procedura"),
        "independence": find_column(struct, "indicatore", "indipendenza"),
        "part_name": find_column(struct, "partecipate", "nome"), "part_type": find_column(struct, "partecipate", "tipologia"),
        "group": find_column(share, "gruppo societario"),
        "sh_name": find_column(share, "azionisti", "nome"), "sh_type": find_column(share, "azionisti", "tipo"),
        "sh_country": find_column(share, "azionisti", "codice iso"), "sh_direct": find_column(share, "azionisti", "% diretta"),
        "sh_total": find_column(share, "azionisti", "% totale"), "sh_bvd": find_column(share, "azionisti", "numero bvd"),
        "csh_name": find_column(share, "csh", "nome"), "csh_type": find_column(share, "csh", "tipologia"),
        "csh_level": find_column(share, "livello"), "ish_name": find_column(share, "ish", "nome"),
        "accounts": find_column(spine, "data di chiusura ultimo bilancio"), "consol": find_column(spine, "codice di consolidamento"),
    }
    absent = [k for k in ("participations", "proc", "independence", "group", "sh_name", "sh_type", "sh_direct", "sh_total", "accounts") if not c[k]]
    if absent:
        raise KeyError(f"AIDA exports are missing the columns for: {absent}")

    def cell(df, col, bvd):
        return df.at[bvd, col] if col and bvd in df.index else None

    ids = sorted(set(exports["PL"].index) | set(exports["CAP"].index))
    records: Dict[str, dict] = {}
    for bvd in ids:
        n: Dict[str, Optional[float]] = {}
        for (base, export), found in cols.items():
            df = exports[export]
            for suffix, col in found.items():
                n[f"{base}_{suffix}"] = _num(df.at[bvd, col]) if bvd in df.index else None
        signals = derive_financial_signals(n)

        procedures = parse_procedures(cell(struct, c["proc"], bvd), cell(struct, c["proc_start"], bvd), cell(struct, c["proc_close"], bvd))
        shareholders = parse_shareholders(cell(share, c["sh_name"], bvd), cell(share, c["sh_type"], bvd), cell(share, c["sh_country"], bvd),
                                          cell(share, c["sh_direct"], bvd), cell(share, c["sh_total"], bvd),
                                          bvd_ids=cell(share, c["sh_bvd"], bvd), immediate_names=cell(share, c["ish_name"], bvd))
        accounts_close = pd.to_datetime(cell(spine, c["accounts"], bvd), errors="coerce")
        accounts_close = None if pd.isna(accounts_close) else accounts_close.to_pydatetime()
        independence = cell(struct, c["independence"], bvd) if bvd in struct.index else cell(share, find_column(share, "indicatore", "indipendenza"), bvd)
        signals.update(derive_structure_signals(
            participations=cell(struct, c["participations"], bvd), procedures=procedures, independence=independence,
            group_size=cell(share, c["group"], bvd), accounts_close=accounts_close, shareholders=shareholders))

        blob = {"company_id_by_aida": bvd, **n}
        for (export, slug), found in extra.items():
            for suffix, col in found.items():
                blob[f"x_{slug}_{suffix}"] = _num(exports[export].at[bvd, col]) if bvd in exports[export].index else None
        blob.update({f"derived_{k}": d["value"] for k, d in signals.items() if d["value"] is not None})
        blob.update({
            "accounts_close": accounts_close.isoformat() if accounts_close else None,
            "consolidation_code": cell(spine, c["consol"], bvd), "bvd_independence_class": independence,
            "group_size": _num(cell(share, c["group"], bvd)),
            "participations": [{"name": a, "type": b} for a, b in zip(_lines(cell(struct, c["part_name"], bvd)), _lines(cell(struct, c["part_type"], bvd)))][:20],
            "shareholders": [{k: l[k] for k in ("name", "type", "country", "direct", "total")} for l in shareholders["lines"]][:20],
            "controlling_shareholders": [{"name": a, "type": b, "level": lv} for a, b, lv in zip(
                _lines(cell(share, c["csh_name"], bvd)), _lines(cell(share, c["csh_type"], bvd)), _lines(cell(share, c["csh_level"], bvd)))][:10],
            "immediate_shareholders": _lines(cell(share, c["ish_name"], bvd))[:10],
            "procedures": procedures["names"], "procedure_closings": procedures["closings"],
        })
        records[bvd] = {"signals": signals, "blob": _json_safe(blob), "stale_accounts": bool(accounts_close and accounts_close < datetime(2024, 9, 1))}
    return records


# ---- writing --------------------------------------------------------------------------------------------------------------

def _differs(a, b) -> bool:
    if a is None or b is None:
        return (a is None) != (b is None)
    return abs(float(a) - float(b)) > 1e-6 * max(1.0, abs(float(a)), abs(float(b)))


def _aida_owned(sig) -> bool:
    """A row this importer may overwrite: nothing real there yet (empty or simulated), or already AIDA's."""
    return sig.status == "not_yet_checked" or bool(sig.is_simulated) or (sig.source or "").strip().lower().startswith("aida")


def _bulk_update_signals(db: Session, rows: List[dict]) -> None:
    """One statement per ~1,000 rows on Postgres (an executemany over a remote connection is a round trip per row)."""
    if not rows:
        return
    if db.bind.dialect.name == "postgresql":
        from psycopg2.extras import execute_values
        sql = ("UPDATE signal_records AS s SET numeric_value = v.numeric_value, status = v.status, text_value = v.text_value, "
               "source = v.source, confidence = v.confidence, is_simulated = v.is_simulated, fetched_at = v.fetched_at, "
               "raw_payload_ref = v.raw_payload_ref FROM (VALUES %s) AS v(id, numeric_value, status, text_value, source, confidence, "
               "is_simulated, fetched_at, raw_payload_ref) WHERE s.id = v.id")
        template = "(%s, %s::double precision, %s, %s, %s, %s::double precision, %s::boolean, %s::timestamp, %s)"
        tuples = [(r["id"], r["numeric_value"], r["status"], r["text_value"], r["source"], r["confidence"], r["is_simulated"],
                   r["fetched_at"], r["raw_payload_ref"]) for r in rows]
        cur = db.connection().connection.cursor()
        try:
            execute_values(cur, sql, tuples, template=template, page_size=1000)
        finally:
            cur.close()
    else:
        db.bulk_update_mappings(SignalRecord, rows)


def apply_records(db: Session, records: Dict[str, dict], apply: bool = False) -> dict:
    """
    Writes the records: signals (insert/update), one RawImportRecord blob per company, and the SourceHealth row.
    Returns {"companies", "unmatched": [bvd...], "inserted", "updated", "unchanged", "conflicts": [...],
             "by_key": {key: {"present": n, ...}}, "stale_accounts": n, "backup": [old rows overwritten]}.
    dry_run (apply=False) computes the same report and writes nothing.
    """
    companies = {c.external_ref_id: c for c in db.query(Company).filter(Company.external_ref_id.in_(list(records))).all()}
    keys = sorted({k for r in records.values() for k in r["signals"]})
    existing = {(s.company_id, s.signal_key): s for s in db.query(SignalRecord).filter(
        SignalRecord.company_id.in_([c.id for c in companies.values()]), SignalRecord.signal_key.in_(keys)).all()}

    now = datetime.utcnow()
    report = {"companies": len(companies), "unmatched": sorted(set(records) - set(companies)), "inserted": 0, "updated": 0,
              "unchanged": 0, "conflicts": [], "by_key": {}, "stale_accounts": 0, "backup": []}
    inserts, updates = [], []
    for bvd, rec in records.items():
        company = companies.get(bvd)
        if company is None:
            continue
        report["stale_accounts"] += int(rec["stale_accounts"])
        for key, d in rec["signals"].items():
            stat = report["by_key"].setdefault(key, {"present": 0, "not_yet_checked": 0, "conflict": 0})
            stat[d["status"]] += 1
            payload = json.dumps({"dataset": DATASET_NAME, "signal_key": key, "simulated": False, "basis": d["basis"],
                                  "evidence": {"method": d["basis"], "inputs": _json_safe(d["inputs"])}}, default=str)
            row = {"numeric_value": d["value"], "status": d["status"], "text_value": d["text"], "source": DATASET_NAME,
                   "confidence": 1.0, "is_simulated": False, "fetched_at": now, "raw_payload_ref": payload}
            sig = existing.get((company.id, key))
            if sig is None:
                inserts.append(dict(row, id=str(uuid.uuid4()), company_id=company.id, signal_key=key))
                report["inserted"] += 1
            elif not _aida_owned(sig):
                stat["conflict"] += 1
                report["conflicts"].append({"company_id": company.id, "signal_key": key, "held_by": sig.source, "value": sig.numeric_value})
            elif (sig.status == row["status"] and not _differs(sig.numeric_value, row["numeric_value"])
                  and (sig.source or "") == DATASET_NAME and (sig.text_value or None) == row["text_value"]):
                report["unchanged"] += 1
            else:
                report["backup"].append({"company_id": company.id, "signal_key": key, "numeric_value": sig.numeric_value, "status": sig.status,
                                         "source": sig.source, "text_value": sig.text_value, "is_simulated": sig.is_simulated})
                updates.append(dict(row, id=sig.id))
                report["updated"] += 1

    if not apply:
        return report

    for i in range(0, len(inserts), 2000):
        db.bulk_insert_mappings(SignalRecord, inserts[i:i + 2000])
    _bulk_update_signals(db, updates)

    # the stored raw row: replaced wholesale (delete + insert) so a re-import is a clean overwrite
    blob_company_ids = [companies[b].id for b in records if b in companies]
    db.query(RawImportRecord).filter(RawImportRecord.dataset_name == DATASET_NAME,
                                     RawImportRecord.company_id.in_(blob_company_ids)).delete(synchronize_session=False)
    db.bulk_insert_mappings(RawImportRecord, [
        {"id": str(uuid.uuid4()), "company_id": companies[b].id, "dataset_name": DATASET_NAME, "source_filename": "WAYLAND_*.xls (raw AIDA exports)",
         "raw_row": rec["blob"], "mapping_snapshot": MAPPING_SNAPSHOT, "imported_at": now, "updated_at": now}
        for b, rec in records.items() if b in companies])

    health = db.query(SourceHealth).filter_by(source_name=DATASET_NAME).first()
    if health is None:
        health = SourceHealth(source_name=DATASET_NAME, phase=3, total_calls=0, total_cost=0.0, error_count=0)
        db.add(health)
    health.mode, health.last_status, health.last_run_at, health.last_error_message = "live", "success", now, None
    health.total_calls = (health.total_calls or 0) + 1
    db.commit()
    return report


# ---- the management / board indicators -----------------------------------------------------------------------------------------

def management_signal_keys() -> Tuple[str, ...]:
    from company_service import MANAGEMENT_COMPOSITION_INDICATOR_KEYS, SUCCESSION_INDICATOR_KEY
    return tuple(MANAGEMENT_COMPOSITION_INDICATOR_KEYS) + (SUCCESSION_INDICATOR_KEY,)


def management_snapshot(db: Session) -> List[dict]:
    """The management-signal rows as they are now — the backup taken before run_management_pass rewrites them."""
    return [{"company_id": s.company_id, "signal_key": s.signal_key, "numeric_value": s.numeric_value, "status": s.status,
             "source": s.source, "is_simulated": s.is_simulated}
            for s in db.query(SignalRecord).filter(SignalRecord.signal_key.in_(management_signal_keys())).all()]


def run_management_pass(db: Session, progress=None, limit: Optional[int] = None) -> dict:
    """
    Computes the management/board indicators for every company from the roster already imported (age, gender,
    nationality, tenure, turnover, independent members, family handover). This is the exact code the company page
    runs on open (sync_management_composition_signals / sync_succession_signal) — run here for all companies at
    once, so they are not blank until someone happens to look. Auditors and advisors are excluded from the roster
    (company_service._is_not_management).
    Returns {"companies": n, "written": {key: n}}.
    """
    from company_service import sync_management_composition_signals, sync_succession_signal
    written: Counter = Counter()
    companies = db.query(Company).order_by(Company.legal_name).all()
    if limit:
        companies = companies[:limit]
    for i, company in enumerate(companies, 1):
        for key, res in sync_management_composition_signals(db, company).items():
            written[key] += int(bool(res["written"]))
        if sync_succession_signal(db, company).get("signal_written"):
            written["new_generation_management"] += 1
        if progress and i % 50 == 0:
            progress(i, len(companies))
    return {"companies": len(companies), "written": dict(written)}
