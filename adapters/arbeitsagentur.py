"""
Arbeitsagentur Jobsuche API Adapter (Phase 1 API).
Real integration — no registration gate. `X-API-Key: jobboerse-jobsuche` is the
public client id documented by the Bundesagentur für Arbeit's own reference
client (github.com/bundesAPI/jobsuche-api) and shared by all official
Arbeitsagentur frontends, not a secret specific to this project.
"""

import requests
from sqlalchemy.orm import Session
from adapters.base import run_adapter
from config import ARBEITSAGENTUR_API_KEY

SOURCE_NAME = "Arbeitsagentur"
PHASE = 1
SEARCH_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs"


def _fetch_live(company) -> dict:
    resp = requests.get(
        SEARCH_URL,
        params={"was": company.legal_name, "size": 100},
        headers={"X-API-Key": ARBEITSAGENTUR_API_KEY},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    listings = data.get("stellenangebote", [])
    total = data.get("maxErgebnisse", len(listings))

    status = "present" if total > 0 else "absent"
    return {
        "signals": {"job_posting_velocity": {"value": float(total), "status": status}},
        "raw_payload": {"source": SOURCE_NAME, "was": company.legal_name, "total": total},
        "confidence": 0.85,
    }


def _simulate(company) -> dict:
    char_sum = sum(ord(c) for c in company.legal_name)
    jobs_val = float((char_sum % 12) + 2)
    return {
        "signals": {"job_posting_velocity": {"value": jobs_val, "status": "present"}},
        "raw_payload": {"source": SOURCE_NAME, "company": company.legal_name, "note": "live call failed or unavailable"},
        "confidence": 0.5,
    }


def sync_job_velocity(company, db_session: Session) -> dict:
    return run_adapter(
        db_session, company, SOURCE_NAME, PHASE,
        credentials_ok=True,
        fetch_live=_fetch_live, simulate=_simulate,
    )
