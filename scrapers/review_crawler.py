"""
Wraps Scraper/crawlers/review-crawler (Phase 7). Two independent modes:
Mode A (product/customer reviews — Google Maps, Trustpilot): rating + 12-month
trend -> product_quality_trend. Mode B (employer reviews — Kununu, Glassdoor):
average rating -> kununu_rating.

Mode A always runs (public review aggregators, no ToS caveat beyond the
robots.txt check the crawler itself already does). Mode B is gated behind
config.KUNUNU_CRAWLER_ENABLED, off by default — a fresh, explicit decision
distinct from scrapers/kununu_light.py's paid-reseller gate: this scrapes
Kununu/Glassdoor directly (respecting robots.txt) rather than going through a
compliant reseller contract, and that's a different risk profile the source
plan's own "buy, don't scrape" guidance was written about.
"""

from sqlalchemy.orm import Session

from adapters.base import run_adapter
from config import KUNUNU_CRAWLER_ENABLED
from scrapers.node_crawler_base import (
    CrawlerRunError, run_ts_crawler, rows_for_company, save_crawler_blob,
)

SOURCE_NAME = "Review Crawler"
CRAWLER_DIR = "review-crawler"
PHASE = 7

MODE_A_SOURCES = ["google", "trustpilot"]
MODE_B_SOURCES = ["kununu", "glassdoor"]


def _derive_signals(rows_by_source: dict) -> dict:
    signals = {}

    for src in MODE_A_SOURCES:
        row = rows_by_source.get(src)
        if not row or row.get("status") != "ok":
            continue
        trend = row.get("rating_trend")
        if trend and trend.get("last_12_months_avg") is not None and trend.get("prior_12_months_avg") is not None:
            delta = trend["last_12_months_avg"] - trend["prior_12_months_avg"]
            signals["product_quality_trend"] = {"value": float(delta), "status": "present"}
            break

    for src in MODE_B_SOURCES:
        row = rows_by_source.get(src)
        if not row or row.get("status") != "ok":
            continue
        rating = row.get("avg_employer_rating")
        if rating is not None:
            signals["kununu_rating"] = {"value": float(rating), "status": "present"}
            break

    return signals


def sync_reviews(company, db_session: Session) -> dict:
    captured = {}
    source_types = list(MODE_A_SOURCES) + (list(MODE_B_SOURCES) if KUNUNU_CRAWLER_ENABLED else [])

    def _fetch_live(c):
        rows = run_ts_crawler(CRAWLER_DIR, [
            {"company_id": c.id, "company_name": c.legal_name, "source_type": st}
            for st in source_types
        ])
        matches = rows_for_company(rows, c.id)
        if not matches:
            raise CrawlerRunError("review-crawler returned no rows for this company")
        rows_by_source = {r.get("source_type"): r for r in matches}
        captured["rows_by_source"] = rows_by_source
        if all(r.get("status") in ("error",) for r in matches):
            raise CrawlerRunError("review-crawler errored on every configured source")
        return {
            "signals": _derive_signals(rows_by_source),
            "raw_payload": {st: {"status": r.get("status"), "review_count": r.get("review_count")}
                            for st, r in rows_by_source.items()},
            "confidence": 0.65,
        }

    def _simulate(c):
        return {
            "signals": {},
            "raw_payload": {"note": "review-crawler unavailable"},
            "confidence": 0.5,
        }

    result = run_adapter(
        db_session, company, SOURCE_NAME, PHASE,
        credentials_ok=True,
        fetch_live=_fetch_live, simulate=_simulate, timeout=100,
    )

    for src, row in captured.get("rows_by_source", {}).items():
        save_crawler_blob(db_session, company, f"crawler_reviews_{src}", row)

    return result
