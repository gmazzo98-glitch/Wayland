"""Competitor news/press-share benchmark remains raw, with measured zero distinct from unknown."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base, Company, Competitor, RawImportRecord, SignalRecord
from scrapers import competitor_news_benchmark as benchmark


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'news.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()
    engine.dispose()


def _own_blob(mention_count=3):
    return {
        "search_phrase": "Maker", "locale": "it-IT",
        "headlines_about_company": mention_count,
        "headlines_by_concept": {"partnership": 1},
    }


def _peer_result(name, mention_count=2):
    return {"name": name, "skipped": None, "search_phrase": name, "locale": "it-IT",
            "mention_count": mention_count, "attribution_mode": "tokens",
            "headlines_by_concept": {}, "headlines": []}


def _company(db, with_peer=True, with_own=True, own_mention_count=3):
    company = Company(
        legal_name="Maker S.p.A.", registration_number="NEWS-1",
        country="Italy", website_url="https://maker.example",
    )
    db.add(company)
    db.flush()
    if with_peer:
        db.add(Competitor(company_id=company.id, name="Peer Industries", homepage_url="https://peer.example"))
    if with_own:
        db.add(RawImportRecord(
            company_id=company.id, dataset_name=benchmark.OWN_NEWS_DATASET,
            raw_row=_own_blob(own_mention_count),
        ))
    db.commit()
    return company


def test_press_mentions_skips_generic_single_token_name():
    result = benchmark.press_mentions("Global Srl", country="Italy")
    assert result["mention_count"] is None
    assert result["skipped"] == "name is too generic to attribute news coverage safely"


def test_skips_without_named_competitor(db):
    company = _company(db, with_peer=False)
    result = benchmark.sync_competitor_news_benchmark(company, db)
    assert result["status"] == "skipped"
    assert "No named competitor" in result["reason"]


def test_skips_without_own_news_measurement(db):
    company = _company(db, with_own=False)
    result = benchmark.sync_competitor_news_benchmark(company, db)
    assert result["status"] == "skipped"
    assert "Own Google News RSS" in result["reason"]


def test_skips_when_own_mention_count_unavailable(db):
    company = _company(db, own_mention_count=None)
    result = benchmark.sync_competitor_news_benchmark(company, db)
    assert result["status"] == "skipped"
    assert "unavailable" in result["reason"]


def test_live_result_saves_raw_comparison_without_signal(db, monkeypatch):
    company = _company(db)
    monkeypatch.setattr(benchmark, "press_mentions", lambda name, country=None: _peer_result(name, 2))
    result = benchmark.sync_competitor_news_benchmark(company, db)
    assert result["status"] == "success"
    assert result["signals"] == {}
    raw = db.query(RawImportRecord).filter_by(
        company_id=company.id, dataset_name=benchmark.DATASET_NAME
    ).one().raw_row
    assert raw["company"]["mention_count"] == 3
    assert raw["competitors"][0]["mention_count"] == 2
    assert raw["competitors"][0]["competitor_name"] == "Peer Industries"
    assert db.query(SignalRecord).filter_by(company_id=company.id).count() == 0


def test_failed_peer_is_saved_as_unknown_and_run_still_succeeds(db, monkeypatch):
    company = _company(db)
    db.add(Competitor(company_id=company.id, name="Second Peer Group"))
    db.commit()

    def flaky(name, country=None):
        if name == "Peer Industries":
            raise RuntimeError("network unavailable")
        return _peer_result(name, 5)

    monkeypatch.setattr(benchmark, "press_mentions", flaky)
    result = benchmark.sync_competitor_news_benchmark(company, db)
    assert result["status"] == "success"
    raw = db.query(RawImportRecord).filter_by(
        company_id=company.id, dataset_name=benchmark.DATASET_NAME
    ).one().raw_row
    by_name = {c["competitor_name"]: c for c in raw["competitors"]}
    assert by_name["Peer Industries"]["mention_count"] is None
    assert "network unavailable" in by_name["Peer Industries"]["error"]
    assert by_name["Second Peer Group"]["mention_count"] == 5


def test_run_errors_when_every_peer_is_unmeasurable(db, monkeypatch):
    company = _company(db)
    monkeypatch.setattr(benchmark, "press_mentions",
                         lambda name, country=None: (_ for _ in ()).throw(RuntimeError("down")))
    result = benchmark.sync_competitor_news_benchmark(company, db)
    assert result["status"] == "error"
    assert db.query(SignalRecord).filter_by(company_id=company.id).count() == 0


def test_competitor_cap_is_three():
    assert benchmark.MAX_COMPETITORS == 3
