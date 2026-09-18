"""
run_phase7_batch: several companies' Phase 7 crawler runs in flight at once, each
worker on its own DB session. No crawler is spawned — the per-company sync is
stubbed — and the configured DATABASE_URL is never touched (in-memory SQLite
behind a StaticPool so every thread's session shares the one connection).
"""

import threading
import time

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import company_service
from models import Base, Company


def _factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    s = factory()
    for i in range(5):
        s.add(Company(id=f"c{i}", legal_name=f"Company {i} S.R.L.", registration_number=f"REG-{i}", country="Italy"))
    s.commit()
    s.close()
    return factory


def test_batch_runs_companies_in_parallel_on_separate_sessions(monkeypatch):
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
        ["c0", "c1", "c2", "c3", "c4"], max_workers=5, session_factory=_factory(),
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


def test_batch_isolates_a_company_whose_run_blows_up(monkeypatch):
    def fake_sync(company, db, phases=None):
        if company.id == "c1":
            raise RuntimeError("crawler folder missing")
        return {"ok": True}

    monkeypatch.setattr(company_service, "sync_company_applicable_sources", fake_sync)
    results = company_service.run_phase7_batch(["c0", "c1", "c2", "missing"], max_workers=2, session_factory=_factory())
    assert results["c0"] == {"ok": True} and results["c2"] == {"ok": True}
    assert "RuntimeError: crawler folder missing" in results["c1"]["error"]
    assert results["missing"]["error"] == "company not found"


def test_batch_with_no_companies_is_a_noop():
    assert company_service.run_phase7_batch([], session_factory=_factory()) == {}
