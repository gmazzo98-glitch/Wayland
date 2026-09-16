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
    hit_rows = [r for r in rows if r.get("appears_in_directory")]
    maybe_rows = [r for r in rows if r.get("possible_match") and not r.get("appears_in_directory")]
    checked_rows = [r for r in rows if r.get("status") == "ok"]
    if not checked_rows:
        return signals

    directories_checked = [{"label": r.get("directory_name") or r.get("directory_url"),
                             "url": r.get("listing_url") or r.get("directory_url")} for r in checked_rows]

    if hit_rows:
        listed = [{"label": f"{r.get('directory_name')}"
                            + (f" (member/exhibitor since {r.get('membership_or_exhibitor_since')})"
                               if r.get("membership_or_exhibitor_since") else ""),
                    "url": r.get("listing_url") or r.get("directory_url")} for r in hit_rows]
        signals["trade_fair_participation"] = {
            "value": float(len(hit_rows)), "status": "present",
            "summary": "listed in " + ", ".join(str(l["label"]) for l in listed),
            "evidence": {"method": "exact name match in each directory's own exhibitor/member search",
                          "found": listed, "directories_checked": directories_checked},
        }
    else:
        # A fuzzy-only match is explicitly flagged for human verification by the
        # crawler, so it scores half rather than counting as a confirmed listing.
        near = [{"label": f"possible match in {r.get('directory_name')} — needs human verification",
                  "url": r.get("listing_url") or r.get("directory_url")} for r in maybe_rows]
        signals["trade_fair_participation"] = {
            "value": 0.5 if maybe_rows else 0.0, "status": "absent",
            "summary": (f"no confirmed listing; {len(maybe_rows)} fuzzy match(es) need checking"
                        if maybe_rows else
                        "searched " + ", ".join(str(d["label"]) for d in directories_checked) + " — not listed"),
            "evidence": {"method": "exact name match in each directory's own exhibitor/member search",
                          "found": near, "directories_checked": directories_checked},
        }
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
        return {
            "signals": {},
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
