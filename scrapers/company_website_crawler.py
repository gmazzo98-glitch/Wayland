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
    signals = {}
    field_status = row.get("field_status") or {}

    product_lines = row.get("product_lines_count")
    if product_lines is not None:
        signals["product_portfolio_diversity"] = {"value": float(product_lines), "status": "present"}
    elif field_status.get("product_lines_count") == "not_found":
        signals["product_portfolio_diversity"] = {"value": None, "status": "absent"}

    report = row.get("has_sustainability_report") or {}
    if report.get("present"):
        year = report.get("first_publication_year")
        if year:
            signals["esg_reporting_recency"] = {"value": float(datetime.utcnow().year - int(year)), "status": "present"}
    elif report.get("present") is False:
        signals["esg_reporting_recency"] = {"value": 99.0, "status": "absent"}

    founding_year = row.get("founding_or_product_launch_year")
    if founding_year:
        signals["product_age"] = {"value": float(datetime.utcnow().year - int(founding_year)), "status": "present"}

    store_regions = row.get("store_regions") or []
    store_count = row.get("store_count")
    if store_regions:
        signals["store_geo_distribution"] = {"value": float(len(store_regions)), "status": "present"}
    elif field_status.get("store_regions") == "not_applicable":
        pass  # pure B2B / no retail footprint — leave not_yet_checked rather than scoring "zero = need"
    elif field_status.get("store_regions") == "not_found":
        signals["store_geo_distribution"] = {"value": None, "status": "absent"}

    trend = _stores_trend(db, company, store_count)
    if trend is not None:
        signals["physical_stores_trend"] = {"value": float(trend), "status": "present"}

    return signals


def sync_company_website(company, db_session: Session) -> dict:
    captured = {}

    def _fetch_live(c):
        rows = run_ts_crawler(CRAWLER_DIR, [{"company_id": c.id, "homepage_url": c.website_url}])
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
        char_sum = sum(ord(ch) for ch in c.legal_name)
        return {
            "signals": {"product_portfolio_diversity": {"value": float((char_sum % 8) + 1), "status": "present"}},
            "raw_payload": {"note": "company-website-crawler unavailable or no website_url on record"},
            "confidence": 0.5,
        }

    result = run_adapter(
        db_session, company, SOURCE_NAME, PHASE,
        credentials_ok=bool(company.website_url),
        fetch_live=_fetch_live, simulate=_simulate, timeout=90,
    )

    if captured.get("row"):
        save_crawler_blob(db_session, company, DATASET_NAME, captured["row"])

    return result
