"""
Indicative company valuation — the third lens beside NEED and READINESS.

It answers "what is this company roughly worth, and how much of that is at stake in the
gaps we score" — never as a fairness opinion, and never blended into either axis (see
project-vienna-overview: Need and Readiness stay separate; valuation is a separate read).

Design rules, in the same spirit as the rest of the tool:

  * No LLM and no free text in the numbers. Everything below is deterministic arithmetic on
    (a) the company's own imported financials, (b) published reference data stored with its
    "as of" date (valuation_data.py — Damodaran's free industry datasets), and (c) named
    assumptions in valuation_assumptions, each carrying its source or its honest "judgment"
    label. Every result lists exactly which of those it used.
  * Ranges, never a single point: each method gives low / base / high, and the headline is a
    range too.
  * Refuse rather than guess: no revenue/EBITDA, negative normalised earnings, no sector
    reference or no country data -> status "not_valued" with the reason, not a number.
  * Only real data. Simulated signals are never read here — only imported financial columns.

Two method families, always shown side by side:

  1. Multiples (Tier 1). A listed-company sector multiple (Damodaran, Europe) applied to the
     company's NORMALISED earnings. A listed multiple embeds low risk and easy liquidity, so
     it is re-based for the company's higher discount rate using the perpetuity relationship
     multiple ~ 1 / (discount rate - growth):
         adjusted = listed x (sector rate - g) / (company rate - g)
     with company rate = sector rate + country risk + size/illiquidity premium. The
     derivation is shown, not hidden in a "discount %".
  2. DCF (Tier 2). Five-year free-cash-flow forecast + terminal value, discounted at that same
     company rate. Growth blends the company's own trend with the median of its peers in this
     database and fades to long-run growth; margin mean-reverts toward the peer median only
     downwards by default (no turnaround credit — that upside is reported separately as
     "value at stake").

Known limits, carried into every result's warnings: normalisation is mechanical (no
owner-pay or one-off adjustments — the data has none), net debt excludes leases, pensions and
severance (TFR) because the source doesn't carry them, and listed-peer multiples embed a
growth/quality premium that a small private company may not earn — which is why the multiples
and the DCF are shown separately, the headline range stretches to cover both, and their gap is
reported as its own signal (kept apart from the input-quality grade).
"""

import re
from dataclasses import dataclass, field
from statistics import median, mean
from typing import Dict, List, Optional, Tuple

from valuation_data import Assumptions, Reference, OVERRIDE_KEY

FORECAST_YEARS = 5
YEAR_SUFFIXES = ("latest", "y-1", "y-2")
MIN_SPREAD = 0.03          # discount rate must exceed growth by at least this or the maths is meaningless

NOT_VALUED_REASONS = {
    "no_financials": "Revenue and EBITDA for the latest year are not available.",
    "no_reference": "Market reference data has not been loaded yet (Valuation → Assumptions → Refresh).",
    "no_country": "No country-risk data for this company's country.",
    "no_sector": "No sector reference for this company's NACE/ATECO code.",
    "negative_earnings": "Normalised EBITDA is zero or negative — not valuable on earnings.",
}


# =============================================================================
# Company financials from imported rows
# =============================================================================

FIELD_ALIASES = {
    "revenue": ["revenue", "revenues", "turnover", "sales", "ricavi", "fatturato"],
    "ebitda": ["ebitda"],
    "ebit": ["ebit", "operating_profit", "operating_income"],
    "cash": ["cash", "cash_and_equivalents", "cassa"],
    "debt": ["total_debt", "financial_debt", "debt"],
    "capex_material": ["materialcapex", "material_capex", "capex_material"],
    "capex_immaterial": ["immaterialcapex", "immaterial_capex", "capex_immaterial"],
    "employees": ["employees", "number_of_employees"],
}


def _to_float(v) -> Optional[float]:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) if v == v else None
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None        # 'n.s.' and friends
    return None


@dataclass
class Financials:
    """A company's imported figures, latest year first, in the source's native units."""
    dataset: str
    unit_eur: float
    series: Dict[str, List[Optional[float]]] = field(default_factory=dict)

    def val(self, name: str, i: int = 0) -> Optional[float]:
        s = self.series.get(name)
        return s[i] if s and i < len(s) else None

    def eur(self, name: str, i: int = 0) -> Optional[float]:
        v = self.val(name, i)
        return v * self.unit_eur if v is not None else None

    def completeness(self) -> int:
        return sum(1 for f in ("revenue", "ebitda") for v in self.series.get(f, []) if v is not None)


def extract_financials(raw_row: dict, dataset: str, unit_eur: float) -> Optional[Financials]:
    """Reads '<field>_latest / _y-1 / _y-2' columns from one imported row (the convention
    company_service.detect_column_groups already relies on). None if there is no revenue."""
    norm = {re.sub(r"\s+", "_", str(k).strip().lower()): v for k, v in raw_row.items()}
    series = {}
    for fld, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            vals = [_to_float(norm.get(f"{alias}_{suf}")) for suf in YEAR_SUFFIXES]
            if all(v is None for v in vals):
                single = _to_float(norm.get(alias))
                vals = [single, None, None] if single is not None else vals
            if any(v is not None for v in vals):
                series[fld] = vals
                break
    if "revenue" not in series:
        return None
    return Financials(dataset=dataset, unit_eur=unit_eur, series=series)


def best_financials(candidates: List[Financials]) -> Optional[Financials]:
    """When a company has several imported datasets, the one with the most revenue+EBITDA years."""
    if not candidates:
        return None
    return sorted(candidates, key=lambda f: (-f.completeness(), f.dataset))[0]


# =============================================================================
# Country, sector, peers
# =============================================================================

COUNTRY_ALIASES = {
    "italy": "Italy", "italia": "Italy", "it": "Italy", "ita": "Italy",
    "germany": "Germany", "deutschland": "Germany", "de": "Germany", "deu": "Germany",
}


def resolve_country(recorded: Optional[str], default: str) -> Tuple[str, bool]:
    """(country, assumed). `assumed` is True when the company has none and the default was used."""
    c = (recorded or "").strip()
    if not c:
        return default, True
    return COUNTRY_ALIASES.get(c.lower(), c), False


def nace_digits(code) -> str:
    return re.sub(r"\D", "", str(code or ""))


def map_industry(nace_code, sector_map: Dict[str, Tuple[str, str]]) -> Optional[Tuple[str, str, str]]:
    """(industry, note, matched_prefix) by longest matching prefix, else None."""
    d = nace_digits(nace_code)
    for n in range(len(d), 0, -1):
        hit = sector_map.get(d[:n])
        if hit:
            return hit[0], hit[1], d[:n]
    return None


def division(nace_code) -> str:
    return nace_digits(nace_code)[:2]


@dataclass
class PeerStats:
    n: int
    median_margin: Optional[float]
    median_growth: Optional[float]


def weighted_margin(fin: Financials, weights: Tuple[float, float, float]) -> Tuple[Optional[float], int]:
    """Weighted EBITDA margin over the years that have both revenue and EBITDA."""
    num = den = 0.0
    years = 0
    for i, w in enumerate(weights):
        rev, e = fin.val("revenue", i), fin.val("ebitda", i)
        if rev and rev > 0 and e is not None:
            num += w * (e / rev)
            den += w
            years += 1
    return (num / den if den else None), years


def revenue_cagr(fin: Financials) -> Optional[float]:
    r0 = fin.val("revenue", 0)
    if not r0 or r0 <= 0:
        return None
    for k in (2, 1):
        rk = fin.val("revenue", k)
        if rk and rk > 0:
            return (r0 / rk) ** (1.0 / k) - 1.0
    return None


def build_peer_stats(observations: List[Tuple[tuple, Optional[float], Optional[float]]]) -> Dict[tuple, PeerStats]:
    """observations: [((country, division), normalised_margin, cagr), ...] -> per-group medians."""
    groups: Dict[tuple, list] = {}
    for key, margin, growth in observations:
        groups.setdefault(key, []).append((margin, growth))
    out = {}
    for key, rows in groups.items():
        ms = [m for m, _ in rows if m is not None]
        gs = [g for _, g in rows if g is not None]
        out[key] = PeerStats(n=len(rows),
                             median_margin=median(ms) if ms else None,
                             median_growth=median(gs) if gs else None)
    return out


@dataclass
class ValuationContext:
    assumptions: Assumptions
    reference: Reference
    sector_map: Dict[str, Tuple[str, str]]
    peers: Dict[tuple, PeerStats] = field(default_factory=dict)

    def weights(self) -> Tuple[float, float, float]:
        A = self.assumptions
        return (A.num("weight_latest"), A.num("weight_y1"), A.num("weight_y2"))

    def default_country(self) -> str:
        return self.assumptions.text("default_country") or "Italy"


def peer_observation(company: dict, fin: Financials, ctx: ValuationContext):
    country, _ = resolve_country(company.get("country"), ctx.default_country())
    margin, _ = weighted_margin(fin, ctx.weights())
    return (country, division(company.get("nace_code"))), margin, revenue_cagr(fin)


# =============================================================================
# Discount rate, DCF, re-based multiples
# =============================================================================

@dataclass
class DiscountRate:
    sector_wacc: float
    beta: float
    equity_weight: float
    country_erp: float
    region_erp: float
    country_adjustment: float
    size_premium: float
    wacc: float


def build_discount_rate(ref: Reference, industry: str, country_info: dict, size_premium: float) -> Optional[DiscountRate]:
    w = ref.industry("wacc", industry)
    region_erp = ref.region_erp
    if not w or w.get("wacc_eur") is None or w.get("beta") is None or region_erp is None:
        return None
    ew = w.get("equity_weight") if w.get("equity_weight") is not None else 1.0
    adj = ew * w["beta"] * (country_info["erp_total"] - region_erp)
    return DiscountRate(
        sector_wacc=w["wacc_eur"], beta=w["beta"], equity_weight=ew,
        country_erp=country_info["erp_total"], region_erp=region_erp,
        country_adjustment=adj, size_premium=size_premium,
        wacc=w["wacc_eur"] + adj + size_premium,
    )


@dataclass
class DcfInputs:
    revenue0: float          # latest revenue, EUR
    growth1: float           # year-1 growth
    margin0: float           # normalised EBITDA margin
    margin_target: float     # margin reached by year 5
    da_pct: float
    capex_pct: float
    tax_rate: float
    nwc_pct: float


def run_dcf(inp: DcfInputs, wacc: float, g: float) -> dict:
    """Five-year forecast + Gordon terminal value, mid-year discounting. Raises ValueError when
    the discount rate does not exceed growth by a workable margin."""
    if wacc - g < MIN_SPREAD:
        raise ValueError(f"discount rate {wacc:.1%} is too close to long-run growth {g:.1%}")
    n = FORECAST_YEARS
    rows = []
    rev_prev = inp.revenue0
    pv_sum = 0.0
    for t in range(1, n + 1):
        growth = inp.growth1 + (g - inp.growth1) * (t - 1) / (n - 1)
        margin = inp.margin0 + (inp.margin_target - inp.margin0) * t / n
        capex_pct = inp.capex_pct + (inp.da_pct - inp.capex_pct) * (t - 1) / (n - 1)
        rev = rev_prev * (1 + growth)
        ebitda = margin * rev
        da = inp.da_pct * rev
        ebit = ebitda - da
        tax = max(ebit, 0.0) * inp.tax_rate
        capex = capex_pct * rev
        d_nwc = inp.nwc_pct * (rev - rev_prev)
        fcf = ebit - tax + da - capex - d_nwc
        pv = fcf / (1 + wacc) ** (t - 0.5)
        pv_sum += pv
        rows.append({"year": t, "growth": growth, "revenue": rev, "ebitda_margin": margin, "ebitda": ebitda,
                     "da": da, "ebit": ebit, "tax": tax, "capex": capex, "d_nwc": d_nwc, "fcf": fcf, "pv": pv})
        rev_prev = rev
    last = rows[-1]
    rev_t1 = last["revenue"] * (1 + g)
    ebit_t1 = (last["ebitda_margin"] - inp.da_pct) * rev_t1
    fcf_t1 = ebit_t1 - max(ebit_t1, 0.0) * inp.tax_rate - inp.nwc_pct * (rev_t1 - last["revenue"])
    tv = fcf_t1 / (wacc - g)
    pv_tv = tv / (1 + wacc) ** (n - 0.5)
    return {"ev": pv_sum + pv_tv, "pv_explicit": pv_sum, "pv_terminal": pv_tv, "terminal_value": tv,
            "terminal_fcf": fcf_t1, "rows": rows}


def rebased_multiple(listed: float, sector_wacc: float, company_wacc: float, g: float) -> float:
    """Listed sector multiple re-based for a higher discount rate (perpetuity relationship)."""
    if sector_wacc - g <= 0 or company_wacc - g <= 0:
        raise ValueError("discount rate must exceed long-run growth")
    return listed * (sector_wacc - g) / (company_wacc - g)


# =============================================================================
# Result types
# =============================================================================

@dataclass
class MethodResult:
    key: str
    label: str
    family: str                 # 'multiples' | 'dcf'
    ev_low: float
    ev_base: float
    ev_high: float
    detail: dict = field(default_factory=dict)


@dataclass
class ValuationResult:
    status: str                                   # 'valued' | 'not_valued'
    reason_code: str = ""
    reason: str = ""
    company_id: Optional[str] = None
    company_name: str = ""
    country: str = ""
    country_assumed: bool = False
    param_country: str = ""
    industry: Optional[str] = None
    industry_note: str = ""
    nace_code: str = ""
    dataset: str = ""
    history: List[dict] = field(default_factory=list)
    revenue: Optional[float] = None               # latest, EUR
    ebitda_latest: Optional[float] = None
    ebitda_norm: Optional[float] = None
    margin_norm: Optional[float] = None
    margin_years: int = 0
    net_debt: Optional[float] = None
    equity_withheld: str = ""                     # why equity value is not shown (empty when it is)
    cash: Optional[float] = None
    debt: Optional[float] = None
    discount: Optional[DiscountRate] = None
    methods: List[MethodResult] = field(default_factory=list)
    ev_low: Optional[float] = None
    ev_base: Optional[float] = None
    ev_high: Optional[float] = None
    equity_low: Optional[float] = None
    equity_base: Optional[float] = None
    equity_high: Optional[float] = None
    implied_ev_ebitda: Optional[float] = None
    disagreement: Optional[float] = None          # |DCF - multiples| / their mean; a signal separate from the grade
    value_at_stake: Optional[dict] = None
    peer: Optional[PeerStats] = None
    dcf_table: List[dict] = field(default_factory=list)
    sensitivity: Optional[dict] = None
    confidence_score: int = 0
    confidence_grade: str = ""
    confidence_reasons: List[Tuple[int, str]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    inputs_used: List[dict] = field(default_factory=list)

    def method(self, key: str) -> Optional[MethodResult]:
        return next((m for m in self.methods if m.key == key), None)


def _not_valued(code: str, extra: str = "", **kw) -> ValuationResult:
    reason = NOT_VALUED_REASONS[code] + (f" {extra}" if extra else "")
    return ValuationResult(status="not_valued", reason_code=code, reason=reason, **kw)


def _grade(score: int) -> str:
    return "A" if score >= 85 else "B" if score >= 70 else "C" if score >= 50 else "D"


# =============================================================================
# The valuation
# =============================================================================

def value_company(company: dict, fin: Optional[Financials], ctx: ValuationContext,
                  country_override: Optional[str] = None) -> ValuationResult:
    """company: {id, legal_name, country, nace_code, headcount}. Never raises for a data
    reason — returns a not_valued result naming the reason."""
    A, ref = ctx.assumptions, ctx.reference
    rec_country, assumed = resolve_country(company.get("country"), ctx.default_country())
    param_country = (country_override or rec_country)
    base = dict(company_id=company.get("id"), company_name=company.get("legal_name", ""),
                country=rec_country, country_assumed=assumed, param_country=param_country,
                nace_code=str(company.get("nace_code") or ""))

    rev = fin.eur("revenue") if fin else None
    e0 = fin.eur("ebitda") if fin else None
    if fin is None or rev is None or rev <= 0 or e0 is None:
        return _not_valued("no_financials", **base)
    base["dataset"] = fin.dataset
    if not ref.loaded:
        return _not_valued("no_reference", **base)
    country_info = ref.country(param_country)
    if not country_info or country_info.get("erp_total") is None:
        return _not_valued("no_country", f"({param_country})", **base)
    mapped = map_industry(company.get("nace_code"), ctx.sector_map)
    if not mapped or ref.industry("multiples", mapped[0]) is None or ref.industry("wacc", mapped[0]) is None:
        return _not_valued("no_sector", f"(code {company.get('nace_code')!s})", **base)
    industry, industry_note, prefix = mapped
    base.update(industry=industry, industry_note=industry_note)

    weights = ctx.weights()
    margin_norm, margin_years = weighted_margin(fin, weights)
    if margin_norm is None or margin_norm <= 0:
        return _not_valued("negative_earnings", **base)
    ebitda_norm = margin_norm * rev

    R = ValuationResult(status="valued", revenue=rev, ebitda_latest=e0, ebitda_norm=ebitda_norm,
                        margin_norm=margin_norm, margin_years=margin_years, **base)
    warnings, penalties = R.warnings, R.confidence_reasons

    def penalise(points: int, text: str):
        penalties.append((points, text))

    # ---- history table (EUR), latest first
    for i, label in enumerate(("Latest", "Year -1", "Year -2")):
        r_i, e_i, b_i = fin.eur("revenue", i), fin.eur("ebitda", i), fin.eur("ebit", i)
        if r_i is None and e_i is None:
            continue
        R.history.append({"period": label, "revenue": r_i, "ebitda": e_i, "ebit": b_i,
                          "margin": (e_i / r_i) if (r_i and e_i is not None) else None,
                          "cash": fin.eur("cash", i), "debt": fin.eur("debt", i)})

    # ---- net debt (enterprise value never depends on it; only the equity bridge does)
    debt, cash = fin.eur("debt"), fin.eur("cash")
    R.debt, R.cash = debt, cash
    verified = bool(A.num("net_debt_definition_verified"))
    if debt is not None and cash is not None:
        R.net_debt = debt - cash
    if not verified:
        lev = f" — as imported it is {R.net_debt / ebitda_norm:.1f}× EBITDA" if R.net_debt is not None else ""
        R.equity_withheld = ("The imported 'total debt' has not been confirmed to be financial debt" + lev +
                             ", which may well include trade payables and other liabilities. Equity value is "
                             "withheld until that is checked (Valuation → Assumptions).")
    elif R.net_debt is None:
        R.equity_withheld = "Debt and/or cash missing from the source."
    else:
        warnings.append("Net debt uses only the imported debt and cash. Leases, pension provisions and Italian "
                        "severance (TFR) are not in the source and are not deducted.")

    # ---- data sanity
    emp = fin.val("employees")
    if emp and emp > 0:
        per_head = rev / emp
        if per_head < 5_000 or per_head > 5_000_000:
            warnings.append(f"Revenue per employee is €{per_head:,.0f} — the imported figures are probably not in "
                            f"the assumed units (×{fin.unit_eur:g} EUR). Check 'financials_unit_eur'.")
            penalise(-25, "Revenue per employee implausible — unit mismatch likely")
    if margin_years < 3:
        penalise(-15 if margin_years == 2 else -30, f"Only {margin_years} year(s) of revenue+EBITDA history")
    margins = [h["margin"] for h in R.history if h["margin"] is not None]
    if len(margins) >= 2 and (max(margins) - min(margins)) > 0.08:
        penalise(-10, f"EBITDA margin swung {min(margins):.1%} → {max(margins):.1%} across the years")
    if country_override and country_override != rec_country:
        warnings.append(f"Country parameters overridden to {country_override} (company is recorded in {rec_country}).")
    if assumed:
        warnings.append(f"No country recorded — {rec_country} assumed.")
        penalise(-10, "Country not recorded — default used")
    if industry_note.lower().startswith("approx"):
        warnings.append(f"Sector match is approximate ({industry_note}).")
        penalise(-10, f"Sector reference is only approximate ({industry})")

    # ---- discount rate
    size_prem = A.num("size_premium", param_country)
    dr = build_discount_rate(ref, industry, country_info, size_prem)
    if dr is None:
        return _not_valued("no_sector", "(no cost-of-capital row)", **base)
    R.discount = dr
    g = A.num("terminal_growth", param_country)
    w_step, g_step = A.num("wacc_scenario_step", param_country), A.num("growth_scenario_step", param_country)
    scen = {"low": (dr.wacc + w_step, g - g_step), "base": (dr.wacc, g), "high": (dr.wacc - w_step, g + g_step)}

    # ---- peers
    peer = ctx.peers.get((rec_country, division(company.get("nace_code"))))
    if peer and peer.n >= A.num("min_peers"):
        R.peer = peer
    else:
        penalise(-10, "No usable peer benchmark (too few peers with financials in this country and NACE division)")

    # ---- D&A, capex, NWC, tax (for EV/EBIT and the DCF)
    da_list = [(fin.eur("ebitda", i) - fin.eur("ebit", i)) / fin.eur("revenue", i)
               for i in range(3)
               if fin.eur("ebitda", i) is not None and fin.eur("ebit", i) is not None
               and fin.eur("revenue", i) and fin.eur("revenue", i) > 0]
    da_pct = max(mean(da_list), 0.0) if da_list else None
    capex_list = []
    for i in range(3):
        r_i = fin.eur("revenue", i)
        cm = fin.eur("capex_material", i)
        if r_i and r_i > 0 and cm is not None:
            capex_list.append((abs(cm) + abs(fin.eur("capex_immaterial", i) or 0.0)) / r_i)
    capex_pct = mean(capex_list) if len(capex_list) >= 2 else None
    nwc = ref.industry("working_capital", industry) or {}
    nwc_pct = nwc.get("nwc_sales")
    tax = country_info.get("tax_rate")

    # ===== Method family 1: multiples ========================================================
    mult = ref.industry("multiples", industry)
    override = A.scoped(OVERRIDE_KEY).get(industry)
    band = A.num("override_band")
    ev_ebitda = {}
    if override:
        ev_ebitda = {"low": ebitda_norm * override * (1 - band), "base": ebitda_norm * override,
                     "high": ebitda_norm * override * (1 + band)}
        detail = {"listed_multiple": mult.get("ev_ebitda_pos"), "manual_multiple": override,
                  "multiples": {"low": override * (1 - band), "base": override, "high": override * (1 + band)},
                  "metric": ebitda_norm, "metric_label": "Normalised EBITDA"}
        R.methods.append(MethodResult("ev_ebitda", "EV/EBITDA (manual sector multiple)", "multiples",
                                      ev_ebitda["low"], ev_ebitda["base"], ev_ebitda["high"], detail))
    elif mult.get("ev_ebitda_pos"):
        ms = {k: rebased_multiple(mult["ev_ebitda_pos"], dr.sector_wacc, w, gg) for k, (w, gg) in scen.items()}
        ev_ebitda = {k: ebitda_norm * v for k, v in ms.items()}
        detail = {"listed_multiple": mult["ev_ebitda_pos"], "listed_n_firms": mult.get("n_firms"),
                  "multiples": ms, "metric": ebitda_norm, "metric_label": "Normalised EBITDA"}
        R.methods.append(MethodResult("ev_ebitda", "EV/EBITDA (listed sector, re-based)", "multiples",
                                      ev_ebitda["low"], ev_ebitda["base"], ev_ebitda["high"], detail))
    if da_pct is not None and mult.get("ev_ebit_pos"):
        ebit_norm = (margin_norm - da_pct) * rev
        if ebit_norm > 0:
            ms = {k: rebased_multiple(mult["ev_ebit_pos"], dr.sector_wacc, w, gg) for k, (w, gg) in scen.items()}
            R.methods.append(MethodResult(
                "ev_ebit", "EV/EBIT (listed sector, re-based)", "multiples",
                ebit_norm * ms["low"], ebit_norm * ms["base"], ebit_norm * ms["high"],
                {"listed_multiple": mult["ev_ebit_pos"], "listed_n_firms": mult.get("n_firms"),
                 "multiples": ms, "metric": ebit_norm, "metric_label": "Normalised EBIT"}))
        else:
            warnings.append("Normalised EBIT is not positive, so the EV/EBIT method was skipped.")
    elif da_pct is None:
        warnings.append("EBIT is not in the source, so the EV/EBIT method and the DCF's D&A are unavailable.")

    # ===== Method family 2: DCF ==============================================================
    dcf_ok = all(x is not None for x in (da_pct, nwc_pct, tax))
    if not dcf_ok:
        warnings.append("DCF skipped — needs EBIT (for D&A), sector working-capital and a country tax rate.")
    else:
        if capex_pct is None:
            capex_pct = da_pct
            penalise(-5, "Capex not in the source for ≥2 years — assumed equal to D&A")
        own = revenue_cagr(fin)
        pg = R.peer.median_growth if R.peer else None
        wg = A.num("own_growth_weight")
        if own is not None and pg is not None:
            g1 = wg * own + (1 - wg) * pg
        elif own is not None:
            g1 = wg * own + (1 - wg) * g
        else:
            g1 = pg if pg is not None else g
        g1 = min(max(g1, A.num("growth_cap_low")), A.num("growth_cap_high"))
        if R.peer and R.peer.median_margin is not None:
            gap = R.peer.median_margin - margin_norm
            share = A.num("margin_reversion_up") if gap > 0 else A.num("margin_reversion_down")
            m_target = margin_norm + gap * share
        else:
            m_target = margin_norm
        inp = DcfInputs(revenue0=rev, growth1=g1, margin0=margin_norm, margin_target=m_target,
                        da_pct=da_pct, capex_pct=capex_pct, tax_rate=tax, nwc_pct=nwc_pct)
        try:
            out = {k: run_dcf(inp, w, gg) for k, (w, gg) in scen.items()}
        except ValueError as e:
            out = None
            warnings.append(f"DCF skipped — {e}.")
        if out:
            R.dcf_table = out["base"]["rows"]
            grid_w = [dr.wacc + k * w_step for k in (-2, -1, 0, 1, 2)]
            grid_g = [g + k * g_step for k in (-2, -1, 0, 1, 2)]
            grid = []
            for ww in grid_w:
                line = []
                for gg in grid_g:
                    try:
                        line.append(run_dcf(inp, ww, gg)["ev"])
                    except ValueError:
                        line.append(None)
                grid.append(line)
            R.sensitivity = {"wacc": grid_w, "growth": grid_g, "ev": grid}
            tv_share = out["base"]["pv_terminal"] / out["base"]["ev"] if out["base"]["ev"] else None
            R.methods.append(MethodResult(
                "dcf", "DCF (5-year forecast)", "dcf", out["low"]["ev"], out["base"]["ev"], out["high"]["ev"],
                {"inputs": inp, "terminal_value_share": tv_share, "own_growth": own, "peer_growth": pg,
                 "terminal_growth": g, "pv_explicit": out["base"]["pv_explicit"],
                 "pv_terminal": out["base"]["pv_terminal"], "terminal_value": out["base"]["terminal_value"]}))
            if tv_share is not None and tv_share > 0.75:
                warnings.append(f"{tv_share:.0%} of the DCF value sits in the terminal value — it leans heavily on "
                                "the long-run assumptions.")

    if not R.methods:
        return _not_valued("negative_earnings", "(no method produced a value)", **base)

    # ===== headline ==========================================================================
    # Base = weighted mix of the DCF and the (averaged) multiples methods. The range covers the
    # scenario spread of that mix AND the gap between the methods, so it never looks narrower
    # than the disagreement behind it: when the methods agree it is just the scenario range,
    # when they diverge it stretches to the lowest and highest individual method value.
    mult_methods = [m for m in R.methods if m.family == "multiples"]
    dcf_m = R.method("dcf")
    mult_avg = ({k: mean(getattr(m, f"ev_{k}") for m in mult_methods) for k in ("low", "base", "high")}
                if mult_methods else None)
    w_dcf = A.num("method_weight_dcf")
    if mult_avg and dcf_m:
        head = {k: w_dcf * getattr(dcf_m, f"ev_{k}") + (1 - w_dcf) * mult_avg[k] for k in ("low", "base", "high")}
        mid = (dcf_m.ev_base + mult_avg["base"]) / 2
        R.disagreement = abs(dcf_m.ev_base - mult_avg["base"]) / mid if mid else None
        head["low"] = min([head["low"]] + [m.ev_base for m in R.methods])
        head["high"] = max([head["high"]] + [m.ev_base for m in R.methods])
        if R.disagreement is not None and R.disagreement > A.num("disagreement_flag"):
            warnings.append(
                f"The DCF (€{dcf_m.ev_base/1e6:,.1f}M) and the multiples (€{mult_avg['base']/1e6:,.1f}M) differ by "
                f"{R.disagreement:.0%}. Listed-sector multiples carry a growth/quality premium a small private "
                "company may not earn, while the DCF prices its risk in full — read the range, not the midpoint.")
    else:
        head = mult_avg or {k: getattr(dcf_m, f"ev_{k}") for k in ("low", "base", "high")}
    R.ev_low, R.ev_base, R.ev_high = head["low"], head["base"], head["high"]
    R.implied_ev_ebitda = R.ev_base / ebitda_norm
    if R.net_debt is not None and not R.equity_withheld:
        R.equity_low, R.equity_base, R.equity_high = (R.ev_low - R.net_debt, R.ev_base - R.net_debt,
                                                      R.ev_high - R.net_debt)
        if R.equity_base < 0:
            warnings.append("Net debt exceeds the enterprise value — equity value is negative.")
        if R.net_debt / ebitda_norm > 4:
            penalise(-5, f"High leverage (net debt {R.net_debt / ebitda_norm:.1f}× EBITDA)")

    # ---- value at stake: the margin gap to peers, in euros (NOT part of the valuation)
    if R.peer and R.peer.median_margin is not None and margin_norm < R.peer.median_margin:
        gap = R.peer.median_margin - margin_norm
        uplift = gap * rev
        R.value_at_stake = {"peer_median_margin": R.peer.median_margin, "gap_pp": gap, "peers": R.peer.n,
                            "ebitda_uplift": uplift, "ev_uplift": uplift * R.implied_ev_ebitda,
                            "multiple": R.implied_ev_ebitda}

    # ---- inputs used (transparency)
    R.inputs_used = _inputs_used(ref, industry, param_country, dr, A, mult, override, nwc_pct, tax, da_pct, capex_pct)

    score = max(0, 100 + sum(p for p, _ in penalties))
    R.confidence_score, R.confidence_grade = score, _grade(score)
    return R


def _x(v) -> str:
    return f"{v:.2f}×" if v is not None else "—"


def _inputs_used(ref, industry, country, dr, A, mult, override, nwc_pct, tax, da_pct, capex_pct) -> List[dict]:
    def row(name, value, source):
        return {"Input": name, "Value": value, "Source": source}
    m_asof, w_asof, c_asof = ref.as_of("multiples"), ref.as_of("wacc"), ref.as_of("country_risk")
    rows = [
        row(f"Sector ({industry}) EV/EBITDA, listed Europe", _x(mult.get("ev_ebitda_pos")),
            f"Damodaran, {mult.get('n_firms') or 0:.0f} firms, as of {m_asof}"),
        row("Sector EV/EBIT, listed Europe", _x(mult.get("ev_ebit_pos")), f"Damodaran, as of {m_asof}"),
        row("Sector cost of capital (EUR)", f"{dr.sector_wacc:.2%}", f"Damodaran, as of {w_asof}"),
        row("Sector beta / equity weight", f"{dr.beta:.2f} / {dr.equity_weight:.0%}", f"Damodaran, as of {w_asof}"),
        row(f"Equity risk premium — {country}", f"{dr.country_erp:.2%}", f"Damodaran country risk, as of {c_asof}"),
        row("Equity risk premium — Europe average (baked into the sector rate)", f"{dr.region_erp:.2%}",
            f"Damodaran, as of {w_asof}"),
        row("Country-risk adjustment to the discount rate", f"{dr.country_adjustment:+.2%}",
            "equity weight × beta × (country − Europe ERP)"),
        row("Size & illiquidity premium", f"{dr.size_premium:.2%}", "Assumption — " + A.source("size_premium", country)),
        row("Company discount rate (WACC)", f"{dr.wacc:.2%}", "sector rate + country adjustment + size premium"),
        row("Long-run growth", f"{A.num('terminal_growth', country):.2%}", "Assumption — " + A.source("terminal_growth", country)),
        row("Corporate tax rate", f"{tax:.2%}" if tax is not None else "—", f"Damodaran country tax rates, as of {c_asof}"),
        row("Sector non-cash working capital / sales", f"{nwc_pct:.1%}" if nwc_pct is not None else "—",
            f"Damodaran, as of {ref.as_of('working_capital')}"),
        row("D&A / revenue", f"{da_pct:.1%}" if da_pct is not None else "—", "Company: EBITDA − EBIT, 3-yr average"),
        row("Capex / revenue", f"{capex_pct:.1%}" if capex_pct is not None else "—",
            "Company: material + immaterial capex, 3-yr average (else = D&A)"),
    ]
    if override:
        rows.insert(0, row(f"Manual EV/EBITDA for {industry}", f"{override:.2f}×", "Entered on the Assumptions tab"))
    return rows


def value_universe(companies: List[dict], fins: Dict[str, Financials], ctx: ValuationContext) -> List[ValuationResult]:
    return [value_company(c, fins.get(c["id"]), ctx) for c in companies]


def peers_from_universe(companies: List[dict], fins: Dict[str, Financials], ctx: ValuationContext) -> Dict[tuple, PeerStats]:
    obs = [peer_observation(c, fins[c["id"]], ctx) for c in companies if c["id"] in fins]
    return build_peer_stats(obs)
