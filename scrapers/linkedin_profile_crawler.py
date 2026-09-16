"""
Wraps Scraper/crawlers/linkedin-profile-crawler (Phase 7) — OFF by default via
config.LINKEDIN_CRAWLER_ENABLED. The sourcing plan names LinkedIn a
"highest-risk scrape target" and recommends buying via a compliant reseller
rather than scraping directly; this wrapper exists so the capability is wired
and ready, but nothing calls it live until that flag is deliberately flipped.

Deliberately does NOT write any SignalRecord. The LinkedIn/Bios indicators
(management_age, mgmt_national_diversity, mgmt_education_level, ...) already
have a real producer: the manual CSV-import pipeline documented in
LinkedIn_Extraction_Gem_Prompt.md, which has a human (or a Gemini prompt) read
each exec's profile and infer age/nationality/education — an interpretive
step this project has deliberately kept out of pure automation. Writing a
second, automated producer for the same signal_keys here would silently race
that pipeline (see vienna-api-integration-status memory's "one producer per
signal_key" rule). Instead, this wrapper only refreshes the raw profile text
(name/headline/about/recentRole) into each CompanyPerson.raw_fields blob, so
that manual step has fresher source text to read without a person re-pasting
it by hand — CompanyPerson.linkedin_url is where the URLs to fetch come from.
"""

from datetime import datetime

from sqlalchemy.orm import Session

from adapters.base import get_or_create_source_health
from config import LINKEDIN_CRAWLER_ENABLED, LINKEDIN_LI_AT
from scrapers.node_crawler_base import CrawlerRunError, run_node_entrypoint

SOURCE_NAME = "LinkedIn Profile Crawler"
CRAWLER_DIR = "linkedin-profile-crawler"
PHASE = 7
MAX_PROFILES_PER_RUN = 20  # matches the crawler's own hard cap


def sync_linkedin_profiles(company, db_session: Session) -> dict:
    if not LINKEDIN_CRAWLER_ENABLED:
        return {"status": "skipped", "reason": "LINKEDIN_CRAWLER_ENABLED=false"}

    people = [p for p in company.people if p.linkedin_url][:MAX_PROFILES_PER_RUN]
    if not people:
        return {"status": "skipped", "reason": "no CompanyPerson row with linkedin_url set for this company"}

    source_health = get_or_create_source_health(db_session, SOURCE_NAME, PHASE)
    source_health.total_calls += 1
    source_health.last_run_at = datetime.utcnow()
    db_session.commit()

    by_url = {p.linkedin_url: p for p in people}
    env = {"LINKEDIN_LI_AT": LINKEDIN_LI_AT} if LINKEDIN_LI_AT else {}

    try:
        rows = run_node_entrypoint(CRAWLER_DIR, "src/main.mjs", list(by_url.keys()),
                                    env_overrides=env, run_timeout=100)
    except CrawlerRunError as e:
        db_session.rollback()
        source_health = get_or_create_source_health(db_session, SOURCE_NAME, PHASE)
        source_health.error_count += 1
        source_health.last_status = "error"
        source_health.last_error_message = str(e)[:500]
        db_session.commit()
        return {"status": "error", "error": str(e)}

    updated = []
    for row in rows:
        person = by_url.get(row.get("url"))
        if not person:
            continue
        raw_fields = dict(person.raw_fields or {})
        raw_fields["linkedin_profile"] = {
            "name": row.get("name"), "headline": row.get("headline"),
            "location": row.get("location"), "about": row.get("about"),
            "recent_role": row.get("recentRole"), "status": row.get("status"),
            "scraped_at": row.get("scrapedAt"),
        }
        person.raw_fields = raw_fields
        person.updated_at = datetime.utcnow()
        updated.append(person.full_name or person.linkedin_url)

    source_health.mode = "live"
    source_health.last_status = "success"
    db_session.commit()

    return {"status": "success", "profiles_fetched": len(rows), "people_updated": updated}
