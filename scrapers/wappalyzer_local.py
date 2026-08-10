"""
Local Web Tech Detector (Phase 4).
Real implementation: fetches the company's own public homepage directly (no
third-party API, "no ban risk" per the sourcing plan) and pattern-matches a
small curated signature set against response headers/HTML — a lightweight
stand-in for the full python-Wappalyzer signature database, not a byte-for-byte
port of it. Runs against the company's own site only.
"""

import re
import requests
from sqlalchemy.orm import Session
from adapters.base import run_adapter

SOURCE_NAME = "Wappalyzer"
PHASE = 4

# (label, compiled pattern) checked against "headers text + html" combined, case-insensitive.
SIGNATURES = [
    ("WordPress", re.compile(r"wp-content|wp-includes|/wp-json/", re.I)),
    ("Shopify", re.compile(r"cdn\.shopify\.com|Shopify\.theme", re.I)),
    ("Wix", re.compile(r"static\.wixstatic\.com|wix\.com", re.I)),
    ("Squarespace", re.compile(r"squarespace\.com|squarespace-cdn", re.I)),
    ("Webflow", re.compile(r"webflow\.(com|io)", re.I)),
    ("Drupal", re.compile(r"Drupal\.settings|/sites/default/files/", re.I)),
    ("Joomla", re.compile(r"/media/jui/|Joomla!", re.I)),
    ("TYPO3", re.compile(r"typo3conf|typo3temp", re.I)),
    ("Magento", re.compile(r"Mage\.Cookies|/skin/frontend/", re.I)),
    ("React", re.compile(r"data-reactroot|react-dom", re.I)),
    ("Vue.js", re.compile(r"__vue__|vue\.js", re.I)),
    ("Angular", re.compile(r"ng-version|angular\.js", re.I)),
    ("Next.js", re.compile(r"__NEXT_DATA__|_next/static", re.I)),
    ("jQuery", re.compile(r"jquery(\.min)?\.js", re.I)),
    ("Google Tag Manager", re.compile(r"googletagmanager\.com/gtm", re.I)),
    ("Google Analytics", re.compile(r"google-analytics\.com|gtag\('config'", re.I)),
    ("HubSpot", re.compile(r"js\.hs-scripts\.com|hubspot\.com", re.I)),
    ("Cloudflare", re.compile(r"cloudflare", re.I)),
]


def _fetch_live(company) -> dict:
    if not company.website_url:
        raise RuntimeError("No website_url on record — nothing to fetch")

    resp = requests.get(
        company.website_url,
        timeout=15,
        headers={"User-Agent": "Mozilla/5.0 (compatible; ProjectViennaBot/1.0)"},
    )
    resp.raise_for_status()

    haystack = "\n".join(f"{k}: {v}" for k, v in resp.headers.items()) + "\n" + resp.text
    detected = [label for label, pattern in SIGNATURES if pattern.search(haystack)]

    score = float(min(len(detected), 10))
    status = "present" if score > 0 else "absent"
    return {
        "signals": {"tech_stack_intensity": {"value": score, "status": status}},
        "raw_payload": {"source": SOURCE_NAME, "url": company.website_url, "detected": detected},
        "confidence": 0.8,
    }


def _simulate(company) -> dict:
    char_sum = sum(ord(c) for c in company.legal_name)
    tech_val = float((char_sum % 8) + 2)
    return {
        "signals": {"tech_stack_intensity": {"value": tech_val, "status": "present"}},
        "raw_payload": {"source": SOURCE_NAME, "url": company.website_url, "note": "site unreachable or no website_url on record"},
        "confidence": 0.5,
    }


def sync_tech_stack(company, db_session: Session) -> dict:
    return run_adapter(
        db_session, company, SOURCE_NAME, PHASE,
        credentials_ok=True,
        fetch_live=_fetch_live, simulate=_simulate,
    )
