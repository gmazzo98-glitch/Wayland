"""Competitor hiring remains raw, with measured zero distinct from unknown."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base, Company, Competitor, RawImportRecord, SignalRecord
from scrapers import competitor_hiring_benchmark as benchmark


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'hiring.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()
    engine.dispose()


def _row(status="not_applicable", total=0, digital=0):
    return {
        "field_status": {"technical_digital_roles_count": status},
        "sources_used": [{"url": "https://example.com/careers", "listings_found": total}],
        "total_open_roles": total,
        "technical_digital_roles_count": digital,
        "roles_sample": [],
    }


def _company(db, with_peer=True, with_own=True):
    company = Company(
        legal_name="Maker S.p.A.", registration_number="HIRING-1",
        country="Italy", website_url="https://maker.example",
    )
    db.add(company)
    db.flush()
    if with_peer:
        db.add(Competitor(company_id=company.id, name="Peer", homepage_url="https://peer.example"))
    if with_own:
        db.add(RawImportRecord(
            company_id=company.id, dataset_name="crawler_job_postings",
            raw_row=_row(),
        ))
    db.commit()
    return company


def test_role_counts_keeps_measured_zero():
    counts = benchmark.role_counts(_row(), country="Italy")
    assert counts["open_role_count"] == 0
    assert counts["digital_technical_role_count"] == 0
    assert counts["open_role_status"] == "absent"


def test_role_counts_does_not_turn_failed_search_into_zero():
    row = _row(status="not_found")
    row["sources_used"] = []
    counts = benchmark.role_counts(row, country="Italy")
    assert counts["open_role_count"] is None
    assert counts["digital_technical_role_count"] is None
    assert counts["open_role_status"] == "unavailable"


def test_skips_without_named_peer_or_own_measurement(db):
    without_peer = _company(db, with_peer=False)
    assert benchmark.sync_competitor_hiring_benchmark(without_peer, db)["status"] == "skipped"
    db.query(Company).filter_by(id=without_peer.id).delete()
    db.commit()
    without_own = _company(db, with_own=False)
    result = benchmark.sync_competitor_hiring_benchmark(without_own, db)
    assert result["status"] == "skipped"
    assert "Own Job Postings" in result["reason"]


def test_live_result_saves_raw_comparison_without_signal(db, monkeypatch):
    company = _company(db)
    monkeypatch.setattr(benchmark, "_crawl_competitor", lambda competitor: _row())
    result = benchmark.sync_competitor_hiring_benchmark(company, db)
    assert result["status"] == "success"
    assert result["signals"] == {}
    raw = db.query(RawImportRecord).filter_by(
        company_id=company.id, dataset_name=benchmark.DATASET_NAME
    ).one().raw_row
    assert raw["company"]["open_role_count"] == 0
    assert raw["competitors"][0]["open_role_count"] == 0
    assert raw["competitors"][0]["digital_technical_role_count"] == 0
    assert db.query(SignalRecord).filter_by(company_id=company.id).count() == 0


def test_failed_peer_is_saved_as_unknown_and_run_reports_error(db, monkeypatch):
    company = _company(db)
    def fail(_competitor):
        raise RuntimeError("network unavailable")
    monkeypatch.setattr(benchmark, "_crawl_competitor", fail)
    result = benchmark.sync_competitor_hiring_benchmark(company, db)
    assert result["status"] == "error"
    raw = db.query(RawImportRecord).filter_by(
        company_id=company.id, dataset_name=benchmark.DATASET_NAME
    ).one().raw_row
    assert raw["competitors"][0]["open_role_count"] is None
    assert "network unavailable" in raw["competitors"][0]["error"]
    assert db.query(SignalRecord).filter_by(company_id=company.id).count() == 0


def test_competitor_cap_is_three():
    assert benchmark.MAX_COMPETITORS == 3
