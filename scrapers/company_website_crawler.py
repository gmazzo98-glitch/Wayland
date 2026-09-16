"""
Wraps Scraper/crawlers/company-website-crawler (Phase 7). Crawls a company's
own homepage plus a small set of relevant pages (about/products/store
locator/sustainability) and extracts store count, product-line count,
founding year, and first-sustainability-report year.

Feeds: product_portfolio_diversity, esg_reporting_recency, product_age
(fallback when news-signals-crawler has no launch mentions), store_geo_distribution,
physical_stores_trend (only once a second run gives an actual two-point trend —
see _stores_trend below; a single crawl is a snapshot, not a trend).
"""

from datetime import datetime
from sqlalchemy.orm import Session

from adapters.base import run_adapter
from config import CRAWLER_ANTHROPIC_API_KEY
from models import RawImportRecord
from scrapers.node_crawler_base import (
    CrawlerRunError, run_ts_crawler, rows_for_company, save_crawler_blob,
)

SOURCE_NAME = "Company Website Crawler"
CRAWLER_DIR = "company-website-crawler"
DATASET_NAME = "crawler_company_website"
PHASE = 7
MIN_DAYS_BETWEEN_TREND_POINTS = 30


def _stores_trend(db: Session, company, new_count):
    """% change vs. the last time this crawler ran for this company, only if
    that run is at least MIN_DAYS_BETWEEN_TREND_POINTS old and had a usable
    count — otherwise there's nothing to diff against yet."""
    if new_count is None:
        return None
    prior = db.query(RawImportRecord).filter_by(company_id=company.id, dataset_name=DATASET_NAME).first()
    if not prior or not prior.raw_row:
        return None
    old_count = prior.raw_row.get("store_count")
    if old_count is None or old_count == 0:
        return None
    age_days = (datetime.utcnow() - (prior.updated_at or prior.imported_at)).days
    if age_days < MIN_DAYS_BETWEEN_TREND_POINTS:
        return None
    return ((new_count - old_count) / old_count) * 100.0


def _derive_signals(db: Session, company, row: dict) -> dict:
    """
    Only writes a signal where field_status == 'value'. This crawler's own audit
    notes are explicit that a failed/missing LLM extraction marks a field
    'not_found' rather than claiming stores or reports are absent — so
    'not_found' must NOT become SignalRecord status 'absent', which in this
    codebase means "actively checked, confirmed no record exists". Anything
    unextracted is simply left not_yet_checked for a later pass.
    """
    signals = {}
    field_status = row.get("field_status") or {}

    product_lines = row.get("product_lines_count")
    if field_status.get("product_lines_count") == "value" and product_lines is not None:
        signals["product_portfolio_diversity"] = {"value": float(product_lines), "status": "present"}

    report = row.get("has_sustainability_report") or {}
    if field_status.get("has_sustainability_report") == "value":
        year = report.get("first_publication_year")
        if report.get("present") and year:
            signals["esg_reporting_recency"] = {"value": float(datetime.utcnow().year - int(year)), "status": "present"}
        elif not report.get("present"):
            # Sustainability pages were crawled and carry no report — a real absence.
            signals["esg_reporting_recency"] = {"value": None, "status": "absent"}

    founding_year = row.get("founding_or_product_launch_year")
    if field_status.get("founding_or_product_launch_year") == "value" and founding_year:
        signals["product_age"] = {"value": float(datetime.utcnow().year - int(founding_year)), "status": "present"}

    # 'not_applicable' here means pure B2B / no retail footprint — the indicator's own
    # comment says to treat that as null, never as "zero stores = need".
    store_regions = row.get("store_regions") or []
    if field_status.get("store_regions") == "value" and store_regions:
        signals["store_geo_distribution"] = {"value": float(len(store_regions)), "status": "present"}

    if field_status.get("store_count") == "value":
        trend = _stores_trend(db, company, row.get("store_count"))
        if trend is not None:
            signals["physical_stores_trend"] = {"value": float(trend), "status": "present"}

    return signals


def sync_company_website(company, db_session: Session) -> dict:
    captured = {}

    def _fetch_live(c):
        # subprocess env values must be strings — never hand it a None.
        env = {"ANTHROPIC_API_KEY": CRAWLER_ANTHROPIC_API_KEY} if CRAWLER_ANTHROPIC_API_KEY else {}
        rows = run_ts_crawler(CRAWLER_DIR, [{"company_id": c.id, "homepage_url": c.website_url}],
                               env_overrides=env)
        matches = rows_for_company(rows, c.id)
        if not matches:
            raise CrawlerRunError("company-website-crawler returned no row for this company")
        row = matches[0]
        captured["row"] = row
        if row.get("error"):
            raise CrawlerRunError(row["error"])
        return {
            "signals": _derive_signals(db_session, c, row),
            "raw_payload": {"crawled_pages_count": row.get("crawled_pages_count"), "failed_pages": row.get("failed_pages")},
            "confidence": 0.7,
        }

    def _simulate(c):
        return {
            "signals": {},
            "raw_payload": {"note": "company-website-crawler unavailable, no website_url on record, "
                                     "or CRAWLER_ANTHROPIC_API_KEY not configured"},
            "confidence": 0.5,
        }

    # Every signal-bearing field this crawler produces is LLM-extracted — without a
    # key it still crawls, but returns field_status='not_found' for all of them and
    # can never yield a signal. Verified live against a real company: all 7 fields
    # came back not_found with no key set. Gating here keeps Pipeline Health honest
    # (it reports simulated + "needs a key") rather than showing a green "live" for
    # a source that is structurally incapable of producing anything.
    result = run_adapter(
        db_session, company, SOURCE_NAME, PHASE,
        credentials_ok=bool(company.website_url and CRAWLER_ANTHROPIC_API_KEY),
        fetch_live=_fetch_live, simulate=_simulate, timeout=90,
    )

    if captured.get("row"):
        save_crawler_blob(db_session, company, DATASET_NAME, captured["row"])

    return result
