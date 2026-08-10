"""
EU Funding & Tenders Portal Adapter (Phase 1 API).
Real integration against the portal's public SEDIA search API — no registration
gate, `apiKey=SEDIA` is the documented public value used by the portal's own
frontend. This is a broad text match across the public index (calls, projects,
news mentioning the company), not filtered specifically to awarded-grant
records — good enough as a presence/absence signal, but narrower filtering by
result "type" is a follow-up once that taxonomy is confirmed against current
docs, not guessed here.
"""

import requests
from sqlalchemy.orm import Session
from adapters.base import run_adapter
from config import EU_FUNDING_API_KEY

SOURCE_NAME = "EU Funding Portal"
PHASE = 1
SEARCH_URL = "https://api.tech.ec.europa.eu/search-api/prod/rest/search"


def _fetch_live(company) -> dict:
    resp = requests.post(
        SEARCH_URL,
        # The portal's search-api takes `text` as a query parameter, not a JSON
        # body field — confirmed against the live API (a body-only `text` gets a
        # 400 "Required request parameter 'text' ... is not present").
        params={"apiKey": EU_FUNDING_API_KEY, "text": f'"{company.legal_name}"'},
        json={"query": {"bool": {"must": []}}, "pageSize": 1, "pageNumber": 1},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    total = data.get("totalResults")
    if total is None:
        raise RuntimeError(f"Unexpected EU Funding Portal response shape: keys={list(data.keys())}")

    status = "present" if total > 0 else "absent"
    return {
        "signals": {"public_grant_count": {"value": float(total), "status": status}},
        "raw_payload": {"source": SOURCE_NAME, "text_query": company.legal_name, "total_results": total},
        "confidence": 0.7,
    }


def _simulate(company) -> dict:
    char_sum = sum(ord(c) for c in company.legal_name)
    if char_sum % 3 == 0:
        grant_val, status = 0.0, "absent"
    else:
        grant_val, status = float((char_sum % 3) + 1), "present"
    return {
        "signals": {"public_grant_count": {"value": grant_val, "status": status}},
        "raw_payload": {"source": SOURCE_NAME, "query": company.legal_name, "note": "live call failed or unavailable"},
        "confidence": 0.5,
    }


def sync_company_grants(company, db_session: Session) -> dict:
    # No registration gate for this one — always attempt live, fall back on failure.
    return run_adapter(
        db_session, company, SOURCE_NAME, PHASE,
        credentials_ok=True,
        fetch_live=_fetch_live, simulate=_simulate,
    )
