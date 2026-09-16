"""
Wraps Scraper/crawlers/job-postings-crawler (Phase 7). Finds a company's open
roles (own careers page, optionally Indeed/Stepstone) and counts
technical/digital roles and qualification share.

Feeds: digital_job_postings, skilled_labour_share, digital_lead_role_present
(a title-keyword scan over the sample of open roles — the closest automatable
proxy for "does a named digital/innovation lead role exist", matching that
indicator's own proxy text: "Job title search ... on company website").
ERP systems age (T3 in the source spreadsheet) is deliberately NOT derived
here: the crawler's roles_sample only carries a title, not a full description,
which isn't enough text to detect an ERP vendor mention without guessing.
"""

import re
from sqlalchemy.orm import Session

from adapters.base import run_adapter
from scrapers.node_crawler_base import (
    CrawlerRunError, run_ts_crawler, rows_for_company, save_crawler_blob,
)

SOURCE_NAME = "Job Postings Crawler"
CRAWLER_DIR = "job-postings-crawler"
DATASET_NAME = "crawler_job_postings"
PHASE = 7

DIGITAL_LEAD_TITLE_RE = re.compile(
    r"Head of Digital|Chief Digital Officer|\bCDO\b|Innovation Manager|Head of Innovation|"
    r"Digitalisierungsbeauftragter|Innovationsmanager|Leiter Digitalisierung",
    re.I,
)


def _derive_signals(row: dict) -> dict:
    signals = {}
    field_status = row.get("field_status") or {}

    tech_roles = row.get("technical_digital_roles_count")
    if tech_roles is not None:
        signals["digital_job_postings"] = {"value": float(tech_roles), "status": "present"}
    elif field_status.get("technical_digital_roles_count") == "not_found":
        signals["digital_job_postings"] = {"value": 0.0, "status": "absent"}

    qual_share = row.get("technical_qualification_share")
    if qual_share is not None:
        pct = float(qual_share) * 100.0 if qual_share <= 1.0 else float(qual_share)
        signals["skilled_labour_share"] = {"value": pct, "status": "present"}

    roles_sample = row.get("roles_sample") or []
    if roles_sample or row.get("careers_url"):
        found = any(DIGITAL_LEAD_TITLE_RE.search(r.get("title") or "") for r in roles_sample)
        signals["digital_lead_role_present"] = {"value": 1.0 if found else 0.0, "status": "present"}

    return signals


def sync_job_postings(company, db_session: Session) -> dict:
    captured = {}

    def _fetch_live(c):
        rows = run_ts_crawler(CRAWLER_DIR, [{"company_id": c.id, "company_name": c.legal_name,
                                              "careers_url": c.website_url or ""}])
        matches = rows_for_company(rows, c.id)
        if not matches:
            raise CrawlerRunError("job-postings-crawler returned no row for this company")
        row = matches[0]
        captured["row"] = row
        if row.get("error"):
            raise CrawlerRunError(row["error"])
        return {
            "signals": _derive_signals(row),
            "raw_payload": {"total_open_roles": row.get("total_open_roles"), "sources_used": row.get("sources_used")},
            "confidence": 0.7,
        }

    def _simulate(c):
        char_sum = sum(ord(ch) for ch in c.legal_name)
        return {
            "signals": {"digital_job_postings": {"value": float(char_sum % 10), "status": "present"}},
            "raw_payload": {"note": "job-postings-crawler unavailable"},
            "confidence": 0.5,
        }

    result = run_adapter(
        db_session, company, SOURCE_NAME, PHASE,
        credentials_ok=True,
        fetch_live=_fetch_live, simulate=_simulate, timeout=90,
    )

    if captured.get("row"):
        save_crawler_blob(db_session, company, DATASET_NAME, captured["row"])

    return result
