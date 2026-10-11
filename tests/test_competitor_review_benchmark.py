"""The peer review comparison requires an explicit profile and stays unscored."""

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from database import _migrate_sqlite_schema
from company_service import eligible_phase7_sources, plan_source_steps
from models import Base, Company, Competitor, RawImportRecord, SignalRecord
from scrapers import competitor_review_benchmark as benchmark


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'reviews.db').as_posix()}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()
    engine.dispose()


def _company(db, profile="https://www.kununu.com/de/peer-industries"):
    company = Company(legal_name="Maker S.p.A.", registration_number="REV-1", country="Italy")
    db.add(company)
    db.flush()
    db.add(Competitor(company_id=company.id, name="Peer Industries",
                      review_profile_url=profile))
    db.commit()
    return company


def _row(company_id, count=0, rating=4.0, status="ok"):
    return {"company_id": company_id, "source_type": "kununu", "status": status,
            "profile_url": "https://www.kununu.com/de/peer-industries",
            "avg_employer_rating": rating, "review_count": count,
            "field_status": {"avg_employer_rating": "value", "review_count": "value"}}


@pytest.mark.parametrize("url,source", [
    ("https://www.kununu.com/de/peer", "kununu"),
    ("https://www.glassdoor.com/Reviews/Peer-Reviews-E1.htm", "glassdoor"),
    ("https://www.google.com/maps/place/Peer", "google"),
    ("https://www.trustpilot.com/review/peer.example", None),
    ("https://www.google.com/maps/search/peer", None),
    ("https://www.kununu.com/", None),
    ("https://www.glassdoor.com/", None),
    ("https://kununu.com.evil.example/de/peer", None),
    ("http://www.kununu.com/de/peer", None),
])
def test_profile_source_requires_a_supported_direct_https_url(url, source):
    assert benchmark.profile_source(url) == source


def test_measurement_respects_field_status_and_measured_zero():
    row = _row("peer", count=0)
    assert benchmark.review_measurement(row)["review_count"] == 0
    row["field_status"]["review_count"] = "not_found"
    assert benchmark.review_measurement(row)["review_count"] is None
    row["status"] = "blocked"
    assert benchmark.review_measurement(row)["rating"] is None


def test_disabled_modes_skip_without_invoking_crawler(db, monkeypatch):
    company = _company(db)
    monkeypatch.setattr(benchmark, "KUNUNU_CRAWLER_ENABLED", False)
    monkeypatch.setattr(benchmark, "REVIEW_CRAWLER_MODE_A_ENABLED", False)
    monkeypatch.setattr(benchmark, "run_ts_crawler", lambda *a, **k: pytest.fail("crawler invoked"))
    assert benchmark.sync_competitor_review_benchmark(company, db)["status"] == "skipped"
    assert "Competitor Review Benchmark" not in eligible_phase7_sources(company)
    assert "Competitor Review Benchmark" not in {step.name for step in plan_source_steps(company, [7])[0]}


def test_enabled_profile_is_registered_for_phase7(db, monkeypatch):
    company = _company(db)
    monkeypatch.setattr(benchmark, "KUNUNU_CRAWLER_ENABLED", True)
    assert "Competitor Review Benchmark" in eligible_phase7_sources(company)
    assert "Competitor Review Benchmark" in {step.name for step in plan_source_steps(company, [7])[0]}


def test_live_comparison_saves_raw_only(db, monkeypatch):
    company = _company(db)
    db.add(RawImportRecord(company_id=company.id, dataset_name="crawler_reviews_kununu",
                           raw_row=_row(company.id, count=2, rating=3.5)))
    db.commit()
    monkeypatch.setattr(benchmark, "KUNUNU_CRAWLER_ENABLED", True)
    calls = []

    def crawl(crawler_dir, input_rows, **kwargs):
        calls.extend(input_rows)
        return [_row(input_rows[0]["company_id"], count=0)]

    monkeypatch.setattr(benchmark, "run_ts_crawler", crawl)
    result = benchmark.sync_competitor_review_benchmark(company, db)
    assert result["status"] == "success"
    assert result["signals"] == {}
    assert calls[0]["profile_url"] == "https://www.kununu.com/de/peer-industries"
    raw = db.query(RawImportRecord).filter_by(company_id=company.id,
                                              dataset_name=benchmark.DATASET_NAME).one().raw_row
    assert raw["company"]["reviews_by_platform"]["kununu"]["review_count"] == 2
    assert raw["competitors"][0]["review_count"] == 0
    assert db.query(SignalRecord).filter_by(company_id=company.id).count() == 0


def test_nullable_profile_migrates_existing_sqlite_competitor_table(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'old.db').as_posix()}")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE competitors (id VARCHAR(36) PRIMARY KEY, company_id VARCHAR(36), name VARCHAR(255), homepage_url VARCHAR(500), added_at DATETIME)")
        conn.exec_driver_sql("INSERT INTO competitors (id, company_id, name) VALUES ('old', 'company', 'Old Peer')")
    _migrate_sqlite_schema(engine)
    columns = {col["name"] for col in inspect(engine).get_columns("competitors")}
    assert "review_profile_url" in columns
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT review_profile_url FROM competitors WHERE id='old'").scalar() is None
    engine.dispose()
