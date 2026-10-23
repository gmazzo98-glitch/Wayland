"""
The vocabulary of facts: every key a reader may emit and a hypothesis may read, with its unit and meaning.

This is the contract between agents who never talk to each other: the person writing a hypothesis rule uses
`fin.ebitda_margin_pct` before the reader that produces it exists, because the name, unit and meaning are fixed
here. A reader that emits a key not declared here is rejected at build time (registry.build_facts), so two agents
cannot invent two names for one thing.

state:
  exists   the underlying data is already in the database or on disk; the reader only has to read it.
           `hint` says where it lives today. Verify the hint before relying on it: it is a pointer, not a promise.
  planned  the data is not collected yet; the owning ticket must build the collection (see docs/DIAGNOSIS_TICKETS.md).
           A hypothesis that requires a planned key is simply `not_assessable` until the key exists. That is correct.

Money is thousand EUR (k EUR), exactly as AIDA exports it. Percentages are percent (12.5 means 12.5%), never
fractions. "pctile" is the company's percentile among its peers (0-100, higher = higher VALUE, not "better":
a high personnel-cost percentile means a high personnel-cost share).

Adding keys: additive only, and only for your own ticket's namespace, in diagnosis/factkeys_ext_<ticket>.py via
declare_keys() (auto-imported by registry.discover). Renaming or removing a key is a contract change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List

KEY_PATTERN = re.compile(r"^[a-z]+(\.[a-z0-9_]+)+$")
KINDS = ("number", "text", "bool", "list", "dict", "date")
STATES = ("exists", "planned")


@dataclass(frozen=True)
class FactKey:
    key: str
    label: str
    unit: str
    kind: str
    owner: str       # ticket id that produces it
    state: str       # exists | planned
    hint: str = ""   # where it lives today / what to read; verify before relying on it

    def __post_init__(self):
        if not KEY_PATTERN.match(self.key):
            raise ValueError(f"fact key {self.key!r} must look like 'group.name' (lowercase, digits, underscores)")
        if self.kind not in KINDS:
            raise ValueError(f"{self.key}: kind must be one of {KINDS}")
        if self.state not in STATES:
            raise ValueError(f"{self.key}: state must be one of {STATES}")


FACT_KEYS: Dict[str, FactKey] = {}


def declare_keys(keys: Iterable[FactKey]) -> None:
    for k in keys:
        if k.key in FACT_KEYS:
            raise ValueError(f"fact key {k.key!r} is already declared (owner {FACT_KEYS[k.key].owner})")
        FACT_KEYS[k.key] = k


def _k(key, label, unit, kind, owner, state, hint="") -> FactKey:
    return FactKey(key, label, unit, kind, owner, state, hint)


_E, _P = "exists", "planned"
_core: List[FactKey] = []
add = _core.append

# ------------------------------------------------------------------------------------------------ id.* identity (D01)
for row in [
    ("id.legal_name", "Legal name", "", "text", "Company.legal_name"),
    ("id.vat", "VAT / registration number (join key)", "", "text", "Company.registration_number"),
    ("id.ateco", "ATECO/NACE code (6 digit where available)", "", "text", "Company.nace_code"),
    ("id.ateco_desc", "ATECO description", "", "text", "Company.sector_name"),
    ("id.region", "Region", "", "text", "Company.region"),
    ("id.province", "Province", "", "text", "Company.province"),
    ("id.legal_form", "Legal form (S.p.A., S.r.l., ...)", "", "text", "Company.legal_form"),
    ("id.founded_year", "Year of incorporation", "year", "number", "Company.incorporation_date"),
    ("id.age_years", "Years since incorporation", "years", "number", "derived from incorporation_date"),
    ("id.website", "Own website", "", "text", "Company.website_url (937/953 have one)"),
    ("id.headcount", "Employees (latest)", "people", "number", "Company.headcount / AIDA employees"),
    ("id.size_band", "Size band", "", "text", "derived: '50-99' etc."),
    ("id.registry_status", "Registry status (active/dissolved)", "", "text", "Company.registry_status"),
    ("id.shortlist_status", "GG shortlist status", "", "text", "Company.shortlist_status"),
]:
    add(_k(row[0], row[1], row[2], row[3], "D01", _E, row[4]))

# ------------------------------------------------------------------------------------------------ fin.* financials (D01)
FIN_LINES = [  # (name, label, AIDA blob field)
    ("revenue", "Revenue", "revenue"), ("production_value", "Value of production", "production_value"),
    ("gross_margin", "Gross margin (AIDA 'margine sui consumi')", "gross_margin"), ("ebitda", "EBITDA", "ebitda"),
    ("ebit", "EBIT", "ebit"), ("net_income", "Net income", "net_income"),
    ("production_costs", "Total production costs", "production_costs"),
    ("personnel_costs", "Personnel costs", "personnel_costs"), ("materials", "Raw materials & consumables", "materials"),
    ("total_assets", "Total assets", "total_assets"), ("equity", "Equity", "equity"),
    ("total_liabilities", "Total liabilities (AIDA 'total debt' is total liabilities)", "total_debt"),
    ("cash", "Cash & equivalents", "cash"), ("intangibles", "Intangible assets", "intangibles"),
]
for name, label, blob in FIN_LINES:
    add(_k(f"fin.{name}_k", f"{label}, latest year", "k EUR", "number", "D01", _E, f"AIDA Raw Exports blob '{blob}_latest'"))
    add(_k(f"fin.{name}_series", f"{label}, three years", "k EUR", "dict", "D01", _E,
           f"{{'latest':..,'y-1':..,'y-2':..}} from blob '{blob}_*'"))
for row in [
    ("fin.employees_series", "Employees, three years", "people", "dict", "AIDA blob 'employees_*'"),
    ("fin.last_accounts_year", "Fiscal year of the latest filed accounts", "year", "number", "signal last_accounts_year"),
    ("fin.revenue_growth_pct", "Revenue growth, latest vs previous year", "%", "number", "dossier_data.financial_statement ratios"),
    ("fin.revenue_trend_pct", "Revenue change, latest vs earliest of three years", "%", "number", "signal revenue_trend"),
    ("fin.ebitda_margin_pct", "EBITDA / revenue", "%", "number", "dossier_data ratios"),
    ("fin.ebit_margin_pct", "EBIT / revenue", "%", "number", "signal ebit_margin"),
    ("fin.net_margin_pct", "Net income / revenue", "%", "number", "dossier_data ratios"),
    ("fin.gross_margin_pct", "Gross margin / revenue", "%", "number", "dossier_data ratios"),
    ("fin.margin_compression_pp", "Fall in gross margin share, earliest vs latest year (0 if it did not fall)", "pp", "number", "signal margin_compression"),
    ("fin.personnel_pct_revenue", "Personnel costs / revenue", "%", "number", "dossier_data ratios"),
    ("fin.materials_pct_revenue", "Materials / revenue", "%", "number", "signal materials_cost"),
    ("fin.service_costs_pct_revenue", "Purchased services / revenue (year-matched)", "%", "number", "signal service_costs / cost breakdown"),
    ("fin.energy_pct_revenue", "Energy / revenue", "%", "number", "signal energy_cost (not reported separately in Italian accounts: usually unknown)"),
    ("fin.revenue_per_employee_k", "Revenue per employee", "k EUR", "number", "dossier_data ratios"),
    ("fin.equity_ratio_pct", "Equity / total assets", "%", "number", "dossier_data ratios"),
    ("fin.leverage_x", "Total assets / equity", "x", "number", "signal leverage_ratio"),
    ("fin.interest_cover_x", "EBIT / interest expense", "x", "number", "signal interest_coverage_ratio"),
    ("fin.cash_pct_revenue", "Cash / revenue", "%", "number", "dossier_data ratios"),
    ("fin.capex_pct_revenue", "Capex / revenue", "%", "number", "signal capex_ratio"),
    ("fin.inventory_change_k", "Change in inventories (production-value minus sales proxy for WIP build-up)", "k EUR", "number", "cost breakdown 'cost_inventory_change_k' (97%)"),
    ("fin.distress_procedure", "Open distress/insolvency procedure on the registry", "", "bool", "signal distress_procedure"),
    ("fin.valuation_ev_range_k", "Indicative enterprise value range [low, high]", "k EUR", "list", "valuation_service (withheld when inputs are weak)"),
]:
    add(_k(row[0], row[1], row[2], row[3], "D01", _E, row[4]))
for row in [  # not in the AIDA exports pulled so far: needs the extra-column pull (D09) and its importer (D35)
    ("fin.receivables_k", "Trade receivables", "k EUR", "number"), ("fin.inventories_k", "Inventories (raw, WIP, finished)", "k EUR", "number"),
    ("fin.payables_k", "Trade payables", "k EUR", "number"), ("fin.bank_debt_k", "Bank / financial debt", "k EUR", "number"),
    ("fin.dso_days", "Days sales outstanding", "days", "number"), ("fin.dio_days", "Days inventory outstanding", "days", "number"),
    ("fin.dpo_days", "Days payables outstanding", "days", "number"), ("fin.ccc_days", "Cash conversion cycle", "days", "number"),
    ("fin.rd_costs_k", "R&D / development costs capitalised or expensed", "k EUR", "number"),
    ("fin.export_revenue_pct", "Share of revenue from abroad", "%", "number"),
    ("fin.order_backlog_k", "Order backlog (rare in SME accounts)", "k EUR", "number"),
]:
    add(_k(row[0], row[1], row[2], row[3], "D35", _P, "not in the six AIDA exports pulled so far; see docs/diagnosis/DATA_SHOPPING_LIST.md (D09)"))

# ------------------------------------------------------------------------------------------------ peer.* (D03)
PEER_METRICS = ["revenue_growth_pct", "ebitda_margin_pct", "ebit_margin_pct", "gross_margin_pct", "personnel_pct_revenue",
                "materials_pct_revenue", "service_costs_pct_revenue", "revenue_per_employee_k", "equity_ratio_pct",
                "leverage_x", "interest_cover_x", "cash_pct_revenue", "capex_pct_revenue", "inventory_change_pct_revenue",
                "dso_days", "dio_days", "dpo_days"]
add(_k("peer.group_label", "Peer group used, e.g. 'ATECO 28.93, 50-99 staff, Italy'", "", "text", "D03", _P, "built from the cohort in the database"))
add(_k("peer.group_n", "Number of peers in the group", "companies", "number", "D03", _P))
add(_k("peer.group_level", "How wide the group had to be to reach a usable size (6-digit, 4-digit, 3-digit, 2-digit)", "", "text", "D03", _P))
for m in PEER_METRICS:
    add(_k(f"peer.{m}.pctile", f"Percentile of {m} among peers (higher = higher value)", "pctile", "number", "D03", _P))
    add(_k(f"peer.{m}.median", f"Peer median of {m}", "", "number", "D03", _P))

# ------------------------------------------------------------------------------------------------ mgmt.* / own.* (D01)
for row in [
    ("mgmt.ceo_name", "Chief executive / managing director (best available)", "", "text", "company_people roles"),
    ("mgmt.ceo_age", "Age of that person", "years", "number", "company_people.age"),
    ("mgmt.ceo_tenure_years", "Years in that role", "years", "number", "company_people.appointment_date"),
    ("mgmt.roster", "Directors and officers: name, role, age, since, current/former", "", "list", "company_people (7,967 rows)"),
    ("mgmt.board_size", "Current board/management size", "people", "number", "company_people"),
    ("mgmt.avg_age", "Average age of current management", "years", "number", "signal management_age"),
    ("mgmt.new_appointments_12m", "Appointments in the last 12 months", "people", "number", "company_people.appointment_date"),
    ("mgmt.departures_12m", "Resignations in the last 12 months", "people", "number", "company_people.resignation_date"),
    ("mgmt.turnover_24m", "Management turnover over 24 months", "people", "number", "signal management_turnover"),
    ("mgmt.younger_generation_present", "A manager under 45 sharing the owner's surname or newly appointed", "", "bool", "signal new_generation_management (sparse by design)"),
    ("own.type", "Ownership type: family | holding | private_equity | foreign_group | public | individual | unknown", "", "text", "AIDA shareholders + bvd_independence_class"),
    ("own.family_share_pct", "Share held by family/individuals", "%", "number", "signal family_ownership_share"),
    ("own.foreign_share_pct", "Share held by foreign owners", "%", "number", "signal foreign_ownership_share"),
    ("own.ultimate_owner", "Ultimate owner name where disclosed", "", "text", "AIDA controlling_shareholders"),
    ("own.shareholders", "Shareholders: name, type, country, direct %, total %", "", "list", "AIDA shareholders"),
    ("own.group_size", "Companies in the group", "companies", "number", "AIDA group_size"),
    ("own.independence_class", "BvD independence class (A+...D)", "", "text", "AIDA bvd_independence_class"),
    ("own.parent_name", "Parent company named on its own pages", "", "text", "crawler_website_discovery.parent"),
    ("own.participations_count", "Participations held", "companies", "number", "signal subsidiary_participations"),
]:
    add(_k(row[0], row[1], row[2], row[3], "D01", _E, row[4]))

# ------------------------------------------------------------------------------------------------ dig.* / prod.* / net.* / ops.* (D02)
for row in [
    ("dig.website_live", "Own website reachable and readable", "", "bool", "crawler_* blobs / site evidence status"),
    ("dig.cms_platform", "Website CMS", "", "text", "crawler_digital_maturity.cms_platform"),
    ("dig.ecommerce", "Sells online", "", "bool", "crawler_digital_maturity.has_ecommerce"),
    ("dig.tech_stack_score", "Technology footprint of the homepage", "0-100", "number", "signal tech_stack_intensity (89%)"),
    ("dig.website_maturity_score", "Website digital maturity", "0-100", "number", "signal website_digital_maturity (10%)"),
    ("dig.site_age_years", "Years since the website was last redesigned", "years", "number", "crawler_digital_maturity (Wayback diff)"),
    ("dig.languages", "Languages offered on the site", "", "list", "company website crawl"),
    ("dig.socials", "Social profiles linked", "", "list", "crawler_digital_maturity.social_presence_links"),
    ("dig.erp_mentions", "ERP / MES vendors named in job ads or pages", "", "list", "signal erp_systems_age (job-postings crawler)"),
    ("dig.open_roles_total", "Open roles on the careers page", "roles", "number", "crawler_job_postings.total_open_roles (301 companies crawled)"),
    ("dig.open_roles_technical", "Open engineering/technical/digital roles", "roles", "number", "crawler_job_postings"),
    ("dig.hiring_titles", "Sample of open role titles", "", "list", "crawler_job_postings.roles_sample"),
    ("dig.iiot_signals", "Evidence the company's machines are connected / remotely serviceable", "", "bool", "not collected today; produced by D11 profile"),
    ("prod.catalog_size", "Products found in the online catalog", "products", "number", "crawler_product_catalog (173 companies)"),
    ("prod.priced_share_pct", "Share of catalog products with a visible price", "%", "number", "crawler_product_catalog"),
    ("prod.categories", "Catalog categories", "", "list", "crawler_product_catalog"),
    ("prod.product_types", "Product lines named on the company's site", "", "list", "crawler_company_website.product_types (263 companies)"),
    ("prod.patent_families", "Patent families found", "families", "number", "signal patent_count (EPO OPS; 650 of 900 calls errored)"),
    ("prod.trademarks", "EU trademarks found", "marks", "number", "signal trademark_count (EUIPO is simulated today: do not use)"),
    ("prod.last_update", "Most recent dated product/press update on the site", "", "date", "signal product_innovativeness"),
    ("prod.differentiation_score", "Commoditisation vs named peers (0 = distinctive, 15 = commoditised)", "0-15", "number", "signal product_differentiation (1 company)"),
    ("net.directories", "Trade directories the company is confirmed in (MECSPE, ANIMA, Kompass...)", "", "list", "crawler_directory_* (confirmed listings only)"),
    ("net.trade_fairs", "Trade fairs / exhibitions attended", "", "list", "crawler_directory_mecspe; signal trade_fair_participation"),
    ("net.competitors_named", "Competitors recorded by hand", "", "list", "Competitor rows (3 in total today)"),
    ("net.competitor_digital_gap", "Years of website age vs named peers", "years", "number", "signal competitor_digital_gap"),
    ("net.suggested_peers", "Likely direct competitors suggested by similarity, NOT yet confirmed", "", "list", "D32"),
    ("pub.tender_awards", "Public-tender award notices naming the company", "", "list", "crawler_ted_contract_awards (158 companies)"),
    ("pub.grants", "Public grants / state aid received", "", "list", "signals public_grant_count, RNA state aid"),
    ("pub.recalls", "Regulatory product recalls", "", "list", "crawler_fda_recalls (US FDA only; rarely relevant to machinery)"),
    ("pub.certifications", "Management-system / product certifications held", "", "list", "not collected systematically; D10/D34"),
    ("rev.rating", "Customer/employer review rating", "stars", "number", "crawler_reviews_* (8 companies only)"),
    ("rev.review_count", "Number of reviews behind the rating", "reviews", "number", "crawler_reviews_*"),
    ("news.headlines", "Verified headlines about the company: headline, outlet, date, url", "", "list", "crawler_news_rss.kept_headlines; precision is poor until D17"),
    ("news.verified_events", "News events verified to be about THIS company", "", "list", "D17"),
]:
    owner = "D17" if row[0] == "news.verified_events" else "D32" if row[0] == "net.suggested_peers" else "D11" if row[0] == "dig.iiot_signals" else "D02"
    state = _P if row[0] in ("dig.iiot_signals", "net.suggested_peers", "news.verified_events", "pub.certifications") else _E
    add(_k(row[0], row[1], row[2], row[3], owner, state, row[4]))

# ------------------------------------------------------------------------------------------------ prof.* business profile (D11)
for row in [
    ("prof.machine_types", "What machines/equipment the company builds (own words, verbatim-checked)", "", "list"),
    ("prof.end_markets", "Industries the company says it serves (food, packaging, ceramics, automotive, pharma...)", "", "list"),
    ("prof.business_model", "engineer_to_order | configure_to_order | series | mixed | unknown", "", "text"),
    ("prof.has_aftermarket", "Spare parts / service / retrofit offered", "", "bool"),
    ("prof.remote_service", "Remote assistance / monitoring offered", "", "bool"),
    ("prof.export_markets", "Countries or regions the company says it sells to", "", "list"),
    ("prof.export_share_pct", "Export share, only where the site states it", "%", "number"),
    ("prof.customers_named", "Named customers / references", "", "list"),
    ("prof.partners_named", "Named technology / research partners", "", "list"),
    ("prof.certifications_claimed", "Certifications the site claims (ISO 9001/14001/45001/50001, CE, ATEX...)", "", "list"),
    ("prof.rd_claimed", "Company says it has an R&D / innovation function", "", "bool"),
    ("prof.software_in_product", "Product includes software / controls / connectivity the company develops", "", "bool"),
    ("prof.positioning", "One-line self-description, verbatim from the site", "", "text"),
    ("prof.founded_story", "Year/story of founding as the site tells it (context for the owner conversation)", "", "text"),
    ("prof.sustainability_claims", "Energy-efficiency / sustainability claims about the product or the plant", "", "list"),
]:
    add(_k(row[0], row[1], row[2], row[3], "D11", _P, "local-model reader with verbatim-quote verification (Scraper tools/site_evidence pattern)"))

# ------------------------------------------------------------------------------------------------ mkt.* (D14, D15)
for row in [
    ("mkt.sector_growth_pct", "Sector growth (Eurostat), latest", "%", "number", "D15", _E, "signal revenue_growth_vs_sector inputs (Eurostat Sector Growth, 710 companies)"),
    ("mkt.export_exposure", "Sector export exposure (Eurostat)", "%", "number", "D15", _E, "signal sector_export_exposure"),
    ("mkt.orders_index", "Latest association order index for the company's sub-sector: value, period, change", "", "dict", "D14", _P, "UCIMU, UCIMA, ACIMIT, ANIMA, Federmacchine, Istat"),
    ("mkt.headwinds", "Dated market items hurting this company's product/end-markets, each with its source", "", "list", "D15", _P, ""),
    ("mkt.tailwinds", "Dated market items helping this company's product/end-markets, each with its source", "", "list", "D15", _P, ""),
    ("mkt.exposure_end_markets", "The company's end-markets mapped to market items", "", "dict", "D15", _P, ""),
    ("mkt.input_cost_trend", "Direction of the input costs this product depends on (steel, aluminium, electronics, energy)", "", "dict", "D14", _P, ""),
    ("mkt.district", "Industrial district / cluster the company sits in (Packaging Valley, Food Valley, ...)", "", "text", "D04", _P, "province + ATECO mapping in the sector playbooks"),
]:
    add(_k(*row))

# ------------------------------------------------------------------------------------------------ reg.* (D05)
for row in [
    ("reg.machinery_regulation", "(EU) 2023/1230 applies to what the company places on the market (applies 20 Jan 2027)", "", "dict"),
    ("reg.cra_exposure", "Cyber Resilience Act exposure: product has digital elements (reporting from 11 Sep 2026, full from 11 Dec 2027)", "", "dict"),
    ("reg.nis2_scope", "NIS2 scope reading for the company (sector, size) with the legal basis cited", "", "dict"),
    ("reg.ai_act_exposure", "AI Act exposure: AI in the product or in HR/operations", "", "dict"),
    ("reg.cbam_exposure", "Customer or input exposure to CBAM", "", "dict"),
    ("reg.esg_pull", "Customer-driven ESG / supply-chain reporting pressure", "", "dict"),
    ("reg.incentives", "Incentive windows open to this company (e.g. iperammortamento 2026-2028) with dates and conditions", "", "list"),
    ("reg.deadlines", "Dated obligations or windows that apply, each with date, source URL and as-of", "", "list"),
]:
    add(_k(row[0], row[1], row[2], row[3], "D05", _P, "curated, dated, source-linked table; screening only, never legal advice"))

# ------------------------------------------------------------------------------------------------ trg.* (D16)
for row in [
    ("trg.events", "Verified dated events (Trigger dicts), most relevant first", "", "list"),
    ("trg.count_6m", "Opening events in the last 6 months", "events", "number"),
    ("trg.count_12m", "Opening events in the last 12 months", "events", "number"),
    ("trg.latest_date", "Date of the most recent opening event", "", "date"),
    ("trg.accounts_filed_recently", "New annual accounts filed in the last 6 months", "", "bool"),
    ("trg.leadership_change_12m", "New CEO / managing director in 12 months", "", "bool"),
    ("trg.closing_events", "Events that make a call a bad idea right now (distress, litigation, layoffs)", "", "list"),
]:
    add(_k(row[0], row[1], row[2], row[3], "D16", _P, "event engine over news, registry, roster, grants, tenders, hiring"))

# ------------------------------------------------------------------------------------------------ pay.* (D20) / reach.* (D23) / src.* (D12)
for row in [
    ("pay.free_cash_proxy_k", "EBITDA minus capex minus interest, latest year (a proxy, not cash flow)", "k EUR", "number", "D20"),
    ("pay.pilot_budget_band", "<25k | 25-75k | 75-200k | >200k : what a pilot could plausibly cost them without strain", "", "text", "D20"),
    ("pay.affordability", "low | medium | high | unknown : capacity to fund a pilot, with the reasons in the note", "", "text", "D20"),
    ("reach.decision_maker", "Who to call: name and role", "", "text", "D23"),
    ("reach.decision_maker_age", "Their age", "years", "number", "D23"),
    ("reach.generic_email", "A generic company email address is published", "", "bool", "D23"),
    ("reach.phone", "A switchboard number is published", "", "bool", "D23"),
    ("reach.contact_url", "Contact page", "", "text", "D23"),
    ("reach.language", "Language to open in: it | en | de", "", "text", "D23"),
    ("reach.warm_routes", "Shared directories, fairs, associations, or parent links that could open the door", "", "list", "D23"),
    ("src.layers_present", "Which data layers exist for this company", "", "list", "D01"),
    ("src.coverage_pct", "Share of the facts a full diagnosis wants that are known", "%", "number", "D12"),
    ("src.simulated_ignored", "Simulated placeholder signals ignored", "signals", "number", "D01"),
    ("src.oldest_input_days", "Age of the stalest input used", "days", "number", "D12"),
]:
    add(_k(row[0], row[1], row[2], row[3], row[4], _E if row[0].startswith("src.") and row[4] == "D01" else _P))

# ------------------------------------------------------------------------------------------------ pp.* / score.* (D01)
# The existing indicator-backed pain-point engine (painpoints.py) and the two scoring axes (scoring.py) are inputs
# to the diagnosis, not things to rebuild. A reader runs them read-only and emits their results as facts.
PAIN_POINT_KEYS = ["growth_stall", "profit_erosion", "debt_service_strain", "insolvency_procedure", "liquidity_squeeze",
                   "underinvestment", "input_cost_pressure", "concentration_risk", "logistics_complexity", "legacy_systems",
                   "digital_channel_lag", "product_obsolescence", "quality_decline", "market_reach_gap", "workforce_strain",
                   "leadership_transition", "compliance_and_risk"]
for pk in PAIN_POINT_KEYS:
    add(_k(f"pp.{pk}", f"Pain point '{pk}': {{status: clear|emerging|moderate|severe|insufficient_data, score, coverage}}",
           "", "dict", "D01", _E, "painpoints.py detection over real (non-simulated) indicators"))
for row in [
    ("score.need", "NEED axis score (context only: never blended with readiness, never used to prove a hypothesis)", "0-100", "number", "Company.need_score"),
    ("score.readiness", "READINESS axis score (context only)", "0-100", "number", "Company.readiness_score"),
    ("score.need_completeness", "Share of NEED weight backed by real data", "%", "number", "scoring.calculate_company_scores"),
    ("score.readiness_completeness", "Share of READINESS weight backed by real data", "%", "number", "scoring.calculate_company_scores"),
]:
    add(_k(row[0], row[1], row[2], row[3], "D01", _E, row[4]))

declare_keys(_core)


def keys_owned_by(owner: str) -> List[FactKey]:
    return [k for k in FACT_KEYS.values() if k.owner == owner]


def is_declared(key: str) -> bool:
    return key in FACT_KEYS
