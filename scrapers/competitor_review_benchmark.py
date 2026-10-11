"""Raw review comparison for hand-named competitors with hand-entered profile URLs.

This source never discovers a competitor or a review profile by search. It runs the
existing Review Crawler only for an explicitly recorded platform profile and only
when that platform's existing mode is enabled. The output is a raw blob, never a
SignalRecord: GG has not approved a peer-relative review signal key or weight.
"""

from urllib.parse import urlparse

from sqlalchemy.orm import Session

from adapters.base import run_adapter
from config import KUNUNU_CRAWLER_ENABLED, REVIEW_CRAWLER_MODE_A_ENABLED
from models import RawImportRecord
from scrapers import review_crawler
from scrapers.node_crawler_base import CrawlerRunError, rows_for_company, run_ts_crawler, save_crawler_blob

SOURCE_NAME = "Competitor Review Benchmark"
DATASET_NAME = "crawler_competitor_reviews"
PHASE = 7
MAX_COMPETITORS = 3


def profile_source(url: str | None) -> str | None:
    """Only recognize a direct profile on a platform the existing crawler supports."""
    if not url:
        return None
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        return None
    if (host == "kununu.com" or host.endswith(".kununu.com")) and parsed.path.strip("/"):
        return "kununu"
    if (host == "glassdoor.com" or host.endswith(".glassdoor.com")) and "/Reviews/" in parsed.path:
        return "glassdoor"
    if (host == "google.com" or host.endswith(".google.com")) and parsed.path.startswith("/maps/place/"):
        return "google"
    # Trustpilot's robots.txt disallows crawling. Do not offer it as a route.
    return None


def mode_enabled(source: str) -> bool:
    return REVIEW_CRAWLER_MODE_A_ENABLED if source == "google" else KUNUNU_CRAWLER_ENABLED


def has_enabled_review_profile(company) -> bool:
    return any(
        (source := profile_source(c.review_profile_url)) and mode_enabled(source)
        for c in (company.competitors or [])
    )


def review_measurement(row: dict | None) -> dict:
    """Preserve unknown versus measured zero, and honor each field_status."""
    row = row or {}
    source = row.get("source_type")
    fields = row.get("field_status") or {}
    usable = review_crawler._usable(row)
    rating_key = "avg_rating" if source == "google" else "avg_employer_rating"

    def measured(key):
        if usable and fields.get(key) == "value" and row.get(key) is not None:
            return row[key]
        return None

    return {
        "source_type": source,
        "profile_url": row.get("profile_url"),
        "status": row.get("status") or "unavailable",
        "status_detail": row.get("status_detail"),
        "field_status": fields,
        "rating": measured(rating_key),
        "review_count": measured("review_count"),
        "rating_trend": measured("rating_trend") if source == "google" else None,
        "notes": row.get("notes") or {},
    }


def _crawl_competitor(competitor, source: str) -> dict:
    rows = run_ts_crawler(review_crawler.CRAWLER_DIR, [{
        "company_id": competitor.id,
        "company_name": competitor.name,
        "source_type": source,
        "profile_url": competitor.review_profile_url,
    }], run_timeout=100)
    matches = rows_for_company(rows, competitor.id)
    if not matches:
        raise CrawlerRunError(f"review-crawler returned no row for '{competitor.name}'")
    return matches[0]


def sync_competitor_review_benchmark(company, db_session: Session) -> dict:
    competitors = [c for c in (company.competitors or []) if c.review_profile_url][:MAX_COMPETITORS]
    if not competitors:
        return {"status": "skipped", "reason": "No named competitor has a review profile URL recorded."}

    eligible = [(c, profile_source(c.review_profile_url)) for c in competitors]
    eligible = [(c, src) for c, src in eligible if src and mode_enabled(src)]
    if not eligible:
        return {"status": "skipped", "reason": "No supported review profile has its existing crawler mode enabled."}

    own = {}
    for source in {src for _, src in eligible}:
        record = db_session.query(RawImportRecord).filter_by(
            company_id=company.id, dataset_name=f"crawler_reviews_{source}",
        ).first()
        if record and isinstance(record.raw_row, dict):
            own[source] = review_measurement(record.raw_row)

    captured = {}

    def _fetch_live(_company):
        peers = []
        for competitor, source in eligible:
            entry = {"competitor_name": competitor.name, "review_profile_url": competitor.review_profile_url,
                     "source_type": source, "error": None}
            try:
                entry.update(review_measurement(_crawl_competitor(competitor, source)))
            except Exception as exc:  # one peer failure must not hide the rest
                entry.update({"status": "error", "rating": None, "review_count": None,
                              "rating_trend": None, "field_status": {}, "error": str(exc)})
            peers.append(entry)
        captured["blob"] = {
            "note": "Raw review comparison only; no peer-relative signal key or weight has been approved.",
            "company": {"name": company.legal_name, "reviews_by_platform": own},
            "competitors": peers,
        }
        if not any(peer["rating"] is not None or peer["review_count"] is not None for peer in peers):
            raise CrawlerRunError("No named competitor yielded a measured review rating or count")
        return {"signals": {}, "raw_payload": {
            "competitors_recorded": len(peers),
            "competitors_measured": sum(p["rating"] is not None or p["review_count"] is not None for p in peers),
        }, "confidence": 0.6}

    def _simulate(_company):
        return {"signals": {}, "raw_payload": {"note": "competitor review benchmark unavailable"},
                "confidence": 0.5}

    result = run_adapter(
        db_session, company, SOURCE_NAME, PHASE, credentials_ok=True,
        fetch_live=_fetch_live, simulate=_simulate, timeout=len(eligible) * 130 + 30,
    )
    if "blob" in captured:
        save_crawler_blob(db_session, company, DATASET_NAME, captured["blob"],
                          source_filename="Competitor Review Benchmark — raw, unscored review counts")
    return result
