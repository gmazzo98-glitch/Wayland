"""
Wraps Scraper/crawlers/news-signals-crawler (Phase 7). Runs named press-search
templates per company (+ once per sector) and LLM-extracts structured signal
from each article.

Feeds: external_collaboration, university_partnership, press_launch_mentions,
sector_pilot_precedent (context tag, weight 0 — kept for display only).

Deliberately does NOT feed board_innovation_statements or partnership_news_count
— adapters/google_news.py already writes both of those from a different
mechanism (Google CSE keyword count); this project's convention is one
producer per signal_key (see vienna-api-integration-status memory), so this
wrapper only covers the News/Press rows nothing else already writes.

Without NEWSAPI_KEY the crawler's own default (SEARCH_PROVIDER=mock) returns a
FIXED FAKE Wikipedia search result — never acceptable to present as a live
pull — so credentials_ok below is gated on NEWSAPI_KEY specifically, and
fetch_live is never invoked without it (run_adapter goes straight to simulate()).
"""

from datetime import datetime
from sqlalchemy.orm import Session

from adapters.base import run_adapter
from config import NEWSAPI_KEY, CRAWLER_ANTHROPIC_API_KEY
from scrapers.node_crawler_base import (
    CrawlerRunError, run_ts_crawler, rows_for_company, save_crawler_blob,
)

SOURCE_NAME = "News Signals Crawler"
CRAWLER_DIR = "news-signals-crawler"
DATASET_NAME = "crawler_news_signals"
PHASE = 7


def _derive_signals(row: dict) -> dict:
    signals = {}
    field_status = row.get("field_status") or {}

    collabs = row.get("external_collaboration") or []
    if collabs:
        signals["external_collaboration"] = {"value": 1.0, "status": "present"}
    elif field_status.get("external_collaboration") == "not_found":
        signals["external_collaboration"] = {"value": 0.0, "status": "absent"}

    uni = row.get("university_partnership") or []
    if uni:
        signals["university_partnership"] = {"value": 1.0, "status": "present"}
    elif field_status.get("university_partnership") == "not_found":
        signals["university_partnership"] = {"value": 0.0, "status": "absent"}

    launches = row.get("product_launch_mentions") or []
    if launches:
        signals["press_launch_mentions"] = {"value": float(len(launches)), "status": "present"}
    elif field_status.get("product_launch_mentions") == "not_found":
        signals["press_launch_mentions"] = {"value": 0.0, "status": "absent"}

    precedent = row.get("sector_pilot_precedent") or {}
    if precedent.get("count") is not None:
        signals["sector_pilot_precedent"] = {"value": float(precedent["count"]), "status": "present"}

    return signals


def sync_news_signals(company, db_session: Session) -> dict:
    captured = {}

    def _fetch_live(c):
        env = {"SEARCH_PROVIDER": "newsapi", "NEWSAPI_KEY": NEWSAPI_KEY}
        if CRAWLER_ANTHROPIC_API_KEY:
            env["ANTHROPIC_API_KEY"] = CRAWLER_ANTHROPIC_API_KEY
        rows = run_ts_crawler(
            CRAWLER_DIR,
            [{"company_id": c.id, "company_name": c.legal_name, "sector_name": c.sector_name or ""}],
            env_overrides=env,
        )
        matches = rows_for_company(rows, c.id)
        if not matches:
            raise CrawlerRunError("news-signals-crawler returned no row for this company")
        row = matches[0]
        captured["row"] = row
        return {
            "signals": _derive_signals(row),
            "raw_payload": {"articles_considered": row.get("articles_considered"),
                             "search_errors": row.get("search_errors")},
            "confidence": 0.6,
        }

    def _simulate(c):
        char_sum = sum(ord(ch) for ch in c.legal_name)
        return {
            "signals": {"external_collaboration": {"value": float(char_sum % 2), "status": "present"}},
            "raw_payload": {"note": "NEWSAPI_KEY not configured — news-signals-crawler not invoked"},
            "confidence": 0.5,
        }

    result = run_adapter(
        db_session, company, SOURCE_NAME, PHASE,
        credentials_ok=bool(NEWSAPI_KEY),
        fetch_live=_fetch_live, simulate=_simulate, timeout=100,
    )

    if captured.get("row"):
        save_crawler_blob(db_session, company, DATASET_NAME, captured["row"])

    return result
