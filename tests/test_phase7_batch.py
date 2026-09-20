"""
run_phase7_batch: several companies' Phase 7 crawler runs in flight at once, each
worker on its own DB session. No crawler is spawned — the per-company sync is
stubbed — and the configured DATABASE_URL is never touched: a throwaway SQLite
FILE in the test's tmp dir, so each worker thread gets its own connection (an
in-memory DB behind a StaticPool shares one connection across threads, which
intermittently returned no rows under 5 concurrent workers).
"""

import threading
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import company_service
from models import Base, Company


@pytest.fixture
def factory(tmp_path):
    return _factory(tmp_path)


def _factory(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'batch.db').as_posix()}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    s = factory()
    for i in range(5):
        s.add(Company(id=f"c{i}", legal_name=f"Company {i} S.R.L.", registration_number=f"REG-{i}", country="Italy"))
    s.commit()
    s.close()
    return factory


def test_batch_runs_companies_in_parallel_on_separate_sessions(monkeypatch, factory):
    seen = {}
    lock = threading.Lock()

    def fake_sync(company, db, phases=None):
        assert phases == [7]
        with lock:
            seen[company.id] = (threading.current_thread().name, id(db))
        time.sleep(0.2)
        return {"Job Postings Crawler": {"status": "success"}}

    monkeypatch.setattr(company_service, "sync_company_applicable_sources", fake_sync)
    ticks = []
    t0 = time.time()
    results = company_service.run_phase7_batch(
        ["c0", "c1", "c2", "c3", "c4"], max_workers=5, session_factory=factory,
        progress_cb=lambda done, total, name: ticks.append((done, total, name)),
    )
    elapsed = time.time() - t0

    assert set(results) == {"c0", "c1", "c2", "c3", "c4"}
    assert all(r == {"Job Postings Crawler": {"status": "success"}} for r in results.values())
    assert elapsed < 0.8, f"5 x 0.2s should overlap, took {elapsed:.2f}s"
    assert len({thread for thread, _ in seen.values()}) > 1, "work never left the calling thread"
    assert len({session for _, session in seen.values()}) == 5, "workers must not share a session"
    assert [t[0] for t in ticks] == [1, 2, 3, 4, 5] and all(t[1] == 5 for t in ticks)
    assert all(name.startswith("Company ") for _, _, name in ticks)


def test_batch_isolates_a_company_whose_run_blows_up(monkeypatch, factory):
    def fake_sync(company, db, phases=None):
        if company.id == "c1":
            raise RuntimeError("crawler folder missing")
        return {"ok": True}

    monkeypatch.setattr(company_service, "sync_company_applicable_sources", fake_sync)
    results = company_service.run_phase7_batch(["c0", "c1", "c2", "missing"], max_workers=2, session_factory=factory)
    assert results["c0"] == {"ok": True} and results["c2"] == {"ok": True}
    assert "RuntimeError: crawler folder missing" in results["c1"]["error"]
    assert results["missing"]["error"] == "company not found"


def test_batch_with_no_companies_is_a_noop(factory):
    assert company_service.run_phase7_batch([], session_factory=factory) == {}


def test_phase7_reports_each_crawler_before_it_starts(monkeypatch, factory):
    calls = []
    for module, fn in [
        (company_service.company_website_crawler, "sync_company_website"),
        (company_service.job_postings_crawler, "sync_job_postings"),
        (company_service.review_crawler, "sync_reviews"),
        (company_service.news_signals_crawler, "sync_news_signals"),
        (company_service.directory_listing_crawler, "sync_directory_listing"),
        (company_service.innovation_participation_crawler, "sync_innovation_participation"),
        (company_service.digital_maturity_crawler, "sync_digital_maturity"),
        (company_service.linkedin_profile_crawler, "sync_linkedin_profiles"),
    ]:
        monkeypatch.setattr(module, fn, lambda company, db, _n=fn: calls.append(("ran", _n)) or {"status": "success"})

    steps = []
    session = factory()
    company = session.query(Company).first()
    results = company_service.sync_company_applicable_sources(
        company, session, phases=[7], on_step=lambda i, n, name: steps.append((i, n, name)) or calls.append(("step", i)),
    )
    session.close()

    assert [s[0] for s in steps] == list(range(8)) and all(s[1] == 8 for s in steps)
    assert steps[0][2] == "Company Website Crawler" and steps[-1][2] == "LinkedIn Profile Crawler"
    assert list(results) == [s[2] for s in steps]
    # The hook fires BEFORE each crawler: step i is reported, then that crawler runs.
    assert calls[0] == ("step", 0) and calls[1][0] == "ran" and calls[2] == ("step", 1)


def test_run_phase7_for_company_only_passes_the_hook_when_given(monkeypatch, factory):
    seen = []

    def fake_sync(company, db, phases=None, **kwargs):
        seen.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(company_service, "sync_company_applicable_sources", fake_sync)
    hook = lambda i, n, name: None  # noqa: E731
    assert company_service.run_phase7_for_company("c0", factory) == ("c0", "Company 0 S.R.L.", {"ok": True})
    company_service.run_phase7_for_company("c1", factory, on_step=hook)
    assert seen == [{}, {"on_step": hook}]
