"""Catalog comparison math and orchestration for T04."""

from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base, Company, Competitor, RawImportRecord, SignalRecord
from scrapers import competitor_catalog_benchmark as benchmark


def _peer(name):
    return SimpleNamespace(name=name, homepage_url=f"https://{name.lower()}.example")


def _product(name, price, category="Pumps", currency="EUR", price_type="fixed"):
    return {"product_name": name, "sku": name, "category": category,
            "price_value": price, "price_currency": currency, "price_type": price_type,
            "source_url": f"https://catalog.example/{name}"}


def _row(*products, partial=False):
    return {"products": list(products), "products_count": len(products), "is_partial": partial}


def test_score_uses_same_category_currency_and_confirmed_price_band():
    own = _row(_product("A", 100), _product("B", 200), _product("C", 300, "Motors"))
    results = [
        {"competitor": _peer("Alpha"), "row": _row(_product("X", 110), _product("Y", 210),
                                                       _product("Z", 110, "Motors")), "error": None},
        {"competitor": _peer("Beta"), "row": _row(_product("Q", 95), _product("R", 100, currency="USD"),
                                                      _product("S", 100, price_type="starting_from")), "error": None},
    ]
    signal = benchmark._derive_signals(own, results)["product_differentiation"]
    assert signal["value"] == pytest.approx(7.5)  # peer matches 2 and 1, averaged / 3 own
    assert signal["status"] == "present"
    evidence = signal["evidence"]
    assert evidence["own_sku_count"] == 3
    assert evidence["own_category_breadth"] == 2
    assert evidence["own_priced_sku_count"] == 3
    assert [p["comparable_sku_count"] for p in evidence["competitors_benchmarked"]] == [2, 1]
    assert evidence["competitors_benchmarked"][0]["name"] == "Alpha"
    assert evidence["competitors_benchmarked"][0]["comparable_products"][0]["peer_product"]["source_url"]


def test_more_distinct_own_products_lowers_commoditization():
    peer = [{"competitor": _peer("Alpha"), "row": _row(_product("X", 100)), "error": None}]
    one = benchmark._derive_signals(_row(_product("A", 100)), peer)["product_differentiation"]["value"]
    two = benchmark._derive_signals(_row(_product("A", 100), _product("B", 400)), peer)["product_differentiation"]["value"]
    assert one == 15 and two == 7.5


def test_no_price_or_uncomparable_categories_never_become_zero():
    peer = _peer("Alpha")
    assert benchmark._derive_signals(_row(_product("A", None)),
                                      [{"competitor": peer, "row": _row(_product("X", 100)), "error": None}]) == {}
    assert benchmark._derive_signals(_row(_product("A", 100)),
                                      [{"competitor": peer, "row": _row(_product("X", 100, "Motors")), "error": None}]) == {}
    assert benchmark._derive_signals(_row(_product("A", 100)),
                                      [{"competitor": peer, "row": {"products": [], "field_status": {"products": "not_found"}}, "error": None}]) == {}


def test_partial_crawl_cannot_prove_zero_overlap():
    peer = [{"competitor": _peer("Alpha"), "row": _row(_product("X", 1000), partial=True), "error": None}]
    assert benchmark._derive_signals(_row(_product("A", 100)), peer) == {}
    peer[0]["row"]["is_partial"] = False
    assert benchmark._derive_signals(_row(_product("A", 100)), peer)["product_differentiation"]["value"] == 0


def test_failed_peer_skipped_if_another_is_usable():
    signal = benchmark._derive_signals(_row(_product("A", 100)), [
        {"competitor": _peer("Alpha"), "row": None, "error": "timeout"},
        {"competitor": _peer("Beta"), "row": _row(_product("X", 100)), "error": None},
    ])["product_differentiation"]
    assert signal["value"] == 15
    assert signal["evidence"]["competitors_skipped"][0]["reason"] == "timeout"


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'catalog_test.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


def _company(db, suffix):
    c = Company(legal_name=f"Acme {suffix}", registration_number=f"R-{suffix}", website_url="https://acme.example")
    db.add(c)
    db.commit()
    return c


def test_sync_skips_without_named_competitors(db):
    assert benchmark.sync_competitor_catalog_benchmark(_company(db, "1"), db)["status"] == "skipped"


def test_sync_skips_without_own_priced_catalog(db):
    c = _company(db, "2")
    db.add(Competitor(company_id=c.id, name="Rival", homepage_url="https://rival.example"))
    db.commit()
    assert benchmark.sync_competitor_catalog_benchmark(c, db)["status"] == "skipped"
    db.add(RawImportRecord(company_id=c.id, dataset_name="crawler_product_catalog",
                           raw_row=_row(_product("A", None)), updated_at=datetime.utcnow()))
    db.commit()
    assert benchmark.sync_competitor_catalog_benchmark(c, db)["status"] == "skipped"


def test_sync_writes_real_signal_and_auditable_blob(db, monkeypatch):
    c = _company(db, "3")
    db.add(Competitor(company_id=c.id, name="Rival", homepage_url="https://rival.example"))
    db.add(RawImportRecord(company_id=c.id, dataset_name="crawler_product_catalog",
                           raw_row=_row(_product("A", 100)), updated_at=datetime.utcnow()))
    db.commit()
    monkeypatch.setattr(benchmark, "_crawl_competitor", lambda competitor: _row(_product("X", 105)))
    result = benchmark.sync_competitor_catalog_benchmark(c, db)
    assert result["status"] == "success" and result["mode"] == "live", result
    signal = db.query(SignalRecord).filter_by(company_id=c.id, signal_key="product_differentiation").one()
    assert signal.numeric_value == 15 and not signal.is_simulated
    assert signal.source == benchmark.SOURCE_NAME
    assert "Rival" in signal.raw_payload_ref
    blob = db.query(RawImportRecord).filter_by(company_id=c.id, dataset_name=benchmark.DATASET_NAME).one()
    assert blob.raw_row["competitors"][0]["row"]["products_count"] == 1


def test_competitor_cap_is_three():
    assert benchmark.MAX_COMPETITORS == 3


def test_registration_and_eligibility(db):
    from company_service import PHASE_7_SOURCES, eligible_phase7_sources, plan_source_steps
    from config import has_credentials

    assert "Competitor Catalog Benchmark" in PHASE_7_SOURCES
    assert has_credentials("Competitor Catalog Benchmark") is True

    c = _company(db, "reg")
    assert "Competitor Catalog Benchmark" not in eligible_phase7_sources(c)
    db.add(Competitor(company_id=c.id, name="Rival", homepage_url="https://rival.example"))
    db.commit()
    assert "Competitor Catalog Benchmark" in eligible_phase7_sources(c)

    steps, _ = plan_source_steps(c, [7])
    step_names = [s.name for s in steps]
    assert "Competitor Catalog Benchmark" in step_names


def test_crawl_competitor_normalizes_url(monkeypatch):
    captured = []

    def fake_run(crawler_dir, inputs, **kwargs):
        captured.extend(inputs)
        return [{"company_id": "c1", "products": []}]

    monkeypatch.setattr(benchmark, "run_ts_crawler", fake_run)
    peer = SimpleNamespace(id="c1", name="Rival", homepage_url="rival.example")
    benchmark._crawl_competitor(peer)
    assert captured[0]["homepage_url"] == "https://rival.example"


def test_category_compatibility_with_generic_and_token_overlap():
    assert benchmark._categories_compatible("Pumps", "Pumps") is True
    assert benchmark._categories_compatible("Pumps", "Motors") is False
    assert benchmark._categories_compatible("Products &#8211; old", "Macchine per la pasta") is True
    assert benchmark._categories_compatible("Macchine per la pasta", "Utensili pasta") is True
    assert benchmark._categories_compatible("Compressor Accessories", "Accessories") is True


