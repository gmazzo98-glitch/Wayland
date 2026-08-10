"""
Destatis GENESIS-Online Sector Export Statistics Adapter (Phase 1 API).
Real REST integration against the GENESIS-Online `data/tablefile` endpoint,
matching a row by the company's NACE code prefix. Requires DESTATIS_USERNAME/
PASSWORD (free registration at https://www-genesis.destatis.de) AND
DESTATIS_EXPORT_TABLE_CODE — the specific foreign-trade-by-sector table to pull
from, which the sourcing plan names the API for but doesn't pin to one table
code. That's a real open decision, not guessed here (Section 7 of the Technical
Brief) — falls back to a clearly-tagged simulated value until all three are set.
"""

import csv
import io
import requests
from sqlalchemy.orm import Session
from adapters.base import run_adapter
from config import DESTATIS_USERNAME, DESTATIS_PASSWORD, DESTATIS_EXPORT_TABLE_CODE, has_credentials

SOURCE_NAME = "Destatis"
PHASE = 1
BASE_URL = "https://www-genesis.destatis.de/genesisWS/rest/2020"


def _fetch_live(company) -> dict:
    resp = requests.get(
        f"{BASE_URL}/data/tablefile",
        params={
            "username": DESTATIS_USERNAME,
            "password": DESTATIS_PASSWORD,
            "name": DESTATIS_EXPORT_TABLE_CODE,
            "area": "all",
            "format": "ffcsv",
            "language": "en",
        },
        timeout=30,
    )
    resp.raise_for_status()

    rows = list(csv.reader(io.StringIO(resp.text), delimiter=";"))
    if len(rows) < 2:
        raise RuntimeError(f"Destatis table {DESTATIS_EXPORT_TABLE_CODE} returned no data rows")

    nace_prefix = (company.nace_code or "")[:3]
    target_row = next((r for r in rows[1:] if any(nace_prefix in cell for cell in r)), None)
    if target_row is None:
        raise RuntimeError(f"No row in table {DESTATIS_EXPORT_TABLE_CODE} matched NACE prefix '{nace_prefix}'")

    numeric_cells = [
        c.replace(",", ".") for c in target_row
        if c.replace(",", ".").replace("-", "").replace(".", "").isdigit()
    ]
    if not numeric_cells:
        raise RuntimeError(f"No numeric value found in matched row for table {DESTATIS_EXPORT_TABLE_CODE}")

    raw_val = float(numeric_cells[-1])
    # Normalize a plausible raw scale (e.g. a 0-100 percentage) to the model's 0.0-1.0 ratio.
    export_ratio = raw_val / 100.0 if raw_val > 1.0 else raw_val

    return {
        "signals": {"sector_export_exposure": {"value": round(export_ratio, 3), "status": "present"}},
        "raw_payload": {
            "source": SOURCE_NAME, "table": DESTATIS_EXPORT_TABLE_CODE,
            "nace_prefix": nace_prefix, "raw_value": raw_val,
        },
        "confidence": 0.85,
    }


def _simulate(company) -> dict:
    nace = company.nace_code or "A01.1"
    base_val = 0.65 if "A01" in nace or nace.startswith("10.") else 0.45
    return {
        "signals": {"sector_export_exposure": {"value": base_val, "status": "present"}},
        "raw_payload": {
            "source": SOURCE_NAME, "nace_code": nace,
            "note": "DESTATIS_USERNAME/PASSWORD/EXPORT_TABLE_CODE not fully configured",
        },
        "confidence": 0.5,
    }


def sync_sector_export_exposure(company, db_session: Session) -> dict:
    return run_adapter(
        db_session, company, SOURCE_NAME, PHASE,
        credentials_ok=has_credentials(SOURCE_NAME),
        fetch_live=_fetch_live, simulate=_simulate,
    )
