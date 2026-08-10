"""
EUIPO Trademark Data Ingestion Adapter (Phase 1 API).
Real REST integration: OAuth2 client-credentials against the EUIPO CAS server,
then an eSearch Plus trademark search by applicant name. Requires
EUIPO_CLIENT_ID/SECRET (free registration at https://dev.euipo.europa.eu) —
falls back to a clearly-tagged simulated value when absent.

EUIPO_TOKEN_URL / EUIPO_SEARCH_URL default to the documented pattern but are
env-overridable: confirm the current values on your app's dashboard at
dev.euipo.europa.eu before relying on the defaults, since EUIPO issues these
per-app and has changed hosts before.
"""

import requests
from sqlalchemy.orm import Session
from adapters.base import run_adapter
from config import EUIPO_CLIENT_ID, EUIPO_CLIENT_SECRET, EUIPO_TOKEN_URL, EUIPO_SEARCH_URL, has_credentials

SOURCE_NAME = "EUIPO"
PHASE = 1


def _get_access_token() -> str:
    resp = requests.post(
        EUIPO_TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": EUIPO_CLIENT_ID,
            "client_secret": EUIPO_CLIENT_SECRET,
            "scope": "uid",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _fetch_live(company) -> dict:
    token = _get_access_token()
    resp = requests.get(
        EUIPO_SEARCH_URL,
        params={"query": f'applicantName=="{company.legal_name}"', "size": 50},
        headers={
            "Authorization": f"Bearer {token}",
            "X-IBM-Client-Id": EUIPO_CLIENT_ID,
        },
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()

    # Response envelope shape isn't fully nailed down without a live app to test
    # against — check the common field names defensively rather than assume one.
    total = data.get("totalElements", data.get("total", data.get("totalCount")))
    if total is None:
        results = data.get("trademarks", data.get("content", data.get("results", [])))
        total = len(results)

    status = "present" if total > 0 else "absent"
    return {
        "signals": {"trademark_count": {"value": float(total), "status": status}},
        "raw_payload": {"source": SOURCE_NAME, "query": company.legal_name, "total": total},
        "confidence": 0.9,
    }


def _simulate(company) -> dict:
    char_sum = sum(ord(c) for c in company.legal_name)
    if char_sum % 4 == 0:
        tm_val, status = 0.0, "absent"
    else:
        tm_val, status = float((char_sum % 5) + 1), "present"
    return {
        "signals": {"trademark_count": {"value": tm_val, "status": status}},
        "raw_payload": {
            "source": SOURCE_NAME, "query": company.legal_name,
            "note": "EUIPO_CLIENT_ID/SECRET not configured",
        },
        "confidence": 0.5,
    }


def sync_company_trademarks(company, db_session: Session) -> dict:
    return run_adapter(
        db_session, company, SOURCE_NAME, PHASE,
        credentials_ok=has_credentials(SOURCE_NAME),
        fetch_live=_fetch_live, simulate=_simulate,
    )
