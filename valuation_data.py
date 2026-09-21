"""
Everything the valuation engine (valuation.py) reads that is not a company's own figures:
published reference data, editable assumptions and the sector map, plus the code that
fetches, parses and stores them.

Reference data is Prof. Aswath Damodaran's free industry datasets (NYU Stern, updated each
January) — Europe edition:
  * vebitdaEurope   sector EV/EBITDA and EV/EBIT multiples
  * waccEurope      sector beta and cost of capital (in euros)
  * wcdataEurope    sector non-cash working capital as % of sales
  * ctryprem        country equity-risk premiums and corporate tax rates
Those describe LISTED companies. valuation.py never applies a listed multiple to a small
private company as-is — it re-bases it for the extra risk, and shows the derivation.

Nothing here invents a number. A value is either fetched (with the publisher's own "as of"
date kept next to it) or is a named assumption in DEFAULT_ASSUMPTIONS with its reasoning and
its honest status (sourced, or a judgment call to be replaced).
"""

import io
import re
import warnings
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

import requests
from sqlalchemy.orm import Session

import valuation_models  # noqa: F401  (registers the tables on Base)
from valuation_models import ValuationReference, ValuationAssumption, ValuationSectorMap

DAMODARAN_BASE = "https://pages.stern.nyu.edu/~adamodar/pc/datasets/"
REGION = "Europe"

# name -> (file, sheet)
REFERENCE_FILES = {
    "multiples": (f"vebitda{REGION}.xls", "Industry Averages"),
    "wacc": (f"wacc{REGION}.xls", "Industry Averages"),
    "working_capital": (f"wcdata{REGION}.xls", "Industry Averages"),
    "country_risk": ("ctryprem.xlsx", "ERPs by country"),
}
COUNTRY_TAX_SHEET = "Country Tax Rates"
REFERENCE_LABELS = {
    "multiples": "Sector EV/EBITDA & EV/EBIT multiples",
    "wacc": "Sector beta & cost of capital (EUR)",
    "working_capital": "Sector working capital / sales",
    "country_risk": "Country equity-risk premium & tax rate",
}


# =============================================================================
# Default assumptions
# =============================================================================
# Anything in this list is a modelling choice, not an observation. `source` says where the
# number comes from; where it says "Judgment", nobody published it — it is a defensible
# starting point that should be replaced the moment a better-sourced figure exists.

def _a(key, value, label, unit, rationale, source, text=None):
    return dict(key=key, scope="*", value_num=value, value_text=text, label=label,
                unit=unit, rationale=rationale, source=source)


DEFAULT_ASSUMPTIONS: List[dict] = [
    _a("default_country", None, "Country used when a company has none recorded", "text",
       "Only ever a fallback: a company's own country decides its tax rate and country risk. "
       "Any valuation that used this fallback says so.",
       "Project decision (Italy first).", text="Italy"),
    _a("size_premium", 0.035, "Size & illiquidity premium added to the discount rate", "pct",
       "Damodaran's sector cost of capital describes large listed companies. A small private "
       "company is riskier and cannot be sold quickly; this adds that on top. It is the single "
       "most influential assumption in the model — both the DCF and the re-based multiples move "
       "with it.",
       "Judgment. No free published size premium exists; replace with a sourced figure "
       "(e.g. Kroll/CRSP size-premium tables) if you get one."),
    _a("terminal_growth", 0.02, "Long-run growth after the forecast", "pct",
       "Nominal growth a business can sustain forever; must stay below the euro risk-free rate "
       "(Damodaran's own rule). The ECB's medium-term inflation target is 2%.",
       "ECB inflation target (2%)."),
    _a("wacc_scenario_step", 0.01, "Discount-rate step for the low / high case", "pct",
       "Low case = discount rate + step, high case = discount rate - step.",
       "Judgment (presentation of a range)."),
    _a("growth_scenario_step", 0.005, "Long-run-growth step for the low / high case", "pct",
       "Low case = long-run growth - step, high case = long-run growth + step.",
       "Judgment (presentation of a range)."),
    _a("weight_latest", 0.5, "Weight of the latest year in normalised earnings", "weight",
       "Normalised EBITDA margin = weighted average of up to three years, so one unusual year "
       "does not set the valuation. Weights are renormalised when fewer years exist.",
       "Judgment (standard 3-year weighted normalisation)."),
    _a("weight_y1", 0.3, "Weight of the prior year in normalised earnings", "weight", "", "Judgment."),
    _a("weight_y2", 0.2, "Weight of two years ago in normalised earnings", "weight", "", "Judgment."),
    _a("own_growth_weight", 0.5, "Weight of the company's own growth vs. its peers' in year-1 growth", "weight",
       "Year-1 revenue growth = this share of the company's own past growth + the rest from the "
       "median of its peers in this database (same country, same NACE division); growth then "
       "fades linearly to the long-run rate by year 5.",
       "Judgment. Peer growth is measured from the imported financials — no external benchmark "
       "is invented."),
    _a("growth_cap_low", -0.10, "Floor on forecast year-1 growth", "pct",
       "A three-year swing is not extrapolated below this.", "Judgment."),
    _a("growth_cap_high", 0.15, "Ceiling on forecast year-1 growth", "pct",
       "A three-year swing is not extrapolated above this.", "Judgment."),
    _a("margin_reversion_down", 0.5, "Share of an above-peer margin assumed to erode by year 5", "weight",
       "Margins far above the peer median tend to mean-revert. 0.5 = half the gap closes over "
       "the forecast.", "Judgment (conservative mean-reversion)."),
    _a("margin_reversion_up", 0.0, "Share of a below-peer margin assumed to recover by year 5", "weight",
       "Deliberately zero: the base valuation gives no credit for a turnaround. That upside is "
       "shown separately as 'value at stake' so it is never blended into the valuation itself.",
       "Judgment."),
    _a("min_peers", 15, "Minimum peers needed to use a peer benchmark", "count",
       "With fewer companies than this in the same country + NACE division, no peer benchmark "
       "is used at all (a median of a handful is noise).", "Judgment."),
    _a("method_weight_dcf", 0.5, "Weight of the DCF in the headline value (rest = multiples)", "weight",
       "Headline = weighted mix of the DCF and the multiples methods. Both are always shown "
       "separately, and the headline range stretches to cover both.",
       "Judgment (neutral 50/50)."),
    _a("disagreement_flag", 0.35, "Method gap above which a warning is shown", "pct",
       "If the DCF and the multiples value differ by more than this share of their average, "
       "a warning says so. It does not change the input-quality grade — the two are separate "
       "signals (the grade is about the data, the gap is about the model).", "Judgment."),
    _a("override_band", 0.20, "Band around a manual sector multiple", "pct",
       "A sector EV/EBITDA you enter yourself (Assumptions tab) is used as the base; low/high "
       "are base -/+ this share.", "Judgment."),
    _a("financials_unit_eur", 1000.0, "Euros per unit in imported financial columns", "eur",
       "AIDA exports, and this app's Financial Profile page, treat monetary columns as "
       "thousands of euros. Override per dataset name if a source uses different units.",
       "Repo convention (views/company_detail.py: 'uploaded in thousands, k')."),
    _a("net_debt_definition_verified", 0.0, "Imported 'total debt' confirmed to mean financial debt (1 = yes)", "flag",
       "Enterprise value -> equity value subtracts net debt. In the AIDA import 'total debt' runs at a "
       "median ~50% of revenue (≈3.7× EBITDA) — far above what financial debt normally is for these "
       "companies — so it probably counts trade payables and other liabilities, and would push many "
       "equity values negative. Until someone confirms the field (or re-imports AIDA's financial debt / "
       "net financial position instead), equity value is withheld and only enterprise value is shown.",
       "Open question about the AIDA field definition — set to 1 once confirmed."),
]

# Sector multiples entered by hand (see the Assumptions tab): key 'ev_ebitda_override',
# scope = Damodaran industry name, value = EV/EBITDA. Not seeded — absent means "use the
# Damodaran-derived multiple".
OVERRIDE_KEY = "ev_ebitda_override"


# =============================================================================
# NACE / ATECO prefix -> Damodaran industry
# =============================================================================
# Best-effort crosswalk; "approx" in the note flags a fit that is only roughly right.
# Deliberately unmapped: NACE 64-66 (financial firms are not valued on EV/EBITDA) and
# anything not listed — those report "no sector reference" instead of a guessed peer group.

def _m(prefix, industry, note=""):
    return (prefix, industry, note)


DEFAULT_SECTOR_MAP: List[tuple] = [
    _m("01", "Farming/Agriculture"), _m("02", "Paper/Forest Products", "approx: forestry"),
    _m("03", "Farming/Agriculture", "approx: fishing/aquaculture"),
    _m("05", "Coal & Related Energy"), _m("06", "Oil/Gas (Production and Exploration)"),
    _m("07", "Metals & Mining"), _m("08", "Metals & Mining", "approx: quarrying"),
    _m("09", "Oilfield Svcs/Equip."),
    _m("10", "Food Processing"), _m("11", "Beverage (Alcoholic)"), _m("1107", "Beverage (Soft)"),
    _m("12", "Tobacco"), _m("13", "Apparel", "approx: textiles"), _m("14", "Apparel"),
    _m("15", "Apparel", "approx: leather goods"), _m("152", "Shoe"),
    _m("16", "Paper/Forest Products", "approx: wood products"), _m("17", "Paper/Forest Products"),
    _m("18", "Publishing & Newspapers", "approx: printing"),
    _m("19", "Oil/Gas (Integrated)", "approx: refined petroleum"),
    _m("20", "Chemical (Diversified)"), _m("21", "Drugs (Pharmaceutical)"),
    _m("22", "Rubber& Tires", "approx: rubber & plastic products"),
    _m("23", "Building Materials"), _m("24", "Steel"), _m("244", "Metals & Mining", "non-ferrous metals"),
    _m("25", "Machinery", "approx: fabricated metal products"),
    _m("26", "Electronics (General)", "approx"), _m("261", "Semiconductor", "approx"),
    _m("262", "Computers/Peripherals"), _m("263", "Telecom. Equipment"),
    _m("264", "Electronics (Consumer & Office)"), _m("266", "Healthcare Products", "approx"),
    _m("27", "Electrical Equipment"), _m("28", "Machinery"),
    _m("29", "Auto Parts"), _m("291", "Auto & Truck"),
    _m("30", "Aerospace/Defense", "approx: other transport equipment"),
    _m("301", "Shipbuilding & Marine"), _m("302", "Machinery", "approx: rail rolling stock"),
    _m("309", "Recreation", "approx"),
    _m("31", "Furn/Home Furnishings"), _m("32", "Recreation", "approx: misc. manufacturing"),
    _m("325", "Healthcare Products", "approx: medical instruments"),
    _m("33", "Machinery", "approx: repair & installation of machinery"),
    _m("35", "Utility (General)", "approx"), _m("36", "Utility (Water)"),
    _m("37", "Environmental & Waste Services"), _m("38", "Environmental & Waste Services"),
    _m("39", "Environmental & Waste Services"),
    _m("41", "Homebuilding", "approx: building development"), _m("42", "Engineering/Construction"),
    _m("43", "Engineering/Construction", "approx: specialised construction"),
    _m("45", "Retail (Automotive)"), _m("46", "Retail (Distributors)", "approx: wholesale"),
    _m("4631", "Food Wholesalers"), _m("47", "Retail (General)", "approx"),
    _m("49", "Transportation"), _m("494", "Trucking"), _m("50", "Transportation"),
    _m("51", "Air Transport"), _m("52", "Transportation", "approx: warehousing & support"),
    _m("53", "Transportation", "approx: postal & courier"),
    _m("55", "Hotel/Gaming"), _m("56", "Restaurant/Dining"),
    _m("58", "Publishing & Newspapers"), _m("59", "Entertainment"), _m("60", "Broadcasting"),
    _m("61", "Telecom. Services"), _m("62", "Computer Services", "approx"),
    _m("63", "Information Services"),
    _m("68", "Real Estate (Operations & Services)", "approx"),
    _m("69", "Business & Consumer Services"), _m("70", "Business & Consumer Services"),
    _m("71", "Engineering/Construction", "approx: architecture & engineering"),
    _m("72", "Business & Consumer Services", "approx: R&D services"), _m("73", "Advertising"),
    _m("74", "Business & Consumer Services"), _m("75", "Business & Consumer Services"),
    _m("77", "Business & Consumer Services"), _m("78", "Business & Consumer Services"),
    _m("79", "Business & Consumer Services"), _m("80", "Business & Consumer Services"),
    _m("81", "Business & Consumer Services"), _m("82", "Business & Consumer Services"),
    _m("85", "Education"), _m("86", "Healthcare Support Services", "approx"),
    _m("861", "Hospitals/Healthcare Facilities"), _m("87", "Healthcare Support Services", "approx"),
    _m("88", "Healthcare Support Services", "approx"),
    _m("90", "Entertainment"), _m("91", "Entertainment", "approx"), _m("93", "Recreation"),
    _m("96", "Business & Consumer Services", "approx: personal services"),
]


# =============================================================================
# Parsing the Damodaran workbooks
# =============================================================================
# The parsers work on plain lists of rows so they can be tested without a spreadsheet file
# (tests/test_valuation.py), and locate everything by header text rather than fixed cell
# positions, since the publisher shifts rows between yearly updates.

def read_sheet_rows(content: bytes, filename: str, sheet: str) -> List[list]:
    """A worksheet as a list of row-lists (.xls via xlrd, .xlsx via openpyxl)."""
    if filename.lower().endswith(".xlsx"):
        import openpyxl
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
        ws = wb[sheet]
        return [list(r) for r in ws.iter_rows(values_only=True)]
    import xlrd
    wb = xlrd.open_workbook(file_contents=content)
    s = wb.sheet_by_name(sheet)
    return [s.row_values(r) for r in range(s.nrows)]


def _num(v) -> Optional[float]:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) if v == v else None
    return None


def _txt(v) -> str:
    return str(v).strip() if v is not None else ""


def _header_row(rows: List[list], first_cell: str, also_contains: str = "") -> int:
    want = first_cell.lower()
    for i, r in enumerate(rows):
        if r and _txt(r[0]).lower() == want:
            if not also_contains or any(also_contains.lower() in _txt(c).lower() for c in r):
                return i
    raise ValueError(f"header row '{first_cell}' not found — the source layout has changed")


def _col(header: List, *needles: str, startswith: bool = False, nth: int = 0) -> Optional[int]:
    """Column index whose header text contains (or starts with) any needle — the nth match."""
    hits = []
    for j, h in enumerate(header):
        t = _txt(h).lower()
        if any((t.startswith(n) if startswith else n in t) for n in needles):
            hits.append(j)
    return hits[nth] if len(hits) > nth else None


def _as_of(rows: List[list]) -> Optional[str]:
    """The publisher's own 'Date updated' stamp — an Excel serial number or a real date."""
    for r in rows[:6]:
        for j, c in enumerate(r[:-1]):
            if "date" in _txt(c).lower() and "updat" in _txt(c).lower():
                v = r[j + 1]
                if isinstance(v, datetime):
                    return v.date().isoformat()
                if isinstance(v, date):
                    return v.isoformat()
                n = _num(v)
                if n and n > 30000:
                    return (date(1899, 12, 30) + timedelta(days=int(n))).isoformat()
    return None


def parse_multiples(rows: List[list]) -> dict:
    """Sector EV/EBITDA and EV/EBIT, for positive-EBITDA firms and for all firms."""
    h = _header_row(rows, "industry name")
    header, block_row = rows[h], rows[h - 1]
    pos_start = _col(block_row, "positive")
    all_start = _col(block_row, "all firms")
    if pos_start is None or all_start is None:
        raise ValueError("multiples file: the 'positive EBITDA' / 'all firms' blocks were not found")

    def block(start, end):
        found = {}
        for j in range(start, end):
            t = _txt(header[j]).lower().replace(" ", "")
            if t == "ev/ebitda":
                found["ebitda"] = j
            elif t == "ev/ebit":
                found["ebit"] = j
        return found

    pos, allf = block(pos_start, all_start), block(all_start, len(header))
    n_col = _col(header, "number of firms")
    out = {}
    for r in rows[h + 1:]:
        name = _txt(r[0])
        if not name:
            continue
        rec = {"n_firms": _num(r[n_col]) if n_col is not None else None}
        for tag, cols in (("pos", pos), ("all", allf)):
            for k, j in cols.items():
                rec[f"ev_{k}_{tag}"] = _num(r[j])
        out[name] = rec
    if not out:
        raise ValueError("multiples file: no industries parsed")
    return {"industries": out}


def parse_wacc(rows: List[list]) -> dict:
    """Sector beta / capital structure / cost of capital in euros, plus the file's own inputs."""
    inputs = {}
    for r in rows[:20]:
        label = _txt(r[0]).lower()
        first_num = next((n for n in (_num(c) for c in r[1:]) if n is not None), None)
        if label.startswith("long term treasury") and first_num is not None:
            inputs["risk_free_usd"] = first_num
        elif label.startswith("risk premium to use") and first_num is not None:
            inputs["erp_region"] = first_num
    h = _header_row(rows, "industry name", also_contains="cost of capital")
    header = rows[h]
    cols = {
        "n_firms": _col(header, "number of firms"),
        "beta": _col(header, "beta", startswith=True),
        "cost_equity": _col(header, "cost of equity", startswith=True),
        "equity_weight": _col(header, "e/(d+e)", startswith=True),
        "debt_weight": _col(header, "d/(d+e)", startswith=True),
        "tax_rate": _col(header, "tax rate", startswith=True),
        "wacc_eur": _col(header, "cost of capital (euro", startswith=True),
    }
    if cols["wacc_eur"] is None or cols["beta"] is None:
        raise ValueError("wacc file: beta / 'Cost of Capital (Euros)' columns not found")
    out = {}
    for r in rows[h + 1:]:
        name = _txt(r[0])
        if not name:
            continue
        out[name] = {k: (_num(r[j]) if j is not None else None) for k, j in cols.items()}
    if "erp_region" not in inputs:
        raise ValueError("wacc file: the regional equity risk premium input was not found")
    return {"industries": out, "inputs": inputs}


def parse_working_capital(rows: List[list]) -> dict:
    h = _header_row(rows, "industry name")
    header = rows[h]
    cols = {
        "n_firms": _col(header, "number of firms"),
        "ar_sales": _col(header, "acc rec"),
        "inv_sales": _col(header, "inventory"),
        "ap_sales": _col(header, "acc pay"),
        "nwc_sales": _col(header, "non-cash wc"),
    }
    if cols["nwc_sales"] is None:
        raise ValueError("working-capital file: 'Non-cash WC/ Sales' column not found")
    out = {}
    for r in rows[h + 1:]:
        name = _txt(r[0])
        if name:
            out[name] = {k: (_num(r[j]) if j is not None else None) for k, j in cols.items()}
    return {"industries": out}


def parse_country_risk(erp_rows: List[list], tax_rows: List[list]) -> dict:
    """Country equity risk premium (rating-based) and corporate tax rate."""
    mature = None
    for r in erp_rows[:15]:
        if _txt(r[0]).lower().startswith("enter the current risk premium for a mature"):
            mature = next((n for n in (_num(c) for c in r[1:]) if n is not None), None)
    h = _header_row(erp_rows, "country", also_contains="total equity risk premium")
    header = erp_rows[h]
    c_rating = _col(header, "moody")
    c_spread = _col(header, "default spread")
    c_erp = _col(header, "total equity risk premium", nth=0)
    c_crp = _col(header, "country risk premium", nth=0)
    countries = {}
    for r in erp_rows[h + 1:]:
        name = _txt(r[0])
        erp = _num(r[c_erp]) if c_erp is not None else None
        if name and erp is not None:
            countries[name] = {
                "region": _txt(r[1]) or None,
                "rating": _txt(r[c_rating]) if c_rating is not None else None,
                "default_spread": _num(r[c_spread]) if c_spread is not None else None,
                "erp_total": erp,
                "crp": _num(r[c_crp]) if c_crp is not None else None,
                "tax_rate": None,
            }
    th = _header_row(tax_rows, "country", also_contains="tax rate")
    for r in tax_rows[th + 1:]:
        name = _txt(r[0])
        rate = _num(r[2]) if len(r) > 2 and _num(r[2]) is not None else (_num(r[1]) if len(r) > 1 else None)
        if name in countries and rate is not None:
            countries[name]["tax_rate"] = rate
    if not countries:
        raise ValueError("country-risk file: no countries parsed")
    return {"mature_erp": mature, "countries": countries}


# =============================================================================
# Fetch + store
# =============================================================================

def _download(filename: str, timeout: int = 60) -> bytes:
    resp = requests.get(DAMODARAN_BASE + filename, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
    resp.raise_for_status()
    return resp.content


def fetch_reference_payloads(download=_download) -> Dict[str, dict]:
    """Downloads and parses all four datasets. Raises (fetching nothing partially usable)
    if any download or parse fails — the caller stores nothing on failure, so a half-refresh
    can never leave a mismatched set behind. `download` is injectable for tests."""
    out = {}
    for name, (fname, sheet) in REFERENCE_FILES.items():
        content = download(fname)
        rows = read_sheet_rows(content, fname, sheet)
        if name == "multiples":
            payload = parse_multiples(rows)
        elif name == "wacc":
            payload = parse_wacc(rows)
        elif name == "working_capital":
            payload = parse_working_capital(rows)
        else:
            payload = parse_country_risk(rows, read_sheet_rows(content, fname, COUNTRY_TAX_SHEET))
        as_of = _as_of(rows)
        if as_of is None and name == "country_risk":
            as_of = _country_as_of(rows)
        out[name] = {"payload": payload, "as_of": as_of, "source_url": DAMODARAN_BASE + fname}
    return out


def _country_as_of(rows: List[list]) -> Optional[str]:
    for r in rows[:6]:
        if _txt(r[0]).lower().startswith("date of update"):
            v = r[1]
            if isinstance(v, datetime):
                return v.date().isoformat()
            if isinstance(v, date):
                return v.isoformat()
            m = re.search(r"\d{4}-\d{2}-\d{2}", _txt(v))
            return m.group(0) if m else None
    return None


def refresh_reference_data(db: Session, download=_download) -> List[dict]:
    """Fetch everything, then replace the stored copies in one commit. Returns a summary."""
    fetched = fetch_reference_payloads(download)
    now = datetime.utcnow()  # naive UTC, like every other timestamp column in this schema
    summary = []
    for name, item in fetched.items():
        row = db.get(ValuationReference, name) or ValuationReference(name=name)
        row.region = REGION
        row.as_of = item["as_of"]
        row.source_url = item["source_url"]
        row.fetched_at = now
        row.payload = item["payload"]
        db.merge(row)
        n = len(item["payload"].get("industries") or item["payload"].get("countries") or {})
        summary.append({"dataset": name, "as_of": item["as_of"], "rows": n})
    db.commit()
    return summary


# =============================================================================
# Seeding + loading the context the engine works from
# =============================================================================

def seed_valuation_defaults(db: Session) -> None:
    """Insert-only, like the indicator catalog: rows that exist (and may have been edited
    on the Assumptions tab) are never overwritten."""
    have = {(a.key, a.scope) for a in db.query(ValuationAssumption.key, ValuationAssumption.scope).all()}
    for a in DEFAULT_ASSUMPTIONS:
        if (a["key"], a["scope"]) not in have:
            db.add(ValuationAssumption(**a))
    have_map = {m.nace_prefix for m in db.query(ValuationSectorMap.nace_prefix).all()}
    for prefix, industry, note in DEFAULT_SECTOR_MAP:
        if prefix not in have_map:
            db.add(ValuationSectorMap(nace_prefix=prefix, industry=industry, note=note))
    db.commit()


@dataclass
class Assumptions:
    """Scope-aware lookup over the assumption rows: country/dataset override first, then '*'."""
    rows: Dict[Tuple[str, str], dict] = field(default_factory=dict)

    def _row(self, key: str, scope: Optional[str]) -> Optional[dict]:
        if scope and (key, scope) in self.rows:
            return self.rows[(key, scope)]
        return self.rows.get((key, "*"))

    def num(self, key: str, scope: Optional[str] = None) -> float:
        row = self._row(key, scope)
        if row is None or row.get("value_num") is None:
            raise KeyError(f"valuation assumption '{key}' is missing — run seed_valuation_defaults")
        return float(row["value_num"])

    def text(self, key: str, scope: Optional[str] = None) -> str:
        row = self._row(key, scope)
        return (row or {}).get("value_text") or ""

    def source(self, key: str, scope: Optional[str] = None) -> str:
        """The recorded provenance of an assumption — sourced, or an honest 'Judgment'."""
        return (self._row(key, scope) or {}).get("source") or "—"

    def scoped(self, key: str) -> Dict[str, float]:
        """{scope: value} for every non-global row of a key (e.g. sector multiple overrides)."""
        return {s: r["value_num"] for (k, s), r in self.rows.items()
                if k == key and s != "*" and r.get("value_num") is not None}


@dataclass
class Reference:
    """The stored Damodaran datasets, with the publisher's date and source next to each."""
    data: Dict[str, dict] = field(default_factory=dict)   # name -> payload
    meta: Dict[str, dict] = field(default_factory=dict)   # name -> {as_of, fetched_at, source_url}

    @property
    def loaded(self) -> bool:
        return all(n in self.data for n in REFERENCE_FILES)

    def as_of(self, name: str) -> Optional[str]:
        return (self.meta.get(name) or {}).get("as_of")

    def industry(self, dataset: str, industry: str) -> Optional[dict]:
        return ((self.data.get(dataset) or {}).get("industries") or {}).get(industry)

    def country(self, name: str) -> Optional[dict]:
        return ((self.data.get("country_risk") or {}).get("countries") or {}).get(name)

    @property
    def region_erp(self) -> Optional[float]:
        return ((self.data.get("wacc") or {}).get("inputs") or {}).get("erp_region")

    @property
    def industries(self) -> List[str]:
        return sorted(((self.data.get("multiples") or {}).get("industries") or {}).keys())


def load_assumptions(db: Session) -> Assumptions:
    return Assumptions({
        (a.key, a.scope or "*"): {"value_num": a.value_num, "value_text": a.value_text,
                                  "label": a.label, "unit": a.unit,
                                  "rationale": a.rationale, "source": a.source}
        for a in db.query(ValuationAssumption).all()
    })


def load_reference(db: Session) -> Reference:
    ref = Reference()
    for r in db.query(ValuationReference).all():
        ref.data[r.name] = r.payload
        ref.meta[r.name] = {"as_of": r.as_of, "fetched_at": r.fetched_at, "source_url": r.source_url}
    return ref


def load_sector_map(db: Session) -> Dict[str, Tuple[str, str]]:
    return {m.nace_prefix: (m.industry, m.note or "") for m in db.query(ValuationSectorMap).all()}


# =============================================================================
# Editing (used by the Assumptions tab; kept here so it can be tested without the UI)
# =============================================================================

def apply_assumption_changes(db: Session, changes: Dict[str, float], default_country: Optional[str] = None) -> int:
    """Save edited global (scope '*') numeric assumptions, and optionally the default-country
    text. Unknown keys are ignored. Returns how many rows actually changed."""
    changed = 0
    for key, value in changes.items():
        row = db.get(ValuationAssumption, (key, "*"))
        if row is not None and value is not None and row.value_num != float(value):
            row.value_num = float(value)
            changed += 1
    if default_country and default_country.strip():
        row = db.get(ValuationAssumption, ("default_country", "*"))
        if row is not None and (row.value_text or "") != default_country.strip():
            row.value_text = default_country.strip()
            changed += 1
    db.commit()
    return changed


def set_net_debt_verified(db: Session, verified: bool) -> None:
    row = db.get(ValuationAssumption, ("net_debt_definition_verified", "*"))
    row.value_num = 1.0 if verified else 0.0
    db.commit()


def set_sector_overrides(db: Session, overrides: Dict[str, float]) -> None:
    """Make the manual sector-multiple rows match `overrides` ({industry: EV/EBITDA})."""
    existing = {a.scope: a for a in db.query(ValuationAssumption).filter_by(key=OVERRIDE_KEY).all()}
    for scope, row in existing.items():
        if scope not in overrides:
            db.delete(row)
    for scope, value in overrides.items():
        if value is None or value <= 0:
            continue
        row = existing.get(scope)
        if row:
            row.value_num = float(value)
        else:
            db.add(ValuationAssumption(key=OVERRIDE_KEY, scope=scope, value_num=float(value),
                                       label="Manual sector EV/EBITDA", unit="x",
                                       source="Entered by hand on the Assumptions tab"))
    db.commit()


def replace_sector_map(db: Session, mapping: Dict[str, Tuple[str, str]]) -> int:
    """Make the sector map match `mapping` ({digits-only prefix: (industry, note)}).
    Prefixes are normalised to digits; blanks are dropped. Returns the resulting row count."""
    clean = {}
    for prefix, (industry, note) in mapping.items():
        digits = re.sub(r"\D", "", str(prefix or ""))
        if digits and industry:
            clean[digits] = (industry, note or "")
    for row in db.query(ValuationSectorMap).all():
        if row.nace_prefix not in clean:
            db.delete(row)
    for prefix, (industry, note) in clean.items():
        row = db.get(ValuationSectorMap, prefix)
        if row:
            row.industry, row.note = industry, note
        else:
            db.add(ValuationSectorMap(nace_prefix=prefix, industry=industry, note=note))
    db.commit()
    return len(clean)
