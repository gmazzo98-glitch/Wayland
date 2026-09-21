"""
Wraps Scraper/crawlers/company-website-crawler (Phase 7). Crawls a company's
own homepage plus a small set of relevant pages (about/products/store
locator/sustainability) and extracts store count, product-line count,
founding year, and first-sustainability-report year.

Feeds: product_portfolio_diversity, esg_reporting_recency, store_geo_distribution,
physical_stores_trend (only once a second run gives an actual two-point trend —
see _stores_trend below; a single crawl is a snapshot, not a trend).

Deliberately does NOT feed product_age. The extracted `founding_or_product_launch_year` is
"founding year, else first-product-launch year" (merge.ts) and in practice it is the founding
year (live values: 93, 61, 52 years). product_age means "years since the CORE PRODUCT LINE
launched", so an old company is not an old product — writing it there scored every
long-established firm as maximum product-obsolescence need. The year is still kept, in the
crawler blob, as evidence.
"""

from datetime import datetime
from sqlalchemy.orm import Session

from adapters.base import run_adapter
from config import CRAWLER_LLM_API_KEY, CRAWLER_LLM_BASE_URL, CRAWLER_LLM_MODEL
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
    homepage = row.get("homepage_url")
    pages = row.get("crawled_pages_count")
    base_evidence = {"source_urls": [homepage] if homepage else [], "pages_crawled": pages,
                      "method": "LLM extraction over the company's own about/products/store-locator/sustainability pages"}

    product_lines = row.get("product_lines_count")
    if field_status.get("product_lines_count") == "value" and product_lines is not None:
        product_types = row.get("product_types") or []
        # The count and the list came apart in a live run (count 3 next to a 25-entry
        # list); the merged crawler output now keeps them consistent, and this stays
        # defensive for blobs written by the older build.
        product_lines = max(int(product_lines), len(product_types))
        signals["product_portfolio_diversity"] = {
            "value": float(product_lines), "status": "present",
            "summary": f"{int(product_lines)} product line(s): " + ", ".join(product_types[:5])
                        + ("" if len(product_types) <= 5 else f" (+{len(product_types)-5} more)"),
            "evidence": {**base_evidence, "product_lines_count": product_lines, "product_types": product_types},
        }

    report = row.get("has_sustainability_report") or {}
    if field_status.get("has_sustainability_report") == "value":
        year = report.get("first_publication_year")
        if report.get("present") and year:
            signals["esg_reporting_recency"] = {
                "value": float(datetime.utcnow().year - int(year)), "status": "present",
                "summary": f"first sustainability/ESG report published {int(year)}",
                "evidence": {**base_evidence, "first_publication_year": year},
            }
        elif not report.get("present"):
            # field_status 'value' with present=false now means (merge.ts) either a
            # sustainability-type page was read and carries no report, or a small site
            # was crawled to the end without one — a real absence, not a data gap. A
            # one-page "Coming Soon" placeholder no longer qualifies.
            signals["esg_reporting_recency"] = {
                "value": None, "status": "absent",
                "summary": f"crawled {pages} pages of the company site, no sustainability/ESG report found",
                "evidence": base_evidence,
            }

    # 'not_applicable' here means pure B2B / no retail footprint — the indicator's own
    # comment says to treat that as null, never as "zero stores = need".
    store_regions = row.get("store_regions") or []
    if field_status.get("store_regions") == "value" and store_regions:
        signals["store_geo_distribution"] = {
            "value": float(len(store_regions)), "status": "present",
            "summary": f"{len(store_regions)} region(s) served: " + ", ".join(map(str, store_regions[:6])),
            "evidence": {**base_evidence, "store_regions": store_regions, "store_count": row.get("store_count")},
        }

    if field_status.get("store_count") == "value":
        trend = _stores_trend(db, company, row.get("store_count"))
        if trend is not None:
            signals["physical_stores_trend"] = {
                "value": float(trend), "status": "present",
                "summary": f"store count {trend:+.0f}% vs the previous crawl of this site",
                "evidence": {**base_evidence, "store_count_now": row.get("store_count"),
                              "pct_change": round(trend, 1),
                              "method": "current store-locator count vs the count stored by the previous crawl "
                                         f"(at least {MIN_DAYS_BETWEEN_TREND_POINTS} days apart)"},
            }

    return signals


def sync_company_website(company, db_session: Session) -> dict:
    captured = {}

    def _fetch_live(c):
        # subprocess env values must be strings — never hand it a None. Defaults to
        # Groq (free, no card) rather than Anthropic — see config.py's docstring on
        # CRAWLER_LLM_BASE_URL for how to point this at Gemini/Cerebras/a local
        # Ollama instead, no code change needed on either side.
        env = {"LLM_API_KEY": CRAWLER_LLM_API_KEY, "LLM_BASE_URL": CRAWLER_LLM_BASE_URL,
               "LLM_MODEL": CRAWLER_LLM_MODEL} if CRAWLER_LLM_API_KEY else {}
        # 130s, not the 90s default: enabling SDK retries on 429s (see llm.ts) makes a
        # 13-15 page crawl legitimately take up to ~60-90s under free-tier rate limiting
        # — verified live to time out at 90s for a real company ("No response within 90s"),
        # losing the entire crawl (no blob, no signal) rather than the graceful per-page
        # degradation this crawler is designed for.
        rows = run_ts_crawler(CRAWLER_DIR, [{"company_id": c.id, "homepage_url": c.website_url}],
                               env_overrides=env, run_timeout=130)
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
                                     "or CRAWLER_LLM_API_KEY not configured"},
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
        credentials_ok=bool(company.website_url and CRAWLER_LLM_API_KEY),
        fetch_live=_fetch_live, simulate=_simulate, timeout=150,
    )

    if captured.get("row"):
        save_crawler_blob(db_session, company, DATASET_NAME, captured["row"])

    return result
