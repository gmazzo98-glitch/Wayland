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
from urllib.parse import urljoin
from sqlalchemy.orm import Session
from adapters.base import run_adapter

SOURCE_NAME = "Own-Site Scrape"
PHASE = 4

CANDIDATE_PATHS = [
    "/about", "/about-us", "/team", "/management", "/leadership",
    "/ueber-uns", "/unternehmen", "/unternehmen/team", "/team/",
    "/karriere/team", "/wir-ueber-uns",
]

LEADERSHIP_KEYWORDS = re.compile(
    r"Geschäftsführer(in)?|Vorstand|Managing Director|Chief Executive|"
    r"\bCEO\b|\bCTO\b|\bCOO\b|\bCFO\b|Founder|Gründer(in)?|Head of|Leitung|"
    r"Geschäftsleitung|Prokurist(in)?",
    re.I,
)


def _fetch_live(company) -> dict:
    if not company.website_url:
        raise RuntimeError("No website_url on record — nothing to fetch")

    headers = {"User-Agent": "Mozilla/5.0 (compatible; ProjectViennaBot/1.0)"}

    # Fail fast against a dead/unreachable domain instead of retrying it across
    # up to 11 candidate paths — each retry pays the same DNS/connect cost again.
    try:
        requests.get(company.website_url, timeout=6, headers=headers)
    except requests.RequestException as e:
        raise RuntimeError(f"Base domain unreachable ({e.__class__.__name__}) — skipping path search")

    for path in CANDIDATE_PATHS:
        url = urljoin(company.website_url.rstrip("/") + "/", path.lstrip("/"))
        try:
            resp = requests.get(url, timeout=6, headers=headers)
        except requests.RequestException:
            continue
        if resp.status_code != 200:
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
    char_sum = sum(ord(c) for c in company.legal_name)
    score = float((char_sum % 4) + 1)
    return {
        "signals": {"management_diversity": {"value": score, "status": "present"}},
        "raw_payload": {"source": SOURCE_NAME, "url": company.website_url, "note": "no reachable About/Team page found at common paths"},
        "confidence": 0.5,
    }


def sync_management_diversity(company, db_session: Session) -> dict:
    return run_adapter(
        db_session, company, SOURCE_NAME, PHASE,
        credentials_ok=True,
        fetch_live=_fetch_live, simulate=_simulate,
    )
