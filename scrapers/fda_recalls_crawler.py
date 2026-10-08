"""Company-matched FDA recall evidence for relevant product sectors.

This is a US regulatory event source, not a general EU compliance assessment.
Search is bounded and firm names are checked client-side; no hit is inconclusive.
"""

import re
from datetime import datetime
from sqlalchemy.orm import Session

from adapters.base import run_adapter
from models import RawImportRecord
from scrapers.node_crawler_base import CrawlerRunError, run_ts_crawler, rows_for_company

SOURCE_NAME = "FDA Recalls Crawler"
DATASET_NAME = "crawler_fda_recalls"
PHASE = 7


def relevant_endpoints(company) -> list[str]:
    """Only request APIs that plausibly match the company's product sector."""
    code = re.sub(r"^[A-Z]", "", str(getattr(company, "nace_code", "") or "").upper()).replace(".", "")
    sector = (getattr(company, "sector_name", "") or "").casefold()
    use_text = not code or code == "0"
    endpoints = []
    food_text = any(word in sector for word in (
        "food", "beverage", "aliment", "bevande", "lebensmittel", "nahrung"))
    device_text = any(word in sector for word in (
        "medical device", "dispositivo medico", "medizintechnik"))
    if code.startswith(("10", "11")) or (use_text and food_text):
        endpoints.append("food")
    if code.startswith("21") or (use_text and any(word in sector for word in ("pharma", "farmaceut"))):
        endpoints.append("drug")
    if code.startswith("325") or (use_text and device_text):
        endpoints.append("device")
    return endpoints


def _fetch_live(company) -> dict:
    if not relevant_endpoints(company):
        return {"signals": {}, "confidence": 0.0,
                "raw_payload": {"skipped": "No FDA food, drug or medical-device sector match"}}
    rows = run_ts_crawler("fda-recalls-crawler", [{"company_id": company.id,
                               "company_name": company.legal_name,
                               "nace_code": company.nace_code or "",
                               "sector_name": company.sector_name or ""}])
    matches = rows_for_company(rows, company.id)
    if not matches or matches[0].get("error"):
        raise CrawlerRunError(str(matches[0].get("error") if matches else "FDA crawler returned no row"))
    row = matches[0]
    if row.get("skipped"):
        return {"signals": {}, "confidence": 0.0, "raw_payload": {"skipped": "No FDA food, drug or medical-device sector match"}}
    return {"signals": {}, "confidence": 0.9, "raw_payload": {
        key: value for key, value in row.items() if key not in {"company_id", "company_name"}
    }}


def sync_fda_recalls(company, db_session: Session) -> dict:
    if not relevant_endpoints(company):
        return {"status": "skipped", "reason": "Company sector is outside FDA food, drug and medical-device coverage"}
    captured = {}

    def fetch(c):
        result = _fetch_live(c)
        captured["payload"] = result["raw_payload"]
        return result

    result = run_adapter(
        db_session, company, SOURCE_NAME, PHASE,
        credentials_ok=True, fetch_live=fetch,
        simulate=lambda _: {"signals": {}, "confidence": 0.0,
                            "raw_payload": {"note": "FDA API unavailable"}},
        timeout=60,
    )
    if result.get("status") == "success" and result.get("mode") == "live" and captured:
        rec = db_session.query(RawImportRecord).filter_by(
            company_id=company.id, dataset_name=DATASET_NAME).first()
        if rec is None:
            rec = RawImportRecord(company_id=company.id, dataset_name=DATASET_NAME)
            db_session.add(rec)
        rec.source_filename = "FDA food/drug/device enforcement reports"
        rec.raw_row = captured["payload"]
        rec.updated_at = datetime.utcnow()
        db_session.commit()
    return result
