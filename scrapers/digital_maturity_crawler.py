"""
Wraps Scraper/crawlers/digital-maturity-crawler (Phase 7). Assesses a
company's website via the Wayback Machine (snapshot history, a structural-
diff redesign-year estimate) and optionally BuiltWith (tech stack) — falls
back to lightweight heuristics without a BuiltWith key.

Feeds: website_digital_maturity (years since last major redesign — the raw
value IS the need signal, no inversion: an old redesign directly means high
digital-neglect need), online_market_presence (0-5 composite of ecommerce +
social presence + snapshot activity, matching that indicator's own proxy
text). Distinct from scrapers/wappalyzer_local.py's tech_stack_intensity,
which is a live regex signature match against the site's own HTML/headers,
not a Wayback-based history read — the two measure different things and both
stay wired.
"""

from datetime import datetime
from sqlalchemy.orm import Session

from adapters.base import run_adapter
from scrapers.node_crawler_base import (
    CrawlerRunError, run_ts_crawler, rows_for_company, save_crawler_blob,
)

SOURCE_NAME = "Digital Maturity Crawler"
CRAWLER_DIR = "digital-maturity-crawler"
DATASET_NAME = "crawler_digital_maturity"
PHASE = 7


def _derive_signals(row: dict) -> dict:
    """
    Same field_status-first rule as the other Phase 7 wrappers: Wayback coverage
    is patchy for smaller company sites, and this indicator's own catalog comment
    says to treat "no snapshot found" as inconclusive, NOT as evidence of an old
    site — so a not_found redesign estimate writes nothing at all.
    """
    signals = {}
    field_status = row.get("field_status") or {}
    homepage = row.get("homepage_url")

    redesign = row.get("last_major_redesign_estimate") or {}
    year = redesign.get("estimated_year")
    if field_status.get("last_major_redesign_estimate") == "value" and year:
        age = float(min(datetime.utcnow().year - int(year), 8))
        signals["website_digital_maturity"] = {
            "value": age, "status": "present",
            "summary": f"last major redesign estimated {int(year)} ({int(age)} years ago)",
            "evidence": {
                "method": "structural diff between Wayback Machine snapshots year over year",
                "estimated_redesign_year": int(year),
                "snapshot_comparisons": redesign.get("comparisons") or [],
                "earliest_snapshot_date": row.get("earliest_snapshot_date"),
                "source_urls": [f"https://web.archive.org/web/*/{homepage}"] if homepage else [],
            },
        }

    # The 0-5 composite is only meaningful if the homepage was actually fetched —
    # has_ecommerce defaults to false on a failed fetch, which would otherwise
    # read as a confirmed "no online presence" and score maximum need.
    has_ecommerce = row.get("has_ecommerce")
    social_links = row.get("social_presence_links") or []
    snapshot_count = row.get("snapshot_count_last_5_years")
    if field_status.get("has_ecommerce") == "value" or field_status.get("social_presence_links") == "value":
        ecom_pts = 2.0 if has_ecommerce else 0.0
        social_pts = float(min(len(social_links), 2))
        activity_pts = 1.0 if (snapshot_count or 0) >= 10 else 0.0
        score = min(ecom_pts + social_pts + activity_pts, 5.0)
        parts = [f"e-commerce {'detected' if has_ecommerce else 'not detected'} (+{ecom_pts:.0f})",
                 f"{len(social_links)} social profile(s) (+{social_pts:.0f})",
                 f"{snapshot_count if snapshot_count is not None else 'unknown'} archive snapshots (+{activity_pts:.0f})"]
        signals["online_market_presence"] = {
            "value": score, "status": "present" if score > 0 else "absent",
            "summary": f"{score:.0f}/5 — " + "; ".join(parts),
            "evidence": {
                "method": "composite: e-commerce +2, each social profile +1 (max 2), 10+ archive snapshots +1",
                "score_breakdown": {"ecommerce": ecom_pts, "social_profiles": social_pts, "archive_activity": activity_pts},
                "has_ecommerce": has_ecommerce,
                "social_profiles": [{"label": s.get("platform"), "url": s.get("url")} for s in social_links],
                "snapshot_count_last_5_years": snapshot_count,
                "data_source": row.get("data_source"),
                "source_urls": [homepage] if homepage else [],
            },
        }

    return signals


def sync_digital_maturity(company, db_session: Session) -> dict:
    captured = {}

    def _fetch_live(c):
        # Wayback's CDX API is deliberately rate-limited (WAYBACK_DELAY_MS=800 between
        # requests, several snapshots fetched for the redesign-year comparison) —
        # measured live against example.com as consistently exceeding the 90s default
        # other Phase 7 wrappers use, so this one gets a larger budget end to end.
        rows = run_ts_crawler(CRAWLER_DIR, [{"company_id": c.id, "homepage_url": c.website_url}], run_timeout=130)
        matches = rows_for_company(rows, c.id)
        if not matches:
            raise CrawlerRunError("digital-maturity-crawler returned no row for this company")
        row = matches[0]
        captured["row"] = row
        confidence = 0.75 if row.get("data_source") == "builtwith_api" else 0.55
        return {
            "signals": _derive_signals(row),
            "raw_payload": {"data_source": row.get("data_source"),
                             "earliest_snapshot_date": row.get("earliest_snapshot_date")},
            "confidence": confidence,
        }

    def _simulate(c):
        return {
            "signals": {},
            "raw_payload": {"note": "digital-maturity-crawler unavailable or no website_url on record"},
            "confidence": 0.5,
        }

    result = run_adapter(
        db_session, company, SOURCE_NAME, PHASE,
        credentials_ok=bool(company.website_url),
        fetch_live=_fetch_live, simulate=_simulate, timeout=150,
    )

    if captured.get("row"):
        save_crawler_blob(db_session, company, DATASET_NAME, captured["row"])

    return result
