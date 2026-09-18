"""
Wraps Scraper/crawlers/review-crawler (Phase 7). Two independent modes:
Mode A (customer reviews — Google Maps): rating + 12-month trend ->
product_quality_trend. Mode B (employer reviews — Kununu, Glassdoor): average
rating -> kununu_rating.

BOTH modes are off by default (config.REVIEW_CRAWLER_MODE_A_ENABLED,
config.KUNUNU_CRAWLER_ENABLED). Mode B scrapes Kununu/Glassdoor directly rather
than through a compliant reseller — the risk profile the sourcing plan's own
"buy, don't scrape" guidance is about. Mode A was switched off after the
2026-09-18 verification run against 16 real manufacturers: robots.txt allows the
Maps URLs, but Google's Maps Additional Terms prohibit copying content and using
Maps to augment a business-listings database (which storing ratings and review
snippets per prospect is), the crawler auto-dismisses the GDPR consent screen,
and the signal cannot be computed for this segment anyway (4-67 reviews per
firm; the trend needs 10+ dated reviews in each of two consecutive years).
Trustpilot is gone: its robots.txt is `User-agent: *` / `Disallow: /`, so every
call was a wasted subprocess that correctly returned skipped_robots.

When a mode IS enabled, a search-based match is only used if the crawler's own
matcher marked it confident, and the matched listing (name + URL) travels in
the evidence — the same run found "Tecnoinox S.r.l. - Magazzino" (a warehouse,
2 reviews) matched for a company whose head-office listing has 13.
"""

from sqlalchemy.orm import Session

from adapters.base import run_adapter
from config import KUNUNU_CRAWLER_ENABLED, REVIEW_CRAWLER_MODE_A_ENABLED
from scrapers.node_crawler_base import (
    CrawlerRunError, run_ts_crawler, rows_for_company, save_crawler_blob,
)

SOURCE_NAME = "Review Crawler"
CRAWLER_DIR = "review-crawler"
PHASE = 7

MODE_A_SOURCES = ["google"]
MODE_B_SOURCES = ["kununu", "glassdoor"]


def _usable(row: dict) -> bool:
    """A crawled row whose profile we can stand behind: status ok, and if the
    profile came from the platform's search, the crawler's own matcher must have
    called the match confident — its unconfident fallback is "the platform's top
    result, whatever it was"."""
    if not row or row.get("status") != "ok":
        return False
    if row.get("matched_via_search"):
        return bool((row.get("search_match") or {}).get("confident"))
    return True


def _match_evidence(row: dict) -> dict:
    match = row.get("search_match") or {}
    return {"matched_via_search": bool(row.get("matched_via_search")),
            "matched_listing": match.get("name"), "matched_listing_url": match.get("url"),
            "note": "verify this is the right listing (not a warehouse/branch/homonym) before trusting the number"}


def _derive_signals(rows_by_source: dict) -> dict:
    signals = {}

    for src in MODE_A_SOURCES:
        row = rows_by_source.get(src)
        if not _usable(row):
            continue
        trend = row.get("rating_trend")
        if trend and trend.get("last_12_months_avg") is not None and trend.get("prior_12_months_avg") is not None:
            last, prior = trend["last_12_months_avg"], trend["prior_12_months_avg"]
            delta = last - prior
            signals["product_quality_trend"] = {
                "value": float(delta), "status": "present",
                "summary": f"{src}: {prior:.2f} → {last:.2f} ({delta:+.2f}) over the last 12 months "
                            f"across {row.get('review_count')} reviews",
                "evidence": {
                    "method": "mean customer rating, last 12 months vs the 12 months before",
                    "platform": src, "last_12_months_avg": last, "prior_12_months_avg": prior,
                    "delta": round(delta, 3), "review_count": row.get("review_count"),
                    "recent_review_snippets": row.get("recent_review_snippets") or [],
                    "source_urls": [row.get("profile_url")] if row.get("profile_url") else [],
                    **_match_evidence(row),
                },
            }
            break

    for src in MODE_B_SOURCES:
        row = rows_by_source.get(src)
        if not _usable(row):
            continue
        rating = row.get("avg_employer_rating")
        if rating is not None:
            samples = row.get("reviews_sample") or []
            signals["kununu_rating"] = {
                "value": float(rating), "status": "present",
                "summary": f"{src}: {rating:.2f}/5 across {row.get('review_count')} employer reviews",
                "evidence": {
                    "method": "mean employer rating from the platform's own profile page",
                    "platform": src, "avg_employer_rating": rating, "review_count": row.get("review_count"),
                    "reviews_sample": [{"label": (r.get("title") or "")[:120],
                                         "body": (r.get("body") or "")[:300]} for r in samples[:5]],
                    "source_urls": [row.get("profile_url")] if row.get("profile_url") else [],
                    **_match_evidence(row),
                },
            }
            break

    return signals


def sync_reviews(company, db_session: Session) -> dict:
    captured = {}
    source_types = ((list(MODE_A_SOURCES) if REVIEW_CRAWLER_MODE_A_ENABLED else [])
                    + (list(MODE_B_SOURCES) if KUNUNU_CRAWLER_ENABLED else []))

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
            "raw_payload": {"note": ("review-crawler not invoked: REVIEW_CRAWLER_MODE_A_ENABLED and "
                                     "KUNUNU_CRAWLER_ENABLED are both false (see config.py)")
                            if not source_types else "review-crawler unavailable"},
            "confidence": 0.5,
        }

    result = run_adapter(
        db_session, company, SOURCE_NAME, PHASE,
        credentials_ok=bool(source_types),
        fetch_live=_fetch_live, simulate=_simulate, timeout=110,
    )

    for src, row in captured.get("rows_by_source", {}).items():
        save_crawler_blob(db_session, company, f"crawler_reviews_{src}", row)

    return result
