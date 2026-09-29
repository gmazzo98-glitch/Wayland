"""
Tests for scrapers/product_catalog_crawler.py — the Phase 7 wrapper around
Scraper/crawlers/product-catalog-crawler.

Deliberately does NOT spawn a real Node process or touch the configured
DATABASE_URL — product_catalog_crawler.run_ts_crawler (the name as imported
into that module, not scrapers.node_crawler_base.run_ts_crawler itself) is
monkeypatched, same convention as tests/test_crawler_scrapers.py.

Uses a tmp-dir SQLite FILE, not sqlite:///:memory: — adapters.base.run_adapter
runs fetch_live() on a worker thread (its hard-timeout executor), and an
in-memory SQLite database is per-connection: the pool hands that thread a
different, empty database the moment the ORM re-touches an expired `company`
attribute after run_adapter's pre-fetch db.commit(). Same fix, same reason,
as tests/test_phase7_batch.py's own file-backed fixture.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base, Company, RawImportRecord
from scrapers import node_crawler_base, product_catalog_crawler


@pytest.fixture
def memory_session(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
                            connect_args={"check_same_thread": False, "timeout": 30})
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


def _row(**overrides):
    row = {
        "company_id": None, "homepage_url": "https://example.com",
        "products": [{"source_url": "https://example.com/p/1", "extraction_method": "structured_data",
                       "product_name": "Widget X", "category": "Widgets", "price_value": 99.0,
                       "price_currency": "EUR"}],
        "products_count": 1, "categories": ["Widgets"],
        "price_range": {"min": 99.0, "max": 99.0, "currency": "EUR"},
        "catalog_narrative": "Sells a small range of widgets.",
        "is_partial": False, "crawled_pages_count": 5, "failed_pages": [],
    }
    row.update(overrides)
    return row


def test_no_website_url_takes_the_simulate_path_and_writes_no_signals(memory_session):
    c = Company(legal_name="No Site GmbH", registration_number="TESTREG-2", website_url=None)
    memory_session.add(c)
    memory_session.commit()

    result = product_catalog_crawler.sync_product_catalog(c, memory_session)
    assert result["status"] == "success"
    assert result["mode"] == "simulated"
    assert result["signals"] == {}


def test_writes_no_signals_ever_by_design(monkeypatch, memory_session, company):
    """This wrapper deliberately owns no signal_key — see its module docstring on why
    (product_portfolio_diversity already has a producer, and no indicator exists yet
    for catalog breadth/pricing). A future regression here would silently start a
    second producer race on whatever signal_key someone adds without checking that."""
    row = _row(company_id=company.id)
    monkeypatch.setattr(product_catalog_crawler, "run_ts_crawler", lambda *a, **k: [row])

    result = product_catalog_crawler.sync_product_catalog(company, memory_session)
    assert result["status"] == "success"
    assert result["signals"] == {}


def test_live_run_saves_the_full_catalog_as_a_raw_blob(monkeypatch, memory_session, company):
    row = _row(company_id=company.id)
    monkeypatch.setattr(product_catalog_crawler, "run_ts_crawler", lambda *a, **k: [row])

    product_catalog_crawler.sync_product_catalog(company, memory_session)

    # save_crawler_blob stores the crawler's own row verbatim (same convention as every other
    # scrapers/*_crawler.py wrapper) — the Raw Data & Mapping tab reads products_count/
    # categories/catalog_narrative/is_partial straight off it, not off a separately computed summary.
    rec = memory_session.query(RawImportRecord).filter_by(
        company_id=company.id, dataset_name="crawler_product_catalog").first()
    assert rec is not None
    assert rec.raw_row["products_count"] == 1
    assert rec.raw_row["categories"] == ["Widgets"]
    assert rec.raw_row["catalog_narrative"] == "Sells a small range of widgets."
    assert rec.raw_row["products"][0]["product_name"] == "Widget X"


def test_partial_crawl_is_flagged_honestly_in_the_saved_blob(monkeypatch, memory_session, company):
    row = _row(company_id=company.id, is_partial=True)
    monkeypatch.setattr(product_catalog_crawler, "run_ts_crawler", lambda *a, **k: [row])

    product_catalog_crawler.sync_product_catalog(company, memory_session)

    rec = memory_session.query(RawImportRecord).filter_by(
        company_id=company.id, dataset_name="crawler_product_catalog").first()
    assert rec.raw_row["is_partial"] is True


def test_credentials_ok_does_not_require_an_llm_key(monkeypatch, memory_session, company):
    """Unlike Company Website Crawler, this crawler can find real products via schema.org
    JSON-LD with no LLM key at all — gating it on CRAWLER_LLM_API_KEY would hide that."""
    monkeypatch.setattr(product_catalog_crawler, "CRAWLER_LLM_API_KEY", None)
    row = _row(company_id=company.id)
    monkeypatch.setattr(product_catalog_crawler, "run_ts_crawler", lambda *a, **k: [row])

    result = product_catalog_crawler.sync_product_catalog(company, memory_session)
    assert result["mode"] == "live"


def test_confidence_is_higher_when_structured_data_was_used(monkeypatch, memory_session, company):
    structured_row = _row(company_id=company.id, products=[
        {"source_url": "https://x/1", "extraction_method": "structured_data", "product_name": "A"},
    ])
    captured = {}
    original_upsert = node_crawler_base.save_crawler_blob

    def spy_save(db, comp, dataset_name, raw_row, **kw):
        captured["row"] = raw_row
        return original_upsert(db, comp, dataset_name, raw_row, **kw)

    monkeypatch.setattr(product_catalog_crawler, "run_ts_crawler", lambda *a, **k: [structured_row])
    monkeypatch.setattr(product_catalog_crawler, "save_crawler_blob", spy_save)

    product_catalog_crawler.sync_product_catalog(company, memory_session)
    assert captured["row"]["products"][0]["extraction_method"] == "structured_data"


def test_llm_only_row_still_succeeds_with_lower_confidence(monkeypatch, memory_session, company):
    llm_row = _row(company_id=company.id, products=[
        {"source_url": "https://x/1", "extraction_method": "llm", "product_name": "A"},
    ])
    monkeypatch.setattr(product_catalog_crawler, "run_ts_crawler", lambda *a, **k: [llm_row])

    result = product_catalog_crawler.sync_product_catalog(company, memory_session)
    assert result["status"] == "success"


def test_an_error_row_falls_back_to_simulate_and_still_saves_the_blob(monkeypatch, memory_session, company):
    """Mirrors company_website_crawler's contract: a row carrying an 'error' field raises
    CrawlerRunError (handled by run_adapter's simulate fallback), but the row itself — including
    whatever partial data it carries — is still captured for the blob, not discarded."""
    row = _row(company_id=company.id, error="Playwright timeout after 45s")
    monkeypatch.setattr(product_catalog_crawler, "run_ts_crawler", lambda *a, **k: [row])

    result = product_catalog_crawler.sync_product_catalog(company, memory_session)
    assert result["status"] == "error"
    assert result["mode"] == "simulated"

    rec = memory_session.query(RawImportRecord).filter_by(
        company_id=company.id, dataset_name="crawler_product_catalog").first()
    assert rec is not None and rec.raw_row.get("error") == "Playwright timeout after 45s"


def test_no_matching_row_for_company_id_raises_and_falls_back(monkeypatch, memory_session, company):
    monkeypatch.setattr(product_catalog_crawler, "run_ts_crawler", lambda *a, **k: [_row(company_id="someone-else")])

    result = product_catalog_crawler.sync_product_catalog(company, memory_session)
    assert result["status"] == "error"
    assert "no row for this company" in result["error"]
