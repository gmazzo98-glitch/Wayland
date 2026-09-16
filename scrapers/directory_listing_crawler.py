"""
Wraps Scraper/crawlers/directory-listing-crawler (Phase 7). Checks whether a
company appears in trade fair exhibitor lists / industry association member
directories.

Feeds: trade_fair_participation. Only MECSPE (Italian manufacturing trade
fair, mecspe.com/portale/it/espositori — the exact URL live-verified in
Scraper/CRAWLER_AUDIT.md) has a registered plugin today; every other
directory_url would just come back status="no_config". Run for every company
regardless of country — a German company simply won't match MECSPE, which is
a cheap, harmless "no match" rather than a wasted call, and nothing here
carries the LinkedIn/Kununu-style ToS caveat (public exhibitor search,
respects robots.txt).
"""

from sqlalchemy.orm import Session

from adapters.base import run_adapter
from scrapers.node_crawler_base import (
    CrawlerRunError, run_ts_crawler, rows_for_company, save_crawler_blob,
)

SOURCE_NAME = "Directory Listing Crawler"
CRAWLER_DIR = "directory-listing-crawler"
PHASE = 7

# Only directory with a registered plugin as of the 2026-09-16 crawler audit —
# see Scraper/crawlers/directory-listing-crawler/src/directories/index.ts.
# Add more here as new directory plugins are registered there.
DEFAULT_DIRECTORY_URLS = ["https://www.mecspe.com/portale/it/espositori"]


def _derive_signals(rows: list) -> dict:
    signals = {}
    hits = sum(1 for r in rows if r.get("appears_in_directory"))
    possible = sum(1 for r in rows if r.get("possible_match") and not r.get("appears_in_directory"))
    checked = any(r.get("status") == "ok" for r in rows)
    if hits:
        signals["trade_fair_participation"] = {"value": float(hits), "status": "present"}
    elif checked:
        signals["trade_fair_participation"] = {"value": 0.0 if not possible else 0.5, "status": "absent"}
    return signals


def sync_directory_listing(company, db_session: Session) -> dict:
    captured = {}

    def _fetch_live(c):
        rows = run_ts_crawler(CRAWLER_DIR, [{
            "company_id": c.id, "company_name": c.legal_name,
            "directory_urls": "|".join(DEFAULT_DIRECTORY_URLS),
        }])
        matches = rows_for_company(rows, c.id)
        if not matches:
            raise CrawlerRunError("directory-listing-crawler returned no rows for this company")
        captured["rows"] = matches
        return {
            "signals": _derive_signals(matches),
            "raw_payload": {r.get("directory_url"): r.get("status") for r in matches},
            "confidence": 0.75,
        }

    def _simulate(c):
        char_sum = sum(ord(ch) for ch in c.legal_name)
        return {
            "signals": {"trade_fair_participation": {"value": float(char_sum % 3), "status": "present"}},
            "raw_payload": {"note": "directory-listing-crawler unavailable"},
            "confidence": 0.5,
        }

    result = run_adapter(
        db_session, company, SOURCE_NAME, PHASE,
        credentials_ok=True,
        fetch_live=_fetch_live, simulate=_simulate, timeout=90,
    )

    for row in captured.get("rows", []):
        directory_id = row.get("directory_id") or "unknown"
        save_crawler_blob(db_session, company, f"crawler_directory_{directory_id}", row)

    return result
