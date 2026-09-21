"""
run_phase7_batch / run_company_phases: several companies AND each company's own sources in flight
at once, every source on its own DB session. No crawler is spawned — the eight per-source syncs
are stubbed — and the configured DATABASE_URL is never touched: a throwaway SQLite FILE in the
test's tmp dir, so each thread gets its own connection (an in-memory DB behind a StaticPool
shares one connection across threads, which intermittently returned no rows under concurrency).
"""

import threading
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import company_service
from models import Base, Company

PHASE7_SOURCES = [
    (company_service.company_website_crawler, "sync_company_website", "Company Website Crawler"),
    (company_service.job_postings_crawler, "sync_job_postings", "Job Postings Crawler"),
    (company_service.review_crawler, "sync_reviews", "Review Crawler"),
    (company_service.news_signals_crawler, "sync_news_signals", "News Signals Crawler"),
    (company_service.directory_listing_crawler, "sync_directory_listing", "Directory Listing Crawler"),
    (company_service.innovation_participation_crawler, "sync_innovation_participation", "Innovation Participation Crawler"),
    (company_service.digital_maturity_crawler, "sync_digital_maturity", "Digital Maturity Crawler"),
    (company_service.linkedin_profile_crawler, "sync_linkedin_profiles", "LinkedIn Profile Crawler"),
]


@pytest.fixture
def factory(tmp_path):
    return _factory(tmp_path)


def _factory(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'batch.db').as_posix()}", connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    s = factory()
    for i in range(5):
        s.add(Company(id=f"c{i}", legal_name=f"Company {i} S.R.L.", registration_number=f"REG-{i}", country="Italy"))
    s.commit()
    s.close()
    return factory


def _stub_crawlers(monkeypatch, behaviour):
    """Replaces every Phase 7 sync with behaviour(source_name, company, db) -> result."""
    for module, fn, source in PHASE7_SOURCES:
        monkeypatch.setattr(module, fn, lambda company, db, _s=source: behaviour(_s, company, db))


def _ok(source, company, db):
    return {"status": "success", "mode": "live"}


def test_batch_runs_companies_in_parallel_on_separate_sessions(monkeypatch, factory):
    seen = {}
    lock = threading.Lock()

    def behaviour(source, company, db):
        with lock:
            seen[(company.id, source)] = (threading.current_thread().name, db)  # keep it alive: a freed session's id() is reused
        if source == "Job Postings Crawler":
            time.sleep(0.2)
        return {"status": "success", "mode": "live"}

    _stub_crawlers(monkeypatch, behaviour)
    ticks = []
    t0 = time.time()
    results = company_service.run_phase7_batch(
        ["c0", "c1", "c2", "c3", "c4"], max_workers=5, session_factory=factory,
        progress_cb=lambda done, total, name: ticks.append((done, total, name)),
    )
    elapsed = time.time() - t0

    assert set(results) == {"c0", "c1", "c2", "c3", "c4"}
    assert all(set(r) == {s for _, _, s in PHASE7_SOURCES} for r in results.values())
    assert all(r["Job Postings Crawler"] == {"status": "success", "mode": "live"} for r in results.values())
    assert elapsed < 0.8, f"5 x 0.2s should overlap, took {elapsed:.2f}s"
    assert len({thread for thread, _ in seen.values()}) > 1, "work never left the calling thread"
    assert len({id(session) for _, session in seen.values()}) == 5 * 8, "no two sources may share a session"
    assert [t[0] for t in ticks] == [1, 2, 3, 4, 5] and all(t[1] == 5 for t in ticks)
    assert all(name.startswith("Company ") for _, _, name in ticks)


def test_one_companys_sources_run_at_the_same_time(monkeypatch, factory):
    """The point of the change: a company takes as long as its slowest source, not the sum."""
    running, peak, lock = [0], [0], threading.Lock()

    def behaviour(source, company, db):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.3)
        with lock:
            running[0] -= 1
        return {"status": "success"}

    _stub_crawlers(monkeypatch, behaviour)
    t0 = time.time()
    cid, name, results = company_service.run_phase7_for_company("c0", factory)
    elapsed = time.time() - t0

    assert (cid, name) == ("c0", "Company 0 S.R.L.")
    assert len(results) == 8 and all(r == {"status": "success"} for r in results.values())
    assert peak[0] == 8, "all eight sources should have been in flight together"
    assert elapsed < 1.2, f"8 x 0.3s sequentially is 2.4s; concurrently ~0.3s — took {elapsed:.2f}s"


def test_results_keep_the_plan_order_whatever_order_the_sources_finish_in(monkeypatch, factory):
    def behaviour(source, company, db):
        time.sleep(0.25 if source == "Company Website Crawler" else 0.01)  # the first-listed finishes last
        return {"status": "success"}

    _stub_crawlers(monkeypatch, behaviour)
    _, _, results = company_service.run_phase7_for_company("c0", factory)
    assert list(results) == [source for _, _, source in PHASE7_SOURCES]


def test_a_failing_source_is_recorded_and_does_not_stop_the_others(monkeypatch, factory):
    def behaviour(source, company, db):
        if source == "Digital Maturity Crawler" and company.id == "c1":
            raise RuntimeError("crawler folder missing")
        return {"status": "success"}

    _stub_crawlers(monkeypatch, behaviour)
    results = company_service.run_phase7_batch(["c0", "c1", "c2", "missing"], max_workers=2, session_factory=factory)
    assert all(r["Digital Maturity Crawler"] == {"status": "success"} for r in (results["c0"], results["c2"]))
    failed = results["c1"]["Digital Maturity Crawler"]
    assert failed["status"] == "error" and "RuntimeError: crawler folder missing" in failed["error"]
    assert sum(1 for r in results["c1"].values() if r.get("status") == "success") == 7, "the other 7 must still have run"
    assert results["missing"]["error"] == "company not found"


def test_each_source_gets_a_session_that_keeps_its_attributes_across_commits(monkeypatch, factory):
    """A default session expires everything on commit, so a crawler thread's first read of
    company.website_url opened a transaction that pinned a pooled connection for the whole crawl."""
    flags = []

    def behaviour(source, company, db):
        flags.append(db.expire_on_commit)
        return {"status": "success"}

    _stub_crawlers(monkeypatch, behaviour)
    company_service.run_phase7_for_company("c0", factory)
    assert flags == [False] * 8


def test_batch_with_no_companies_is_a_noop(factory):
    assert company_service.run_phase7_batch([], session_factory=factory) == {}


def test_phase7_reports_each_crawler_before_it_starts(monkeypatch, factory):
    """The sequential path (company creation, one-off buttons) keeps its one-at-a-time contract."""
    calls = []
    for module, fn, _ in PHASE7_SOURCES:
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


def test_the_hook_reports_every_start_and_end_with_the_number_finished(monkeypatch, factory):
    _stub_crawlers(monkeypatch, _ok)
    events, lock = [], threading.Lock()

    def hook(index, total, name, event="start"):
        with lock:
            events.append((event, index, total, name))

    company_service.run_phase7_for_company("c0", factory, on_step=hook)
    starts = [e for e in events if e[0] == "start"]
    dones = [e for e in events if e[0] == "done"]
    assert len(starts) == 8 and len(dones) == 8 and all(e[2] == 8 for e in events)
    assert sorted(e[1] for e in dones) == list(range(1, 9)), "each end reports one more finished"
    assert {e[3] for e in starts} == {s for _, _, s in PHASE7_SOURCES}
    # The slowest-first ordering: the website and digital-maturity crawlers start before the quick ones.
    assert {starts[0][3], starts[1][3]} == {"Company Website Crawler", "Digital Maturity Crawler"}


def test_run_phase7_for_company_works_without_a_hook(monkeypatch, factory):
    _stub_crawlers(monkeypatch, _ok)
    cid, name, results = company_service.run_phase7_for_company("c0", factory)
    assert (cid, name) == ("c0", "Company 0 S.R.L.") and len(results) == 8


def test_parallel_false_falls_back_to_the_sequential_path(monkeypatch, factory):
    order = []
    _stub_crawlers(monkeypatch, lambda source, company, db: order.append(source) or {"status": "success"})
    company_service.run_company_phases("c0", [7], factory, parallel=False)
    assert order == [s for _, _, s in PHASE7_SOURCES]


def test_phase_one_computation_runs_after_all_of_its_sources_have_finished(monkeypatch, factory):
    """Revenue growth vs sector reads what the Eurostat step writes, so it must not start until the
    concurrent steps are all done."""
    finished, lock = [], threading.Lock()

    def make(name, delay):
        def sync(company, db):
            time.sleep(delay)
            with lock:
                finished.append(name)
            return {"status": "success"}
        return sync

    monkeypatch.setattr(company_service.epo_ops, "sync_company_patents", make("EPO OPS", 0.05))
    monkeypatch.setattr(company_service.euipo, "sync_company_trademarks", make("EUIPO", 0.01))
    monkeypatch.setattr(company_service.eu_funding, "sync_company_grants", make("EU Funding Portal", 0.01))
    monkeypatch.setattr(company_service.eurostat_sector_growth, "sync_sector_growth_benchmark", make("Eurostat", 0.15))
    at_compute = []
    monkeypatch.setattr(company_service, "compute_revenue_growth_vs_sector",
                        lambda db, company: at_compute.append(list(finished)))

    _, _, results = company_service.run_company_phases("c0", [1], factory)
    assert list(results) == ["EPO OPS", "EUIPO", "EU Funding Portal", "Eurostat Sector Growth"]  # Italy: no German sources
    assert len(at_compute) == 1 and sorted(at_compute[0]) == ["EPO OPS", "EU Funding Portal", "EUIPO", "Eurostat"]


def test_source_plan_is_country_aware():
    de = Company(id="d", legal_name="X GmbH", registration_number="HRB 1", country="Germany")
    it = Company(id="i", legal_name="X SRL", registration_number="REG", country="Italy")
    de_steps, _ = company_service.plan_source_steps(de, [1, 2, 4, 7])
    it_steps, _ = company_service.plan_source_steps(it, [1, 2, 4, 7])
    de_names, it_names = [s.name for s in de_steps], [s.name for s in it_steps]
    assert {"Destatis", "Arbeitsagentur", "Handelsregister"} <= set(de_names)
    assert not ({"Destatis", "Arbeitsagentur", "Handelsregister"} & set(it_names))
    assert len([s for s in it_steps if s.phase == 7]) == 8
