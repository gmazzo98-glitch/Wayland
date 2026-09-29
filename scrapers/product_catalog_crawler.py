"""
Wraps Scraper/crawlers/product-catalog-crawler (Phase 7). Crawls a company's own
catalog/shop section and extracts every product it can find: name, category,
free-form specs, and price (when shown) — first from the page's own schema.org
JSON-LD (no LLM, no tokens), then via an LLM fallback for pages with no usable
structured data. After the crawl, if any products were found, one extra LLM
call turns the extracted rows into a short natural-language catalog_narrative.

Deliberately writes NO SignalRecord. product_portfolio_diversity (the one
existing indicator this data could plausibly feed) already has a real producer
in scrapers/company_website_crawler.py — this project's one-producer-per-
signal_key rule (see vienna-api-integration-status memory) means a second
writer here would race it. There is also no existing indicator in
indicators.py's INDICATOR_SEED for "has a priced catalog" / "catalog depth" /
"pricing transparency" — inventing one unilaterally is exactly what the
project's own convention says not to do (see indicators.py's reconciliation
notes: never add a signal_key without checking the seed first). So, like
scrapers/linkedin_profile_crawler.py, this wrapper only refreshes raw data —
here the full extracted catalog, saved via save_crawler_blob() so it shows up
in the Company Intelligence "Raw Data & Mapping" tab and the per-signal audit
trail. If a future indicator is added for catalog breadth/pricing, a
_derive_signals() step can be layered on top of the same raw row without
touching the crawler itself.
"""

from datetime import datetime

from sqlalchemy.orm import Session

from adapters.base import run_adapter
from config import (
    CRAWLER_LLM_API_KEY, CRAWLER_LLM_BASE_URL, CRAWLER_LLM_MODEL,
    CRAWLER_LLM_FALLBACK_API_KEY, CRAWLER_LLM_FALLBACK_BASE_URL, CRAWLER_LLM_FALLBACK_MODEL,
    CRAWLER_LLM_EXTRA_FALLBACKS,
)
from scrapers.node_crawler_base import (
    CrawlerRunError, run_ts_crawler, rows_for_company, save_crawler_blob,
)

SOURCE_NAME = "Product Catalog Crawler"
CRAWLER_DIR = "product-catalog-crawler"
DATASET_NAME = "crawler_product_catalog"
PHASE = 7


def sync_product_catalog(company, db_session: Session) -> dict:
    captured = {}

    def _fetch_live(c):
        # Same LLM env-passing as company_website_crawler.py — but unlike that crawler, every
        # field here is NOT LLM-gated: many catalog/e-commerce sites carry schema.org Product
        # JSON-LD, which this crawler reads for free with no key at all. A key only widens
        # coverage to sites without structured data.
        env = {"LLM_API_KEY": CRAWLER_LLM_API_KEY, "LLM_BASE_URL": CRAWLER_LLM_BASE_URL,
               "LLM_MODEL": CRAWLER_LLM_MODEL} if CRAWLER_LLM_API_KEY else {}
        if CRAWLER_LLM_API_KEY and CRAWLER_LLM_FALLBACK_API_KEY:
            env["LLM_FALLBACK_API_KEY"] = CRAWLER_LLM_FALLBACK_API_KEY
            if CRAWLER_LLM_FALLBACK_BASE_URL:
                env["LLM_FALLBACK_BASE_URL"] = CRAWLER_LLM_FALLBACK_BASE_URL
            if CRAWLER_LLM_FALLBACK_MODEL:
                env["LLM_FALLBACK_MODEL"] = CRAWLER_LLM_FALLBACK_MODEL
            for extra in CRAWLER_LLM_EXTRA_FALLBACKS:
                env[f"LLM_FALLBACK{extra['suffix']}_API_KEY"] = extra["api_key"]
                if extra["base_url"]:
                    env[f"LLM_FALLBACK{extra['suffix']}_BASE_URL"] = extra["base_url"]
                if extra["model"]:
                    env[f"LLM_FALLBACK{extra['suffix']}_MODEL"] = extra["model"]
        # This crawler visits up to 45 pages (vs. company-website-crawler's fixed 15) because
        # finding the whole catalog is the point, not a side effect — real headroom needed.
        # The crawler's own soft deadline (node_crawler_base.SOFT_DEADLINE_MARGIN=30) stops it
        # 30s early and writes whatever was read, flagged is_partial, rather than losing the row.
        rows = run_ts_crawler(CRAWLER_DIR, [{"company_id": c.id, "homepage_url": c.website_url}],
                               env_overrides=env, run_timeout=280)
        matches = rows_for_company(rows, c.id)
        if not matches:
            raise CrawlerRunError("product-catalog-crawler returned no row for this company")
        row = matches[0]
        captured["row"] = row
        if row.get("error"):
            raise CrawlerRunError(row["error"])

        # run_adapter only ever reads raw_payload while iterating "signals" (empty here, see the
        # module docstring on why) — this project's real record of what the crawl found is the
        # blob save_crawler_blob() writes below, straight off the crawler's own row. This is kept
        # only to satisfy fetch_live's contract shape and for SourceHealth-adjacent debugging.
        return {
            "signals": {},
            "raw_payload": {
                "products_count": row.get("products_count"),
                "is_partial": row.get("is_partial"),
                "crawled_pages_count": row.get("crawled_pages_count"),
            },
            # Structured-data hits are ground truth; an all-LLM catalog (or a thin one) is less certain.
            "confidence": 0.8 if any(p.get("extraction_method") == "structured_data" for p in row.get("products") or []) else 0.6,
        }

    def _simulate(c):
        return {
            "signals": {},
            "raw_payload": {"note": "product-catalog-crawler unavailable or no website_url on record"},
            "confidence": 0.5,
        }

    result = run_adapter(
        db_session, company, SOURCE_NAME, PHASE,
        credentials_ok=bool(company.website_url),
        fetch_live=_fetch_live, simulate=_simulate, timeout=300,
    )

    if captured.get("row"):
        save_crawler_blob(db_session, company, DATASET_NAME, captured["row"])

    return result
