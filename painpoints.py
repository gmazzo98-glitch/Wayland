"""
Pain-point detection: from indicator values to named, evidenced problems.

Need/Readiness (scoring.py) say HOW MUCH a company could use a pilot. A pain point
says WHAT the company is struggling with, in terms a startup can be matched against
("debt-service strain", "legacy systems", "stalling revenue") — and it never says
anything the indicators don't back:

  * A pain point is defined only by indicator rules (key, direction, warn/severe
    thresholds, weight). There is no free-text guessing and no LLM in the loop.
  * Every detection carries its evidence: which indicator, what value, what threshold,
    from which source, fetched when, live or stale.
  * Only real observations count. A simulated placeholder is never evidence
    (scoring.py still scores those; a claim about a company's problems must not).
  * "Not checked" is not "no pain". A driver with no usable data is listed as a gap —
    with the source that would fill it — and the pain point reports how much of its
    evidence it actually has. A pain point with no usable driver at all is
    "insufficient_data", never "clear".
  * Severity is capped by evidence coverage: one lonely low-weight driver can raise
    an "emerging" flag but cannot, on its own, claim a "severe" pain point.

Pain points are a diagnosis layered on top of the two axes, never blended into either
(see project-vienna-overview: Need and Readiness stay separate). They read indicators
and write nothing.

Growing the evidence base is meant to be cheap: a rule may name an indicator that the
catalog doesn't have yet (see PROPOSED_INDICATORS). Until a producer (API, crawler,
derivation or manual entry) writes that signal, the driver shows up as a named gap;
the day it does, the pain point starts using it with no code change here.

The seed thresholds below are starting judgment calls, same status as the weights in
indicators.py — they are tuned from the Pain Points page. Where a number was set
against real data, the comment says so.
"""

from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy.orm import Session

from models import PainPointDefinition

# ---- severity ---------------------------------------------------------------------------------

# A pain point's score is the weight-averaged intensity (0-100) of the drivers that could be
# assessed. These cut it into named bands.
EMERGING_FROM = 15.0
MODERATE_FROM = 40.0
SEVERE_FROM = 70.0

# Guardrails: how much of a pain point's total driver weight has real data behind it caps how
# bad the flag may say it is. Below 30% coverage: at most "emerging"; below 50%: at most "moderate".
COVERAGE_CAP_EMERGING = 0.30
COVERAGE_CAP_MODERATE = 0.50
WELL_EVIDENCED_COVERAGE = 0.60

STATUS_ORDER = ["clear", "emerging", "moderate", "severe"]   # weakest -> strongest
DETECTED_STATUSES = ("emerging", "moderate", "severe")
CONFIRMED_STATUSES = ("moderate", "severe")

UNASSESSED_REASONS_THAT_ARE_GAPS = ("not_yet_checked", "not_in_catalog", "simulated")


# ---- the catalog ------------------------------------------------------------------------------

def _r(indicator: str, worse_when: str, warn: float, severe: float, weight: float = 1.0, unit: str = "",
       absent_means: str = "ignore", valid_range: Optional[tuple] = None, negative_is_severe: bool = False,
       note: str = None) -> dict:
    """One driver. `warn` is where pain begins (intensity 0), `severe` where it is at full
    strength (intensity 1); intensity is linear in between. worse_when: 'higher' means a bigger
    raw value is worse (cost share), 'lower' means a smaller one is (interest cover)."""
    rule = {"indicator": indicator, "worse_when": worse_when, "warn": warn, "severe": severe,
            "weight": weight, "unit": unit, "absent_means": absent_means, "active": True}
    if valid_range:
        rule["valid_range"] = list(valid_range)
    if negative_is_severe:
        rule["negative_is_severe"] = True
    if note:
        rule["note"] = note
    return rule


CAT_FIN = "Financial"
CAT_COST = "Cost & supply chain"
CAT_DIGITAL = "Digital & systems"
CAT_MARKET = "Market & product"
CAT_PEOPLE = "People & leadership"
CAT_RISK = "Risk & compliance"

# A trend of +/-999% is the base-year-near-zero artefact company_detail.py already refuses to
# display (`_trend_headline`), so trend rules exclude it rather than score it.
_TREND_RANGE = (-999, 999)

_SEED_ROWS = [
    # ------------------------------------------------------------------ Financial
    dict(key="growth_stall", label="Stalling or shrinking top line", category=CAT_FIN,
         description="Revenue is flat or falling — in nominal terms, so with inflation the company is losing ground.",
         pilot_angle="New sales channels, demand generation, pricing optimisation, customer-retention tooling.",
         caveat="Check whether the whole sector is flat before reading it as company-specific (Revenue Growth vs. Sector does this when available).",
         rules=[
             _r("revenue_trend", "lower", 3, -20, 3, "%", valid_range=_TREND_RANGE,
                note="% change, latest fiscal year vs the earliest of the last three. Set against the real portfolio: -20% is its 10th percentile."),
             _r("revenue_growth_vs_sector", "lower", -2, -15, 2, " pp"),
         ]),
    dict(key="profit_erosion", label="Eroding profitability", category=CAT_FIN,
         description="Operating profit is falling, or the margin left is thin — cost or pricing pressure the company has not solved.",
         pilot_angle="Process automation, yield/waste reduction, procurement and dynamic-pricing tools.",
         caveat="A severe multi-year EBIT decline is also a distress flag: budget for a pilot may be scarce (a Readiness matter, kept separate).",
         rules=[
             _r("ebit_trend", "lower", -10, -60, 3, "%", valid_range=_TREND_RANGE,
                note="% change in EBIT, sign-safe when the base year was a loss."),
             _r("ebit_margin", "lower", 4, -2, 3, "%",
                note="EBIT ÷ revenue, latest year. Real portfolio: median 5.9%, 10th percentile -3.6%."),
             _r("margin_compression", "higher", 1, 8, 2, " pp", valid_range=(0, 100),
                note="Fall in gross margin as % of revenue, earliest vs latest year (0 when it did not fall). "
                     "Real portfolio: 10th percentile of the change is -6.1 pp."),
         ]),
    dict(key="debt_service_strain", label="Debt-service strain", category=CAT_FIN,
         description="Earnings barely cover the interest bill, or the balance sheet is heavily geared — money is going to lenders, not to change.",
         pilot_angle="Working-capital and cash-flow tooling, cost-out programmes with fast payback, alternative financing.",
         caveat="Also lowers Readiness: covenants and thin cash often block committing to a pilot. Confirm budget at first contact.",
         rules=[
             _r("interest_coverage_ratio", "lower", 3, 1.5, 3, "×",
                note="EBIT ÷ interest expense. Below ~3× is generally read as weak cover; real portfolio 10th percentile is 2.9×."),
             _r("leverage_ratio", "higher", 4, 8, 2, "×", negative_is_severe=True,
                note="AIDA's 'Rapporto di indebitamento' = total assets ÷ equity (checked on all 954 rows). 4× means equity funds a quarter "
                     "of the balance sheet; negative means negative equity, which counts as fully severe."),
         ]),
    dict(key="liquidity_squeeze", label="Thin cash cushion", category=CAT_FIN,
         description="Cash on hand covers only a few weeks of revenue — little room to absorb a bad quarter or fund change.",
         pilot_angle="Receivables/payables automation, inventory optimisation, cash-flow forecasting.",
         caveat="Also lowers Readiness (no discretionary budget).",
         rules=[
             _r("cash_to_revenue", "lower", 0.75, 0.15, 1, " months of revenue",
                note="Cash ÷ (revenue ÷ 12). Real portfolio: median 1.4 months, 25th percentile 0.5, 10th 0.1."),
         ]),
    dict(key="underinvestment", label="Under-investment in capacity", category=CAT_FIN,
         description="Capital spending is very low relative to revenue — equipment and technology are being sweated rather than renewed.",
         pilot_angle="Retrofit/upgrade kits, leasing-based automation, predictive maintenance.",
         caveat="Can also be deliberate cash preservation; it may extend to reluctance to fund a pilot.",
         rules=[
             _r("capex_ratio", "lower", 1.5, 0.3, 1, "% of revenue",
                note="Set against the real portfolio: median 1.8% of revenue, 10th percentile ~0%."),
         ]),

    # ------------------------------------------------------------------ Cost & supply chain
    dict(key="input_cost_pressure", label="Input-cost pressure", category=CAT_COST,
         description="Energy, materials or purchased services take an unusually large share of revenue — and the share is the company's to fix.",
         pilot_angle="Energy-efficiency and metering, material substitution, procurement analytics.",
         caveat="Sector-dependent: compare against the sector's typical cost structure before reading a high share as a company problem.",
         rules=[
             _r("energy_cost", "higher", 8, 13, 3, "% of revenue"),
             _r("materials_cost", "higher", 50, 65, 2, "% of revenue",
                note="Materials ÷ revenue. Machinery makers in the real portfolio: median 44%, 75th percentile 53%, 90th 63%."),
             _r("raw_material_cost", "higher", 45, 62, 2, "% of COGS"),
             _r("cogs_ratio", "higher", 70, 85, 2, "% of revenue"),
             _r("service_costs", "higher", 14, 22, 1, "% of revenue"),
         ]),
    dict(key="concentration_risk", label="Customer / supplier concentration", category=CAT_COST,
         description="Revenue rides on a few customers, or critical inputs on a few suppliers — losing one is a crisis.",
         pilot_angle="Customer-diversification and lead-generation tools, supplier discovery and dual-sourcing platforms.",
         caveat="Concentration is normal for some OEM-supplier models; weigh against what is typical for the sector.",
         rules=[
             _r("customer_concentration", "higher", 40, 65, 3, "% of revenue"),
             _r("supplier_concentration", "lower", 3, 1, 2, " alternative suppliers"),
             _r("raw_material_rarity", "lower", 3, 1, 1, " alternative suppliers"),
         ]),
    dict(key="logistics_complexity", label="Complex or costly logistics", category=CAT_COST,
         description="Goods pass through many hands on the way to the customer, or freight takes a big cut of revenue.",
         pilot_angle="Route and load optimisation, warehouse automation, shipment-visibility platforms.",
         caveat="Not applicable to services or software businesses.",
         rules=[
             _r("logistics_difficulty", "higher", 3.5, 5, 3, " handling steps"),
             _r("logistics_cost", "higher", 10, 16, 2, "% of revenue"),
         ]),

    # ------------------------------------------------------------------ Digital & systems
    dict(key="legacy_systems", label="Legacy core systems", category=CAT_DIGITAL,
         description="Old, poorly integrated ERP and supply-chain processes — manual work and data that can't feed anything new.",
         pilot_angle="ERP integration layers, process-mining, supplier-portal and EDI automation.",
         caveat="A legacy ERP also means data-readiness must be scoped explicitly before any pilot starts.",
         rules=[
             _r("erp_systems_age", "higher", 8, 12, 3, " yrs"),
             _r("supply_chain_digitization", "lower", 2.5, 1, 2, " / 5"),
         ]),
    dict(key="digital_channel_lag", label="Weak digital & online presence", category=CAT_DIGITAL,
         description="An outdated website, thin online presence and little digital selling — the company is hard to find and hard to buy from.",
         pilot_angle="E-commerce and B2B portals, marketing automation, product-configurator tools.",
         caveat="Pure B2B firms with no consumer channel score low on some of these without it being a problem.",
         rules=[
             _r("website_digital_maturity", "higher", 4, 7, 3, " yrs since redesign"),
             _r("online_market_presence", "lower", 2.5, 1, 3, " / 5"),
             _r("online_sales_volume", "lower", 10, 2, 2, "% of sales"),
             _r("tech_stack_intensity", "lower", 3, 1, 1.5, " technologies"),
             _r("competitor_digital_gap", "higher", 15, 40, 2, " pts"),
             _r("linkedin_activity", "lower", 2, 0.5, 1, " posts/month"),
             _r("physical_stores_trend", "lower", -5, -25, 1.5, "%",
                note="Shrinking physical footprint without a compensating digital channel. Not applicable to pure B2B."),
         ]),

    # ------------------------------------------------------------------ Market & product
    dict(key="product_obsolescence", label="Ageing or undifferentiated products", category=CAT_MARKET,
         description="The product line has not changed in years, launches are absent from the press, and it sits among many look-alikes.",
         pilot_angle="Product-development and co-innovation partners, digital add-on services, rapid prototyping.",
         caveat="A quiet innovator can look stagnant in the press — corroborate with trademarks and R&D before concluding.",
         rules=[
             _r("product_innovativeness", "higher", 4, 8, 3, " yrs since last update"),
             _r("press_launch_mentions", "lower", 1, 0, 2, " launch mentions", absent_means="zero"),
             _r("product_differentiation", "higher", 8, 13, 2, " comparable products"),
             _r("product_portfolio_diversity", "lower", 3, 1, 1, " product lines",
                note="A narrow line-up in a stable niche is not automatically a problem."),
         ]),
    dict(key="quality_decline", label="Declining product quality", category=CAT_MARKET,
         description="Customer reviews are trending down — a directly visible, externally verifiable quality problem.",
         pilot_angle="Inline quality inspection, complaint analytics, traceability tooling.",
         caveat="Review volume is thin for most B2B manufacturers; treat a single low-count reading as directional.",
         rules=[
             _r("product_quality_trend", "lower", -0.2, -1.0, 1, " rating pts"),
         ]),
    dict(key="market_reach_gap", label="Unrealised market reach", category=CAT_MARKET,
         description="Little export business and short international experience — a viable product not yet sold beyond its home region.",
         pilot_angle="Cross-border sales enablement, market-entry and localisation services, export compliance tooling.",
         caveat="Also a Readiness caveat: no internal playbook for cross-border expansion.",
         rules=[
             _r("international_sales_volume", "lower", 20, 5, 3, "% of sales"),
             _r("years_international_activity", "lower", 5, 1, 2, " yrs"),
             _r("store_geo_distribution", "lower", 3, 1, 1, " regions",
                note="Not applicable to pure B2B."),
         ]),

    # ------------------------------------------------------------------ People & leadership
    dict(key="workforce_strain", label="Workforce strain", category=CAT_PEOPLE,
         description="Heavy hiring, high staff turnover, poor employer reviews or a heavy payroll share — the company struggles to staff and keep its people.",
         pilot_angle="Recruiting and onboarding platforms, workforce planning, automation of repetitive work.",
         caveat="Can reflect a tight regional labour market rather than a company-specific problem.",
         rules=[
             _r("job_posting_velocity", "higher", 8, 15, 2, " open roles"),
             _r("employee_turnover", "higher", 15, 25, 3, "%"),
             _r("kununu_rating", "lower", 3.4, 2.8, 2, " / 5"),
             _r("labour_cost", "higher", 35, 50, 2, "% of revenue"),
         ]),
    dict(key="leadership_transition", label="Leadership transition risk", category=CAT_PEOPLE,
         description="An ageing, long-entrenched or frequently changing top team — the company has no settled next generation steering it.",
         pilot_angle="Succession-support and knowledge-capture tooling, management-information dashboards for a new generation.",
         caveat="Read jointly with New Generation of Management and family ownership: a completed handover reduces this pain and is itself a strong outreach trigger.",
         rules=[
             _r("management_age", "higher", 58, 64, 3, " yrs"),
             _r("senior_mgmt_tenure", "higher", 15, 25, 2, " yrs"),
             _r("management_turnover", "higher", 2, 5, 1.5, " changes in 3 yrs",
                note="Counted from the imported roster, which can include non-executive roles — low weight for that reason."),
         ]),

    # ------------------------------------------------------------------ Risk & compliance
    dict(key="compliance_and_risk", label="Compliance deadline or rising risk cost", category=CAT_RISK,
         description="A mandatory regime (CSRD, CBAM, supply-chain due diligence…) is about to apply, or insurance/claims costs are climbing.",
         pilot_angle="Reporting and data-collection automation, compliance monitoring, risk analytics and safety tech.",
         caveat="Regulatory scope thresholds move; the exposure count is recorded manually until a maintained threshold table exists.",
         rules=[
             _r("regulatory_compliance_exposure", "higher", 1, 2, 4, " regimes"),
             _r("insurance_claims_trend", "higher", 8, 25, 1, "%"),
         ]),
]

PAIN_POINT_SEED = [dict(row, sort_order=(i + 1) * 10) for i, row in enumerate(_SEED_ROWS)]


# Need-axis indicators deliberately NOT driving any pain point, each with the reason. A test
# fails when a need-axis indicator is neither used by a rule nor listed here, so a newly
# catalogued indicator can't silently stay invisible to pain-point detection.
EXEMPT_NEED_INDICATORS = {
    "new_generation_management": "A trigger event and outreach-timing signal, not a problem the company has.",
    "debt_level": "An absolute k€ level says nothing without a size denominator; debt burden is judged through "
                  "interest_coverage_ratio and leverage_ratio.",
    "sector_export_exposure": "A sector-level macro figure: it would fire identically for every company in a sector, "
                              "so it is not evidence about any one company.",
    "product_age": "Today's producer records years since the company's founding / first product (93, 61, 52…), not the age "
                   "of the core product line — it would flag every long-established firm as obsolete.",
}


# Indicators the pain-point rules already name but the catalog doesn't have (or has without a producer).
# Each entry is the plan for filling that gap; the Pain Points page lists them, ranked by what they'd
# unlock. `status`: 'ready_to_build' = the inputs already sit in the database, only the derivation is
# missing; 'needs_decision' = buildable, but it would change existing Need scores or needs a
# calibration call first; 'needs_source' = a new data source or crawler step.
PROPOSED_INDICATORS = {
    "ebit_margin": dict(
        label="EBIT margin", kind="derivation", status="ready_to_build",
        how="EBIT ÷ revenue (latest year), from the imported AIDA rows.",
        evidence="ebit_latest and revenue_latest are present for all 953 imported companies.",
        feeds=["profit_erosion"]),
    "cash_to_revenue": dict(
        label="Cash cover (months of revenue)", kind="derivation", status="ready_to_build",
        how="Cash ÷ (revenue ÷ 12), from the imported AIDA rows.",
        evidence="cash_latest and revenue_latest are present for all 953 imported companies.",
        feeds=["liquidity_squeeze"]),
    "capex_ratio": dict(
        label="CapEx ratio (existing indicator, no producer yet)", kind="derivation", status="needs_decision",
        how="(Material + immaterial capex) ÷ revenue, from the imported AIDA rows.",
        evidence="Inputs present for all 953. But it is a Need-axis indicator (weight 2), so populating it moves Need scores, "
                 "and its 1-15% bounds would rate the median company (1.8%) as near-maximum need — recalibrate first.",
        feeds=["underinvestment"]),
    "cogs_ratio": dict(
        label="COGS ratio (existing indicator, no valid input)", kind="needs_source", status="needs_decision",
        how="Italian statutory accounts have no cost-of-goods-sold line. Use Materials Cost (materie prime e consumo) instead.",
        evidence="The imported 'cogs' column is AIDA's 'Costi della produzione' — total production costs, median 96% of revenue — "
                 "so it must not be used as COGS.",
        feeds=["input_cost_pressure"]),
    "materials_cost": dict(
        label="Materials cost (existing indicator, no producer yet)", kind="derivation", status="ready_to_build",
        how="'Materie prime e consumo' ÷ 'Ricavi vendite e prestazioni' (latest year) — add the column to the AIDA import.",
        evidence="Filled for 954 of 954 companies in WAYLAND_FINANCIAL_PL_C28_SME_50_99_V0.xls but not in the imported Main file. "
                 "Median 44% of revenue, 90th percentile 63%. It is a Need-axis indicator, so it moves Need scores once populated.",
        feeds=["input_cost_pressure"]),
    "labour_cost": dict(
        label="Labour cost (existing indicator, no producer yet)", kind="derivation", status="ready_to_build",
        how="'Totale costi del personale' ÷ 'Ricavi vendite e prestazioni' (latest year) — add the column to the AIDA import.",
        evidence="Filled for 954 of 954 companies in WAYLAND_FINANCIAL_PL_C28_SME_50_99_V0.xls but not in the imported Main file. "
                 "Median 23% of revenue, 90th percentile 37.5%. Need-axis indicator: moves Need scores once populated.",
        feeds=["workforce_strain"]),
    "reported_distress_news": dict(
        label="Distress mentions in the press", kind="crawler", status="needs_source",
        how="Add insolvency / short-time-work (Cassa Integrazione) / lay-off / energy-cost concept buckets to the "
            "Google News RSS adapter, which already fetches and buckets each company's headlines locally.",
        evidence="adapters/google_news_rss.py — a new bucket, not a new source.",
        feeds=["liquidity_squeeze", "workforce_strain", "profit_erosion"]),
    "review_complaint_themes": dict(
        label="Customer-complaint themes", kind="crawler", status="needs_source",
        how="Classify the review crawler's recent review snippets into delivery / quality / service themes.",
        evidence="The review crawler already returns recent_review_snippets (empty when no profile is found).",
        feeds=["quality_decline", "logistics_complexity"]),
    "legal_procedure_flag": dict(
        label="Open insolvency / legal procedure", kind="api", status="needs_source",
        how="The AIDA export has a legal_procedure_flag column; it was empty in the imported file — re-export it filled.",
        evidence="Column exists in the imported raw rows (all values blank).",
        feeds=["liquidity_squeeze", "debt_service_strain"]),
}


# ---- catalog access ---------------------------------------------------------------------------

def validate_rules(rules: List[dict]) -> List[str]:
    """Human-readable problems with a rule list (empty when fine) — used by the editor before saving."""
    problems = []
    for r in rules:
        name = r.get("indicator", "?")
        if r.get("worse_when") not in ("higher", "lower"):
            problems.append(f"{name}: worse_when must be 'higher' or 'lower'")
            continue
        warn, severe = r.get("warn"), r.get("severe")
        if warn is None or severe is None:
            problems.append(f"{name}: warn and severe are both required")
        elif r["worse_when"] == "higher" and not severe > warn:
            problems.append(f"{name}: severe ({severe}) must be above warn ({warn}) when higher is worse")
        elif r["worse_when"] == "lower" and not severe < warn:
            problems.append(f"{name}: severe ({severe}) must be below warn ({warn}) when lower is worse")
        if r.get("weight") is None or r["weight"] < 0:
            problems.append(f"{name}: weight must be 0 or more")
        if r.get("absent_means", "ignore") not in ("ignore", "zero", "pain"):
            problems.append(f"{name}: absent_means must be ignore, zero or pain")
    return problems


def seed_pain_point_definitions(db: Session) -> int:
    """Insert catalog rows not already present. Never overwrites an existing row — a threshold
    edited from the Pain Points page must survive every restart (same rule as the indicators)."""
    existing = {k for (k,) in db.query(PainPointDefinition.key).all()}
    inserted = 0
    for row in PAIN_POINT_SEED:
        if row["key"] not in existing:
            db.add(PainPointDefinition(**row))
            inserted += 1
    if inserted:
        db.commit()
    return inserted


# Guarded edits to rows that already exist in a database (the seed never overwrites, so a better default
# can't otherwise reach them). Same contract as indicators.CATALOG_MIGRATIONS: an edit applies only while
# the rule still holds exactly the values the seed used to ship; a rule a human has changed is left alone.
# `replace_with` swaps the whole rule (its indicator changed); `changes` edits fields of one rule.
PAIN_POINT_MIGRATIONS = [
    dict(id="margin-compression-fixed", key="profit_erosion", indicator="gross_margin_change_pp",
         guard=dict(worse_when="lower", warn=-1, severe=-8, weight=2),
         replace_with=_r("margin_compression", "higher", 1, 8, 2, " pp", valid_range=(0, 100),
                         note="Fall in gross margin as % of revenue, earliest vs latest year (0 when it did not fall). "
                              "Real portfolio: 10th percentile of the change is -6.1 pp.")),
    dict(id="leverage-definition", key="debt_service_strain", indicator="leverage_ratio",
         changes={"note": (
             "Catalogued as Debt/EBITDA or Debt/Equity. A negative value means negative equity or negative EBITDA under either reading, so it counts as fully severe.",
             "AIDA's 'Rapporto di indebitamento' = total assets ÷ equity (checked on all 954 rows). 4× means equity funds a quarter "
             "of the balance sheet; negative means negative equity, which counts as fully severe.")}),
    dict(id="materials-calibrated", key="input_cost_pressure", indicator="materials_cost",
         changes={"warn": (40, 50), "severe": (55, 65),
                  "note": (None, "Materials ÷ revenue. Machinery makers in the real portfolio: median 44%, 75th percentile 53%, 90th 63%.")}),
]


def apply_pain_point_migrations(db: Session, dry_run: bool = False) -> dict:
    """Applies PAIN_POINT_MIGRATIONS. Returns {"applied": [(key, indicator)...], "skipped": [(key, indicator, why)...]}.
    Idempotent: a rule already in its new shape is neither changed nor reported. dry_run reports and writes nothing."""
    applied, skipped = [], []
    for mig in PAIN_POINT_MIGRATIONS:
        row = db.query(PainPointDefinition).filter_by(key=mig["key"]).first()
        if row is None:
            continue
        rules = [dict(r) for r in (row.rules or [])]
        idx = next((i for i, r in enumerate(rules) if r["indicator"] == mig["indicator"]), None)
        if idx is None:
            continue   # already replaced, or removed by a human
        rule, changed = rules[idx], False
        if "replace_with" in mig:
            if all(rule.get(k) == v for k, v in mig["guard"].items()):
                if mig["replace_with"]["indicator"] in {r["indicator"] for r in rules}:
                    rules.pop(idx)      # the replacement is already there; just drop the old rule
                else:
                    rules[idx] = dict(mig["replace_with"])
                changed = True
            else:
                skipped.append((mig["key"], mig["indicator"], "rule was edited"))
        else:
            for field, (old, new) in mig["changes"].items():
                if rule.get(field) == new:
                    continue
                if rule.get(field) == old:
                    rule[field] = new
                    changed = True
                else:
                    skipped.append((mig["key"], mig["indicator"], f"{field} was edited"))
        if changed:
            if not dry_run:
                row.rules = rules          # a fresh list: SQLAlchemy doesn't see in-place JSON mutation
            applied.append((mig["key"], mig["indicator"]))
    if applied and not dry_run:
        db.commit()
    return {"applied": applied, "skipped": skipped}


def fetch_pain_point_defs(db: Session, include_inactive: bool = False) -> List[dict]:
    q = db.query(PainPointDefinition)
    if not include_inactive:
        q = q.filter_by(is_active=True)
    return [r.to_dict() for r in q.order_by(PainPointDefinition.sort_order, PainPointDefinition.key).all()]


# ---- evaluation -------------------------------------------------------------------------------

def _num(v: float) -> str:
    if v is None:
        return "—"
    if abs(v - round(v)) < 1e-9:
        return f"{int(round(v)):,}"
    if abs(v) >= 10:
        return f"{v:,.1f}".rstrip("0").rstrip(".")
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _with_unit(v: float, unit: str) -> str:
    return f"{_num(v)}{unit or ''}"


def _threshold_text(rule: dict) -> str:
    unit = rule.get("unit", "")
    if rule["worse_when"] == "higher":
        return f"pain from {_with_unit(rule['warn'], unit)}, full strength at {_with_unit(rule['severe'], unit)}"
    return f"pain below {_with_unit(rule['warn'], unit)}, full strength at {_with_unit(rule['severe'], unit)}"


def _intensity(value: float, rule: dict) -> float:
    """0.0 (no pain) .. 1.0 (full strength): linear between the rule's warn and severe values."""
    warn, severe = rule["warn"], rule["severe"]
    if rule["worse_when"] == "higher":
        span, gone = severe - warn, value - warn
    else:
        span, gone = warn - severe, warn - value
    if span <= 0:
        return 1.0 if gone > 0 else 0.0
    return min(1.0, max(0.0, gone / span))


def _producer_info(defn: Optional[dict], indicator: str) -> Dict[str, Any]:
    if defn:
        return {"source_system": defn.get("source_system"), "tier": defn.get("automation_tier"),
                "phase": defn.get("phase"), "proposed": False}
    prop = PROPOSED_INDICATORS.get(indicator)
    if prop:
        return {"source_system": None, "tier": None, "phase": None, "proposed": True,
                "kind": prop["kind"], "status": prop["status"], "how": prop["how"]}
    return {"source_system": None, "tier": None, "phase": None, "proposed": False}


def _unassessed(driver: dict, reason: str, text: str) -> dict:
    driver.update(assessed=False, state="unassessed", intensity=None, unassessed_reason=reason, unassessed_text=text)
    return driver


def _evaluate_rule(rule: dict, signal_map: Dict[str, Dict[str, Any]], indicator_defs: Dict[str, Dict[str, Any]],
                   weight_share: float) -> dict:
    key = rule["indicator"]
    defn = indicator_defs.get(key)
    sig = signal_map.get(key)
    producer = _producer_info(defn, key)
    driver = {
        "indicator": key, "label": (defn or {}).get("label") or PROPOSED_INDICATORS.get(key, {}).get("label") or key,
        "weight": rule["weight"], "weight_share": weight_share, "unit": rule.get("unit", ""),
        "worse_when": rule["worse_when"], "warn": rule["warn"], "severe": rule["severe"],
        "threshold_text": _threshold_text(rule), "note": rule.get("note"), "producer": producer,
        "value": None, "value_text": None, "source": None, "fetched_at": None, "confidence": None,
        "is_stale": False, "summary": None, "assessed": False, "state": "unassessed", "intensity": None,
        "unassessed_reason": None, "unassessed_text": None,
    }

    if defn is None:
        prop = PROPOSED_INDICATORS.get(key)
        return _unassessed(driver, "not_in_catalog",
                           f"No indicator '{key}' in the catalog yet" + (f" — {prop['how']}" if prop else ""))
    if sig is None or sig["status"] == "not_yet_checked":
        src = producer.get("source_system")
        return _unassessed(driver, "not_yet_checked",
                           "Not checked yet" + (f" — source: {src} ({producer.get('tier') or '?'}, phase {producer.get('phase')})" if src else ""))

    driver.update(source=sig.get("source"), fetched_at=sig.get("fetched_at"), confidence=sig.get("confidence"),
                  summary=sig.get("summary"), is_stale=(sig["status"] == "stale"))
    # Strict: only a signal positively recorded as a real observation counts. A missing flag
    # (a hand-built dict) is not proof of a real fetch.
    if sig.get("is_simulated") is not False:
        return _unassessed(driver, "simulated", "Only a simulated placeholder exists — never used as evidence")

    value = sig.get("value")
    if sig["status"] == "absent" and value is None:
        mode = rule.get("absent_means", "ignore")
        if mode == "pain":
            driver.update(assessed=True, state="fired", intensity=1.0, value_text="checked — none found")
            return driver
        if mode == "zero":
            value = 0.0
        else:
            return _unassessed(driver, "absent_not_informative",
                               "Checked, nothing found — not treated as evidence either way")
    if value is None:
        return _unassessed(driver, "no_value", "Recorded without a numeric value")

    driver["value"], driver["value_text"] = value, _with_unit(value, rule.get("unit", ""))
    if value < 0 and rule.get("negative_is_severe"):
        driver.update(assessed=True, state="fired", intensity=1.0)
        return driver
    rng = rule.get("valid_range")
    if rng and not (rng[0] <= value <= rng[1]):
        return _unassessed(driver, "implausible",
                           f"Value {_num(value)}{rule.get('unit', '')} is outside the plausible range "
                           f"{_num(rng[0])}..{_num(rng[1])} (typically a near-zero base year) — ignored")

    intensity = _intensity(value, rule)
    driver.update(assessed=True, intensity=intensity, state="fired" if intensity > 0 else "clear")
    return driver


def status_for_score(score: float) -> str:
    if score >= SEVERE_FROM:
        return "severe"
    if score >= MODERATE_FROM:
        return "moderate"
    if score >= EMERGING_FROM:
        return "emerging"
    return "clear"


def _cap_for_coverage(status: str, coverage: float) -> str:
    if coverage < COVERAGE_CAP_EMERGING:
        cap = "emerging"
    elif coverage < COVERAGE_CAP_MODERATE:
        cap = "moderate"
    else:
        cap = "severe"
    return status if STATUS_ORDER.index(status) <= STATUS_ORDER.index(cap) else cap


def evaluate_pain_point(pdef: dict, signal_map: Dict[str, Dict[str, Any]],
                        indicator_defs: Dict[str, Dict[str, Any]]) -> dict:
    """
    One pain point against one company's signal map (scoring.build_signal_map).

    Returns the definition's identity plus: status (severe|moderate|emerging|clear|
    insufficient_data), score (0-100, None when nothing could be assessed), coverage
    (share of driver weight with usable data), confidence, `capped` (status held back by
    thin coverage), a one-line `headline`, and the per-driver `drivers` with full evidence.
    """
    rules = [r for r in (pdef.get("rules") or []) if r.get("active", True)]
    total_weight = sum(r["weight"] for r in rules) or 0.0
    drivers = [_evaluate_rule(r, signal_map, indicator_defs, (r["weight"] / total_weight) if total_weight else 0.0)
               for r in rules]
    assessed = [d for d in drivers if d["assessed"]]
    assessed_weight = sum(d["weight"] for d in assessed)

    out = {
        "key": pdef["key"], "label": pdef["label"], "category": pdef["category"],
        "description": pdef.get("description"), "pilot_angle": pdef.get("pilot_angle"), "caveat": pdef.get("caveat"),
        "drivers": drivers, "drivers_total": len(drivers), "drivers_assessed": len(assessed),
        "coverage": (assessed_weight / total_weight) if total_weight else 0.0,
        "score": None, "status": "insufficient_data", "capped": False, "confidence": None, "headline": "",
    }
    if not assessed or assessed_weight <= 0:
        return out

    score = 100.0 * sum(d["weight"] * d["intensity"] for d in assessed) / assessed_weight
    raw_status = status_for_score(score)
    status = _cap_for_coverage(raw_status, out["coverage"])
    out.update(score=round(score, 1), status=status, capped=(status != raw_status),
               confidence=("well_evidenced" if out["coverage"] >= WELL_EVIDENCED_COVERAGE
                           and len(assessed) >= min(2, len(drivers)) else "partial"))
    fired = sorted((d for d in assessed if d["state"] == "fired"), key=lambda d: -d["intensity"] * d["weight"])
    out["headline"] = "; ".join(f"{d['label']} {d['value_text']}" for d in fired[:2] if d["value_text"])
    return out


def evaluate_pain_points(signal_map: Dict[str, Dict[str, Any]], indicator_defs: Dict[str, Dict[str, Any]],
                         pain_defs: Iterable[dict]) -> List[dict]:
    return [evaluate_pain_point(p, signal_map, indicator_defs) for p in pain_defs]


def rank_detected(results: List[dict]) -> List[dict]:
    """Detected pain points (emerging and up), strongest first."""
    hits = [r for r in results if r["status"] in DETECTED_STATUSES]
    return sorted(hits, key=lambda r: (-STATUS_ORDER.index(r["status"]), -(r["score"] or 0)))


# ---- portfolio views --------------------------------------------------------------------------

def summarize_portfolio(results_by_company: Dict[str, List[dict]], pain_defs: Iterable[dict]) -> List[dict]:
    """Per pain point: how many companies sit in each status, in the catalog's own order."""
    counts = {p["key"]: Counter() for p in pain_defs}
    for results in results_by_company.values():
        for r in results:
            if r["key"] in counts:
                counts[r["key"]][r["status"]] += 1
    rows = []
    for p in pain_defs:
        c = counts[p["key"]]
        rows.append({
            "key": p["key"], "label": p["label"], "category": p["category"],
            "severe": c["severe"], "moderate": c["moderate"], "emerging": c["emerging"],
            "clear": c["clear"], "insufficient_data": c["insufficient_data"],
            "assessed": sum(c[s] for s in STATUS_ORDER),
            "total": sum(c.values()),
        })
    return rows


def collect_gaps(results_by_company: Dict[str, List[dict]]) -> Dict[str, List[dict]]:
    """
    What is stopping pain points from being assessed, across the portfolio.

    `gaps`: one row per indicator that is missing for at least one company, ranked by `unlock`
    — the sum, over companies, of the share of each pain point's total driver weight that
    indicator would add. It answers "which single indicator, once populated, would give the most
    pain-point evidence?", which is the order in which to build APIs/crawlers/derivations.
    `quality`: indicators that HAVE data but where some values were unusable (implausible).
    """
    gap = {}
    quality = defaultdict(lambda: {"companies": set(), "pain_points": set(), "label": None})
    n_companies = len(results_by_company)
    for cid, results in results_by_company.items():
        for r in results:
            for d in r["drivers"]:
                if d["assessed"]:
                    continue
                reason = d["unassessed_reason"]
                if reason == "implausible":
                    q = quality[d["indicator"]]
                    q["companies"].add(cid)
                    q["pain_points"].add(r["label"])
                    q["label"] = d["label"]
                    continue
                if reason not in UNASSESSED_REASONS_THAT_ARE_GAPS:
                    continue
                g = gap.setdefault(d["indicator"], {
                    "indicator": d["indicator"], "label": d["label"], "producer": d["producer"],
                    "pain_points": {}, "companies_missing": set(), "reasons": Counter(), "unlock": 0.0,
                })
                g["companies_missing"].add(cid)
                g["reasons"][reason] += 1
                g["pain_points"][r["key"]] = r["label"]
                g["unlock"] += d["weight_share"]
    gaps = []
    for g in gap.values():
        gaps.append({
            "indicator": g["indicator"], "label": g["label"], "producer": g["producer"],
            "pain_points": sorted(g["pain_points"].values()),
            "companies_missing": len(g["companies_missing"]), "companies_total": n_companies,
            "reasons": dict(g["reasons"]), "unlock": round(g["unlock"], 2),
            "proposal": PROPOSED_INDICATORS.get(g["indicator"]),
        })
    gaps.sort(key=lambda g: -g["unlock"])
    quality_rows = [{"indicator": k, "label": v["label"], "companies": len(v["companies"]),
                     "pain_points": sorted(v["pain_points"])} for k, v in quality.items()]
    quality_rows.sort(key=lambda q: -q["companies"])
    return {"gaps": gaps, "quality": quality_rows}
