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
import requests
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

def test_no_phase7_wrapper_fabricates_a_value_when_unavailable():
    """scoring.py reads numeric_value without consulting is_simulated, so a
    placeholder would be scored exactly like verified data. Every Phase 7
    simulate() must therefore write no signals and leave the row
    not_yet_checked — the honest state for 'we couldn't check'."""
    import inspect
    from scrapers import (company_website_crawler, job_postings_crawler, review_crawler,
                          news_signals_crawler, directory_listing_crawler,
                          innovation_participation_crawler, digital_maturity_crawler)

    modules = [company_website_crawler, job_postings_crawler, review_crawler, news_signals_crawler,
               directory_listing_crawler, innovation_participation_crawler, digital_maturity_crawler]
    for mod in modules:
        src = inspect.getsource(mod)
        assert "char_sum" not in src, f"{mod.__name__} still derives a placeholder value from the company name"
        assert '"signals": {}' in src, f"{mod.__name__}'s simulate() should return no signals"


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
        "field_status": {
            "product_lines_count": "value", "has_sustainability_report": "value",
            "founding_or_product_launch_year": "value", "store_regions": "value",
        },
    }
    signals = company_website_crawler._derive_signals(None, None, row)
    assert signals["product_portfolio_diversity"] == {"value": 6.0, "status": "present"}
    assert signals["esg_reporting_recency"] == {"value": 3.0, "status": "present"}
    assert signals["product_age"] == {"value": 20.0, "status": "present"}
    assert signals["store_geo_distribution"] == {"value": 2.0, "status": "present"}
    assert "physical_stores_trend" not in signals  # no prior snapshot to diff against


def test_company_website_not_found_is_never_written_as_absent():
    """'not_found' means the extraction failed, not that the company has no
    stores/report — writing it as 'absent' would assert a confirmed negative."""
    row = {
        "product_lines_count": None,
        "has_sustainability_report": {"present": False, "first_publication_year": None},
        "founding_or_product_launch_year": None,
        "store_regions": [],
        "store_count": None,
        "field_status": {
            "product_lines_count": "not_found", "has_sustainability_report": "not_found",
            "founding_or_product_launch_year": "not_found", "store_regions": "not_found",
            "store_count": "not_found",
        },
    }
    assert company_website_crawler._derive_signals(None, None, row) == {}


def test_company_website_crawled_sustainability_page_with_no_report_is_absent():
    row = {
        "has_sustainability_report": {"present": False, "first_publication_year": None},
        "field_status": {"has_sustainability_report": "value"},
    }
    signals = company_website_crawler._derive_signals(None, None, row)
    assert signals["esg_reporting_recency"] == {"value": None, "status": "absent"}


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
        "sources_used": [{"id": "careers", "kind": "careers_page", "listings_found": 12}],
        "field_status": {"technical_digital_roles_count": "value", "technical_qualification_share": "value"},
    }
    signals = job_postings_crawler._derive_signals(row)
    assert signals["digital_job_postings"] == {"value": 4.0, "status": "present"}
    assert signals["skilled_labour_share"] == {"value": 60.0, "status": "present"}
    assert signals["digital_lead_role_present"] == {"value": 1.0, "status": "present"}


def test_job_postings_no_digital_lead_when_no_match():
    row = {
        "roles_sample": [{"title": "Warehouse Assistant"}],
        "careers_url": "https://example.com/careers",
        "sources_used": [{"id": "careers", "kind": "careers_page", "listings_found": 1}],
        "field_status": {"technical_digital_roles_count": "value"},
    }
    signals = job_postings_crawler._derive_signals(row)
    assert signals["digital_lead_role_present"] == {"value": 0.0, "status": "absent"}


def test_job_postings_unreachable_careers_page_writes_nothing():
    """The crawler still emits count=0 when no source was reachable and flags it
    via field_status/sources_used — that 0 must never become a scored signal,
    least of all digital_lead_role_present, which gates the readiness axis."""
    row = {
        "technical_digital_roles_count": 0,
        "total_open_roles": 0,
        "technical_qualification_share": None,
        "roles_sample": [],
        "careers_url": "https://example.com",
        "sources_used": [],
        "sources_skipped": [{"id": "careers", "url": "https://example.com", "reason": "robots.txt disallow"}],
        "field_status": {
            "total_open_roles": "not_found", "technical_digital_roles_count": "not_found",
            "technical_qualification_share": "not_found", "roles_sample": "not_found",
        },
    }
    assert job_postings_crawler._derive_signals(row) == {}


def test_find_careers_url_returns_none_for_unreachable_domain(monkeypatch):
    def boom(*args, **kwargs):
        raise requests.RequestException("dead domain")
    monkeypatch.setattr(job_postings_crawler.requests, "get", boom)
    assert job_postings_crawler._find_careers_url("www.example.com") is None


def test_find_careers_url_picks_first_reachable_path(monkeypatch):
    class Resp:
        def __init__(self, code):
            self.status_code = code

    seen = []

    def fake_get(url, **kwargs):
        seen.append(url)
        if url.endswith("/lavora-con-noi"):
            return Resp(200)
        return Resp(404)

    monkeypatch.setattr(job_postings_crawler.requests, "get", fake_get)
    assert job_postings_crawler._find_careers_url("www.example.com") == "https://www.example.com/lavora-con-noi"
    assert seen[0] == "https://www.example.com"  # base probed first, fails fast on dead domains


def test_job_postings_crawled_board_with_zero_roles_is_a_real_absent():
    row = {
        "technical_digital_roles_count": 0,
        "total_open_roles": 0,
        "roles_sample": [],
        "sources_used": [{"id": "careers", "kind": "careers_page", "listings_found": 0}],
        "field_status": {"total_open_roles": "value", "technical_digital_roles_count": "not_applicable",
                          "roles_sample": "not_applicable"},
    }
    signals = job_postings_crawler._derive_signals(row)
    assert signals["digital_job_postings"] == {"value": 0.0, "status": "absent"}
    assert "digital_lead_role_present" not in signals  # no postings retrieved to scan


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
        "field_status": {"university_partnership": "not_found", "sector_pilot_precedent": "value"},
        "search_errors": [],
    }
    signals = news_signals_crawler._derive_signals(row)
    assert signals["external_collaboration"] == {"value": 1.0, "status": "present"}
    assert signals["university_partnership"] == {"value": 0.0, "status": "absent"}
    assert signals["press_launch_mentions"] == {"value": 2.0, "status": "present"}
    assert signals["sector_pilot_precedent"] == {"value": 3.0, "status": "present"}


def test_news_signals_failed_search_does_not_assert_absence():
    """The crawler warns its own 'not_found' may just mean 'couldn't search' —
    external_collaboration/university_partnership are weight-5.0 readiness rows,
    so a failed search must write nothing rather than a confirmed zero."""
    row = {
        "external_collaboration": [],
        "university_partnership": [],
        "product_launch_mentions": [],
        "field_status": {"external_collaboration": "not_found", "university_partnership": "not_found",
                          "product_launch_mentions": "not_found"},
        "search_errors": [{"query_id": "collab", "error": "429 rate limited"}],
    }
    assert news_signals_crawler._derive_signals(row) == {}


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
        {"has_prior_innovation_participation": False, "field_status": {"events_found": "not_found"},
         "search_errors": []}
    ) == {"prior_open_innovation_usage": {"value": 0.0, "status": "absent"}}


def test_innovation_participation_failed_search_does_not_assert_absence():
    assert innovation_participation_crawler._derive_signals({
        "has_prior_innovation_participation": False,
        "field_status": {"events_found": "not_found"},
        "search_errors": [{"source": "general_search", "error": "missing API credentials"}],
    }) == {}


def test_digital_maturity_derive_signals():
    row = {
        "last_major_redesign_estimate": {"estimated_year": datetime.utcnow().year - 5},
        "has_ecommerce": True,
        "social_presence_links": [{"platform": "linkedin", "url": "x"}, {"platform": "instagram", "url": "y"}],
        "snapshot_count_last_5_years": 15,
        "field_status": {"last_major_redesign_estimate": "value", "has_ecommerce": "value",
                          "social_presence_links": "value"},
    }
    signals = digital_maturity_crawler._derive_signals(row)
    assert signals["website_digital_maturity"] == {"value": 5.0, "status": "present"}
    assert signals["online_market_presence"]["value"] == pytest.approx(5.0)  # 2 (ecommerce) + 2 (social, capped) + 1 (active)


def test_digital_maturity_no_wayback_coverage_writes_nothing():
    """This indicator's own catalog comment says to treat 'no snapshot found' as
    inconclusive, not as evidence of an outdated site."""
    row = {
        "last_major_redesign_estimate": {"estimated_year": None, "comparisons": []},
        "has_ecommerce": None,
        "social_presence_links": [],
        "snapshot_count_last_5_years": None,
        "field_status": {"last_major_redesign_estimate": "not_found", "has_ecommerce": "not_found",
                          "social_presence_links": "not_found"},
    }
    assert digital_maturity_crawler._derive_signals(row) == {}
