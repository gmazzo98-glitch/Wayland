"""
Company Management & Country-Specific Ingestion Service.
Supports German (DE) and Italian (IT) companies with automated registration
normalization, indicator signal initialization, and country-gated API execution.
"""

import io
import csv
import json
import re
import unicodedata
import itertools
from collections import Counter
from datetime import datetime, timedelta
import pandas as pd
from sqlalchemy import or_
from sqlalchemy.orm import Session

from models import Company, SignalRecord, ColumnMappingProfile, IndicatorDefinition, PilotOutcome, RawImportRecord, CompanyPerson
from indicators import fetch_indicator_defs, TREND_INDICATOR_KEYS, CAT_CONTEXT
from utils import normalize_registration_nr
from config import has_credentials
from adapters import epo_ops, euipo, eu_funding, arbeitsagentur, google_news, google_news_rss, eurostat_sector_growth, eurostat_export_exposure
from scrapers import handelsregister_free, wappalyzer_local, management_diversity
from scrapers import (
    company_website_crawler, job_postings_crawler, review_crawler, news_signals_crawler,
    directory_listing_crawler, innovation_participation_crawler, digital_maturity_crawler,
    linkedin_profile_crawler,
)

SUPPORTED_COUNTRIES = ["Germany", "Italy"]

# Source applicability by country:
# - Universal / EU: EPO OPS, EUIPO, EU Funding Portal, Eurostat Sector Growth, Eurostat Export
#   Exposure, Wappalyzer, Google News, Own-Site Scrape
# - Germany only: Arbeitsagentur, Handelsregister Free Snapshot, Bundesanzeiger, Kununu Reseller
# - Italy only: Italian national registers / ISTAT (future integration hooks)
# Phase 7 (Node-based crawlers, see scrapers/*_crawler.py) is universal too — none of
# the 8 are Germany/Italy-specific by construction, so both country rows list all 8.
PHASE_7_SOURCES = [
    "Company Website Crawler", "Job Postings Crawler", "Review Crawler",
    "News Signals Crawler", "Directory Listing Crawler", "Innovation Participation Crawler",
    "Digital Maturity Crawler", "LinkedIn Profile Crawler",
]

COUNTRY_SOURCE_MAP = {
    "Germany": {
        "Phase 1": ["EPO OPS", "EUIPO", "EU Funding Portal", "Arbeitsagentur", "Eurostat Sector Growth", "Eurostat Export Exposure"],
        "Phase 2": ["Handelsregister Free Snapshot"],
        "Phase 3": ["Bundesanzeiger"],
        "Phase 4": ["Wappalyzer", "Google News", "Own-Site Scrape"],
        "Phase 5": ["Kununu Reseller"],
        "Phase 7": PHASE_7_SOURCES,
    },
    "Italy": {
        "Phase 1": ["EPO OPS", "EUIPO", "EU Funding Portal", "Eurostat Sector Growth", "Eurostat Export Exposure"],
        "Phase 2": [],  # German Handelsregister not applicable
        "Phase 3": [],  # Bundesanzeiger not applicable
        "Phase 4": ["Wappalyzer", "Google News", "Own-Site Scrape"],
        "Phase 5": [],  # Kununu (DACH focus) not applicable
        "Phase 7": PHASE_7_SOURCES,
    }
}

GERMAN_ONLY_SOURCES = {
    "Arbeitsagentur", "Handelsregister Free Snapshot",
    "Bundesanzeiger", "Kununu Reseller"
}


def is_source_applicable(source_name: str, country: str) -> bool:
    """Checks if an ingestion source / API is applicable for the given country."""
    if country == "Italy" and source_name in GERMAN_ONLY_SOURCES:
        return False
    return True


def get_applicable_sources_for_company(company: Company) -> list:
    """Returns a list of all source names applicable to the company's country."""
    country = company.country or "Germany"
    country_phases = COUNTRY_SOURCE_MAP.get(country, COUNTRY_SOURCE_MAP["Germany"])
    applicable = []
    for sources in country_phases.values():
        applicable.extend(sources)
    return applicable


class SourceStep:
    """One source's sync for one company: what to call, and how long it usually takes."""
    __slots__ = ("name", "fn", "phase")

    def __init__(self, name: str, fn, phase: int):
        self.name, self.fn, self.phase = name, fn, phase


# Slowest first when a company's steps are started together: a run lasts as long as its slowest
# step, so that one must never start last. Measured 2026-09-21: website ~100-170s (token-bound),
# digital ~40-170s (archive-bound), jobs ~70s, directory ~50s; everything else is quick or gated.
_SLOWEST_FIRST = (
    "Company Website Crawler", "Digital Maturity Crawler", "Job Postings Crawler", "Directory Listing Crawler",
    "Review Crawler", "LinkedIn Profile Crawler", "News Signals Crawler", "Innovation Participation Crawler",
)


def plan_source_steps(company: Company, phases: list) -> tuple:
    """
    The single definition of what each phase runs for this company: (steps, after), where `steps`
    are the independent per-source syncs — in the order the phases list them — and `after` are
    computations over what those steps just wrote, which must run once they have all finished.
    Both the sequential path (sync_company_applicable_sources) and the concurrent one
    (run_company_phases) execute exactly this plan, so they can never drift apart.
    """
    steps, after = [], []
    country = company.country or "Germany"

    if 1 in phases:
        # EU / Universal Phase 1 APIs (Both DE & IT)
        steps += [
            SourceStep("EPO OPS", epo_ops.sync_company_patents, 1),
            SourceStep("EUIPO", euipo.sync_company_trademarks, 1),
            SourceStep("EU Funding Portal", eu_funding.sync_company_grants, 1),
            SourceStep("Eurostat Sector Growth", eurostat_sector_growth.sync_sector_growth_benchmark, 1),
            # Eurostat's trade-by-NACE + turnover-by-NACE datasets are EU-wide and keyless, so
            # unlike the Destatis adapter this replaced (Germany-only, and its GENESIS table was
            # never a clean NACE match — see adapters/destatis.py), this runs for every country.
            SourceStep("Eurostat Export Exposure", eurostat_export_exposure.sync_sector_export_exposure, 1),
        ]
        # Germany-specific Phase 1 APIs
        if country == "Germany":
            steps += [
                SourceStep("Arbeitsagentur", arbeitsagentur.sync_job_velocity, 1),
            ]
        # Revenue Growth vs. Sector is a pure computation over two already-
        # stored signals (revenue_trend from a financial import, sector_growth_
        # benchmark from the Eurostat call just above) — re-run every sync so
        # it stays current whichever of the two most recently changed.
        after.append(compute_revenue_growth_vs_sector)

    if 2 in phases and country == "Germany":
        steps.append(SourceStep("Handelsregister", handelsregister_free.index_handelsregister_snapshot, 2))

    if 4 in phases:
        # Phase 4 Web & Social (Both DE & IT)
        steps += [
            SourceStep("Wappalyzer", wappalyzer_local.sync_tech_stack, 4),
            SourceStep("Management Diversity", management_diversity.sync_management_diversity, 4),
        ]
        # News/Press: the Google Programmable Search adapter is better (real search
        # ranking, article snippets) but needs a paid-tier key, and with none set it
        # only ever wrote placeholder values. The keyless Google News RSS adapter
        # covers the same signals for free, so it's the default and CSE takes over
        # only when actually configured — one producer per signal_key either way.
        if has_credentials("Google News"):
            steps += [
                SourceStep("Partnership News", google_news.sync_partnership_news, 4),
                SourceStep("Innovation Statements", google_news.sync_innovation_statements, 4),
            ]
        else:
            steps.append(SourceStep("Google News RSS", google_news_rss.sync_news_rss, 4))

    if 7 in phases:
        # Phase 7 — Node/Crawlee crawlers (Scraper/crawlers/), both DE & IT. Slower
        # than every other phase (each call spawns a subprocess), so never part of
        # the default auto_sync=[1, 4] path — always an explicit trigger.
        steps += [
            SourceStep("Company Website Crawler", company_website_crawler.sync_company_website, 7),
            SourceStep("Job Postings Crawler", job_postings_crawler.sync_job_postings, 7),
            SourceStep("Review Crawler", review_crawler.sync_reviews, 7),
            SourceStep("News Signals Crawler", news_signals_crawler.sync_news_signals, 7),
            SourceStep("Directory Listing Crawler", directory_listing_crawler.sync_directory_listing, 7),
            SourceStep("Innovation Participation Crawler", innovation_participation_crawler.sync_innovation_participation, 7),
            SourceStep("Digital Maturity Crawler", digital_maturity_crawler.sync_digital_maturity, 7),
            SourceStep("LinkedIn Profile Crawler", linkedin_profile_crawler.sync_linkedin_profiles, 7),
        ]
    return steps, after


def sync_company_applicable_sources(company: Company, db: Session, phases: list = None, on_step=None) -> dict:
    """
    Executes live/simulated pipeline adapters for a single company,
    activating ONLY the sources applicable to that company's country.

    Sequential, on the caller's own session — what company creation and the one-off buttons use.
    The background queue runs the very same plan concurrently instead (run_company_phases).

    on_step(index, total, source_name), when given, is called just BEFORE each
    Phase 7 crawler starts (0-based index); the other phases don't report steps.
    """
    if phases is None:
        phases = [1, 4]

    steps, after = plan_source_steps(company, phases)
    phase7_total = sum(1 for s in steps if s.phase == 7)
    results, phase7_index = {}, 0
    for step in steps:
        if step.phase == 7:
            if on_step:
                on_step(phase7_index, phase7_total, step.name)
            phase7_index += 1
        results[step.name] = step.fn(company, db)
    for compute in after:
        compute(db, company)
    return results


def _open_step_session(session_factory):
    """A session that keeps its loaded attributes across commits. A crawl runs for minutes, and a
    default session expires everything on commit — so the crawler thread's first read of
    `company.website_url` opened a transaction that held one pooled connection until the crawl
    ended. Several steps of several companies at once would exhaust the pool (and Supabase's
    session pooler allows only 15 clients). Kept attributes mean the connection is returned
    after each commit and a step only holds one while it is actually reading or writing."""
    try:
        return session_factory(expire_on_commit=False)
    except TypeError:  # a plain callable (tests) that takes no options
        return session_factory()


def run_company_phases(company_id: str, phases: list, session_factory=None, on_step=None, parallel: bool = True) -> tuple:
    """
    One company's pass over `phases` on its OWN DB sessions (SQLAlchemy sessions are not
    thread-safe, and a Streamlit page's session must stay on the page's thread).
    Returns (company_id, legal_name, results) where results is the per-source dict, or
    {"error": ...} when the run itself blew up — one bad company must never take a batch down.

    With parallel=True (the default) the company's sources run at the same time, each on its own
    session, so the run lasts as long as its slowest source instead of the sum of all of them
    (measured 421s sequential vs ~170s for the same company). Which of them actually run
    together is decided by the resource governor (resource_governor.py), not here: this only
    says they MAY. A step that raises is recorded as that source's error and does not affect
    the others.

    on_step(index, total, source_name[, "done"]) fires when a step starts and again when it
    ends; `index` is how many steps had FINISHED at that moment.
    """
    if session_factory is None:
        from database import SessionFactory as session_factory  # lazy: database imports models/config only

    session = _open_step_session(session_factory)
    try:
        company = session.query(Company).filter_by(id=company_id).first()
        if not company:
            return company_id, None, {"error": "company not found"}
        name = company.legal_name
        steps, after = plan_source_steps(company, phases)
        if not parallel or len(steps) <= 1:
            step_kwargs = {"on_step": on_step} if on_step else {}
            return company_id, name, sync_company_applicable_sources(company, session, phases=phases, **step_kwargs)
    except Exception as e:  # noqa: BLE001 — isolate one company's failure from the batch
        session.rollback()
        return company_id, None, {"error": f"{type(e).__name__}: {e}"}
    finally:
        # Closed BEFORE the steps run, on purpose: the read above left a transaction open, which
        # would otherwise pin a pooled connection for the whole (minutes-long) concurrent run.
        session.close()

    try:
        results = _run_steps_concurrently(company_id, steps, session_factory, on_step)
        if after:
            tail = _open_step_session(session_factory)
            try:
                company = tail.query(Company).filter_by(id=company_id).first()
                for compute in after:
                    compute(tail, company)
            finally:
                tail.close()
        return company_id, name, results
    except Exception as e:  # noqa: BLE001
        return company_id, None, {"error": f"{type(e).__name__}: {e}"}


def _run_steps_concurrently(company_id: str, steps: list, session_factory, on_step) -> dict:
    from concurrent.futures import ThreadPoolExecutor
    import contextvars
    import threading

    ordered = sorted(steps, key=lambda s: _SLOWEST_FIRST.index(s.name) if s.name in _SLOWEST_FIRST else len(_SLOWEST_FIRST))
    total = len(ordered)
    lock, finished, results = threading.Lock(), [0], {}

    def run_step(step: SourceStep):
        if on_step:
            with lock:
                started_after = finished[0]
            on_step(started_after, total, step.name)
        session = _open_step_session(session_factory)
        try:
            company = session.query(Company).filter_by(id=company_id).first()
            outcome = step.fn(company, session) if company else {"status": "error", "error": "company not found"}
        except Exception as e:  # noqa: BLE001 — a step must never take the company's other steps down
            session.rollback()
            outcome = {"status": "error", "error": f"{type(e).__name__}: {e}", "mode": "simulated"}
        finally:
            session.close()
        with lock:
            results[step.name] = outcome
            finished[0] += 1
            done = finished[0]
        if on_step:
            on_step(done, total, step.name, "done")

    # Threads do not inherit the caller's context; copying it is what carries the crawl's chosen
    # worker (worker_hub.use_target) into every step.
    with ThreadPoolExecutor(max_workers=total, thread_name_prefix=f"steps-{company_id[:6]}") as pool:
        futures = [pool.submit(contextvars.copy_context().run, run_step, step) for step in ordered]
        for future in futures:
            future.result()
    # Same key order as the sequential path, whatever order the steps happened to finish in.
    return {step.name: results[step.name] for step in steps}


def run_phase7_for_company(company_id: str, session_factory=None, on_step=None) -> tuple:
    """One company's Phase 7 pass — see run_company_phases."""
    return run_company_phases(company_id, [7], session_factory, on_step)


def run_phase7_batch(company_ids: list, max_workers: int = 3, progress_cb=None, session_factory=None) -> dict:
    """
    Runs the Phase 7 crawlers for several companies with `max_workers` companies
    in flight at once. Sequentially, Phase 7 costs 2-4 minutes per company
    (measured 93-222s across real companies), i.e. half an hour for a ten-company
    batch; three in flight brings that to roughly ten minutes on an 8-core/16 GB
    machine while keeping at most three headless Chromium instances alive at
    once (each crawler run spawns one). More workers mostly buys rate-limit
    errors — the Groq free tier behind the company-website LLM extraction and
    Wayback's CDX API are both per-account/per-IP limited — so the default is
    deliberately modest.

    Each worker opens its OWN session from session_factory: SQLAlchemy sessions
    are not thread-safe, and a Streamlit page's session must stay on the page's
    thread. Results are keyed by company_id — the per-source dict that
    sync_company_applicable_sources returns, or {"error": ...} when that one
    company's run itself blew up (one bad company never takes the batch down).
    progress_cb(done, total, legal_name) runs on the CALLING thread after each
    company finishes, so it can safely drive a Streamlit progress bar.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if session_factory is None:
        from database import SessionFactory as session_factory  # lazy: database imports models/config only

    results = {}
    total = len(company_ids)
    if not total:
        return results
    with ThreadPoolExecutor(max_workers=max(1, min(int(max_workers), total)), thread_name_prefix="phase7") as pool:
        futures = [pool.submit(run_phase7_for_company, cid, session_factory) for cid in company_ids]
        for done, fut in enumerate(as_completed(futures), start=1):
            cid, name, res = fut.result()
            results[cid] = res
            if progress_cb:
                progress_cb(done, total, name)
    return results


def create_company(db: Session, data: dict, auto_sync: bool = False) -> tuple:
    """
    Creates a new target company with country-aware registration normalization,
    initializes all SignalRecord rows, and optionally triggers applicable APIs.

    Returns: (Company, None) on success or (None, error_message) on failure.
    """
    legal_name = (data.get("legal_name") or "").strip()
    raw_reg_nr = (data.get("registration_number") or "").strip()
    country = data.get("country", "Germany").strip()
    if country not in SUPPORTED_COUNTRIES:
        country = "Germany"

    if not legal_name:
        return None, "Legal Name is required."
    if not raw_reg_nr:
        return None, "Registration Number is required."

    norm_reg_nr = normalize_registration_nr(raw_reg_nr, country=country)
    if not norm_reg_nr:
        return None, f"Invalid registration number format for {country}: '{raw_reg_nr}'"

    # Check for duplicate registration number
    existing = db.query(Company).filter_by(registration_number=norm_reg_nr).first()
    if existing:
        return None, f"Company with registration number '{norm_reg_nr}' already exists ({existing.legal_name})."

    # Headcount & Segment derivation
    headcount = data.get("headcount")
    if headcount is not None and str(headcount).strip() != "":
        try:
            headcount = int(headcount)
        except ValueError:
            headcount = None

    segment = data.get("segment")
    if not segment or segment not in ("Midcap", "SME"):
        if headcount is not None:
            segment = "SME" if headcount < 250 else "Midcap"
        else:
            segment = "Midcap"

    nace_code = (data.get("nace_code") or "A01.1").strip()
    sector_name = (data.get("sector_name") or "Agrifood & Agriculture").strip()
    website_url = (data.get("website_url") or "").strip() or None
    shortlist_status = data.get("shortlist_status", "candidate")

    company = Company(
        legal_name=legal_name,
        registration_number=norm_reg_nr,
        country=country,
        nace_code=nace_code,
        sector_name=sector_name,
        website_url=website_url,
        segment=segment,
        headcount=headcount,
        headcount_source_tier="T1" if headcount else None,
        shortlist_status=shortlist_status,
        shortlisted_at=datetime.utcnow() if shortlist_status in ("shortlisted", "in_pilot") else None,
    )
    db.add(company)
    db.flush()

    # Initialize all active indicator definitions as not_yet_checked SignalRecords
    indicator_defs = fetch_indicator_defs(db)
    for sig_key, defn in indicator_defs.items():
        source_sys = defn.get("source_system") or "Unknown"
        is_app = is_source_applicable(source_sys, country)

        sig_rec = SignalRecord(
            company_id=company.id,
            signal_key=sig_key,
            source=source_sys,
            numeric_value=None,
            text_value=None,
            status="not_yet_checked",
            confidence=1.0,
            is_simulated=True,
            fetched_at=datetime.utcnow(),
            raw_payload_ref=json.dumps({
                "initialized": True,
                "applicable_for_country": is_app,
                "country": country,
                "signal_key": sig_key
            })
        )
        db.add(sig_rec)

    db.commit()

    # Optional immediate live API sync
    if auto_sync:
        sync_company_applicable_sources(company, db, phases=[1, 4])

    return company, None


def get_csv_template() -> str:
    """Returns a CSV string template with German and Italian sample companies."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "legal_name", "registration_number", "country", "nace_code",
        "sector_name", "website_url", "segment", "headcount"
    ])
    writer.writerow([
        "BioBavaria SmartFarming GmbH", "HRB 789012", "Germany", "A01.11",
        "Agrifood & Smart Farming", "https://biobavaria.de", "Midcap", "320"
    ])
    writer.writerow([
        "AgroTech Lombardia S.r.l.", "IT09876543210", "Italy", "A01.13",
        "Horticulture & Vertical Farming", "https://agrotech-lombardia.it", "SME", "45"
    ])
    writer.writerow([
        "Emilia Romagna Precision AG", "REA BO-1234567", "Italy", "A01.61",
        "Agricultural Machinery & Robotics", "https://er-precision.it", "Midcap", "410"
    ])
    return output.getvalue()


def import_companies_from_csv(db: Session, csv_content_or_file, auto_sync: bool = False,
                               progress_callback=None) -> dict:
    """
    Imports multiple companies from CSV file buffer or text.
    Handles German and Italian entries with column mapping and validation.

    progress_callback, when given, is called as progress_callback(rows_done,
    total_rows) once per row so a caller can track a long import live.
    """
    if hasattr(csv_content_or_file, "read"):
        content = csv_content_or_file.read()
        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")
    else:
        content = str(csv_content_or_file)

    try:
        df = pd.read_csv(io.StringIO(content))
    except Exception as e:
        return {"created": 0, "skipped": 0, "errors": [f"Could not parse CSV: {str(e)}"]}

    # Clean and normalize column names
    col_map = {c: c.lower().strip().replace(" ", "_") for c in df.columns}
    df = df.rename(columns=col_map)

    required_cols = ["legal_name", "registration_number"]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        return {
            "created": 0,
            "skipped": 0,
            "errors": [f"Missing required CSV column(s): {', '.join(missing)}. Required: legal_name, registration_number"]
        }

    created_count = 0
    skipped_count = 0
    errors = []

    total_rows = len(df)
    for idx, row in df.iterrows():
        if progress_callback:
            progress_callback(idx + 1, total_rows)
        row_dict = row.to_dict()
        row_num = idx + 2  # 1-indexed header + 1

        # Clean NaNs
        cleaned_data = {}
        for k, v in row_dict.items():
            if pd.isna(v):
                cleaned_data[k] = None
            else:
                cleaned_data[k] = str(v).strip()

        comp, err = create_company(db, cleaned_data, auto_sync=auto_sync)
        if comp:
            created_count += 1
        else:
            skipped_count += 1
            errors.append(f"Row {row_num} ('{cleaned_data.get('legal_name', 'Unknown')}'): {err}")

    return {
        "created": created_count,
        "skipped": skipped_count,
        "errors": errors
    }


# =============================================================================
# Flexible column-mapping data feeder
#
# Unlike import_companies_from_csv above (one fixed 8-column shape), this lets
# a dataset with ARBITRARY column names/order be mapped onto Company fields
# and IndicatorDefinition signals interactively, then replayed on future
# uploads of the same shape via a saved ColumnMappingProfile. See the
# "Flexible Column-Mapping Data Feeder" plan for the full design rationale.
# =============================================================================

_GROUP_SUFFIX_RE = re.compile(r'^(?P<base>.+)_(?P<suffix>latest|y-\d+)$')

# Fixed Company-field targets a column can be mapped onto, keyed by the
# normalized names real-world exports are likely to use. Auto-suggestion only
# — the mapping UI lets the user pick any of these regardless of this table.
COMPANY_FIELD_ALIASES = {
    "legal_name": "company:legal_name", "ragione_sociale": "company:legal_name",
    "company_name": "company:legal_name", "name": "company:legal_name",
    "company_legal_name": "company:legal_name",
    "registration_number": "company:registration_number", "partita_iva": "company:registration_number",
    "vat_number": "company:registration_number", "p_iva": "company:registration_number",
    "nace_code": "company:nace_code", "ateco_code": "company:nace_code",
    "sector_name": "company:sector_name", "ateco_description": "company:sector_name",
    "website_url": "company:website_url", "website": "company:website_url",
    "region": "company:region", "province": "company:province", "legal_form": "company:legal_form",
    "incorporation_date": "company:incorporation_date",
    "status": "company:registry_status", "registry_status": "company:registry_status",
    "headcount": "company:headcount", "employees": "company:headcount",
    "number_of_employees": "company:headcount",
    "external_ref_id": "company:external_ref_id", "company_id": "company:external_ref_id",
    "company_id_by_aida": "company:external_ref_id", "aida_company_id": "company:external_ref_id",
}

# Base names (after suffix-stripping) that alias onto a computed trend
# indicator — only offered/suggested when the column group actually has 2+
# timepoints (see detect_column_groups / compute_group_value).
TREND_BASE_ALIASES = {"revenue": "revenue_trend", "ebit": "ebit_trend", "gross_margin": "margin_compression",
                      "ebitda": "ebitda_trend"}

# How a derived (multi-column) signal was computed, stored on the SignalRecord so the number can be audited
# without reading this file.
DERIVATION_BASIS = {
    "margin_compression": "percentage points of revenue: gross margin ÷ revenue at the earliest and latest year, "
                          "the fall between them, floored at 0",
    "ebitda_trend": "% change of EBITDA, latest vs earliest year, sign-safe (divided by the absolute base)",
}

# Base names that alias onto a direct-value (non-trend) indicator whose own
# key doesn't literally match the stripped base name.
DIRECT_BASE_ALIASES = {
    "interest_coverage": "interest_coverage_ratio",
    "total_assets": "total_assets",
    "cash": "cash_position",
    "total_debt": "debt_level",
    "leverage_ratio": "leverage_ratio",
    "subsidiary_count": "subsidiary_participations",
}

STRING_COMPANY_FIELDS = {
    "legal_name", "nace_code", "sector_name", "website_url",
    "region", "province", "legal_form", "registry_status", "external_ref_id",
}

COMPANY_TARGET_LABELS = {
    "company:legal_name": "Company: Legal Name",
    "company:registration_number": "Company: Registration Number (required, exactly one)",
    "company:nace_code": "Company: NACE/ATECO Code",
    "company:sector_name": "Company: Sector Name",
    "company:website_url": "Company: Website",
    "company:region": "Company: Region",
    "company:province": "Company: Province",
    "company:legal_form": "Company: Legal Form",
    "company:incorporation_date": "Company: Incorporation Date",
    "company:registry_status": "Company: Registry Status",
    "company:headcount": "Company: Headcount",
    "company:external_ref_id": "Company: External Reference ID",
}


def _norm(s) -> str:
    return str(s).strip().lower().replace(" ", "_")


def create_ad_hoc_indicator(db: Session, label: str, dataset_name: str = None, source_column: str = None) -> str:
    """
    Creates a new context-axis, unscored (weight=0) IndicatorDefinition for a
    column the user chose to map as "genuinely new" rather than link to an
    existing one — same pattern the catalog already uses for informational
    tags (product_type_tag, family_ownership_share, etc.). It's saved into
    the same IndicatorDefinition table every other indicator lives in, so it
    shows up on the Indicator Weights page immediately (no separate list) —
    editable/re-weightable there like any other row, just starting inert.

    dataset_name/source_column (when given) go into source_system/proxy/
    comment so the row is self-explanatory on that page rather than a bare
    label with no context for where it came from.

    Returns the new indicator's key; no-ops (returns the existing key) if one
    with that exact key already exists.
    """
    key = _norm(label)[:100]
    if not key:
        return None
    existing = db.query(IndicatorDefinition).filter_by(key=key).first()
    if existing:
        return key
    origin = f"column '{source_column}' in the '{dataset_name}' dataset" if source_column and dataset_name else "an uploaded dataset column"
    db.add(IndicatorDefinition(
        key=key, label=str(label).strip()[:255] or key, category=CAT_CONTEXT, axis="context",
        weight=0.0, phase=1, is_active=True, source_system=dataset_name,
        proxy=f"As provided in {origin} — not yet mapped onto an existing indicator.",
        comment=(
            f"Created automatically from {origin} via the Flexible Data Import feeder "
            f"on {datetime.utcnow().strftime('%Y-%m-%d')}. Informational/context by default (weight 0, "
            f"never summed into Need/Readiness) — re-categorize the Axis/Weight above if this should "
            f"actually be scored."
        ),
        source_description=dataset_name,
    ))
    db.commit()
    return key


def parse_uploaded_file(file_or_buffer, filename: str = None) -> pd.DataFrame:
    """
    Reads an uploaded company dataset (.csv or .xlsx, one row per company)
    into a DataFrame with normalized column headers — same lower/strip/
    space-to-underscore normalization import_companies_from_csv already uses
    (deliberately NOT touching hyphens: detect_column_groups' 'y-1'-style
    suffix pattern depends on the hyphen surviving normalization).
    """
    name = (filename or getattr(file_or_buffer, "name", "") or "").lower()
    if name.endswith(".xlsx") or name.endswith(".xls"):
        df = pd.read_excel(file_or_buffer)
    else:
        if hasattr(file_or_buffer, "read"):
            content = file_or_buffer.read()
            if isinstance(content, bytes):
                content = content.decode("utf-8", errors="replace")
        else:
            content = str(file_or_buffer)
        df = pd.read_csv(io.StringIO(content))
    df = df.rename(columns={c: str(c).strip().lower().replace(" ", "_") for c in df.columns})
    return df


def detect_column_groups(columns: list) -> dict:
    """
    Groups normalized column names sharing a '<base>_latest' / '<base>_y-1' /
    '<base>_y-2' ... convention into time-series variables, so the mapping UI
    can offer computed trend indicators (revenue_trend, ebit_trend,
    margin_compression) only where a real multi-year series exists. A column
    with no recognized suffix is its own single-point group (base = the
    whole column name, suffix 'value').

    Returns {base: {"points": {suffix: original_column_name}, "is_timeseries": bool}}.
    """
    groups = {}
    for col in columns:
        match = _GROUP_SUFFIX_RE.match(col)
        base, suffix = (match.group("base"), match.group("suffix")) if match else (col, "value")
        groups.setdefault(base, {"points": {}})["points"][suffix] = col
    for g in groups.values():
        g["is_timeseries"] = len(g["points"]) >= 2
    return groups


def suggest_mapping(db: Session, groups: dict, existing_profile: dict = None) -> dict:
    """
    Auto-suggests a target for each detected column group: an existing saved
    profile's assignment (reviewed, never silently trusted — see the mapping
    UI), else a known alias, else an exact match against an indicator's own
    key/label, else None (genuinely unrecognized — the user must assign it).
    """
    indicator_defs = fetch_indicator_defs(db)
    key_by_label = {_norm(defn["label"]): key for key, defn in indicator_defs.items()}

    suggestions = {}
    for base, group in groups.items():
        norm_base = _norm(base)

        if existing_profile and base in existing_profile:
            suggestions[base] = existing_profile[base]
            continue

        target = COMPANY_FIELD_ALIASES.get(norm_base)
        if not target and group["is_timeseries"] and norm_base in TREND_BASE_ALIASES:
            target = f"indicator:{TREND_BASE_ALIASES[norm_base]}"
        if not target and norm_base in DIRECT_BASE_ALIASES:
            target = f"indicator:{DIRECT_BASE_ALIASES[norm_base]}"
        if not target and norm_base in indicator_defs:
            target = f"indicator:{norm_base}"
        if not target and norm_base in key_by_label:
            target = f"indicator:{key_by_label[norm_base]}"
        suggestions[base] = target
    return suggestions


def valid_targets_for_group(db: Session, group: dict) -> dict:
    """
    The full set of {target_key: display_label} the mapping UI may offer for
    one column group — trend-style indicators only appear for a group with
    2+ timepoints, so a single column can never be wired to a formula that
    structurally needs two (the bug-safety mechanism, not a post-hoc check).
    """
    indicator_defs = fetch_indicator_defs(db)
    options = {"": "— Ignore —"}
    options.update(COMPANY_TARGET_LABELS)
    for key, defn in sorted(indicator_defs.items(), key=lambda kv: (kv[1]["category"], kv[1]["label"])):
        if key in TREND_INDICATOR_KEYS and not group["is_timeseries"]:
            continue
        options[f"indicator:{key}"] = f"{defn['category']} — {defn['label']}"
    return options


def _json_safe(v):
    """Coerces a raw pandas cell value into something json.dumps/JSON-column
    can actually serialize — numpy scalars (int64/float64/bool_), Timestamps,
    and NaN all fail otherwise."""
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    if hasattr(v, "item"):  # numpy scalar (int64, float64, bool_, ...)
        try:
            v = v.item()
        except (TypeError, ValueError):
            pass
    if isinstance(v, pd.Timestamp):
        return v.isoformat()
    if isinstance(v, (int, float, str, bool)):
        return v
    return str(v)


def _clean_str(v):
    """Coerces a raw pandas cell value to a plain str for a String column —
    guards against writing a numpy float/int straight into a VARCHAR column,
    which SQLite tolerates silently but Postgres may not adapt cleanly."""
    if v is None:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    s = str(v).strip()
    return s or None


def _to_float(v):
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pick_latest_and_base(point_values: dict):
    """point_values: {suffix: raw_cell_value}. Picks the most-recent value
    ('latest', else the lone 'value', else the smallest y-N as a proxy) and
    the furthest-back value (largest y-N, or None if there isn't one)."""
    if "latest" in point_values:
        latest_val = point_values["latest"]
    elif "value" in point_values:
        latest_val = point_values["value"]
    else:
        y_points = sorted((int(s.split("-")[1]), s) for s in point_values if s.startswith("y-"))
        latest_val = point_values[y_points[0][1]] if y_points else None

    y_points_all = sorted((int(s.split("-")[1]), s) for s in point_values if s.startswith("y-"))
    base_val = point_values[y_points_all[-1][1]] if y_points_all else None
    return latest_val, base_val


def _latest_and_base_suffixes(suffixes) -> tuple:
    """The suffix naming the most recent point and the one naming the furthest-back point — the same
    choice _pick_latest_and_base makes on values, but returning the names so a second column group
    (e.g. revenue next to gross margin) can be read at the very same two points."""
    y_points = sorted((int(sfx.split("-")[1]), sfx) for sfx in suffixes if sfx.startswith("y-"))
    if "latest" in suffixes:
        latest = "latest"
    elif "value" in suffixes:
        latest = "value"
    else:
        latest = y_points[0][1] if y_points else None
    return latest, (y_points[-1][1] if y_points else None)


def _margin_compression_points(group: dict, row: dict, companions: dict = None):
    """
    Fall in gross margin, as percentage points of revenue, between the earliest and the latest year
    (floored at 0) — the unit the margin_compression indicator is defined in (0-25 points).

    Imported margins are usually an AMOUNT, not a percentage (AIDA's "Margine sui consumi" is in
    thousand EUR). The old code subtracted two amounts and stored the k EUR difference as if it were
    points: 365 of 945 real values exceeded 100 "points". So:
      * a revenue column next to the margin (same suffixes) -> the margin is an amount in the same unit
        as revenue; convert each point to % of revenue first. A margin that comes out above 100% of
        revenue can't be an amount of it, so the row is left unchecked.
      * no revenue column -> only trust margins that already look like percentages (|value| <= 100);
        anything else is left unchecked rather than guessed at.
    A file carrying BOTH revenue and a margin already expressed in % would be misread by the first
    rule; the real exports (AIDA) carry amounts, and that ambiguity can't be resolved from the data.
    Returns None when it can't be computed.
    """
    latest_s, base_s = _latest_and_base_suffixes(group["points"])
    if latest_s is None or base_s is None:
        return None
    margin = {s: _to_float(row.get(group["points"][s])) for s in (latest_s, base_s)}
    if any(v is None for v in margin.values()):
        return None

    revenue_group = (companions or {}).get("revenue")
    if revenue_group:
        pct = {}
        for s in (latest_s, base_s):
            col = revenue_group["points"].get(s)
            revenue = _to_float(row.get(col)) if col else None
            if not revenue or revenue <= 0:
                return None
            pct[s] = margin[s] / revenue * 100.0
        if any(abs(v) > 100.0 for v in pct.values()):
            return None
        return max(0.0, pct[base_s] - pct[latest_s])

    if all(abs(v) <= 100.0 for v in margin.values()):
        return max(0.0, margin[base_s] - margin[latest_s])
    return None


def compute_group_value(group: dict, row: dict, indicator_key: str = None, companions: dict = None):
    """
    Resolves one column-group's contribution to a target for a single row.
    Returns (numeric_value_or_None, status) with status 'present' or
    'not_yet_checked' — a missing/zero base is left not_yet_checked rather
    than faked into a number, same tri-state honesty rule the rest of the
    signal pipeline follows.

    indicator_key in TREND_INDICATOR_KEYS computes a change between the
    'latest' point and the furthest-back 'y-N' point (% change, abs-based for
    signed metrics like EBIT so a sign flip in the base doesn't invert the
    trend direction; margin_compression is a direct point-decline instead of
    a %, matching that indicator's own 0-25 "decline points" definition — see
    _margin_compression_points for how an amount is turned into percentage points).
    Anything else just takes the single most recent point.

    companions: every detected column group of the same file ({base: group}), which lets a derived
    signal read a second variable at the same points (margin compression needs revenue).
    """
    point_values = {suffix: row.get(col) for suffix, col in group["points"].items()}
    latest_val, base_val = _pick_latest_and_base(point_values)
    latest_num = _to_float(latest_val)

    if indicator_key in TREND_INDICATOR_KEYS:
        base_num = _to_float(base_val)
        if latest_num is None or base_num is None:
            return None, "not_yet_checked"
        if indicator_key == "margin_compression":
            points = _margin_compression_points(group, row, companions)
            return (None, "not_yet_checked") if points is None else (points, "present")
        if base_num == 0:
            return None, "not_yet_checked"
        return (latest_num - base_num) / abs(base_num) * 100.0, "present"

    if latest_num is None:
        return None, "not_yet_checked"
    return latest_num, "present"


def _parse_date(val):
    try:
        ts = pd.to_datetime(val, errors="coerce")
    except (TypeError, ValueError):
        return None
    if ts is None or pd.isna(ts):
        return None
    return ts.to_pydatetime()


def apply_data_import(db: Session, df: pd.DataFrame, mapping: dict, dataset_name: str,
                       country: str = "Italy", overwrite_conflicts: bool = False,
                       dry_run: bool = False, source_filename: str = None,
                       progress_callback=None) -> dict:
    """
    Applies a reviewed column mapping ({group_base: 'company:<field>' |
    'indicator:<key>' | None}) to every row of df, one row per company.

    progress_callback, when given, is called as progress_callback(rows_done,
    total_rows) once per row (including skipped/errored ones) so a caller
    (e.g. a Streamlit progress bar) can track a long import live.

    Reuses create_company() for brand-new companies (dedup check, segment/
    headcount derivation, full not-yet-checked signal scaffold), then writes
    two things per row on a real (non-dry-run) pass: the mapped signals as
    real SignalRecords (is_simulated=False, status='present') tagged
    source=dataset_name — the structured side — and the row's complete
    original column set as a RawImportRecord blob, independent of what got
    mapped — the blob side, for retroactively applying a new mapping idea
    later without re-uploading the file.

    Conflict rule: a company that already has a *present* signal sourced
    from this exact dataset_name is a conflict — real runs skip it unless
    overwrite_conflicts is set; a *different* dataset_name for the same
    company just merges in, no flag. dataset_name is therefore what scopes
    "same vs. different dataset type" per the user's own rule.

    Match key: exactly one column must be mapped to EITHER Registration
    Number OR Legal Name — registration_number is preferred when both are
    present. Registration-number matching can create brand-new companies
    (a registration number is a reliable enough identifier for that);
    legal-name matching never does — a source whose only identity column
    isn't a real registration number (verified example: AIDA's own "BvD ID
    number" column frequently does NOT match the registration_number
    already on file for the same company) is exactly the case this is for,
    and guessing a company into existence from a name string alone risks a
    real duplicate. Rows that don't match an existing company under
    legal-name mode land in "unmatched" instead.

    dry_run is a genuinely separate read-only classification pass (file
    already parsed into df; only Company lookups happen, zero writes) rather
    than a transaction-rollback trick — create_company() commits internally,
    so it can't be safely wrapped in a savepoint for preview purposes.

    Returns {"created", "merged", "overwritten": int,
             "conflicts": [{"legal_name", "registration_number"}...],
             "unmatched": [legal_name...], "errors": [str...]}.
    """
    result = {"created": 0, "merged": 0, "overwritten": 0, "conflicts": [], "unmatched": [], "errors": []}

    if not dataset_name or not str(dataset_name).strip():
        result["errors"].append("Dataset name is required.")
        return result
    dataset_name = str(dataset_name).strip()

    groups = detect_column_groups(list(df.columns))

    reg_bases = [b for b, t in mapping.items() if t == "company:registration_number"]
    name_bases = [b for b, t in mapping.items() if t == "company:legal_name"]
    if len(reg_bases) == 1:
        match_mode, match_base = "registration_number", reg_bases[0]
    elif len(name_bases) == 1:
        match_mode, match_base = "legal_name", name_bases[0]
    else:
        result["errors"].append(
            "Exactly one column must be mapped to Registration Number or Legal Name "
            f"(found {len(reg_bases)} Registration Number, {len(name_bases)} Legal Name)."
        )
        return result

    indicator_defs = fetch_indicator_defs(db)

    total_rows = len(df)
    for idx, row in df.iterrows():
        if progress_callback:
            progress_callback(idx + 1, total_rows)
        row_dict = row.to_dict()
        row_num = idx + 2  # 1-indexed header + 1

        match_points = {suffix: row_dict.get(col) for suffix, col in groups[match_base]["points"].items()}
        raw_match, _ = _pick_latest_and_base(match_points)
        has_match_val = raw_match is not None and not (isinstance(raw_match, float) and pd.isna(raw_match)) and str(raw_match).strip()

        if match_mode == "registration_number":
            norm_reg_nr = normalize_registration_nr(str(raw_match).strip(), country=country) if has_match_val else ""
            if not norm_reg_nr:
                result["errors"].append(f"Row {row_num}: could not read a registration number.")
                continue
            existing = db.query(Company).filter_by(registration_number=norm_reg_nr).first()
        else:
            match_legal_name = _clean_str(raw_match) if has_match_val else None
            if not match_legal_name:
                result["errors"].append(f"Row {row_num}: could not read a legal name.")
                continue
            existing = db.query(Company).filter_by(legal_name=match_legal_name).first()
            if not existing:
                result["unmatched"].append(match_legal_name)
                continue
            norm_reg_nr = existing.registration_number

        # Resolve every mapped field/indicator for this row up front.
        company_field_updates = {}
        signal_updates = {}  # signal_key -> (value, status)
        for base, target in mapping.items():
            if not target or base == match_base:
                continue
            group = groups.get(base)
            if not group:
                continue
            point_values = {suffix: row_dict.get(col) for suffix, col in group["points"].items()}

            if target.startswith("company:"):
                field = target.split(":", 1)[1]
                val, _ = _pick_latest_and_base(point_values)
                if val is not None and not (isinstance(val, float) and pd.isna(val)):
                    company_field_updates[field] = val
            elif target.startswith("indicator:"):
                sig_key = target.split(":", 1)[1]
                if sig_key in indicator_defs:
                    signal_updates[sig_key] = compute_group_value(group, row_dict, sig_key, companions=groups)

        is_conflict = False
        if existing:
            is_conflict = db.query(SignalRecord).filter_by(
                company_id=existing.id, source=dataset_name, status="present"
            ).first() is not None

        if is_conflict and not (overwrite_conflicts and not dry_run):
            result["conflicts"].append({"legal_name": existing.legal_name, "registration_number": norm_reg_nr})
            continue

        if dry_run:
            if not existing:
                result["created"] += 1
            else:
                result["merged"] += 1
            continue

        # --- real write path ---
        if not existing:
            create_data = {
                "legal_name": _clean_str(company_field_updates.get("legal_name")) or f"Unnamed ({norm_reg_nr})",
                "registration_number": norm_reg_nr,
                "country": country,
                "nace_code": _clean_str(company_field_updates.get("nace_code")),
                "sector_name": _clean_str(company_field_updates.get("sector_name")),
                "website_url": _clean_str(company_field_updates.get("website_url")),
                "headcount": company_field_updates.get("headcount"),
            }
            company, err = create_company(db, create_data, auto_sync=False)
            if err:
                result["errors"].append(f"Row {row_num} ('{create_data['legal_name']}'): {err}")
                continue
            was_new = True
        else:
            company = existing
            was_new = False

        for field, val in company_field_updates.items():
            if field == "incorporation_date":
                val = _parse_date(val)
                if val is None:
                    continue
            elif field == "headcount":
                try:
                    val = int(float(val))
                except (TypeError, ValueError):
                    continue
                company.headcount_source_tier = "T1"
            elif field in STRING_COMPANY_FIELDS:
                val = _clean_str(val)
                if val is None:
                    continue
            if hasattr(company, field):
                setattr(company, field, val)

        fetched_at = datetime.utcnow()
        for sig_key, (value, status) in signal_updates.items():
            sig = db.query(SignalRecord).filter_by(company_id=company.id, signal_key=sig_key).first()
            if not sig:
                sig = SignalRecord(company_id=company.id, signal_key=sig_key, source=dataset_name)
                db.add(sig)
            sig.status = status
            sig.numeric_value = value if status == "present" else None
            sig.confidence = 1.0
            sig.is_simulated = False
            sig.source = dataset_name
            sig.fetched_at = fetched_at
            payload = {"dataset": dataset_name, "signal_key": sig_key}
            if sig_key in DERIVATION_BASIS:
                payload["basis"] = DERIVATION_BASIS[sig_key]
            sig.raw_payload_ref = json.dumps(payload)

        # Blob side: the complete original row, every column, untouched by
        # the mapping — independent of which subset just got interpreted
        # into signals above. One row per (company, dataset); re-importing
        # the same dataset overwrites it, matching the structured side.
        raw_rec = db.query(RawImportRecord).filter_by(company_id=company.id, dataset_name=dataset_name).first()
        if not raw_rec:
            raw_rec = RawImportRecord(company_id=company.id, dataset_name=dataset_name)
            db.add(raw_rec)
        raw_rec.source_filename = source_filename
        raw_rec.source_row_index = row_num
        raw_rec.raw_row = {col: _json_safe(v) for col, v in row_dict.items()}
        raw_rec.mapping_snapshot = mapping
        raw_rec.updated_at = fetched_at

        db.commit()

        if "incorporation_date" in company_field_updates:
            # A newly-set/changed incorporation date could make an already-
            # imported board roster's succession pattern newly detectable or
            # quantifiable — see sync_succession_signal.
            sync_succession_signal(db, company, source=dataset_name)

        if was_new:
            result["created"] += 1
        elif is_conflict:
            result["overwritten"] += 1
        else:
            result["merged"] += 1

    return result


def save_mapping_profile(db: Session, dataset_name: str, country: str, mapping: dict) -> ColumnMappingProfile:
    dataset_name = str(dataset_name).strip()
    profile = db.query(ColumnMappingProfile).filter_by(dataset_name=dataset_name).first()
    if not profile:
        profile = ColumnMappingProfile(dataset_name=dataset_name)
        db.add(profile)
    profile.country = country
    profile.mapping_json = json.dumps(mapping)
    profile.updated_at = datetime.utcnow()
    db.commit()
    return profile


def load_mapping_profile(db: Session, dataset_name: str) -> dict:
    profile = db.query(ColumnMappingProfile).filter_by(dataset_name=str(dataset_name).strip()).first()
    if not profile:
        return {}
    try:
        return json.loads(profile.mapping_json)
    except (TypeError, ValueError):
        return {}


def list_mapping_profiles(db: Session) -> list:
    return db.query(ColumnMappingProfile).order_by(ColumnMappingProfile.dataset_name).all()


def delete_companies(db: Session, company_ids: list) -> dict:
    """
    Permanently deletes the given companies. SignalRecords cascade via the
    Company.signals relationship (cascade="all, delete-orphan" in models.py);
    PilotOutcome rows don't have that cascade (a company shouldn't silently
    take its own pilot history down without that being visible), so they're
    deleted explicitly here instead of letting a FK constraint fail the
    whole operation.

    Irreversible. The caller (the Manage Companies UI) is responsible for
    confirming with the user before calling this — this function does not
    re-confirm.

    Returns {"deleted", "signals_deleted", "pilot_outcomes_deleted", "raw_import_records_deleted": int}.
    """
    deleted = 0
    signals_deleted = 0
    pilots_deleted = 0
    raw_deleted = 0
    people_deleted = 0
    for company_id in company_ids:
        company = db.query(Company).filter_by(id=company_id).first()
        if not company:
            continue
        signals_deleted += db.query(SignalRecord).filter_by(company_id=company_id).count()
        pilots = db.query(PilotOutcome).filter_by(company_id=company_id).all()
        pilots_deleted += len(pilots)
        for pilot in pilots:
            db.delete(pilot)
        raw_records = db.query(RawImportRecord).filter_by(company_id=company_id).all()
        raw_deleted += len(raw_records)
        for rec in raw_records:
            db.delete(rec)
        people = db.query(CompanyPerson).filter_by(company_id=company_id).all()
        people_deleted += len(people)
        for person in people:
            db.delete(person)
        db.delete(company)
        deleted += 1
    db.commit()
    return {
        "deleted": deleted, "signals_deleted": signals_deleted,
        "pilot_outcomes_deleted": pilots_deleted, "raw_import_records_deleted": raw_deleted,
        "people_deleted": people_deleted,
    }


# =============================================================================
# People & ownership roster import
#
# A different shape from the flexible column-mapping feeder above: one source
# row is still one company, but several columns each pack MULTIPLE entities'
# values into a single cell, newline-stacked in matching order across columns
# (AIDA's own export convention — e.g. 'DM\nNome completo' holds N directors'
# names, 'DM\nCarica' the same N people's roles, in the same order). That
# can't go through a one-value-per-column mapper; it needs exploding into one
# CompanyPerson row per person/entity. Originally built for the director/
# advisor board roster (detect_multivalue_groups: every group column shares
# a literal 'Prefix\n...' header), then extended to AIDA's shareholder-
# control and legal-ownership exports, which stack the exact same way but
# DON'T consistently mark every group column with the same header
# convention — 'Azionisti\nNome' (newline) sits next to 'Azionisti Numero
# BvD' (space) and 'Azionista - Ticker Symbol' (dash, a singular/plural
# variant), and the ownership-chain's 'Livello' column shares no header text
# with 'CSH' at all despite being part of the same per-row stack. See
# detect_loose_stacked_groups for how those are still found (by content,
# not naming), and the "Management & Board Roster Import" plan for the
# original design rationale, including why matching is done by legal_name
# rather than the source file's own BvD ID column (verified directly against
# real data: BvD ID doesn't reliably match the registration_number already
# on file for the same company, legal_name does).
# =============================================================================

# Normalized (lower/strip) Italian sub-label -> CompanyPerson structured column.
# Anything not listed here still survives in full inside raw_fields.
_PERSON_FIELD_ALIASES = {
    "nome completo": "full_name",
    "carica": "role",
    "età": "age",
    "eta": "age",
    "genere": "gender",
    "paese di nazionalità": "nationality",
    "paese di nazionalita": "nationality",
    "data nomina": "appointment_date",
    "data dimissioni": "resignation_date",
    "attuale o precedente": "current_or_former",
    # Shareholder/subsidiary entity type (e.g. "Persone fisiche o famiglie",
    # "Società") — not a director-style title, but 'role' is otherwise
    # unused for these groups and this keeps it visible in the same "Role"
    # column the Management & Board table already renders.
    "tipo": "role",
}

# Mirrors COMPANY_TARGET_LABELS' role for the people/roster importer: the
# full set of targets the per-group column-mapping UI may offer for one
# sub-column (a CompanyPerson structured field, or "" to leave it in
# raw_fields only). Deliberately just the fixed structured columns rather
# than IndicatorDefinition keys — an individual person's cell isn't itself
# an indicator value; sync_management_composition_signals is what turns a
# roster of these into the actual Leadership & Succession indicators.
PERSON_TARGET_LABELS = {
    "": "— Ignore (kept in raw data only) —",
    "full_name": "Person: Full Name",
    "role": "Person: Role / Title",
    "age": "Person: Age",
    "gender": "Person: Gender",
    "nationality": "Person: Nationality",
    "appointment_date": "Person: Appointment Date",
    "resignation_date": "Person: Resignation Date",
    "current_or_former": "Person: Current or Former",
}

# ColumnMappingProfile.dataset_name is unique across the whole table and is
# also shared with the flexible importer's profiles above — this prefix
# keeps a people-roster mapping (sub_label -> CompanyPerson field) from ever
# colliding with a flexible-import mapping (column base -> company/indicator
# target) that happens to reuse the same human-chosen dataset name. Purely
# internal: the People & Ownership tab shows/accepts the plain name.
PEOPLE_PROFILE_PREFIX = "people::"


def _person_field_map_key(role_group: str, sub_label: str) -> str:
    return f"{role_group}::{sub_label}"


def suggest_person_mapping(groups: dict, existing_profile: dict = None) -> dict:
    """
    Auto-suggests a CompanyPerson field for each (role_group, sub-column)
    pair detected in a roster file — a saved profile's assignment (reviewed,
    never silently trusted, same as the flexible importer's suggest_mapping)
    wins, else the built-in _PERSON_FIELD_ALIASES guess, else "" (genuinely
    unrecognized — stays in raw_fields only until the user maps it).

    groups: {role_group: [original_column_name, ...]} as returned by
    detect_person_groups. Returns {"role_group::sub_label": target_field}.
    """
    suggestions = {}
    for role_group, columns in groups.items():
        for col in columns:
            sub_label = strip_person_column_prefix(col)
            map_key = _person_field_map_key(role_group, sub_label)
            if existing_profile and map_key in existing_profile:
                suggestions[map_key] = existing_profile[map_key]
            else:
                suggestions[map_key] = _PERSON_FIELD_ALIASES.get(sub_label.strip().lower(), "")
    return suggestions


def save_person_mapping_profile(db: Session, dataset_name: str, mapping: dict) -> ColumnMappingProfile:
    return save_mapping_profile(db, f"{PEOPLE_PROFILE_PREFIX}{str(dataset_name).strip()}", "N/A", mapping)


def load_person_mapping_profile(db: Session, dataset_name: str) -> dict:
    return load_mapping_profile(db, f"{PEOPLE_PROFILE_PREFIX}{str(dataset_name).strip()}")


def list_person_mapping_profile_names(db: Session) -> list:
    return [
        p.dataset_name[len(PEOPLE_PROFILE_PREFIX):]
        for p in list_mapping_profiles(db)
        if p.dataset_name.startswith(PEOPLE_PROFILE_PREFIX)
    ]


# =============================================================================
# Flexible people import — ONE ROW PER PERSON
#
# import_company_people above requires the AIDA-style convention: one row per
# COMPANY, with multiple people's values newline-stacked inside each cell.
# That's the right shape for AIDA's own exports, but it's a genuinely hard
# target for any other source to hit correctly — in particular an LLM-assisted
# extraction pipeline (e.g. a LinkedIn-profile-to-CSV Gem), where getting
# embedded-newline CSV quoting exactly right across many stacked values,
# perfectly positionally aligned across several columns, is an easy thing to
# get subtly wrong.
#
# This mirrors the Flexible Data Import feeder's own architecture instead
# (apply_data_import/suggest_mapping/valid_targets_for_group below) — arbitrary
# column names, auto-suggested + human-reviewed + saved mapping — but for
# CompanyPerson rows, one row per person, company repeated on every row. Any
# source that can produce a flat CSV (which is nearly all of them, including
# an LLM) can be piped in with zero pre-processing.
# =============================================================================

FLAT_IMPORT_ROLE_GROUP = "Flat Import"

# Normalized (via _norm) column name -> target. Deliberately does NOT include
# aliases for education level/field, a "digital/innovation lead" title match,
# or free-text confidence notes — CompanyPerson has no dedicated column for
# any of those yet (see sync_management_composition_signals' own docstring on
# this same gap). They still survive in full inside raw_fields below, ready
# for a future aggregation step without needing the file re-uploaded.
PERSON_FLAT_COLUMN_ALIASES = {
    "company_legal_name": "match:legal_name", "legal_name": "match:legal_name",
    "company": "match:legal_name", "company_name": "match:legal_name",
    "ragione_sociale": "match:legal_name",
    "full_name": "person:full_name", "name": "person:full_name",
    "role": "person:role", "title": "person:role", "role_title": "person:role",
    "estimated_age": "person:age", "age": "person:age",
    "gender": "person:gender",
    "nationality": "person:nationality",
    "appointment_date": "person:appointment_date",
    "resignation_date": "person:resignation_date",
    "current_or_former": "person:current_or_former",
}

PERSON_FLAT_TARGET_LABELS = {
    "": "— Ignore (kept in raw data only) —",
    "match:legal_name": "Match: Company Legal Name (required, exactly one)",
    "person:full_name": "Person: Full Name",
    "person:role": "Person: Role / Title",
    "person:age": "Person: Age",
    "person:gender": "Person: Gender",
    "person:nationality": "Person: Nationality",
    "person:appointment_date": "Person: Appointment Date",
    "person:resignation_date": "Person: Resignation Date",
    "person:current_or_former": "Person: Current or Former",
}

FLAT_PEOPLE_PROFILE_PREFIX = "flatpeople::"


def suggest_flat_person_mapping(columns: list, existing_profile: dict = None) -> dict:
    """Auto-suggests a target for each column: a saved profile's assignment
    (reviewed, never silently trusted — same rule every other mapping UI in
    this module follows) wins, else the built-in alias guess, else "" —
    genuinely unrecognized, stays in raw_fields until the user maps it."""
    suggestions = {}
    for col in columns:
        if existing_profile and col in existing_profile:
            suggestions[col] = existing_profile[col]
        else:
            suggestions[col] = PERSON_FLAT_COLUMN_ALIASES.get(_norm(col), "")
    return suggestions


def save_flat_person_mapping_profile(db: Session, dataset_name: str, mapping: dict) -> ColumnMappingProfile:
    return save_mapping_profile(db, f"{FLAT_PEOPLE_PROFILE_PREFIX}{str(dataset_name).strip()}", "N/A", mapping)


def load_flat_person_mapping_profile(db: Session, dataset_name: str) -> dict:
    return load_mapping_profile(db, f"{FLAT_PEOPLE_PROFILE_PREFIX}{str(dataset_name).strip()}")


def list_flat_person_mapping_profile_names(db: Session) -> list:
    return [
        p.dataset_name[len(FLAT_PEOPLE_PROFILE_PREFIX):]
        for p in list_mapping_profiles(db)
        if p.dataset_name.startswith(FLAT_PEOPLE_PROFILE_PREFIX)
    ]


def apply_person_data_import(db: Session, df: pd.DataFrame, mapping: dict, dataset_name: str,
                              source_filename: str = None, overwrite_conflicts: bool = False,
                              dry_run: bool = False, progress_callback=None) -> dict:
    """
    Applies a reviewed {column: 'match:legal_name' | 'person:<field>' | None}
    mapping to every row of df — one row per PERSON, company legal name
    repeated on every row belonging to it.

    Matches to an EXISTING company by exact legal_name only (same rule and
    same reasoning as import_company_people: a third-party source's own ID
    column isn't reliably trustworthy — never creates a new company from this
    file alone; unmatched names are reported instead).

    Conflict rule mirrors import_company_people: a company that already has
    CompanyPerson rows under this exact dataset_name (role_group
    FLAT_IMPORT_ROLE_GROUP) is a conflict — real runs skip it unless
    overwrite_conflicts, dry runs just report it. Reported once per company
    even though several person-rows can share that company.

    position_in_row is assigned by encounter order within this company across
    THIS call — re-uploading the same file in the same row order updates the
    same person-slots in place rather than duplicating (same semantics as
    explode_person_group's own ordering for the stacked-cell importer).

    Every column value for a row is preserved verbatim in that person's
    raw_fields, whether or not it was mapped to a dedicated column.

    progress_callback, when given, is called as progress_callback(rows_done,
    total_rows) once per row so a caller can track a long import live.

    Returns {"matched", "people_created", "people_updated": int,
             "unmatched": [legal_name...], "conflicts": [{"legal_name",...}],
             "errors": [str...]}.
    """
    result = {"matched": 0, "people_created": 0, "people_updated": 0,
              "unmatched": [], "conflicts": [], "errors": []}

    if not dataset_name or not str(dataset_name).strip():
        result["errors"].append("Dataset name is required.")
        return result
    dataset_name = str(dataset_name).strip()

    match_cols = [col for col, target in mapping.items() if target == "match:legal_name"]
    if len(match_cols) != 1:
        result["errors"].append(
            f"Exactly one column must be mapped to Match: Company Legal Name (found {len(match_cols)})."
        )
        return result
    match_col = match_cols[0]

    field_cols = {col: target.split(":", 1)[1] for col, target in mapping.items()
                  if target and target.startswith("person:")}

    conflict_by_company = {}  # company_id -> bool
    reported_conflict_ids = set()
    position_counters = {}  # company_id -> next position_in_row
    touched_companies = {}  # company_id -> Company, for the post-loop signal sync

    total_rows = len(df)
    for idx, row in df.iterrows():
        if progress_callback:
            progress_callback(idx + 1, total_rows)
        row_dict = row.to_dict()
        row_num = idx + 2  # 1-indexed header + 1

        legal_name = _clean_str(row_dict.get(match_col))
        if not legal_name:
            result["errors"].append(f"Row {row_num}: could not read a company legal name.")
            continue

        company = db.query(Company).filter_by(legal_name=legal_name).first()
        if not company:
            result["unmatched"].append(legal_name)
            continue

        if company.id not in conflict_by_company:
            conflict_by_company[company.id] = db.query(CompanyPerson).filter_by(
                company_id=company.id, dataset_name=dataset_name, role_group=FLAT_IMPORT_ROLE_GROUP,
            ).first() is not None
        is_conflict = conflict_by_company[company.id]

        if is_conflict and not (overwrite_conflicts and not dry_run):
            if company.id not in reported_conflict_ids:
                reported_conflict_ids.add(company.id)
                result["conflicts"].append({"legal_name": legal_name, "registration_number": company.registration_number})
            continue

        result["matched"] += 1
        if dry_run:
            continue

        position = position_counters.get(company.id, 0)
        position_counters[company.id] = position + 1

        person = db.query(CompanyPerson).filter_by(
            company_id=company.id, dataset_name=dataset_name,
            role_group=FLAT_IMPORT_ROLE_GROUP, position_in_row=position,
        ).first()
        if not person:
            person = CompanyPerson(
                company_id=company.id, dataset_name=dataset_name,
                role_group=FLAT_IMPORT_ROLE_GROUP, position_in_row=position,
            )
            db.add(person)
            result["people_created"] += 1
        else:
            result["people_updated"] += 1

        for col, field in field_cols.items():
            value = row_dict.get(col)
            if value is None or (isinstance(value, float) and pd.isna(value)):
                continue
            if field == "age":
                try:
                    person.age = int(float(value))
                except (TypeError, ValueError):
                    pass
            elif field in ("appointment_date", "resignation_date"):
                parsed = _parse_date(value)
                if parsed is not None:
                    setattr(person, field, parsed)
            else:
                setattr(person, field, str(value)[:255])

        person.raw_fields = {str(k): _json_safe(v) for k, v in row_dict.items()}
        person.updated_at = datetime.utcnow()
        touched_companies[company.id] = company
        db.commit()

    if not dry_run:
        for company in touched_companies.values():
            sync_succession_signal(db, company, source=dataset_name)
            sync_management_composition_signals(db, company, source=dataset_name)

    return result


def parse_roster_file(file_or_buffer, filename: str = None) -> pd.DataFrame:
    """
    Reads an uploaded management/board-style dataset (.xls/.xlsx/.csv) with
    headers LEFT UNTOUCHED apart from stripping — deliberately not lowercased
    or space-to-underscore normalized like parse_uploaded_file, because
    detect_multivalue_groups needs the exact original 'Prefix\\n...' header
    text to find the group boundary.

    AIDA's own multi-sheet exports (verified against a real file) put a
    small "search strategy" cover sheet first and the actual data on a
    sheet named "Risultati" — pandas' default (first sheet) would silently
    read the wrong, tiny sheet. When there's more than one sheet, this
    prefers one named "risultati" (case-insensitive); otherwise falls back
    to whichever sheet has the most rows, since the real data table is
    essentially always the biggest sheet in these exports.
    """
    name = (filename or getattr(file_or_buffer, "name", "") or "").lower()
    if name.endswith(".xlsx") or name.endswith(".xls"):
        all_sheets = pd.read_excel(file_or_buffer, sheet_name=None)
        if len(all_sheets) == 1:
            df = next(iter(all_sheets.values()))
        else:
            risultati = next((n for n in all_sheets if n.strip().lower() == "risultati"), None)
            sheet_name = risultati or max(all_sheets, key=lambda n: len(all_sheets[n]))
            df = all_sheets[sheet_name]
    else:
        if hasattr(file_or_buffer, "read"):
            content = file_or_buffer.read()
            if isinstance(content, bytes):
                content = content.decode("utf-8", errors="replace")
        else:
            content = str(file_or_buffer)
        df = pd.read_csv(io.StringIO(content))
    df = df.rename(columns={c: str(c).strip() for c in df.columns})
    return df


def detect_multivalue_groups(columns: list) -> dict:
    """
    Groups columns sharing a 'Prefix\\n...' header convention — the text
    before a header's FIRST newline is the group name (e.g. 'DM\\nCarica' and
    'DM\\nEtà' both belong to group 'DM'). A column with no '\\n' in its
    header, or the only column under a given prefix, isn't a real roster
    group (2+ columns required). Generic on purpose — not hardcoded to
    'DM'/'ADV', so it also picks up whatever a future source calls its
    groups, as long as it uses the same one-cell-per-attribute convention.

    Returns {group_name: [original_column_name, ...]}.
    """
    groups = {}
    for col in columns:
        if "\n" in col:
            prefix, _, _rest = col.partition("\n")
            prefix = prefix.strip()
            if prefix:
                groups.setdefault(prefix, []).append(col)
    return {g: cols for g, cols in groups.items() if len(cols) >= 2}


def _split_header_prefix(col: str) -> tuple:
    """
    Splits a column header into (prefix, remainder) at whichever of '\\n',
    ' - ', or a plain ' ' occurs EARLIEST — e.g. 'DM\\nCarica' ->
    ('DM', 'Carica'), 'Azionisti Numero BvD' -> ('Azionisti', 'Numero BvD'),
    'Azionista - Ticker Symbol' -> ('Azionista', 'Ticker Symbol'). No
    separator at all -> (col, '').

    Shared by detect_loose_stacked_groups (to find a column's own natural
    group prefix) and strip_person_column_prefix (to compute that same
    column's person-level sub-label), so the two stay in lockstep.
    """
    s = str(col)
    candidates = [(s.find(sep), sep) for sep in ("\n", " - ", " ")]
    candidates = [(pos, sep) for pos, sep in candidates if pos != -1]
    if not candidates:
        return s.strip(), ""
    pos, sep = min(candidates, key=lambda t: t[0])
    return s[:pos].strip(), s[pos + len(sep):].strip()


def strip_person_column_prefix(col: str) -> str:
    """The person-level sub-label for one group column: the header with its
    own natural group-prefix stripped (see _split_header_prefix), falling
    back to the whole original header when there's nothing to strip (e.g.
    'Livello', adopted into a group by content rather than by a shared
    header prefix)."""
    _, remainder = _split_header_prefix(col)
    return remainder or str(col)


def _multiline_count(df: pd.DataFrame, col: str) -> int:
    return int(df[col].map(lambda v: isinstance(v, str) and "\n" in v).sum())


def _stack_split_len(v):
    """Split-length of every non-blank string cell, including an un-stacked
    single value (length 1) — unlike the multiline-only check above, this is
    needed for correlation: a group where most rows have exactly one entry
    (e.g. one advisor) would otherwise never have enough genuinely-stacked
    overlapping rows to prove two columns belong together."""
    if isinstance(v, str) and v.strip():
        return len(v.split("\n"))
    return None


def _stack_correlation(df: pd.DataFrame, col_a: str, col_b: str, min_overlap: int):
    """Fraction of rows (where both columns have a value) whose split-length
    matches exactly, or None if there isn't enough overlap to judge."""
    sa, sb = df[col_a].map(_stack_split_len), df[col_b].map(_stack_split_len)
    both = pd.concat([sa, sb], axis=1).dropna()
    if len(both) < min_overlap:
        return None
    return (both.iloc[:, 0] == both.iloc[:, 1]).mean()


def detect_loose_stacked_groups(df: pd.DataFrame, exclude_columns: set = None,
                                  min_multiline_cells: int = 3, match_threshold: float = 0.9,
                                  min_overlap: int = 5) -> dict:
    """
    Finds newline-stacked column groups the same way detect_multivalue_groups
    does, but for sources that DON'T mark every group column with a shared
    'Prefix\\n...' header — needed for real AIDA exports: the shareholder-
    control file mixes 'Azionisti\\nNome' (newline) with 'Azionisti Numero
    BvD' (space) and 'Azionista - Ticker Symbol' (dash, a singular/plural
    variant of the same word), and its ownership-chain 'Livello' column
    shares no header text with 'CSH' at all despite being part of the same
    per-row stack.

    Only ever looks at columns with NO '\\n' in their header (exclude_columns
    should also list anything detect_multivalue_groups already claimed) — it
    can never see, let alone reassign, a column detect_multivalue_groups
    already grouped, so it cannot regress the already-verified DM/ADV
    director/advisor import.

    Three passes, all driven by actual cell content rather than assumed
    naming:
      1. Seed candidate groups from columns sharing a header prefix (via
         _split_header_prefix) — but only keep the seed if at least one
         member has real stacking evidence (>= min_multiline_cells genuinely
         newline-joined cells), and only keep a given NON-anchor member if
         it's near-empty (too little data to judge either way — e.g. a
         comments column that's blank on every real row) or its own per-row
         split-length matches the seed's densest ("anchor") column at
         >= match_threshold. This is what excludes a same-prefix but
         unrelated SCALAR column (e.g. two 'Data di chiusura...' columns
         that happen to start with the same word as a real stacked
         'Data di inizio...' column) without needing it named any
         differently.
      2. Merge two seed groups when their anchors correlate at
         >= match_threshold — catches spelling/grammar variants that don't
         share a literal prefix token (Azionisti/Azionista, Partecipate/
         Partecipazioni).
      3. Adopt a leftover column with real stacking evidence but no 2+-
         column prefix partner (e.g. 'Livello') into whichever surviving
         group's anchor its own split-length correlates with best, if that
         correlation clears match_threshold.

    Verified against real data that this does NOT merge two genuinely
    independent groups just because rows happen to share a count: real
    Directors (DM) vs Advisors (ADV) split-lengths only coincidentally match
    on ~7% of overlapping rows in a real 954-row export, far under
    match_threshold.

    Returns {group_label: [original_column_name, ...]}, label = the seed's
    own header-prefix text (deduplicated with a numeric suffix on a clash).
    """
    exclude_columns = exclude_columns or set()
    cols = [c for c in df.columns if c not in exclude_columns and "\n" not in str(c)]

    by_prefix = {}
    for col in cols:
        prefix, _ = _split_header_prefix(col)
        key = prefix.lower()
        by_prefix.setdefault(key, {"display": prefix, "cols": []})
        by_prefix[key]["cols"].append(col)

    groups = []  # [{"label": str, "cols": [...], "anchor": col}]
    for entry in by_prefix.values():
        member_cols = entry["cols"]
        if len(member_cols) < 2:
            continue
        anchor = max(member_cols, key=lambda c: _multiline_count(df, c))
        if _multiline_count(df, anchor) < min_multiline_cells:
            continue
        kept = [anchor]
        for c in member_cols:
            if c == anchor:
                continue
            if len(df[c].dropna()) < min_overlap:
                kept.append(c)  # too little data to judge either way, harmless to include
                continue
            rate = _stack_correlation(df, anchor, c, min_overlap)
            if rate is not None and rate >= match_threshold:
                kept.append(c)
        if len(kept) >= 2:
            groups.append({"label": entry["display"], "cols": kept, "anchor": anchor})

    changed = True
    while changed:
        changed = False
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                rate = _stack_correlation(df, groups[i]["anchor"], groups[j]["anchor"], min_overlap)
                if rate is not None and rate >= match_threshold:
                    a, b = groups[i], groups[j]
                    winner, loser = (
                        (a, b) if _multiline_count(df, a["anchor"]) >= _multiline_count(df, b["anchor"])
                        else (b, a)
                    )
                    winner["cols"] = list(dict.fromkeys(winner["cols"] + loser["cols"]))
                    groups.remove(loser)
                    changed = True
                    break
            if changed:
                break

    claimed = set(c for g in groups for c in g["cols"])
    orphans = [c for c in cols if c not in claimed and _multiline_count(df, c) >= min_multiline_cells]
    for orphan in orphans:
        best_group, best_rate = None, 0.0
        for g in groups:
            rate = _stack_correlation(df, g["anchor"], orphan, min_overlap)
            if rate is not None and rate > best_rate:
                best_group, best_rate = g, rate
        if best_group and best_rate >= match_threshold:
            best_group["cols"].append(orphan)
            claimed.add(orphan)

    result = {}
    for g in groups:
        ordered = [c for c in cols if c in g["cols"]]
        label, i = g["label"], 2
        while label in result:
            label = f"{g['label']} {i}"
            i += 1
        result[label] = ordered
    return result


def detect_person_groups(df: pd.DataFrame) -> dict:
    """
    The full group-detection pass for the people/ownership importer: exact
    'Prefix\\n...' header groups (detect_multivalue_groups — the original,
    unchanged detector the DM/ADV import already relies on) plus whatever
    detect_loose_stacked_groups additionally finds among the columns it
    didn't claim. When both happen to produce the same group label, their
    column lists are UNIONED rather than one silently overwriting the other
    — needed for real data: AIDA's shareholder file's only two literally
    '\\n'-prefixed 'Azionisti' columns are 'Commenti' and 'Nome', and losing
    'Nome' to an overwrite would drop the shareholder's own name.
    """
    header_groups = detect_multivalue_groups(list(df.columns))
    claimed = set(c for cols in header_groups.values() for c in cols)
    loose_groups = detect_loose_stacked_groups(df, exclude_columns=claimed)

    groups = dict(header_groups)
    for label, cols in loose_groups.items():
        if label in groups:
            groups[label] = list(dict.fromkeys(groups[label] + cols))
        else:
            groups[label] = cols
    return groups


def explode_person_group(row: dict, group_columns: list) -> list:
    """
    Splits each of this group's cells (for one company's row) on '\\n' and
    zips them positionally into one raw dict per person — e.g. the 3rd line
    of every column in the group together form person #2's record. Uses
    zip_longest so one column having fewer stacked lines than another for
    this row degrades to a None for that one field on the extra
    person(s), rather than crashing or silently dropping the whole group.

    Returns a list of {sub_label: value} dicts, sub_label being the
    original column header with the group prefix stripped (e.g.
    'Nome completo', 'Carica', 'Età') — see strip_person_column_prefix,
    which handles the '\\n' / ' - ' / ' ' separator conventions uniformly.
    """
    split_by_col = {}
    for col in group_columns:
        val = row.get(col)
        sub_label = strip_person_column_prefix(col)
        if isinstance(val, str) and val.strip():
            split_by_col[sub_label] = [v.strip() for v in val.split("\n")]
        else:
            split_by_col[sub_label] = []

    max_len = max((len(v) for v in split_by_col.values()), default=0)
    if max_len == 0:
        return []

    people = []
    for i in range(max_len):
        person = {}
        for sub_label, values in split_by_col.items():
            person[sub_label] = values[i] if i < len(values) else None
        people.append(person)
    return people


def _person_structured_fields(raw_person: dict, field_map: dict = None) -> dict:
    """Best-effort maps a raw {sub_label: value} dict onto CompanyPerson's
    structured columns; everything stays in raw_fields regardless of whether
    it also got mapped here.

    field_map, when given, is the (possibly user-edited) {sub_label: target}
    mapping for THIS role_group only — see import_company_people, which
    builds it from suggest_person_mapping/a saved ColumnMappingProfile. A
    sub_label present in field_map is authoritative, including an explicit
    "" (Ignore), so a user's deliberate un-mapping is respected rather than
    falling back to the built-in alias guess. A sub_label absent from
    field_map (e.g. field_map is None, no mapping step was run) falls back
    to _PERSON_FIELD_ALIASES, preserving the original auto-detect-only
    behavior."""
    structured = {}
    for sub_label, value in raw_person.items():
        sub_label_stripped = str(sub_label).strip()
        if field_map is not None and sub_label_stripped in field_map:
            field = field_map[sub_label_stripped] or None
        else:
            # NOT _norm() — that replaces spaces with underscores (for the
            # other importer's column-base names), but these alias keys are
            # natural-language phrases ("nome completo") that need to stay
            # space-separated.
            field = _PERSON_FIELD_ALIASES.get(sub_label_stripped.lower())
        if not field or value in (None, ""):
            continue
        if field == "age":
            try:
                structured[field] = int(float(value))
            except (TypeError, ValueError):
                pass
        elif field in ("appointment_date", "resignation_date"):
            structured[field] = _parse_date(value)
        else:
            structured[field] = str(value)[:255]

    # Some groups (e.g. AIDA's ADV group) split the name into separate
    # 'Nome'/'Cognome' columns instead of one 'Nome completo' — and reuse
    # those same two columns for a company-type advisor (Nome blank,
    # Cognome holding the firm's name, e.g. "KPMG S.P.A."). Only synthesize
    # when no alias already produced a full_name, so DM's own 'Nome
    # completo' (already handled above) always wins where both exist.
    if not structured.get("full_name"):
        by_norm = {str(k).strip().lower(): v for k, v in raw_person.items()}
        nome = by_norm.get("nome")
        cognome = by_norm.get("cognome")
        joined = " ".join(p for p in (nome, cognome) if p)
        if joined:
            structured["full_name"] = joined[:255]
    return structured


def import_company_people(db: Session, df: pd.DataFrame, dataset_name: str,
                           legal_name_column: str = "Ragione sociale",
                           source_filename: str = None,
                           overwrite_conflicts: bool = False, dry_run: bool = False,
                           progress_callback=None, field_overrides: dict = None) -> dict:
    """
    Explodes every detected multi-value group in df into CompanyPerson rows,
    matching each source row to an existing Company by exact legal_name
    (see module docstring above for why not the source's own BvD ID column).
    Never creates a new Company from this file alone — a row that doesn't
    match an existing company is reported in "unmatched", not guessed at.

    field_overrides, when given, is a {"role_group::sub_label": target_field}
    dict — the same shape suggest_person_mapping returns/the People &
    Ownership import UI edits — used instead of the built-in
    _PERSON_FIELD_ALIASES guess for any sub-column it covers (including an
    explicit "" to deliberately leave a column unmapped). None (the default)
    preserves the original auto-detect-only behavior.

    Conflict rule mirrors apply_data_import: a company that already has
    CompanyPerson rows under this exact dataset_name is a conflict — real
    runs skip it unless overwrite_conflicts, dry runs just report it.

    progress_callback, when given, is called as progress_callback(rows_done,
    total_rows) once per row so a caller can track a long import live.

    Returns {"matched", "people_created", "people_updated": int,
             "unmatched": [legal_name...], "conflicts": [{"legal_name",...}],
             "errors": [str...]}.
    """
    result = {"matched": 0, "people_created": 0, "people_updated": 0,
              "unmatched": [], "conflicts": [], "errors": []}

    if not dataset_name or not str(dataset_name).strip():
        result["errors"].append("Dataset name is required.")
        return result
    dataset_name = str(dataset_name).strip()

    if legal_name_column not in df.columns:
        result["errors"].append(f"Column '{legal_name_column}' (company legal name) not found in the uploaded file.")
        return result

    groups = detect_person_groups(df)
    if not groups:
        result["errors"].append("No multi-value stacked-cell column groups detected in this file.")
        return result

    # Per-role_group {sub_label: target_field}, sliced out of the flat
    # "role_group::sub_label" override dict so _person_structured_fields
    # doesn't need to know about the group namespacing.
    group_field_maps = {}
    if field_overrides:
        for role_group, group_columns in groups.items():
            field_map = {}
            for col in group_columns:
                sub_label = strip_person_column_prefix(col)
                map_key = _person_field_map_key(role_group, sub_label)
                if map_key in field_overrides:
                    field_map[sub_label] = field_overrides[map_key] or None
            group_field_maps[role_group] = field_map

    total_rows = len(df)
    for row_idx, (_, row) in enumerate(df.iterrows()):
        if progress_callback:
            progress_callback(row_idx + 1, total_rows)
        row_dict = row.to_dict()
        legal_name = _clean_str(row_dict.get(legal_name_column))
        if not legal_name:
            continue

        company = db.query(Company).filter_by(legal_name=legal_name).first()
        if not company:
            result["unmatched"].append(legal_name)
            continue

        is_conflict = db.query(CompanyPerson).filter_by(company_id=company.id, dataset_name=dataset_name).first() is not None
        if is_conflict and not (overwrite_conflicts and not dry_run):
            result["conflicts"].append({"legal_name": legal_name, "registration_number": company.registration_number})
            continue

        result["matched"] += 1
        if dry_run:
            continue

        fetched_at = datetime.utcnow()
        for role_group, group_columns in groups.items():
            people = explode_person_group(row_dict, group_columns)
            field_map = group_field_maps.get(role_group)
            for position, raw_person in enumerate(people):
                person = db.query(CompanyPerson).filter_by(
                    company_id=company.id, dataset_name=dataset_name,
                    role_group=role_group, position_in_row=position,
                ).first()
                if not person:
                    person = CompanyPerson(
                        company_id=company.id, dataset_name=dataset_name,
                        role_group=role_group, position_in_row=position,
                    )
                    db.add(person)
                    result["people_created"] += 1
                else:
                    result["people_updated"] += 1
                for field, value in _person_structured_fields(raw_person, field_map).items():
                    setattr(person, field, value)
                person.raw_fields = {k: _json_safe(v) for k, v in raw_person.items()}
                person.updated_at = fetched_at

        db.commit()
        sync_succession_signal(db, company, source=dataset_name)
        sync_management_composition_signals(db, company, source=dataset_name)

    return result


# =============================================================================
# Family ownership & generational succession detection
#
# Derived entirely from data already imported above (CompanyPerson board/
# management records, Company.legal_name/incorporation_date) — no new import
# step. Two related but distinct signals:
#
#   - "Family company": 2+ CURRENT managers share a surname. Purely
#     informational/display — indicators.py's own family_ownership_share row
#     wants an equity %, which surname-matching among managers can't honestly
#     give (it's weight=0/context anyway, so nothing is lost by not writing
#     it), so this is surfaced on the Company Profile page rather than
#     written as a SignalRecord.
#   - "New generation management": indicators.py's new_generation_management
#     (weight 5.0, category Leadership & Succession, axis "both" — "the
#     strongest single trigger-event variable in this table"). Detected when
#     a young manager's surname (a) appears in the company's own legal name,
#     (b) belongs to NO current older manager — i.e. the founder generation
#     isn't still there — and (c) predates incorporation by more than the
#     youngest plausible founding age, so the young manager could not have
#     been the original founder themselves (they must have inherited/taken
#     over from an earlier relative). Its proxy is a real number ("years
#     since handover"), so this only writes the SignalRecord when the young
#     manager's own appointment_date is known — same tri-state honesty rule
#     as the rest of the pipeline: detected-but-unquantifiable is not the
#     same as measured, so it's surfaced on the profile page either way but
#     only scored when a real date backs the number.
# =============================================================================

YOUNG_MANAGER_MAX_AGE = 45
OLD_MANAGER_MIN_AGE = 60
MIN_PLAUSIBLE_FOUNDING_AGE = 20
SUCCESSION_INDICATOR_KEY = "new_generation_management"


def _person_surname(person: CompanyPerson) -> str:
    """Best-effort surname: the raw 'Cognome' sub-field when the source
    provided one directly (DM/ADV both do — see _PERSON_FIELD_ALIASES), else
    the last whitespace-separated token of full_name."""
    raw = person.raw_fields or {}
    cognome = raw.get("Cognome") or raw.get("cognome")
    if cognome and str(cognome).strip():
        return str(cognome).strip()
    if person.full_name:
        parts = str(person.full_name).split()
        if parts:
            return parts[-1]
    return ""


def _normalize_for_name_match(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", str(text or "").lower())


_HONORIFIC_TOKENS = {
    "sig", "sigra", "sigg", "signor", "signora", "signorina", "dott", "dr",
    "ing", "geom", "avv", "prof", "rag", "arch", "herr", "frau", "mr", "mrs", "ms", "mx",
}


def _person_identity_key(person: CompanyPerson) -> str:
    """Normalizes full_name for cross-source identity matching: strips
    accents/punctuation, casefolds, drops honorific tokens ("Sig.",
    "Signora", "Dott.", ...), and sorts what's left so word-order
    differences ("Rossi Mario" vs "Mario Rossi") don't look like two
    people. Honorifics matter here specifically because the same board
    roster commonly gets exploded into more than one role_group per
    dataset (e.g. a "DM" director row and an "ADV" advisor row for the
    identical person) and only one of them carries the "Sig./Signora"
    prefix the source file used — without stripping it, "Sig. Andrea
    Parolari" and "Andrea Parolari" look like two different people and a
    lone board member gets double-counted as a family pair.
    Falls back to the row's own id when there's no name left to key on,
    so nameless rows stay distinct rather than colliding on ""."""
    ascii_name = unicodedata.normalize("NFKD", str(person.full_name or "")).encode("ascii", "ignore").decode("ascii")
    tokens = sorted(t for t in re.findall(r"[a-z0-9]+", ascii_name.lower()) if t not in _HONORIFIC_TOKENS)
    return " ".join(tokens) or f"__no_name__{person.id}"


def _dedup_people_by_identity(people: list) -> list:
    """CompanyPerson's uniqueness constraint is per (dataset_name,
    role_group, position_in_row) - it does NOT guarantee one row per real
    person. The same individual routinely gets multiple rows: imported from
    two different sources (e.g. AIDA's director list AND a Handelsregister
    officer filing), or listed under two role_groups within one dataset
    (e.g. both director and shareholder). Anything that COUNTS people
    (family-surname detection, board composition indicators) must collapse
    those duplicates first or it silently double-counts the same human.

    Keeps one representative row per normalized identity, preferring
    whichever duplicate has the most complete data (age, appointment date,
    nationality, gender known) so downstream aggregates aren't starved by
    picking a sparser row arbitrarily.
    """
    best = {}
    for p in people:
        key = _person_identity_key(p)
        completeness = sum(x is not None for x in (p.age, p.appointment_date, p.nationality, p.gender))
        if key not in best or completeness > best[key][0]:
            best[key] = (completeness, p)
    return [p for _, p in best.values()]


# Not management: the statutory-audit committee ("sindaci", BvD position type AudC) and external advisors
# (the AIDA "ADV" group — auditors). AIDA lists them in the same roster as the board, and an audit of the real
# import showed they are a third of it (2,262 of 7,003 rows). Counted as management they aged the team by ~2
# years, DOUBLED the turnover count, and inflated "independent board members" with people who are not on the board.
_STATUTORY_AUDITOR_RE = re.compile(r"\b(sindac[oa]|collegio sindacale|revisor[ei]|statutory auditor|auditor)\b", re.IGNORECASE)
ADVISOR_ROLE_GROUP = "ADV"


def _is_not_management(person: CompanyPerson) -> bool:
    """True for statutory auditors and external advisors — people a roster lists but who don't run the company."""
    if (person.role_group or "").upper() == ADVISOR_ROLE_GROUP:
        return True
    for key, value in (person.raw_fields or {}).items():
        if str(key).strip().lower().endswith("tipologia di posizione") and "AudC" in str(value):
            return True
    return bool(_STATUTORY_AUDITOR_RE.search(person.role or ""))


def _management_roster(db: Session, company: Company) -> list:
    """The company's people with auditors/advisors removed, collapsed to one row per real person."""
    return _dedup_people_by_identity([
        p for p in db.query(CompanyPerson)
        .filter_by(company_id=company.id)
        .filter(or_(CompanyPerson.age.isnot(None), CompanyPerson.role_group == FLAT_IMPORT_ROLE_GROUP))
        .all()
        if not _is_not_management(p)
    ])


def detect_family_and_succession(db: Session, company: Company) -> dict:
    """
    Looks at CompanyPerson rows with a known age, OR rows from the flexible
    one-row-per-person importer (FLAT_IMPORT_ROLE_GROUP) regardless of age.
    The age requirement exists to distinguish a named individual (director/
    advisor) from a shareholder/subsidiary entity row exploded from the
    AIDA-shaped ownership imports, which never carry an age (see
    detect_loose_stacked_groups's module docstring: Azionisti/CSH/Partecipate
    have no age sub-field at all) — that ambiguity structurally doesn't exist
    for the flexible importer (every row there is always a real, named
    person, e.g. from a LinkedIn extraction pipeline that often can't
    determine age at all), so excluding its age-less rows would silently
    drop real people rather than filter out entities. The young/old-manager
    checks below still require age on the specific candidate either way —
    this only affects which rows are eligible to be *considered*.

    Returns {"is_family_company": bool, "family_surnames": [str, ...],
             "new_generation": {"detected": bool, "surname": str or None,
             "young_manager": CompanyPerson or None,
             "years_since_handover": float or None}}.
    """
    people = _management_roster(db, company)

    by_surname = {}
    for p in people:
        surname = _person_surname(p)
        if surname:
            by_surname.setdefault(surname.lower(), []).append(p)

    family_surnames = [s for s, ps in by_surname.items() if len({p.full_name for p in ps}) >= 2]

    new_generation = {"detected": False, "surname": None, "young_manager": None, "years_since_handover": None}
    legal_name_norm = _normalize_for_name_match(company.legal_name)
    current_year = datetime.utcnow().year

    for surname_key, ps in by_surname.items():
        # Word-boundary check so e.g. surname "Or" doesn't match inside an
        # unrelated word in the legal name.
        if not surname_key or not re.search(rf"\b{re.escape(surname_key)}\b", legal_name_norm):
            continue
        if any((p.age or 0) >= OLD_MANAGER_MIN_AGE for p in ps):
            continue  # founder generation is still present -> not a completed handover
        young = [p for p in ps if (p.age or 999) <= YOUNG_MANAGER_MAX_AGE]
        if not young or not company.incorporation_date:
            continue

        candidate = min(young, key=lambda p: p.age or 999)
        birth_year_estimate = current_year - candidate.age
        founding_age_estimate = company.incorporation_date.year - birth_year_estimate
        if founding_age_estimate >= MIN_PLAUSIBLE_FOUNDING_AGE:
            continue  # incorporation date isn't actually incompatible with this manager having founded it

        years_since_handover = None
        if candidate.appointment_date:
            years_since_handover = max(0.0, (datetime.utcnow() - candidate.appointment_date).days / 365.25)

        new_generation = {
            "detected": True, "surname": surname_key.title(),
            "young_manager": candidate, "years_since_handover": years_since_handover,
        }
        break  # one qualifying surname is enough to flag the company

    return {
        "is_family_company": bool(family_surnames),
        "family_surnames": sorted(s.title() for s in family_surnames),
        "new_generation": new_generation,
    }


def sync_succession_signal(db: Session, company: Company, source: str = "Board Roster Analysis") -> dict:
    """
    Runs detect_family_and_succession and, when a handover is detected AND
    the young manager's real appointment_date gives an actual years-since
    number, writes/updates the new_generation_management SignalRecord for
    real (is_simulated=False) — never fabricates a value when the date isn't
    known; the signal is simply left at whatever status it already had.

    Called automatically after a real (non-dry-run) import_company_people
    run and after apply_data_import updates a company's incorporation_date
    (either could newly make a handover detectable/quantifiable); also
    callable on demand from the Company Profile page.

    Returns detect_family_and_succession's dict plus "signal_written": bool.
    """
    result = detect_family_and_succession(db, company)
    result["signal_written"] = False
    ng = result["new_generation"]
    if ng["detected"] and ng["years_since_handover"] is not None:
        sig = db.query(SignalRecord).filter_by(company_id=company.id, signal_key=SUCCESSION_INDICATOR_KEY).first()
        if not sig:
            sig = SignalRecord(company_id=company.id, signal_key=SUCCESSION_INDICATOR_KEY, source=source)
            db.add(sig)
        sig.status = "present"
        sig.numeric_value = round(ng["years_since_handover"], 1)
        sig.confidence = 1.0
        sig.is_simulated = False
        sig.source = source
        sig.fetched_at = datetime.utcnow()
        sig.raw_payload_ref = json.dumps({
            "surname": ng["surname"], "young_manager": ng["young_manager"].full_name,
            "detected_from": "family_surname_vs_company_name_and_incorporation_date",
        })
        db.commit()
        result["signal_written"] = True
    return result


REVENUE_GROWTH_VS_SECTOR_KEY = "revenue_growth_vs_sector"


def compute_revenue_growth_vs_sector(db: Session, company: Company) -> dict:
    """
    Derives Revenue Growth vs. Sector from two already-stored SignalRecords —
    revenue_trend (the company's own 3-fiscal-year revenue % change, from a
    financial spreadsheet import) and sector_growth_benchmark (Eurostat
    sts_inpr_m trailing-12mo-vs-prior-12mo % change, industry NACE sections
    only — see adapters/eurostat_sector_growth.py). A pure computation over
    existing data, not a new external fetch, so it follows the same
    tri-state honesty rule as every TREND_INDICATOR_KEYS computation above:
    left not_yet_checked rather than estimated from just one side.

    Both values are already stored in percentage points (see revenue_trend's
    formula above and eurostat_sector_growth._fetch_live), so the
    differential is a plain subtraction, no unit conversion needed.

    Called automatically at the end of the Phase 1 block in
    sync_company_applicable_sources, since that's the point where the
    Eurostat side of the comparison was just (re)fetched; also safe to call
    on demand after a financial data import changes revenue_trend.
    """
    revenue_sig = db.query(SignalRecord).filter_by(company_id=company.id, signal_key="revenue_trend").first()
    sector_sig = db.query(SignalRecord).filter_by(company_id=company.id, signal_key="sector_growth_benchmark").first()

    out = db.query(SignalRecord).filter_by(company_id=company.id, signal_key=REVENUE_GROWTH_VS_SECTOR_KEY).first()
    if not out:
        out = SignalRecord(company_id=company.id, signal_key=REVENUE_GROWTH_VS_SECTOR_KEY, source="Computed")
        db.add(out)

    revenue_ok = revenue_sig is not None and revenue_sig.numeric_value is not None
    sector_ok = sector_sig is not None and sector_sig.numeric_value is not None

    if not (revenue_ok and sector_ok):
        out.status = "not_yet_checked"
        out.numeric_value = None
        out.confidence = 0.0
        out.is_simulated = True
        out.source = "Computed"
        out.fetched_at = datetime.utcnow()
        out.raw_payload_ref = json.dumps({
            "note": "Needs both revenue_trend (financial import) and sector_growth_benchmark (Eurostat) present.",
            "revenue_trend_present": revenue_ok,
            "sector_growth_benchmark_present": sector_ok,
        })
        db.commit()
        return {"status": "not_yet_checked"}

    differential = round(revenue_sig.numeric_value - sector_sig.numeric_value, 2)
    out.status = "present"
    out.numeric_value = differential
    out.confidence = min(revenue_sig.confidence or 0.5, sector_sig.confidence or 0.5)
    out.is_simulated = bool(revenue_sig.is_simulated or sector_sig.is_simulated)
    out.source = "Computed"
    out.fetched_at = datetime.utcnow()
    out.raw_payload_ref = json.dumps({
        "revenue_trend_pct": revenue_sig.numeric_value,
        "sector_growth_benchmark_pct": sector_sig.numeric_value,
        "differential_pp": differential,
        "note": "Company Revenue Trend minus Sector Growth Benchmark, in percentage points.",
    })
    db.commit()
    return {"status": "present", "value": differential}


# =============================================================================
# Management/board composition signals
#
# indicators.py's Leadership & Succession catalog carries several rows
# (Management Age, Management Gender/National Diversity, Turnover of
# Management, Average Tenure of Senior Management, Independent (Non-Family)
# Board Members) whose raw data — age, gender, nationality, appointment/
# resignation dates — is exactly what import_company_people already writes
# into CompanyPerson, one row per director/advisor. Nothing aggregated that
# roster into these indicators until now: the underlying per-person facts
# sat in the database but the company-level SignalRecords stayed at
# whatever status they started (usually not_yet_checked). This is that
# aggregation step, run automatically after every real people-roster import
# (same hook point as sync_succession_signal) and re-runnable on demand from
# the Company Profile page.
#
# Deliberately does NOT touch mgmt_cultural_diversity or the mgmt_education_*
# indicators — no roster column captures education history or cultural
# background (see indicators.py's own note on this at the
# management_diversity/"Leadership Page Transparency" row), so there is
# nothing honest to compute for them from this data source.
# =============================================================================

FEMALE_GENDER_MARKERS = {"f", "female", "femmina", "donna", "w", "woman"}
MALE_GENDER_MARKERS = {"m", "male", "maschio", "uomo"}
FORMER_STATUS_MARKERS = ("former", "precedente", "past", "ex-", "resigned", "dimission")
TURNOVER_LOOKBACK_DAYS = 3 * 365

MANAGEMENT_COMPOSITION_INDICATOR_KEYS = (
    "management_age", "mgmt_gender_diversity", "mgmt_national_diversity",
    "management_turnover", "senior_mgmt_tenure", "independent_board_members",
)


def _person_gender_category(raw_gender) -> str:
    """Best-effort 'F'/'M'/None from whatever free-text gender value the
    source file used (AIDA's own "Genere" is typically a bare M/F, but
    English-language rosters spell it out) — anything unrecognized is left
    out of the gender-diversity denominator rather than guessed."""
    if not raw_gender:
        return None
    val = str(raw_gender).strip().lower()
    if val in FEMALE_GENDER_MARKERS:
        return "F"
    if val in MALE_GENDER_MARKERS:
        return "M"
    return None


def _person_is_current(person: CompanyPerson) -> bool:
    """A person counts as current management unless the roster explicitly
    says otherwise — a resignation_date that has already passed, or a
    current_or_former value containing a 'former'-style marker (AIDA's own
    'Attuale o precedente' column included).

    resignation_date isn't always an actual departure: Italian board
    filings routinely populate it with the mandate's scheduled end-of-term
    ("in carica fino al ...", e.g. a standard 3-year renewal), which is
    frequently a FUTURE date for a director who is very much still serving.
    Treating any non-null resignation_date as "gone" — regardless of
    whether it's already happened — silently drops every currently-serving
    person with a known term length, which is the common case, not the
    exception."""
    if person.resignation_date is not None and person.resignation_date <= datetime.utcnow():
        return False
    val = (person.current_or_former or "").strip().lower()
    return not any(marker in val for marker in FORMER_STATUS_MARKERS)


def sync_management_composition_signals(db: Session, company: Company, source: str = "Board Roster Analysis") -> dict:
    """
    Aggregates the CompanyPerson roster already imported for this company
    into the management/board-composition indicators listed in
    MANAGEMENT_COMPOSITION_INDICATOR_KEYS. Same tri-state honesty rule as
    sync_succession_signal: an indicator is only written (is_simulated=False)
    when the underlying field was actually present on at least one relevant
    person; otherwise it's left at whatever status it already had rather
    than being faked as zero/absent.

    Uses the same relaxed age rule as detect_family_and_succession: a known
    age, OR a row from the flexible one-row-per-person importer
    (FLAT_IMPORT_ROLE_GROUP), which never carries the AIDA-shaped ownership-
    entity ambiguity the age check exists for in the first place — a
    LinkedIn-sourced person with no disclosed age still has a real role,
    nationality, and tenure worth counting. management_age itself further
    narrows to whichever of those people actually have a known age (see
    below), same honest-denominator pattern gender/nationality already use.

    Returns {indicator_key: {"written": bool, "value": float or None}} for
    every key in MANAGEMENT_COMPOSITION_INDICATOR_KEYS.
    """
    results = {key: {"written": False, "value": None} for key in MANAGEMENT_COMPOSITION_INDICATOR_KEYS}

    people = _management_roster(db, company)
    if not people:
        return results
    current = [p for p in people if _person_is_current(p)]

    def _write(key: str, value: float, payload: dict):
        sig = db.query(SignalRecord).filter_by(company_id=company.id, signal_key=key).first()
        if not sig:
            sig = SignalRecord(company_id=company.id, signal_key=key, source=source)
            db.add(sig)
        sig.status = "present"
        sig.numeric_value = round(value, 2)
        sig.confidence = 1.0
        sig.is_simulated = False
        sig.source = source
        sig.fetched_at = datetime.utcnow()
        sig.raw_payload_ref = json.dumps(payload)
        results[key] = {"written": True, "value": sig.numeric_value}

    if current:
        # Management Age — average age of current management/board members
        # WITH a known age (not every current person necessarily has one,
        # now that flat-imported people without a disclosed age are included
        # above — same honest-denominator pattern as gender/nationality below).
        aged_current = [p for p in current if p.age is not None]
        if aged_current:
            _write("management_age", sum(p.age for p in aged_current) / len(aged_current),
                   {"basis": "average_age_of_current_people_with_known_age", "n": len(aged_current)})

        # Management Gender Diversity — % women among current people with a
        # recognized gender marker.
        known_genders = [g for g in (_person_gender_category(p.gender) for p in current) if g]
        if known_genders:
            pct_female = 100.0 * sum(1 for g in known_genders if g == "F") / len(known_genders)
            _write("mgmt_gender_diversity", pct_female,
                   {"basis": "pct_female_of_current_people_with_known_gender", "n": len(known_genders)})

        # Management National Diversity — a concentration-based proxy: % of
        # current people (with a known nationality) whose nationality is NOT
        # the single most common one in the group. Not a demographic census,
        # just "how homogeneous is this team" from whatever the roster gives.
        nationalities = [p.nationality.strip() for p in current if p.nationality and p.nationality.strip()]
        if nationalities:
            counts = Counter(n.lower() for n in nationalities)
            _, dominant_count = counts.most_common(1)[0]
            pct_diverse = 100.0 * (len(nationalities) - dominant_count) / len(nationalities)
            _write("mgmt_national_diversity", pct_diverse,
                   {"basis": "pct_not_in_most_common_nationality", "n": len(nationalities)})

        # Independent (Non-Family) Board Members — reuses the same surname-
        # match family detection the succession signal is built on; when no
        # family surname is detected at all, every current person counts.
        family_result = detect_family_and_succession(db, company)
        family_surnames_lower = {s.lower() for s in family_result["family_surnames"]}
        independent_count = sum(1 for p in current if _person_surname(p).lower() not in family_surnames_lower)
        _write("independent_board_members", float(independent_count),
               {"basis": "current_people_not_sharing_a_family_surname", "n": len(current)})

        # Average Tenure of Senior Management — years since appointment for
        # current people with a known appointment_date.
        tenured = [p for p in current if p.appointment_date]
        if tenured:
            avg_tenure = sum((datetime.utcnow() - p.appointment_date).days / 365.25 for p in tenured) / len(tenured)
            _write("senior_mgmt_tenure", avg_tenure,
                   {"basis": "average_years_since_appointment_date", "n": len(tenured)})

    # Turnover of Management — appointment/resignation events in the last 3
    # years, counted across EVERY known person (not just current ones — a
    # departure is itself a turnover event). Both bounds are capped at "now"
    # as well as "cutoff": resignation_date in particular is often a
    # scheduled future mandate end-of-term (see _person_is_current), not a
    # departure that has actually happened yet, so an event dated in the
    # future must not count as turnover "in the past 3 years".
    now = datetime.utcnow()
    cutoff = now - timedelta(days=TURNOVER_LOOKBACK_DAYS)
    dated_people = [p for p in people if p.appointment_date or p.resignation_date]
    if dated_people:
        turnover_events = sum(
            1 for p in people
            if (p.appointment_date and cutoff <= p.appointment_date <= now)
            or (p.resignation_date and cutoff <= p.resignation_date <= now)
        )
        _write("management_turnover", float(turnover_events),
               {"basis": "appointment_or_resignation_events_in_last_3_years", "n": len(dated_people)})

    db.commit()
    return results
