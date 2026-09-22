"""
What is still missing from the indicator catalog, and where the data could come from.

Reads the LIVE database READ-ONLY (never writes) and joins, for every seeded indicator:
  * how many companies already have a REAL value (present/absent, not simulated) for it,
  * how it can be filled, per the curated GAP_PLAN below (the analysis, kept next to the code that
    checks it so it can be re-run and argued with instead of going stale in a document).

Usage:
    python scripts/indicator_gaps.py                 # summary + per-class tables
    python scripts/indicator_gaps.py --csv gaps.csv  # also write the full matrix
    python scripts/indicator_gaps.py --db sqlite:///x.db   # any other database

Classes (how the gap closes):
    LIVE          real data already in the database for most companies
    COMPUTE       the inputs are ALREADY in the database; a compute pass over them writes the signal
    IMPORT        the inputs are in files you already HAVE (the raw AIDA exports in Wayland/Data) but were
                  never imported: map the columns, no new AIDA pull
    RUN           a working, free adapter/crawler exists and simply has not been run over the list
    GATED         built, but blocked by a key, a flag or a policy decision
    BUILD         no producer yet; a concrete source exists (see `where`)
    NOT_ITALY     the built producer is Germany-only; the Italian cohort needs an equivalent source
    MANUAL        first-contact interview by design (tier T3) - collected by hand after shortlisting
"""

import argparse
import csv
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# (class, where the data comes from / what closes the gap, effort)
GAP_PLAN = {
    # ---- LIVE: the AIDA financial import -------------------------------------------------------------
    "revenue_trend": ("LIVE", "AIDA import (revenue x3 years)", ""),
    "ebit_trend": ("LIVE", "AIDA import (EBIT x3 years)", ""),
    "margin_compression": ("LIVE", "AIDA import (EBITDA / gross margin)", ""),
    "cash_position": ("LIVE", "AIDA import", ""),
    "debt_level": ("LIVE", "AIDA import", ""),
    "leverage_ratio": ("LIVE", "AIDA import", ""),
    "interest_coverage_ratio": ("LIVE", "AIDA import (78% - 'n.s.' where there is no interest)", ""),
    "ebitda_trend": ("LIVE", "AIDA import (EBITDA x3 years; context, not scored)", ""),

    # ---- COMPUTE: the inputs are already in the database ---------------------------------------------
    "management_age": ("COMPUTE", "company_people roster (age 91%): sync_management_composition_signals - only ever run "
                                  "when a company page is opened or on import", "bulk pass, no network"),
    "senior_mgmt_tenure": ("COMPUTE", "company_people.appointment_date (86%)", "same pass"),
    "management_turnover": ("COMPUTE", "company_people appointment/resignation dates", "same pass"),
    "mgmt_gender_diversity": ("COMPUTE", "company_people.gender (91%)", "same pass"),
    "mgmt_national_diversity": ("COMPUTE", "company_people.nationality (84%)", "same pass"),
    "independent_board_members": ("COMPUTE", "company_people roles (ADV group)", "same pass"),
    "new_generation_management": ("COMPUTE", "roster ages + appointment dates via sync_succession_signal; correctly sparse "
                                             "(only written when a family handover is actually detected)", "same pass"),
    "cogs_ratio": ("COMPUTE", "materials share of revenue = 1 - gross_margin / revenue, where the imported gross_margin is AIDA's "
                              "'Margine sui consumi' (an amount, 100%). NOT the imported cogs_* column: the data audit found that is total "
                              "PRODUCTION costs (~ revenue - EBIT), a different ratio. Label it as a materials-based proxy", "small ratio step"),
    "capex_ratio": ("COMPUTE", "imported material + immaterial capex / revenue (77-89% of companies); take the absolute value "
                               "(investments are stored as outflows)", "small ratio step"),
    "number_of_employees": ("COMPUTE", "AIDA raw employees_latest / Company.headcount (100%) - a segment filter, not scored", "trivial"),

    # ---- RUN: built, free, never run over the list ---------------------------------------------------
    "patent_count": ("RUN", "EPO OPS (key configured, verified live)", "Phase 1 in the background queue"),
    "patent_ipc_diversity": ("RUN", "EPO OPS (same call)", "Phase 1"),
    "trademark_count": ("RUN", "EUIPO (key configured)", "Phase 1"),
    "public_grant_count": ("RUN", "EU Funding & Tenders portal (public key)", "Phase 1"),
    "sector_growth_benchmark": ("RUN", "Eurostat (keyless)", "Phase 1"),
    "revenue_growth_vs_sector": ("RUN", "computed from revenue_trend + Eurostat sector growth - needs the Eurostat run first", "Phase 1"),
    "tech_stack_intensity": ("RUN", "Wappalyzer-style local fingerprinting of the homepage", "Phase 4"),
    "management_diversity": ("RUN", "own-site 'team' page scrape", "Phase 4"),
    "external_collaboration": ("RUN", "Google News RSS (keyless) - weight 5, readiness", "Phase 4"),
    "university_partnership": ("RUN", "Google News RSS - weight 5, readiness", "Phase 4"),
    "prior_open_innovation_usage": ("RUN", "Google News RSS - weight 4, readiness", "Phase 4"),
    "press_launch_mentions": ("RUN", "Google News RSS", "Phase 4"),
    "partnership_news_count": ("RUN", "Google News RSS", "Phase 4"),
    "board_innovation_statements": ("RUN", "Google News RSS", "Phase 4"),
    "recent_ma_activity": ("RUN", "Google News RSS (context, not scored)", "Phase 4"),
    "website_digital_maturity": ("RUN", "digital-maturity crawler (Wayback structural diff) - timed out on 89% of runs; now still returns the homepage fields when Wayback is down", "Phase 7"),
    "online_market_presence": ("RUN", "digital-maturity crawler (e-commerce + socials + archive activity)", "Phase 7"),
    "digital_job_postings": ("RUN", "job-postings crawler (careers page)", "Phase 7"),
    "skilled_labour_share": ("RUN", "job-postings crawler", "Phase 7"),
    "digital_lead_role_present": ("RUN", "job-postings crawler - a GATE (x0.7 on readiness), so its honesty matters most", "Phase 7"),
    "trade_fair_participation": ("RUN", "directory-listing crawler (MECSPE only today; more directories = more plugins)", "Phase 7"),
    "job_posting_velocity": ("RUN", "job-postings crawler's own junk-filtered total-roles count (2026-09-22) - already computed for "
                                    "digital_job_postings' summary, just never written as its own signal. No Germany dependency, works for Italy",
                             "Phase 7"),
    "esg_reporting_recency": ("RUN", "company-website crawler (LLM, token-bound)", "Phase 7"),
    "product_portfolio_diversity": ("RUN", "company-website crawler (noisy: counts vary run to run)", "Phase 7"),
    "store_geo_distribution": ("RUN", "company-website crawler (most B2B machinery makers have no stores: honest not_applicable)", "Phase 7"),
    "physical_stores_trend": ("RUN", "company-website crawler - needs a SECOND crawl 30+ days later (a trend, not a snapshot)", "Phase 7, twice"),
    "product_age": ("RUN", "company-website crawler's product_launch_year field (2026-09-22): only set when a page states a "
                           "launch year distinct from the founding year, so real coverage will be low but honest", "Phase 7"),
    "product_innovativeness": ("RUN", "company-website crawler's last_product_update_signal (2026-09-22): most recent dated "
                                      "product/press update across all crawled pages, was already extracted, wasn't wired up", "Phase 7"),
    "years_international_activity": ("RUN", "company-website crawler's export_since_year field (2026-09-22): only set when a page "
                                             "states an explicit year ('esportiamo dal 1998')", "Phase 7"),
    "international_sales_volume": ("RUN", "company-website crawler's export_share_pct field (2026-09-22): only set when a page "
                                          "states an explicit percentage ('esportiamo il 70%')", "Phase 7"),

    # ---- GATED: built, blocked ------------------------------------------------------------------------
    "sector_pilot_precedent": ("GATED", "news-signals-crawler needs NEWSAPI_KEY (paid). Free route: extend the RSS adapter", "key or small build"),
    "product_quality_trend": ("GATED", "Google Maps reviews - off by policy (Maps ToS). No free compliant source", "policy decision"),
    "kununu_rating": ("GATED", "Kununu direct scrape off by policy; reseller not bought", "policy / budget"),
    "employee_turnover": ("GATED", "LinkedIn headcount history (reseller) or Kununu/Glassdoor tenure signals", "policy / budget"),
    "linkedin_activity": ("GATED", "LinkedIn crawler is off by policy ('highest-risk scrape target')", "policy / budget"),
    "mgmt_education_level": ("GATED", "LinkedIn education (reseller) or the manual Gem-prompt pipeline that already exists", "policy / manual"),
    "mgmt_education_diversity": ("GATED", "same as mgmt_education_level", "policy / manual"),
    "mgmt_cultural_diversity": ("GATED", "same; overlaps nationality (which the roster already gives)", "policy / manual"),

    # ---- NOT_ITALY: built producers are German ---------------------------------------------------------
    "sector_export_exposure": ("NOT_ITALY", "Destatis adapter is DE-only (and its table code is wrong). Italian equivalent: ISTAT Coeweb "
                                            "(trade by ATECO) or Eurostat trade-by-NACE - both free; confirm the table codes live, do not guess",
                               "new adapter"),
    "rd_expense_ratio": ("NOT_ITALY", "Bundesanzeiger (paid, DE). Italy: not in the six exports either (they hold 'Immobilizzazioni immateriali "
                                      "(Investimenti)', a coarse proxy). Options: an AIDA pull with 'costi di sviluppo', or the public Registro Imprese "
                                      "list of 'PMI innovative' (which must show R&D >= 3%) - verify the open-data file", "AIDA re-export"),

    # ---- BUILD: no producer, a source exists ---------------------------------------------------------
    "total_assets": ("IMPORT", "WAYLAND_FINANCIAL_CAPACITY export, 'TOTALE ATTIVO' x3 years (100%); the Main sheet's column is empty (0/953)", "map one column"),
    "labour_cost": ("IMPORT", "WAYLAND_FINANCIAL_PL export, 'Totale costi del personale' x3 years (100%)", "map columns"),
    "average_salary": ("IMPORT", "labour cost / 'Dipendenti' - both in the FINANCIAL_PL export (100%)", "map + ratio"),
    "materials_cost": ("IMPORT", "WAYLAND_FINANCIAL_PL export, 'Materie prime e consumo' x3 years (100%)", "map columns"),
    "raw_material_cost": ("IMPORT", "same 'Materie prime e consumo' line (the catalog splits materials from raw materials; one source feeds both)", "map columns"),
    "service_costs": ("BUILD", "AIDA 'Costi per servizi' (B7) - NOT in the six exports on disk; needs a new AIDA pull", "AIDA re-export"),
    "logistics_cost": ("BUILD", "not separable: Italian statements fold freight into B7. Proxy or first-contact", "proxy / manual"),
    "energy_cost": ("BUILD", "not reported separately in Italian statements; sector-level ISTAT/Eurostat energy intensity is the only "
                             "external proxy - otherwise first-contact", "proxy / manual"),
    "energy_transition_capex": ("BUILD", "not in statements. Signals: sustainability report (website crawler), GSE/Transizione 4.0-5.0 incentives, "
                                         "press. Weak; consider first-contact", "hard"),
    "subsidiary_participations": ("IMPORT", "WAYLAND_STRUCTURE_LEGAL_OWNERSHIP export, 'Numero di partecipazioni disponibili' (622 companies have some) "
                                             "+ the 'Partecipate' list with %", "map columns"),
    "private_funding": ("IMPORT", "WAYLAND_SHAREHOLDERS_CONTROL export, 'Azionisti Tipo' (shareholder type, to detect PE / VC / financial holders) "
                                  "+ '% Diretta/Totale' (100%); news mentions of funding rounds as a complement", "map + classify"),
    "family_ownership_share": ("IMPORT", "same shareholder rows: shareholders sharing the officers' surname, by '% Totale' (context, not scored)", "map + surname match"),
    "online_sales_volume": ("BUILD", "presence of a shop is known (digital-maturity); the VOLUME is not published - first-contact", "manual"),
    "product_differentiation": ("BUILD", "the catalog's own proxy is 'number of comparable competing products at similar price points' - not "
                                         "something a company's own website can honestly state. Counting differentiation claims (certifications, "
                                         "'only manufacturer of...') would invert the definition (a company that doesn't self-promote reads as MORE "
                                         "commoditized) - deliberately not built as a website-LLM field for that reason, same posture as the "
                                         "declined regulatory_compliance_exposure inference", "needs a real competitor-landscape source"),
    "product_type_tag": ("BUILD", "website LLM classification of the home page (context, not scored - lowest priority)", "website LLM"),
    "competitor_digital_gap": ("BUILD", "computed: a company's digital maturity vs its sector peers - needs website_digital_maturity filled first", "small compute, after RUN"),
    "erp_systems_age": ("BUILD", "keyword scan of job-ad TEXT for ERP vendors (SAP, Navision, AS/400, Zucchetti, TeamSystem...) - the "
                                 "crawler only keeps titles today", "extend job crawler"),
    "regulatory_compliance_exposure": ("BUILD", "a curated, dated lookup table from EUR-Lex (NIS2, Machinery Regulation, Cyber Resilience Act, "
                                                "AI Act, CBAM) keyed by NACE and size. For this cohort (all NACE C28, 50-99 staff) the sector/size part is "
                                                "nearly constant - only the company-specific part discriminates. Deliberately not scraped: "
                                                "thresholds move", "curated table + legal review"),

    # ---- MANUAL: tier T3 ------------------------------------------------------------------------------------
    "interdepartmental_collaboration": ("MANUAL", "first-contact questionnaire", ""),
    "org_verticality": ("MANUAL", "first-contact questionnaire", ""),
    "approval_chain_depth": ("MANUAL", "first-contact questionnaire (weight 3)", ""),
    "customer_concentration": ("MANUAL", "first-contact questionnaire (weight 3); not in AIDA", ""),
    "supplier_concentration": ("MANUAL", "first-contact questionnaire", ""),
    "logistics_difficulty": ("MANUAL", "first-contact questionnaire", ""),
    "supply_chain_digitization": ("MANUAL", "first-contact questionnaire", ""),
    "insurance_claims_trend": ("MANUAL", "first-contact questionnaire", ""),
    "vendor_contract_renewal_timing": ("MANUAL", "first-contact questionnaire (sequencing input)", ""),
    "product_demand_elasticity": ("MANUAL", "first-contact questionnaire", ""),
    "raw_material_rarity": ("MANUAL", "first-contact questionnaire", ""),
}

CLASS_ORDER = ["LIVE", "COMPUTE", "IMPORT", "RUN", "GATED", "NOT_ITALY", "BUILD", "MANUAL", "UNCLASSIFIED"]


def _connect(url: str):
    import sqlalchemy as sa
    return sa.create_engine(url)


def real_counts(engine):
    """{signal_key: companies with a REAL present/absent value}, plus the company total."""
    import sqlalchemy as sa
    with engine.connect() as conn:
        total = conn.execute(sa.text("select count(*) from companies")).scalar()
        rows = conn.execute(sa.text(
            "select signal_key, count(*) from signal_records "
            "where status in ('present','absent') and is_simulated = :sim group by signal_key"), {"sim": False}).fetchall()
    return total, {k: c for k, c in rows}


def build_matrix(defs: list, total: int, counts: dict) -> list:
    rows = []
    for d in defs:
        key = d["key"]
        cls, where, effort = GAP_PLAN.get(key, ("UNCLASSIFIED", "not in GAP_PLAN yet", ""))
        real = counts.get(key, 0)
        pct = round(100 * real / total) if total else 0
        # A plan class of LIVE/RUN/... is what the gap IS once the data is there; if a "not LIVE" indicator
        # already has data for most companies, say so instead of repeating an out-of-date plan.
        shown = "LIVE" if pct >= 50 and cls != "MANUAL" else cls
        rows.append({"key": key, "label": d["label"], "axis": d.get("axis"), "weight": d.get("weight") or 0.0,
                     "gate": bool(d.get("is_gate")), "tier": d.get("automation_tier"), "real": real, "pct": pct,
                     "class": shown, "where": where, "effort": effort})
    return rows


def summarise(rows: list) -> dict:
    """Scored weight per axis, by class. A 'both' indicator counts on each axis it scores."""
    out = {"need": defaultdict(float), "readiness": defaultdict(float)}
    for r in rows:
        if r["axis"] in ("need", "both"):
            out["need"][r["class"]] += r["weight"]
        if r["axis"] in ("readiness", "both"):
            out["readiness"][r["class"]] += r["weight"]
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None, help="SQLAlchemy URL; default: DATABASE_URL from .env (transaction pooler if on Supabase)")
    ap.add_argument("--csv", default=None, help="also write the full matrix to this CSV")
    args = ap.parse_args()

    url = args.db
    if not url:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
        url = os.environ["DATABASE_URL"].replace(":5432/", ":6543/")  # the session pooler is capped at 15 clients
    from indicators import INDICATOR_SEED

    total, counts = real_counts(_connect(url))
    rows = build_matrix(INDICATOR_SEED, total, counts)
    summary = summarise(rows)

    print(f"{len(rows)} indicators, {total} companies. Real = present/absent and not simulated.\n")
    print("Scored weight by how the gap closes (this is what a company's score could rest on):")
    print(f"  {'class':<13}{'NEED':>8}{'READINESS':>12}")
    for cls in CLASS_ORDER:
        n, r = summary["need"].get(cls, 0.0), summary["readiness"].get(cls, 0.0)
        if n or r:
            print(f"  {cls:<13}{n:>8.1f}{r:>12.1f}")
    tot_n, tot_r = sum(summary["need"].values()), sum(summary["readiness"].values())
    print(f"  {'total':<13}{tot_n:>8.1f}{tot_r:>12.1f}\n")

    for cls in CLASS_ORDER:
        group = sorted((r for r in rows if r["class"] == cls), key=lambda r: (-r["weight"], r["key"]))
        if not group:
            continue
        print(f"### {cls} ({len(group)})")
        for r in group:
            gate = " GATE" if r["gate"] else ""
            print(f"  {r['key']:<34}{str(r['axis'])[:4]:<5}w={r['weight']:<4}{gate:<5} {r['pct']:>3}%  {r['where']}"
                  + (f"  [{r['effort']}]" if r["effort"] else ""))
        print()

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(sorted(rows, key=lambda r: (CLASS_ORDER.index(r["class"]), -r["weight"], r["key"])))
        print(f"wrote {args.csv}")


if __name__ == "__main__":
    main()
