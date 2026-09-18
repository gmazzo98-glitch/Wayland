"""
Management Background Own-Site Scraper (Phase 4).
Real implementation: fetches the company's own About/Team page (tries the
common DE/EN path variants) and counts leadership-title mentions.

Important limitation, kept visible rather than papered over: this is a proxy
for "is a leadership team publicly disclosed on the company's own site", not
an actual measurement of demographic diversity — that would require identity
inference this project deliberately does not build. Treat the resulting value
as "management transparency signal strength", not literal diversity.
"""

import re
import requests
from urllib.parse import urljoin, urlparse
from sqlalchemy.orm import Session
from adapters.base import run_adapter

SOURCE_NAME = "Own-Site Scrape"
PHASE = 4

CANDIDATE_PATHS = [
    "/about", "/about-us", "/team", "/management", "/leadership", "/company",
    "/ueber-uns", "/unternehmen", "/unternehmen/team", "/team/",
    "/karriere/team", "/wir-ueber-uns",
    # Italian sites (the live DB is currently all Italian manufacturers)
    "/chi-siamo", "/azienda", "/it/chi-siamo", "/it/azienda", "/it/about", "/la-nostra-storia",
]

LEADERSHIP_KEYWORDS = re.compile(
    r"Geschäftsführer(in)?|Vorstand|Managing Director|Chief Executive|"
    r"\bCEO\b|\bCTO\b|\bCOO\b|\bCFO\b|Founder|Gründer(in)?|Head of|Leitung|"
    r"Geschäftsleitung|Prokurist(in)?|"
    r"Amministratore Delegato|Direttore Generale|Presidente|Fondatore|Titolare|Consiglio di Amministrazione",
    re.I,
)


def _normalized(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme.lower()}://{p.netloc.lower()}{p.path.rstrip('/').lower()}"


def _fetch_live(company) -> dict:
    if not company.website_url:
        raise RuntimeError("No website_url on record — nothing to fetch")

    headers = {"User-Agent": "Mozilla/5.0 (compatible; ProjectViennaBot/1.0)"}
    base = company.website_url if re.match(r"^https?://", company.website_url, re.I) else f"https://{company.website_url}"

    # Fail fast against a dead/unreachable domain instead of retrying it across
    # every candidate path — each retry pays the same DNS/connect cost again.
    try:
        home = requests.get(base, timeout=6, headers=headers)
    except requests.RequestException as e:
        raise RuntimeError(f"Base domain unreachable ({e.__class__.__name__}) — skipping path search")
    home_url = _normalized(home.url)

    for path in CANDIDATE_PATHS:
        url = urljoin(base.rstrip("/") + "/", path.lstrip("/"))
        try:
            resp = requests.get(url, timeout=6, headers=headers)
        except requests.RequestException:
            continue
        if resp.status_code != 200:
            continue
        if _normalized(resp.url) == home_url:
            # Soft-404: the CMS answers unknown paths with the homepage (seen live on
            # rcm.it) — counting the homepage's own text as a leadership page is noise.
            continue

        hits = LEADERSHIP_KEYWORDS.findall(resp.text)
        score = float(min(len(hits), 10))
        status = "present" if score > 0 else "absent"
        return {
            "signals": {"management_diversity": {"value": score, "status": status}},
            "raw_payload": {"source": SOURCE_NAME, "url": url, "keyword_hits": len(hits)},
            "confidence": 0.6,  # transparency proxy, not a demographic measurement
        }

    raise RuntimeError(f"No About/Team page found at common paths for {company.website_url}")


def _simulate(company) -> dict:
    # No placeholder value (was a 1-4 score hashed from the company name) — see
    # scrapers/wappalyzer_local.py's _simulate for why: scoring cannot tell a
    # placeholder from a verified value, so an unchecked site must stay unchecked.
    return {
        "signals": {},
        "raw_payload": {"source": SOURCE_NAME, "url": company.website_url, "note": "no reachable About/Team page found at common paths"},
        "confidence": 0.5,
    }


def sync_management_diversity(company, db_session: Session) -> dict:
    return run_adapter(
        db_session, company, SOURCE_NAME, PHASE,
        credentials_ok=True,
        fetch_live=_fetch_live, simulate=_simulate,
    )
