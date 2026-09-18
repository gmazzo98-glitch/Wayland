"""
Wraps Scraper/crawlers/innovation-participation-crawler (Phase 7). Checks
whether a company has previously participated in innovation competitions,
accelerator programs, hackathons, or corporate-venturing initiatives.

Feeds: prior_open_innovation_usage — this crawler is the direct, purpose-built
producer for that indicator (proxy: "any documented sponsorship or
participation in idea competitions, hackathons, or accelerator cohorts"),
so no keyword re-derivation is needed here beyond reading its own boolean.

Same NEWSAPI_KEY hard gate as news_signals_crawler.py and for the same
reason: this crawler's un-keyed default is SEARCH_PROVIDER=mock, a fixed fake
fixture, not an honest "nothing found".
"""

from sqlalchemy.orm import Session

from adapters.base import run_adapter
from config import NEWSAPI_KEY, CRAWLER_ANTHROPIC_API_KEY
from scrapers.node_crawler_base import (
    CrawlerRunError, run_ts_crawler, rows_for_company, save_crawler_blob,
)

SOURCE_NAME = "Innovation Participation Crawler"
CRAWLER_DIR = "innovation-participation-crawler"
DATASET_NAME = "crawler_innovation_participation"
PHASE = 7


def _derive_signals(row: dict) -> dict:
    """
    Absence is only asserted when the searches actually ran — the crawler warns
    that a 'not_found' after a failed search "may just mean couldn't search",
    and this indicator carries weight 4.0 on the readiness axis, so a fabricated
    zero here is expensive.
    """
    field_status = row.get("field_status") or {}
    pages = row.get("pages_considered")
    if row.get("has_prior_innovation_participation"):
        events = row.get("events_found") or []
        cited = [{"label": f"{e.get('event_name')} ({e.get('event_type')}, {e.get('year') or 'year unknown'}) "
                            f"— role: {e.get('role')}, confidence: {e.get('confidence')}",
                   "url": e.get("source_url")} for e in events]
        return {"prior_open_innovation_usage": {
            "value": 1.0, "status": "present",
            # The flag can come back true with an empty event list; still say something
            # meaningful rather than leaving the provenance blank.
            "summary": ("; ".join(c["label"] for c in cited[:2])
                        + ("" if len(cited) <= 2 else f" (+{len(cited)-2} more)")) if cited
                       else "participation reported by the crawler, but it listed no specific event — verify manually",
            "evidence": {
                "method": "press/web search plus a maintained list of accelerator and corporate-venturing programs, "
                           "classified by an LLM into event type / role / confidence",
                "found": cited, "pages_considered": pages,
                "best_confidence": row.get("confidence"),
            },
        }}
    searches_all_ran = not (row.get("search_errors") or [])
    if searches_all_ran and field_status.get("events_found") == "not_found":
        return {"prior_open_innovation_usage": {
            "value": 0.0, "status": "absent",
            "summary": f"searched {pages} pages, no accelerator/hackathon/competition participation found",
            "evidence": {"method": "press/web search plus known-program list", "found": [], "pages_considered": pages},
        }}
    return {}


def sync_innovation_participation(company, db_session: Session) -> dict:
    captured = {}

    def _fetch_live(c):
        env = {"SEARCH_PROVIDER": "newsapi", "NEWSAPI_KEY": NEWSAPI_KEY}
        if CRAWLER_ANTHROPIC_API_KEY:
            env["ANTHROPIC_API_KEY"] = CRAWLER_ANTHROPIC_API_KEY
        rows = run_ts_crawler(CRAWLER_DIR, [{"company_id": c.id, "company_name": c.legal_name}], env_overrides=env)
        matches = rows_for_company(rows, c.id)
        if not matches:
            raise CrawlerRunError("innovation-participation-crawler returned no row for this company")
        row = matches[0]
        captured["row"] = row
        return {
            "signals": _derive_signals(row),
            "raw_payload": {"events_found": row.get("events_found"), "confidence": row.get("confidence")},
            "confidence": 0.65,
        }

    def _simulate(c):
        return {
            "signals": {},
            "raw_payload": {"note": "NEWSAPI_KEY not configured — innovation-participation-crawler not invoked"},
            "confidence": 0.5,
        }

    result = run_adapter(
        db_session, company, SOURCE_NAME, PHASE,
        credentials_ok=bool(NEWSAPI_KEY),
        fetch_live=_fetch_live, simulate=_simulate, timeout=110,
    )

    if captured.get("row"):
        save_crawler_blob(db_session, company, DATASET_NAME, captured["row"])

    return result
