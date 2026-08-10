"""
EPO OPS Patent Data Ingestion Adapter (Phase 1 API).
Real REST integration: OAuth2 client-credentials token, then a published-data
biblio search by applicant name. Populates patent_count and patent_ipc_diversity
from the single search call. Requires EPO_OPS_CONSUMER_KEY/SECRET (free
registration at https://www.epo.org/en/searching-for-patents/data/web-services/ops) —
falls back to a clearly-tagged simulated value when absent. See adapters/base.py.
"""

import requests
import xml.etree.ElementTree as ET
from sqlalchemy.orm import Session
from adapters.base import run_adapter
from config import EPO_OPS_CONSUMER_KEY, EPO_OPS_CONSUMER_SECRET, has_credentials

SOURCE_NAME = "EPO OPS"
PHASE = 1
TOKEN_URL = "https://ops.epo.org/3.2/auth/accesstoken"
SEARCH_URL = "https://ops.epo.org/3.2/rest-services/published-data/search/biblio"


def _get_access_token() -> str:
    resp = requests.post(
        TOKEN_URL,
        data={"grant_type": "client_credentials"},
        auth=(EPO_OPS_CONSUMER_KEY, EPO_OPS_CONSUMER_SECRET),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _fetch_live(company) -> dict:
    token = _get_access_token()
    query = f'pa="{company.legal_name}"'
    resp = requests.get(
        SEARCH_URL,
        params={"q": query, "Range": "1-25"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    if resp.status_code == 404:
        # OPS returns 404 (not an empty 200) when a search yields zero hits.
        return {
            "signals": {
                "patent_count": {"value": 0.0, "status": "absent"},
                "patent_ipc_diversity": {"value": 0.0, "status": "absent"},
            },
            "raw_payload": {"source": SOURCE_NAME, "query": query, "http_status": 404},
            "confidence": 0.95,
        }
    resp.raise_for_status()

    root = ET.fromstring(resp.content)
    biblio_search = root.find(".//{*}biblio-search")
    total_count = int(biblio_search.get("total-result-count", "0")) if biblio_search is not None else 0

    ipc_prefixes = set()
    for ipc_text in root.findall(".//{*}classification-ipcr/{*}text"):
        if ipc_text.text:
            ipc_prefixes.add(ipc_text.text.strip()[:4])
    # A patent portfolio exists but this constituent didn't return classification text —
    # report a floor of 1 rather than falsely reading as "zero diversity".
    diversity = float(len(ipc_prefixes)) if ipc_prefixes else (1.0 if total_count > 0 else 0.0)

    status = "present" if total_count > 0 else "absent"
    return {
        "signals": {
            "patent_count": {"value": float(total_count), "status": status},
            "patent_ipc_diversity": {"value": diversity, "status": status},
        },
        "raw_payload": {
            "source": SOURCE_NAME, "query": query, "total_result_count": total_count,
            "ipc_prefixes": sorted(ipc_prefixes),
        },
        "confidence": 0.95,
    }


def _simulate(company) -> dict:
    char_sum = sum(ord(c) for c in company.legal_name)
    if char_sum % 5 == 0:
        patent_val, ipc_val, status = 0.0, 0.0, "absent"
    else:
        patent_val = float((char_sum % 7) + 1)
        ipc_val = float((char_sum % 3) + 1)
        status = "present"
    return {
        "signals": {
            "patent_count": {"value": patent_val, "status": status},
            "patent_ipc_diversity": {"value": ipc_val, "status": status},
        },
        "raw_payload": {
            "source": SOURCE_NAME, "query": company.legal_name,
            "note": "EPO_OPS_CONSUMER_KEY/SECRET not configured",
        },
        "confidence": 0.5,
    }


def sync_company_patents(company, db_session: Session) -> dict:
    """Populates patent_count and patent_ipc_diversity from one EPO OPS search call."""
    return run_adapter(
        db_session, company, SOURCE_NAME, PHASE,
        credentials_ok=has_credentials(SOURCE_NAME),
        fetch_live=_fetch_live, simulate=_simulate,
    )
