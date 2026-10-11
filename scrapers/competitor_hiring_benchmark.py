"""INACTIVE PROTOTYPE: T05 is blocked on Job Postings Crawler reliability (T03b).

Raw hiring comparison against manually named competitors.

Uses the same careers discovery, Node crawler and field-status-aware count
derivation as Job Postings Crawler. The result is intentionally unscored: GG
has not chosen a signal key or weight for peer-relative hiring.
"""

from sqlalchemy.orm import Session

from adapters.base import run_adapter
from models import RawImportRecord
from scrapers import job_postings_crawler
from scrapers.node_crawler_base import (
    CrawlerRunError, rows_for_company, run_ts_crawler, save_crawler_blob,
)

SOURCE_NAME = "Competitor Hiring Benchmark"
DATASET_NAME = "crawler_competitor_hiring"
PHASE = 7
MAX_COMPETITORS = 3


def role_counts(row: dict, country: str | None = None) -> dict:
    """Extract only counts the existing job wrapper can honestly substantiate."""
    signals = job_postings_crawler._derive_signals(row, country=country)
    open_roles = signals.get("job_posting_velocity")
    digital_roles = signals.get("digital_job_postings")
    return {
        "open_role_count": int(open_roles["value"]) if open_roles else None,
        "digital_technical_role_count": int(digital_roles["value"]) if digital_roles else None,
        "open_role_status": open_roles["status"] if open_roles else "unavailable",
        "digital_technical_role_status": digital_roles["status"] if digital_roles else "unavailable",
        "field_status": (row or {}).get("field_status") or {},
        "sources_used": (row or {}).get("sources_used") or [],
        "careers_discovery": (row or {}).get("careers_discovery"),
        "roles_sample": (row or {}).get("roles_sample") or [],
    }


def _crawl_competitor(competitor) -> dict:
    found = job_postings_crawler._discover_careers_page(competitor.homepage_url)
    careers_url = found["url"] if found else None
    rows = run_ts_crawler(
        job_postings_crawler.CRAWLER_DIR,
        [{"company_id": competitor.id, "company_name": competitor.name,
          "careers_url": careers_url or ""}],
        run_timeout=100,
    )
    matches = rows_for_company(rows, competitor.id)
    if not matches:
        raise CrawlerRunError(f"job-postings-crawler returned no row for '{competitor.name}'")
    row = matches[0]
    row["careers_discovery"] = (
        {"careers_url": careers_url, "found_via": found["found_via"]} if found
        else {"careers_url": None, "note": "no careers page found"}
    )
    row["site_nav_urls"] = found.get("site_nav_urls", []) if found else []
    if row.get("error"):
        raise CrawlerRunError(str(row["error"]))
    return row


def sync_competitor_hiring_benchmark(company, db_session: Session) -> dict:
    competitors = [c for c in (company.competitors or []) if c.homepage_url][:MAX_COMPETITORS]
    if not competitors:
        return {"status": "skipped", "reason": "No named competitor with a homepage recorded."}

    own_record = db_session.query(RawImportRecord).filter_by(
        company_id=company.id, dataset_name=job_postings_crawler.DATASET_NAME
    ).first()
    if own_record is None or not isinstance(own_record.raw_row, dict):
        return {"status": "skipped", "reason": "Own Job Postings Crawler raw data has not been measured yet."}
    own = role_counts(own_record.raw_row, country=company.country)
    if own["open_role_count"] is None and own["digital_technical_role_count"] is None:
        return {"status": "skipped", "reason": "Own job counts are unavailable; re-run Job Postings Crawler first."}

    captured = {}

    def _fetch_live(_company):
        peers = []
        for competitor in competitors:
            entry = {"name": competitor.name, "homepage_url": competitor.homepage_url}
            try:
                row = _crawl_competitor(competitor)
                entry.update(role_counts(row, country=company.country))
                entry["error"] = None
            except Exception as exc:  # one peer failure must not hide the others
                entry.update({"open_role_count": None, "digital_technical_role_count": None,
                              "open_role_status": "unavailable",
                              "digital_technical_role_status": "unavailable",
                              "error": str(exc)})
            peers.append(entry)
        captured["blob"] = {
            "note": "Raw peer hiring counts only; no indicator or weight has been approved.",
            "company": {"name": company.legal_name, "homepage_url": company.website_url, **own},
            "competitors": peers,
        }
        if not any(
            peer["open_role_count"] is not None or peer["digital_technical_role_count"] is not None
            for peer in peers
        ):
            raise CrawlerRunError("No named competitor yielded a measured job count")
        return {"signals": {}, "raw_payload": {"competitors_recorded": len(peers),
                                                "competitors_measured": sum(
                                                    p["open_role_count"] is not None
                                                    or p["digital_technical_role_count"] is not None
                                                    for p in peers)},
                "confidence": 0.7}

    def _simulate(_company):
        return {"signals": {}, "raw_payload": {"note": "competitor hiring unavailable"},
                "confidence": 0.5}

    result = run_adapter(
        db_session, company, SOURCE_NAME, PHASE,
        credentials_ok=True, fetch_live=_fetch_live, simulate=_simulate,
        timeout=len(competitors) * 145 + 30,
    )
    if "blob" in captured:
        save_crawler_blob(
            db_session, company, DATASET_NAME, captured["blob"],
            source_filename="Competitor Hiring Benchmark — raw, unscored job counts",
        )
    return result
