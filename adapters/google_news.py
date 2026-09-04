"""
Partnership/Collaboration News Adapter (Phase 4).
Real integration via the Google Programmable Search JSON API (100 free
queries/day), per the sourcing plan's own recommendation over scraping Google
directly. Requires GOOGLE_CSE_API_KEY (Google Cloud Console) and GOOGLE_CSE_ID
(a Programmable Search Engine configured to search the whole web) — falls back
to a clearly-tagged simulated value when absent.
"""

import requests
from sqlalchemy.orm import Session
from adapters.base import run_adapter
from config import GOOGLE_CSE_API_KEY, GOOGLE_CSE_ID, has_credentials

SOURCE_NAME = "Google News"
PHASE = 4
SEARCH_URL = "https://www.googleapis.com/customsearch/v1"


def _fetch_live(company) -> dict:
    resp = requests.get(
        SEARCH_URL,
        params={
            "key": GOOGLE_CSE_API_KEY,
            "cx": GOOGLE_CSE_ID,
            "q": f'"{company.legal_name}" (partnership OR collaboration OR cooperation OR pilot)',
            "num": 10,
        },
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    items = data.get("items", [])
    count = float(len(items))
    status = "present" if count > 0 else "absent"
    return {
        "signals": {"partnership_news_count": {"value": count, "status": status}},
        "raw_payload": {
            "source": SOURCE_NAME, "query": company.legal_name,
            "result_titles": [i.get("title") for i in items],
        },
        "confidence": 0.75,
    }


def _simulate(company) -> dict:
    char_sum = sum(ord(c) for c in company.legal_name)
    count = float(char_sum % 10)
    status = "present" if count > 0 else "absent"
    return {
        "signals": {"partnership_news_count": {"value": count, "status": status}},
        "raw_payload": {"source": SOURCE_NAME, "query": company.legal_name, "note": "GOOGLE_CSE_API_KEY/ID not configured"},
        "confidence": 0.5,
    }


def sync_partnership_news(company, db_session: Session) -> dict:
    return run_adapter(
        db_session, company, SOURCE_NAME, PHASE,
        credentials_ok=has_credentials(SOURCE_NAME),
        fetch_live=_fetch_live, simulate=_simulate,
    )


# --------------------------------------------------------------------------
# Board / Management Public Statements on Innovation — new in the 2026-08
# gg_indicators.json reconciliation. Same source, same query mechanics as the
# partnership-news signal above, just a different keyword set: this counts
# statements of intent, not evidence of an actual collaboration having
# happened. The doc's own caveat applies here more than anywhere else in this
# file — a raw keyword-search count can't tell genuine specificity ("we're
# piloting X in plant Y") from generic boilerplate ("we value innovation"),
# so extraction_confidence is set lower than the partnership-news signal's,
# and indicators.py's own comment on this row repeats the caveat for the UI.
# --------------------------------------------------------------------------

def _fetch_live_innovation_statements(company) -> dict:
    resp = requests.get(
        SEARCH_URL,
        params={
            "key": GOOGLE_CSE_API_KEY,
            "cx": GOOGLE_CSE_ID,
            "q": f'"{company.legal_name}" (innovation OR "digital transformation" OR Digitalisierung OR Innovationsstrategie)',
            "num": 10,
        },
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    items = data.get("items", [])
    count = float(len(items))
    status = "present" if count > 0 else "absent"
    return {
        "signals": {"board_innovation_statements": {"value": count, "status": status}},
        "raw_payload": {
            "source": SOURCE_NAME, "query": company.legal_name,
            "result_titles": [i.get("title") for i in items],
        },
        "confidence": 0.55,  # keyword match only, no boilerplate-vs-specific filtering yet
    }


def _simulate_innovation_statements(company) -> dict:
    char_sum = sum(ord(c) for c in company.legal_name)
    count = float((char_sum // 7) % 6)
    status = "present" if count > 0 else "absent"
    return {
        "signals": {"board_innovation_statements": {"value": count, "status": status}},
        "raw_payload": {"source": SOURCE_NAME, "query": company.legal_name, "note": "GOOGLE_CSE_API_KEY/ID not configured"},
        "confidence": 0.5,
    }


def sync_innovation_statements(company, db_session: Session) -> dict:
    return run_adapter(
        db_session, company, SOURCE_NAME, PHASE,
        credentials_ok=has_credentials(SOURCE_NAME),
        fetch_live=_fetch_live_innovation_statements, simulate=_simulate_innovation_statements,
    )
