"""
Handelsregister Free Snapshot Scraper (Phase 2).
Anchors legal entities strictly by Handelsregister-Nummer (HRB/HRA) — the real
part implemented so far is entity-resolution normalization on the company
record itself, which is genuine, not simulated.

NOT YET BUILT: parsing filing event types/dates from the free snapshot search
pages (HTML-only, no API — see sourcing plan Phase 2). Deliberately left
unimplemented rather than faked; this scraper does not write any SignalRecord
today.
"""

from datetime import datetime
from sqlalchemy.orm import Session
from models import Company, SignalRecord, SourceHealth
from utils import normalize_handelsregister_nr

SOURCE_NAME = "Handelsregister Free Snapshot"

def index_handelsregister_snapshot(company, db_session: Session) -> dict:
    """
    Parses company snapshot data from free Handelsregister search pages.
    """
    source_health = db_session.query(SourceHealth).filter_by(source_name=SOURCE_NAME).first()
    if not source_health:
        source_health = SourceHealth(source_name=SOURCE_NAME, phase=2)
        db_session.add(source_health)

    try:
        source_health.total_calls += 1
        source_health.last_run_at = datetime.utcnow()
        source_health.last_status = "running"
        db_session.commit()

        # Ensure Handelsregister registration number is normalized
        company.registration_number = normalize_handelsregister_nr(company.registration_number)

        source_health.last_status = "success"
        db_session.commit()
        return {
            "status": "success",
            "legal_name": company.legal_name,
            "registration_number": company.registration_number
        }

    except Exception as e:
        db_session.rollback()
        source_health.error_count += 1
        source_health.last_status = "error"
        source_health.last_error_message = str(e)
        db_session.commit()
        return {"status": "error", "error": str(e)}
