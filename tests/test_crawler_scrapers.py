"""
Tests for the Phase 7 Node-crawler wrappers (scrapers/*_crawler.py) and their
shared harness (scrapers/node_crawler_base.py).

Deliberately does NOT spawn a real Node process or touch the configured
DATABASE_URL (which may point at the live Supabase project — see
feedback-git-workflow memory) — subprocess calls are monkeypatched, and the
one test that needs a DB uses a throwaway in-memory SQLite engine created
inline rather than database.get_db_session().
"""

import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base, Company, RawImportRecord
from scrapers import node_crawler_base
from scrapers.node_crawler_base import CrawlerRunError, rows_for_company, save_crawler_blob
from scrapers import company_website_crawler, job_postings_crawler, review_crawler
from scrapers import news_signals_crawler, directory_listing_crawler, innovation_participation_crawler
from scrapers import digital_maturity_crawler


@pytest.fixture
def memory_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture
def company(memory_session):
    c = Company(legal_name="Test GmbH", registration_number="TESTREG-1", website_url="https://example.com")
    memory_session.add(c)
    memory_session.commit()
    return c


# ---------------------------------------------------------------- node_crawler_base

def test_rows_for_company_filters_by_id():
    rows = [{"company_id": "a", "x": 1}, {"company_id": "b", "x": 2}, {"company_id": "a", "x": 3}]
    assert rows_for_company(rows, "a") == [{"company_id": "a", "x": 1}, {"company_id": "a", "x": 3}]
    assert rows_for_company(rows, "missing") == []


def test_save_crawler_blob_upserts_one_row_per_dataset(memory_session, company):
    save_crawler_blob(memory_session, company, "crawler_company_website", {"store_count": 3})
    rec = memory_session.query(RawImportRecord).filter_by(company_id=company.id, dataset_name="crawler_company_website").first()
    assert rec is not None
    assert rec.raw_row == {"store_count": 3}

    save_crawler_blob(memory_session, company, "crawler_company_website", {"store_count": 5})
    recs = memory_session.query(RawImportRecord).filter_by(company_id=company.id, dataset_name="crawler_company_website").all()
    assert len(recs) == 1
    assert recs[0].raw_row == {"store_count": 5}


def test_save_crawler_blob_keeps_separate_rows_per_dataset_name(memory_session, company):
    save_crawler_blob(memory_session, company, "crawler_reviews_google", {"avg_rating": 4.2})
    save_crawler_blob(memory_session, company, "crawler_reviews_kununu", {"avg_employer_rating": 3.1})
    recs = memory_session.query(RawImportRecord).filter_by(company_id=company.id).all()
    assert {r.dataset_name for r in recs} == {"crawler_reviews_google", "crawler_reviews_kununu"}


def test_ensure_built_raises_for_missing_crawler_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(node_crawler_base, "SCRAPER_CRAWLERS_DIR", str(tmp_path))
    with pytest.raises(CrawlerRunError):
        node_crawler_base.crawler_dir("does-not-exist")


def test_run_ts_crawler_raises_on_nonzero_exit(monkeypatch, tmp_path):
    crawler_root = tmp_path / "fake-crawler"
    (crawler_root / "dist").mkdir(parents=True)
    (crawler_root / "dist" / "main.js").write_text("// stub", encoding="utf-8")
    monkeypatch.setattr(node_crawler_base, "SCRAPER_CRAWLERS_DIR", str(tmp_path))

    def fake_run(args, cwd, env, timeout):
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(node_crawler_base, "_run", fake_run)
    with pytest.raises(CrawlerRunError, match="boom"):
        node_crawler_base.run_ts_crawler("fake-crawler", [{"company_id": "1"}])


def test_run_ts_crawler_reads_dataset_rows(monkeypatch, tmp_path):
    crawler_root = tmp_path / "fake-crawler"
    (crawler_root / "dist").mkdir(parents=True)
    (crawler_root / "dist" / "main.js").write_text("// stub", encoding="utf-8")
    monkeypatch.setattr(node_crawler_base, "SCRAPER_CRAWLERS_DIR", str(tmp_path))

    def fake_run(args, cwd, env, timeout):
        storage = Path(env["CRAWLEE_STORAGE_DIR"]) / "datasets" / "default"
        storage.mkdir(parents=True)
        (storage / "000000001.json").write_text(json.dumps({"company_id": "1", "value": 42}), encoding="utf-8")
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(node_crawler_base, "_run", fake_run)
    rows = node_crawler_base.run_ts_crawler("fake-crawler", [{"company_id": "1"}])
    assert rows == [{"company_id": "1", "value": 42}]


# ---------------------------------------------------------------- per-crawler signal derivation

def test_company_website_derive_signals_basic():
    row = {
        "product_lines_count": 6,
        "has_sustainability_report": {"present": True, "first_publication_year": datetime.utcnow().year - 3},
        "founding_or_product_launch_year": datetime.utcnow().year - 20,
        "store_regions": ["Bavaria", "Hesse"],
        "field_status": {},
    }
    signals = company_website_crawler._derive_signals(None, None, row)
    assert signals["product_portfolio_diversity"] == {"value": 6.0, "status": "present"}
    assert signals["esg_reporting_recency"] == {"value": 3.0, "status": "present"}
    assert signals["product_age"] == {"value": 20.0, "status": "present"}
    assert signals["store_geo_distribution"] == {"value": 2.0, "status": "present"}
    assert "physical_stores_trend" not in signals  # no prior snapshot to diff against


def test_company_website_stores_trend_needs_prior_snapshot(memory_session, company):
    old_ts = datetime.utcnow() - timedelta(days=60)
    save_crawler_blob(memory_session, company, "crawler_company_website", {"store_count": 10})
    rec = memory_session.query(RawImportRecord).filter_by(company_id=company.id).first()
    rec.updated_at = old_ts
    memory_session.commit()

    trend = company_website_crawler._stores_trend(memory_session, company, 12)
    assert trend == pytest.approx(20.0)  # (12-10)/10 * 100


def test_job_postings_derive_signals_digital_lead_keyword_match():
    row = {
        "technical_digital_roles_count": 4,
        "technical_qualification_share": 0.6,
        "roles_sample": [{"title": "Head of Digital Transformation"}, {"title": "Warehouse Assistant"}],
        "careers_url": "https://example.com/careers",
    }
    signals = job_postings_crawler._derive_signals(row)
    assert signals["digital_job_postings"] == {"value": 4.0, "status": "present"}
    assert signals["skilled_labour_share"] == {"value": 60.0, "status": "present"}
    assert signals["digital_lead_role_present"] == {"value": 1.0, "status": "present"}


def test_job_postings_no_digital_lead_when_no_match():
    row = {"roles_sample": [{"title": "Warehouse Assistant"}], "careers_url": "https://example.com/careers"}
    signals = job_postings_crawler._derive_signals(row)
    assert signals["digital_lead_role_present"] == {"value": 0.0, "status": "present"}


def test_review_crawler_derive_signals_mode_a_and_b():
    rows_by_source = {
        "google": {"status": "ok", "rating_trend": {"last_12_months_avg": 4.0, "prior_12_months_avg": 4.5}},
        "kununu": {"status": "ok", "avg_employer_rating": 3.2},
    }
    signals = review_crawler._derive_signals(rows_by_source)
    assert signals["product_quality_trend"] == {"value": pytest.approx(-0.5), "status": "present"}
    assert signals["kununu_rating"] == {"value": 3.2, "status": "present"}


def test_review_crawler_skips_blocked_sources():
    rows_by_source = {"trustpilot": {"status": "skipped_robots"}, "glassdoor": {"status": "error"}}
    assert review_crawler._derive_signals(rows_by_source) == {}


def test_news_signals_derive_signals():
    row = {
        "external_collaboration": [{"partner_name": "TU Munich"}],
        "university_partnership": [],
        "product_launch_mentions": [{"product_name": "X1"}, {"product_name": "X2"}],
        "sector_pilot_precedent": {"count": 3},
        "field_status": {"university_partnership": "not_found"},
    }
    signals = news_signals_crawler._derive_signals(row)
    assert signals["external_collaboration"] == {"value": 1.0, "status": "present"}
    assert signals["university_partnership"] == {"value": 0.0, "status": "absent"}
    assert signals["press_launch_mentions"] == {"value": 2.0, "status": "present"}
    assert signals["sector_pilot_precedent"] == {"value": 3.0, "status": "present"}


def test_directory_listing_derive_signals_hit_and_miss():
    hit_rows = [{"appears_in_directory": True, "status": "ok"}]
    assert directory_listing_crawler._derive_signals(hit_rows)["trade_fair_participation"] == {"value": 1.0, "status": "present"}

    miss_rows = [{"appears_in_directory": False, "possible_match": False, "status": "ok"}]
    assert directory_listing_crawler._derive_signals(miss_rows)["trade_fair_participation"] == {"value": 0.0, "status": "absent"}

    unchecked_rows = [{"appears_in_directory": False, "status": "no_config"}]
    assert directory_listing_crawler._derive_signals(unchecked_rows) == {}


def test_innovation_participation_derive_signals():
    assert innovation_participation_crawler._derive_signals({"has_prior_innovation_participation": True}) == {
        "prior_open_innovation_usage": {"value": 1.0, "status": "present"}
    }
    assert innovation_participation_crawler._derive_signals(
        {"has_prior_innovation_participation": False, "field_status": {"events_found": "not_found"}}
    ) == {"prior_open_innovation_usage": {"value": 0.0, "status": "absent"}}


def test_digital_maturity_derive_signals():
    row = {
        "last_major_redesign_estimate": {"estimated_year": datetime.utcnow().year - 5},
        "has_ecommerce": True,
        "social_presence_links": [{"platform": "linkedin", "url": "x"}, {"platform": "instagram", "url": "y"}],
        "snapshot_count_last_5_years": 15,
    }
    signals = digital_maturity_crawler._derive_signals(row)
    assert signals["website_digital_maturity"] == {"value": 5.0, "status": "present"}
    assert signals["online_market_presence"]["value"] == pytest.approx(5.0)  # 2 (ecommerce) + 2 (social, capped) + 1 (active)
