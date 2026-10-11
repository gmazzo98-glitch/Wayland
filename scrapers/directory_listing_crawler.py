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

# Registered directory plugins in Scraper/crawlers/directory-listing-crawler/src/directories/index.ts:
# MECSPE (trade fair exhibitor search) and ANIMA (industry association member directory).
# Add more here as new directory plugins are registered there.
DEFAULT_DIRECTORY_URLS = [
    "https://www.mecspe.com/portale/it/espositori",
    "https://www.anima.it/aziende/",
]

# A single MECSPE search completes in 10-17s against an uncontended machine (25/25 real
# companies succeeded that way, 2026-10-11 — see docs/CRAWLER_ROADMAP.md ticket T03c). The old
# 75s budget was the tightest of any browser-using Phase 7 wrapper (company-website gets 170s,
# job-postings/linkedin-profile get 100s) despite sharing the exact same scarce "browser"
# resource-governor slot as all of them — so a real concurrent Phase 7 batch (several of those
# crawlers' Chromium instances competing for the machine at once) starved it first. Live-verified:
# a controlled 8-company concurrent repro killed 2/8 at exactly the old 75s mark
# ("directory-listing-crawler timed out after 75s") while the same two companies' other sources
# (company-website's own 170s budget, in one case) were still running — this crawler's own
# MECSPE-search logic was never the problem, only its budget relative to its peers.
RUN_TIMEOUT_SECONDS = 150


def _derive_signals(rows: list) -> dict:
    signals = {}
    # The crawler's buildResult sets appears_in_directory=true for ANY match it kept,
    # exact or fuzzy, and marks the fuzzy ones with possible_match=true on top. So a
    # confirmed listing is "appears AND NOT possible_match" — reading appears alone
    # (as this did before) counted "BREMBOMATIC PEDRALI SRL" as Brembo exhibiting.
    hit_rows = [r for r in rows if r.get("appears_in_directory") and not r.get("possible_match")]
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
    # A miss in the configured directories (MECSPE, ANIMA) cannot establish
    # absence from trade fairs or associations. Fuzzy candidates remain in
    # the saved raw crawler record for manual inspection.
    return signals


def sync_directory_listing(company, db_session: Session) -> dict:
    captured = {}

    def _fetch_live(c):
        rows = run_ts_crawler(CRAWLER_DIR, [{
            "company_id": c.id, "company_name": c.legal_name,
            "directory_urls": "|".join(DEFAULT_DIRECTORY_URLS),
        }], run_timeout=RUN_TIMEOUT_SECONDS)
        matches = rows_for_company(rows, c.id)
        if not matches:
            raise CrawlerRunError("directory-listing-crawler returned no rows for this company")
        captured["rows"] = matches
        if not any(r.get("status") == "ok" for r in matches):
            raise CrawlerRunError("directory-listing-crawler did not complete a searchable directory")
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
        # The subprocess is killed at run_timeout; the adapter's wall-clock budget must
        # stay above that so the kill (and its error message) is what ends a hung run,
        # not a silent hard-timeout in run_adapter with the process still alive.
        fetch_live=_fetch_live, simulate=_simulate, timeout=RUN_TIMEOUT_SECONDS + 30,
    )

    for row in captured.get("rows", []):
        directory_id = row.get("directory_id") or "unknown"
        save_crawler_blob(db_session, company, f"crawler_directory_{directory_id}", row)

    return result
