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
from urllib.parse import urljoin, urlparse
from sqlalchemy.orm import Session

from adapters.base import run_adapter
from scrapers.node_crawler_base import (
    CrawlerRunError, run_ts_crawler, rows_for_company, save_crawler_blob,
)

SOURCE_NAME = "Job Postings Crawler"
CRAWLER_DIR = "job-postings-crawler"
DATASET_NAME = "crawler_job_postings"
PHASE = 7
# Minimum plausible listings before the digital-lead gate may be asserted absent.
MIN_LISTINGS_FOR_GATE = 3

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

# A careers page should say so somewhere. Guards against a path that 200s but is
# really a catch-all/marketing page.
_CAREERS_CONTENT_RE = re.compile(
    r"lavora con noi|posizioni aperte|offerte di lavoro|candidatur|"          # IT
    r"stellenangebote|karriere|bewerb|offene stellen|"                         # DE
    r"job opening|open position|vacanc|careers|apply now|join our team",       # EN
    re.I,
)


def _normalized(url: str) -> str:
    """scheme+host+path, lowercased, no trailing slash — for comparing a probe's
    landing page against the homepage."""
    try:
        p = urlparse(url)
        return f"{p.scheme.lower()}://{p.netloc.lower()}{p.path.rstrip('/').lower()}"
    except ValueError:
        return (url or "").rstrip("/").lower()


def _find_careers_url(website_url: str):
    """
    Returns a URL that is genuinely a careers page, or None.

    A plain `status_code == 200` check is not enough: many CMS sites answer every
    unknown path with a 200 that redirects to (or renders) the homepage. Verified
    live on rcm.it, where /careers, /jobs and /lavora-con-noi all returned 200 with
    byte-identical homepage content — which previously got passed to the crawler as
    a careers page and produced 28 "open roles" that were really sector-page links.
    So a candidate must also land somewhere other than the homepage AND actually
    read like a careers page.
    """
    if not website_url:
        return None
    base = website_url if re.match(r"^https?://", website_url, re.I) else f"https://{website_url}"
    try:
        home = requests.get(base, timeout=6, headers=_PROBE_HEADERS)
    except requests.RequestException:
        return None
    home_url = _normalized(home.url)

    for path in CAREERS_PATHS:
        url = urljoin(base.rstrip("/") + "/", path.lstrip("/"))
        try:
            resp = requests.get(url, timeout=6, headers=_PROBE_HEADERS)
        except requests.RequestException:
            continue
        if resp.status_code != 200:
            continue
        if _normalized(resp.url) == home_url:
            continue  # soft-404 / catch-all redirect back to the homepage
        if not _CAREERS_CONTENT_RE.search(resp.text or ""):
            continue
        return resp.url
    return None


# The crawler's generic adapter treats link-ish elements on the page as listings, so
# a real careers page can still yield non-jobs — "CARICA IL TUO CURRICULUM VITAE"
# (upload your CV) with href="javascript:;" was returned as an open role by a live
# run. A listing is only counted when it points at a real, distinct document.
_CTA_TITLE_RE = re.compile(
    r"carica il tuo|curriculum vitae|invia (la tua )?candidatura|upload (your )?cv|"
    r"privacy|cookie|newsletter|scarica|download|contatt|contact us|vedi tutt|view all",
    re.I,
)


def _plausible_listings(roles_sample: list, source_urls: list) -> list:
    """Drops CTA buttons, javascript:/anchor hrefs, and links back to the careers
    page itself — none of which are job postings."""
    source_norms = {_normalized(u) for u in source_urls}
    keep = []
    for r in roles_sample or []:
        title = (r.get("title") or "").strip()
        url = (r.get("url") or "").strip()
        if not title or not url.lower().startswith(("http://", "https://")):
            continue
        if _normalized(url) in source_norms:
            continue
        if _CTA_TITLE_RE.search(title):
            continue
        keep.append(r)
    return keep

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

    Every signal carries the postings behind it, so "1 digital role" can be
    checked against the actual titles and their URLs rather than taken on faith.
    """
    signals = {}
    field_status = row.get("field_status") or {}
    sources_used = row.get("sources_used") or []
    if not sources_used:
        return signals  # no source reached — every field below would be a fabricated zero

    source_urls = list(dict.fromkeys(s.get("url") for s in sources_used if s.get("url")))
    roles_sample = _plausible_listings(row.get("roles_sample"), source_urls)
    total_roles = row.get("total_open_roles")
    sampled = [{"label": r.get("title"), "url": r.get("url")} for r in roles_sample if r.get("title")]

    roles_status = field_status.get("technical_digital_roles_count")
    tech_roles = row.get("technical_digital_roles_count")
    # The crawler's own count is computed over every "listing" it extracted, junk
    # included, and can't be recomputed here (only a 10-item sample comes back, and
    # the keyword list lives in the crawler). So if plausibility filtering rejected
    # ALL of the listings, the extraction isn't trustworthy enough to derive a count
    # from either — bortolinkemo.com's only "role" was a CV-upload button, which
    # would otherwise have been reported as "0 of 1 open roles".
    extraction_is_junk = bool(row.get("roles_sample")) and not roles_sample
    if extraction_is_junk:
        return signals

    if roles_status == "value" and tech_roles is not None:
        # The crawler matches keywords against title + snippet + full description,
        # but roles_sample carries titles only and is capped at 10 — so the sample
        # can legitimately show fewer title-matches than the count. Say so rather
        # than presenting the sample as the definitive list of what was counted.
        signals["digital_job_postings"] = {
            "value": float(tech_roles), "status": "present",
            "summary": f"{int(tech_roles)} of {total_roles} open roles matched digital/technical keywords"
                        + (f" ({len(roles_sample)} of the listings shown below survived junk-filtering)"
                           if len(roles_sample) < len(row.get("roles_sample") or []) else ""),
            "evidence": {
                "method": "keyword match over each posting's title, snippet and description",
                "counted": int(tech_roles), "considered": total_roles,
                "source_urls": source_urls,
                "open_roles_sample": sampled,
                "note": "sample shows up to 10 titles; matches can also come from description text not shown here",
            },
        }
    elif roles_status == "not_applicable":
        # A source was crawled and it advertises no open roles at all — a real,
        # established zero for a hiring-velocity signal, not a failed lookup.
        signals["digital_job_postings"] = {
            "value": 0.0, "status": "absent",
            "summary": "careers page crawled, advertises no open roles at all",
            "evidence": {"method": "careers page crawled, zero listings found",
                          "counted": 0, "considered": 0, "source_urls": source_urls},
        }

    qual_share = row.get("technical_qualification_share")
    if field_status.get("technical_qualification_share") == "value" and qual_share is not None:
        pct = float(qual_share) * 100.0 if qual_share <= 1.0 else float(qual_share)
        signals["skilled_labour_share"] = {
            "value": pct, "status": "present",
            "summary": f"{pct:.0f}% of the postings whose description was fetched require a technical/university qualification",
            "evidence": {
                "method": "qualification-keyword match, over postings whose full description was retrieved",
                "share_pct": round(pct, 1), "source_urls": source_urls,
                "open_roles_sample": sampled,
            },
        }

    # Gating indicator (gate_penalty_multiplier=0.7) — a false "absent" here costs the
    # company 30% of its readiness score, so it needs more than one stray link to fire.
    # Generic careers-page scraping is noisy enough that a single plausible listing is
    # not evidence that no digital-lead role exists; below the threshold, stay silent.
    if len(roles_sample) >= MIN_LISTINGS_FOR_GATE or any(
        DIGITAL_LEAD_TITLE_RE.search(r.get("title") or "") for r in roles_sample
    ):
        matched = [r.get("title") for r in roles_sample if DIGITAL_LEAD_TITLE_RE.search(r.get("title") or "")]
        found = bool(matched)
        signals["digital_lead_role_present"] = {
            "value": 1.0 if found else 0.0,
            "status": "present" if found else "absent",
            "summary": (f"matched open role: {matched[0]}" if found
                        else f"no digital/innovation-lead title among {len(roles_sample)} open roles"),
            "evidence": {
                "method": f"title regex over open roles: {DIGITAL_LEAD_TITLE_RE.pattern}",
                "matched_titles": matched, "source_urls": source_urls,
                "open_roles_sample": sampled,
            },
        }

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
