"""Coverage selection and queue draining on a file-backed SQLite database."""

from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base, Company, CompanySourceRun, SignalRecord
from scripts.coverage_sweep import select_backlog, sweep


def _factory(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'sweep.db').as_posix()}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _covered(db, cid, when, phases=(1, 4, 7)):
    if 1 in phases:
        db.add(SignalRecord(company_id=cid, signal_key="patent_count", source="EPO OPS",
                            status="absent", is_simulated=False))
    if 4 in phases:
        db.add(SignalRecord(company_id=cid, signal_key="external_collaboration", source="Google News RSS",
                            status="absent", is_simulated=False))
    if 7 in phases:
        for source in ("Directory Listing Crawler", "TED Contract Awards Crawler"):
            db.add(CompanySourceRun(company_id=cid, source_name=source, phase=7,
                                    status="live", finished_at=when))


def test_backlog_includes_old_phase7_and_missing_sources(tmp_path):
    factory = _factory(tmp_path)
    now = datetime(2026, 10, 10, 12)
    with factory() as db:
        for cid in ("fresh", "old", "missing"):
            db.add(Company(id=cid, legal_name=cid, registration_number=cid, country="Italy"))
        db.flush()
        _covered(db, "fresh", now - timedelta(days=2))
        _covered(db, "old", now - timedelta(days=61))
        db.add(SignalRecord(company_id="missing", signal_key="patent_count", source="EPO OPS",
                            status="absent", is_simulated=True))
        db.add(CompanySourceRun(company_id="missing", source_name="Directory Listing Crawler",
                                phase=7, status="skipped", finished_at=now))
        db.commit()
        companies = db.query(Company).order_by(Company.id).all()
        assert select_backlog(db, companies, now=now) == {
            "old": (7,), "missing": (1, 4, 7),
        }


def test_sweep_resubmits_bounded_batches_until_drained(tmp_path):
    factory = _factory(tmp_path)
    with factory() as db:
        for i in range(5):
            cid = f"c{i}"
            db.add(Company(id=cid, legal_name=cid, registration_number=cid, country="Italy"))
        db.commit()

    class CompletingManager:
        def __init__(self):
            self.calls = []

        def snapshot(self):
            return None

        def submit(self, companies, workers, phases):
            self.calls.append((tuple(companies), phases, workers))
            with factory() as db:
                for cid in companies:
                    _covered(db, cid, datetime.utcnow(), phases)
                db.commit()
            return type("Result", (), {"stopping": False, "added": len(companies)})()

        def wait(self, timeout):
            return True

    manager = CompletingManager()
    remaining, submitted = sweep(batch_size=2, workers=2, manager=manager,
                                 session_factory=factory, max_minutes=10)
    assert (remaining, submitted) == (0, 15)
    assert [len(ids) for ids, _, _ in manager.calls] == [2, 2, 1] * 3
    assert [phases for _, phases, _ in manager.calls] == [(1,)] * 3 + [(4,)] * 3 + [(7,)] * 3
    assert all(workers == 2 for _, _, workers in manager.calls)


def test_short_budget_does_not_start_a_batch(tmp_path):
    factory = _factory(tmp_path)
    with factory() as db:
        db.add(Company(id="c", legal_name="Company", registration_number="C", country="Italy"))
        db.commit()

    class NoSubmission:
        def snapshot(self):
            return None

        def submit(self, *args, **kwargs):
            raise AssertionError("batch should not start with insufficient time")

    remaining, submitted = sweep(batch_size=1, workers=1, manager=NoSubmission(),
                                 session_factory=factory, max_minutes=0.01)
    assert (remaining, submitted) == (1, 0)
