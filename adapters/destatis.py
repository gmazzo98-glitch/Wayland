"""
Destatis GENESIS-Online Sector Export Statistics Adapter (Phase 1 API).
Real REST integration against the GENESIS-Online `data/tablefile` endpoint,
matching a row by the company's NACE code prefix. Requires DESTATIS_USERNAME/
PASSWORD (free registration at https://www-genesis.destatis.de) AND
DESTATIS_EXPORT_TABLE_CODE — the specific foreign-trade-by-sector table to pull
from, which the sourcing plan names the API for but doesn't pin to one table
code. That's a real open decision, not guessed here (Section 7 of the Technical
Brief) — falls back to a clearly-tagged simulated value until all three are set.

Destatis retired GET entirely on 2025-06-30 ("Genesis API: Änderungen zum 30.
Juni 2025" — https://genesis.destatis.de/datenbank/online/docs/
20250505_vba_post_logincheck_tablefile.pdf): the API is now POST-only, with
credentials sent as `username`/`password` HTTP *headers* rather than query
params or form fields, specifically so they stop landing in logfiles. A GET
call against the old genesisWS/rest/2020 path no longer 401s — it now silently
200s with the React frontend's index.html, which is why this previously
looked like a dead/misconfigured endpoint even with valid credentials. The
`data/tablefile` response is also now a zip archive (one flat CSV inside),
not raw CSV text.
"""

import csv
import io
import zipfile
import requests
from sqlalchemy.orm import Session
from adapters.base import run_adapter
from config import DESTATIS_USERNAME, DESTATIS_PASSWORD, DESTATIS_EXPORT_TABLE_CODE, has_credentials

SOURCE_NAME = "Destatis"
PHASE = 1
BASE_URL = "https://www-genesis.destatis.de/genesisWS/rest/2020"

# value_unit strings that plausibly mean "this cell is already a 0-100(%) or
# 0-1 ratio" rather than an absolute count/currency/mass figure. A table whose
# matched row's value_unit isn't one of these can't honestly be normalized
# into the 0.0-1.0 export-exposure ratio this signal is defined as — writing
# a number anyway would be exactly the "silently fabricate a real-looking
# value" failure mode this adapter's own docstring warns against.
_RATIO_LIKE_UNITS = {"%", "PC", "PZ", "ratio", "quota"}


def _fetch_live(company) -> dict:
    resp = requests.post(
        f"{BASE_URL}/data/tablefile",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "username": DESTATIS_USERNAME,
            "password": DESTATIS_PASSWORD,
        },
        data={
            "name": DESTATIS_EXPORT_TABLE_CODE,
            "area": "all",
            "format": "ffcsv",
            "language": "en",
        },
        timeout=30,
    )
    resp.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        csv_bytes = zf.read(zf.namelist()[0])
    rows = list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8-sig")), delimiter=";"))
    if not rows:
        raise RuntimeError(f"Destatis table {DESTATIS_EXPORT_TABLE_CODE} returned no data rows")

    # Match on the classification code/label columns only (not every column —
    # "time"/"value" are numeric and could coincidentally contain a 3-digit
    # NACE prefix), so a match means the row is actually about that sector.
    match_fields = ("1_variable_code", "1_variable_attribute_code", "1_variable_attribute_label",
                     "2_variable_code", "2_variable_attribute_code", "2_variable_attribute_label")
    nace_prefix = (company.nace_code or "")[:3]
    target_row = next(
        (r for r in rows if any(nace_prefix in (r.get(f) or "") for f in match_fields)),
        None,
    )
    if target_row is None:
        raise RuntimeError(f"No row in table {DESTATIS_EXPORT_TABLE_CODE} matched NACE prefix '{nace_prefix}'")

    raw_str = (target_row.get("value") or "").strip()
    if not raw_str:
        raise RuntimeError(f"Matched row in table {DESTATIS_EXPORT_TABLE_CODE} has no 'value' cell")
    raw_val = float(raw_str.replace(",", "."))

    value_unit = (target_row.get("value_unit") or "").strip()
    if value_unit not in _RATIO_LIKE_UNITS:
        # e.g. table 51000-0005 (Foreign trade by WA commodity class) reports
        # absolute EUR/USD/tonnes figures, never a ratio — normalizing those
        # into 0.0-1.0 would produce a plausible-looking but meaningless
        # number. Surface that plainly and let it fall back to simulated
        # rather than writing it as if it were real.
        raise RuntimeError(
            f"Table {DESTATIS_EXPORT_TABLE_CODE}'s matched row is in units '{value_unit}' "
            f"(value={raw_val}), not a percentage/ratio — this table can't answer "
            f"sector_export_exposure as configured; a different table code is needed."
        )

    export_ratio = raw_val / 100.0 if raw_val > 1.0 else raw_val

    return {
        "signals": {"sector_export_exposure": {"value": round(export_ratio, 3), "status": "present"}},
        "raw_payload": {
            "source": SOURCE_NAME, "table": DESTATIS_EXPORT_TABLE_CODE,
            "nace_prefix": nace_prefix, "raw_value": raw_val, "value_unit": value_unit,
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
