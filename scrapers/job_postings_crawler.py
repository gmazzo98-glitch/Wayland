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
import requests
from urllib.parse import urljoin
from sqlalchemy.orm import Session

from adapters.base import run_adapter
from scrapers.node_crawler_base import (
    CrawlerRunError, run_ts_crawler, rows_for_company, save_crawler_blob,
)

SOURCE_NAME = "Job Postings Crawler"
CRAWLER_DIR = "job-postings-crawler"
DATASET_NAME = "crawler_job_postings"
PHASE = 7

# The crawler treats whatever careers_url it's handed AS the careers page. Handing it
# a bare homepage makes its generic adapter harvest every link on that page as a
# "listing" — verified live against a real Italian manufacturer, which produced 12
# "roles" titled "SCARICA IL CATALOGO" / "VISUALIZZA PRODOTTO". So a careers URL is
# probed for first (same approach as scrapers/management_diversity.py's path probe),
# and when none exists the crawler is given no careers source at all rather than the
# homepage — no source means no signal, which is the honest outcome.
CAREERS_PATHS = [
    "/careers", "/jobs", "/work-with-us",
    "/lavora-con-noi", "/it/lavora-con-noi", "/azienda/lavora-con-noi", "/carriere",
    "/karriere", "/stellenangebote", "/jobs-karriere",
    "/en/careers", "/en/jobs",
]
_PROBE_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ProjectViennaBot/1.0)"}


def _find_careers_url(website_url: str):
    """Returns a reachable careers-page URL for this domain, or None. Fails fast on
    a dead domain instead of paying the DNS/connect cost for every candidate path."""
    if not website_url:
        return None
    base = website_url if re.match(r"^https?://", website_url, re.I) else f"https://{website_url}"
    try:
        requests.get(base, timeout=6, headers=_PROBE_HEADERS)
    except requests.RequestException:
        return None

    for path in CAREERS_PATHS:
        url = urljoin(base.rstrip("/") + "/", path.lstrip("/"))
        try:
            resp = requests.get(url, timeout=6, headers=_PROBE_HEADERS)
        except requests.RequestException:
            continue
        if resp.status_code == 200:
            return url
    return None

DIGITAL_LEAD_TITLE_RE = re.compile(
    r"Head of Digital|Chief Digital Officer|\bCDO\b|Innovation Manager|Head of Innovation|"
    r"Digitalisierungsbeauftragter|Innovationsmanager|Leiter Digitalisierung",
    re.I,
)


def _derive_signals(row: dict) -> dict:
    """
    Reads field_status BEFORE the value. The crawler still emits
    technical_digital_roles_count=0 when it never reached a single source
    (robots disallow, unreachable careers page) and flags that with
    field_status='not_found' — writing that 0 as a scored signal would turn
    "we couldn't look" into "confirmed: this company isn't hiring for digital
    roles", which is exactly the distinction SignalRecord's four-state status
    exists to preserve. Nothing is written unless a source was actually crawled.
    """
    signals = {}
    field_status = row.get("field_status") or {}
    sources_used = row.get("sources_used") or []
    if not sources_used:
        return signals  # no source reached — every field below would be a fabricated zero

    roles_status = field_status.get("technical_digital_roles_count")
    tech_roles = row.get("technical_digital_roles_count")
    if roles_status == "value" and tech_roles is not None:
        signals["digital_job_postings"] = {"value": float(tech_roles), "status": "present"}
    elif roles_status == "not_applicable":
        # A source was crawled and it advertises no open roles at all — a real,
        # established zero for a hiring-velocity signal, not a failed lookup.
        signals["digital_job_postings"] = {"value": 0.0, "status": "absent"}

    qual_share = row.get("technical_qualification_share")
    if field_status.get("technical_qualification_share") == "value" and qual_share is not None:
        pct = float(qual_share) * 100.0 if qual_share <= 1.0 else float(qual_share)
        signals["skilled_labour_share"] = {"value": pct, "status": "present"}

    # Gating indicator (gate_penalty_multiplier=0.7) — only ever asserted off the
    # back of postings actually retrieved, never off an empty/failed crawl.
    roles_sample = row.get("roles_sample") or []
    if roles_sample:
        found = any(DIGITAL_LEAD_TITLE_RE.search(r.get("title") or "") for r in roles_sample)
        signals["digital_lead_role_present"] = {"value": 1.0 if found else 0.0,
                                                 "status": "present" if found else "absent"}

    return signals


def sync_job_postings(company, db_session: Session) -> dict:
    captured = {}

    def _fetch_live(c):
        careers_url = _find_careers_url(c.website_url)
        rows = run_ts_crawler(CRAWLER_DIR, [{"company_id": c.id, "company_name": c.legal_name,
                                              "careers_url": careers_url or ""}])
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
        return {
            "signals": {},
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
