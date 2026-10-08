"""Find named public-sector customers in EU TED contract-award notices.

TED covers published public procurement, not private contracts or all national
contracts. A zero-hit or bounded result is never evidence of no customers.
The search API flattens winners and lot values, so this crawler deliberately
does not attribute notice/lot amounts to an individual company.
"""

from datetime import datetime

from sqlalchemy.orm import Session

from adapters.base import run_adapter
from models import RawImportRecord
from scrapers.node_crawler_base import CrawlerRunError, run_ts_crawler, rows_for_company

SOURCE_NAME = "TED Contract Awards Crawler"
DATASET_NAME = "crawler_ted_contract_awards"
PHASE = 7
def _fetch_live(company) -> dict:
    rows = run_ts_crawler("ted-awards-crawler", [{"company_id": company.id,
                               "company_name": company.legal_name}])
    matches = rows_for_company(rows, company.id)
    if not matches or matches[0].get("error"):
        raise CrawlerRunError(str(matches[0].get("error") if matches else "TED crawler returned no row"))
    row = matches[0]
    return {"signals": {}, "confidence": 0.9, "raw_payload": {
        key: value for key, value in row.items() if key not in {"company_id", "company_name"}
    }}


def sync_ted_awards(company, db_session: Session) -> dict:
    captured = {}

    def fetch(c):
        result = _fetch_live(c)
        captured["payload"] = result["raw_payload"]
        return result

    result = run_adapter(
        db_session, company, SOURCE_NAME, PHASE,
        credentials_ok=True, fetch_live=fetch,
        simulate=lambda _: {"signals": {}, "confidence": 0.0,
                            "raw_payload": {"note": "TED API unavailable"}},
        timeout=90,
    )
    if result.get("status") == "success" and result.get("mode") == "live" and captured:
        rec = db_session.query(RawImportRecord).filter_by(
            company_id=company.id, dataset_name=DATASET_NAME).first()
        if rec is None:
            rec = RawImportRecord(company_id=company.id, dataset_name=DATASET_NAME)
            db_session.add(rec)
        rec.source_filename = "TED published contract award notices"
        rec.raw_row = captured["payload"]
        rec.updated_at = datetime.utcnow()
        db_session.commit()
    return result
