"""
Crawler Worker: the server side (worker_hub), the crawl routing, and the setup file the
helper downloads. No real crawler, worker or Supabase is involved — a throwaway SQLite file
stands in for the database and a thread plays the worker by writing its result straight into
the task row (the vienna_worker_* SQL functions themselves are Postgres-only and are covered
by the live end-to-end check, not here).
"""

import base64
import io
import json
import threading
import time
import zipfile
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import worker_hub
import worker_installer
from models import Base, CrawlerTask, CrawlerWorker
from scrapers import node_crawler_base
from scrapers.node_crawler_base import CrawlerRunError

EXPECTED = {"build": "abc123", "version": "1.0.0", "crawlers": ["a"]}


@pytest.fixture
def factory(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'hub.db').as_posix()}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def db(factory):
    s = factory()
    yield s
    s.close()


def _live_worker(db, name="Anna's laptop", **info):
    worker, token = worker_hub.create_worker(db, name)
    worker.last_seen_at = datetime.utcnow()
    worker.info = {"protocol": worker_hub.PROTOCOL, "build": EXPECTED["build"], "version": "1.0.0",
                   "checks": {"node": {"ok": True, "detail": "Node 22"}}, **info}
    db.commit()
    return worker, token


# ---- registration ------------------------------------------------------------------------------

def test_token_is_only_stored_hashed(db):
    worker, token = worker_hub.create_worker(db, "  Anna's   laptop ")
    assert worker.name == "Anna's laptop"
    assert token not in (worker.token_hash, worker.name)
    assert worker.token_hash == worker_hub.hash_token(token) and len(worker.token_hash) == 64


def test_reinstalling_under_the_same_name_rotates_the_token_instead_of_duplicating(db):
    first, t1 = worker_hub.create_worker(db, "Anna's laptop")
    first.last_seen_at = datetime.utcnow()
    second, t2 = worker_hub.create_worker(db, "Anna's laptop")
    assert second.id == first.id and t1 != t2
    assert second.token_hash == worker_hub.hash_token(t2)
    assert second.last_seen_at is None  # the old install stopped working; the new one hasn't reported yet
    assert len(worker_hub.list_workers(db)) == 1


def test_a_name_is_required(db):
    with pytest.raises(ValueError):
        worker_hub.create_worker(db, "   ")


# ---- "is the right thing installed?" -------------------------------------------------------------

def test_status_walks_through_every_state(db):
    fresh, _ = worker_hub.create_worker(db, "fresh")
    assert worker_hub.worker_status(fresh, EXPECTED)["state"] == worker_hub.WAITING

    ok, _ = _live_worker(db, "ok")
    s = worker_hub.worker_status(ok, EXPECTED)
    assert (s["state"], s["usable"]) == (worker_hub.READY, True)

    old, _ = _live_worker(db, "old", build="oldbuild")
    s = worker_hub.worker_status(old, EXPECTED)
    assert (s["state"], s["usable"]) == (worker_hub.OUTDATED, True)  # works, but flagged

    broken, _ = _live_worker(db, "broken", checks={"browser": {"ok": False, "detail": "the headless browser is not installed"}})
    s = worker_hub.worker_status(broken, EXPECTED)
    assert (s["state"], s["usable"]) == (worker_hub.BROKEN, False)
    assert "browser" in s["problems"][0]

    other, _ = _live_worker(db, "other", protocol=99)
    assert worker_hub.worker_status(other, EXPECTED)["state"] == worker_hub.INCOMPATIBLE

    gone, _ = _live_worker(db, "gone")
    gone.last_seen_at = datetime.utcnow() - timedelta(seconds=worker_hub.ONLINE_SECONDS + 30)
    s = worker_hub.worker_status(gone, EXPECTED)
    assert (s["state"], s["usable"]) == (worker_hub.OFFLINE, False)

    worker_hub.revoke_worker(db, ok.id)
    assert worker_hub.worker_status(ok, EXPECTED)["state"] == worker_hub.REVOKED
    assert ok.id not in [w.id for w in worker_hub.list_workers(db)]


def test_without_a_bundle_to_compare_against_an_older_build_is_not_flagged(db):
    old, _ = _live_worker(db, "old", build="whatever")
    assert worker_hub.worker_status(old, None)["state"] == worker_hub.READY


# ---- routing -------------------------------------------------------------------------------------

def test_target_defaults_to_local_and_is_scoped_to_the_context():
    assert worker_hub.current_target() is None
    with worker_hub.use_target("w1"):
        assert worker_hub.current_target() == "w1"
    assert worker_hub.current_target() is None


def test_target_survives_the_adapter_thread_pool():
    # run_adapter executes fetch_live on a pool thread; the crawl's target has to arrive there too.
    from adapters.base import _call_with_hard_timeout
    with worker_hub.use_target("w1"):
        assert _call_with_hard_timeout(lambda c: worker_hub.current_target(), object(), timeout=5) == "w1"
    assert _call_with_hard_timeout(lambda c: worker_hub.current_target(), object(), timeout=5) is None


def test_a_targeted_crawl_goes_to_the_worker_and_never_spawns_locally(monkeypatch):
    sent = {}
    monkeypatch.setattr(worker_hub, "run_remote",
                        lambda wid, name, kind, request, timeout: sent.update(wid=wid, name=name, kind=kind,
                                                                            request=request, timeout=timeout) or [{"ok": 1}])
    monkeypatch.setattr(node_crawler_base, "ensure_built", lambda *a, **k: pytest.fail("spawned locally"))
    with worker_hub.use_target("w1"):
        rows = node_crawler_base.run_ts_crawler("digital-maturity-crawler",
                                                [{"company_id": "c1", "homepage_url": "https://x.de"}],
                                                extra_args=["--flag"], env_overrides={"NEWSAPI_KEY": "k", "N": 5},
                                                run_timeout=77)
    assert rows == [{"ok": 1}]
    assert (sent["wid"], sent["name"], sent["kind"], sent["timeout"]) == ("w1", "digital-maturity-crawler", "csv", 77)
    assert sent["request"]["input_csv"].splitlines()[0] == "company_id,homepage_url"
    assert sent["request"]["extra_args"] == ["--flag"]
    # Subprocess env values are always strings, and every crawler is told to hand back what it has
    # 30s before its 77s kill timer (SOFT_DEADLINE_SECONDS) — see node_crawler_base.
    assert sent["request"]["env"] == {"SOFT_DEADLINE_SECONDS": "47", "NEWSAPI_KEY": "k", "N": "5"}


def test_the_node_entrypoint_crawler_routes_too(monkeypatch):
    sent = {}
    monkeypatch.setattr(worker_hub, "run_remote", lambda wid, name, kind, request, timeout: sent.update(kind=kind, request=request) or [])
    with worker_hub.use_target("w1"):
        node_crawler_base.run_node_entrypoint("linkedin-profile-crawler", "src/main.mjs", ["https://li/x"])
    assert sent["kind"] == "node"
    assert sent["request"]["entry"] == "src/main.mjs" and sent["request"]["cli_args"] == ["https://li/x"]


# ---- running a crawl on a worker ---------------------------------------------------------------------

def _play_worker(factory, outcome, delay=0.15):
    """Stands in for the worker: waits for the queued task, then finishes it the way the SQL function would."""
    def run():
        for _ in range(100):
            s = factory()
            task = s.query(CrawlerTask).filter_by(status="queued").first()
            if task:
                time.sleep(delay)
                task.status = "running"
                s.commit()
                task.status, task.result, task.error = outcome
                task.request = {k: v for k, v in task.request.items() if k != "env"}
                task.finished_at = datetime.utcnow()
                s.commit()
                s.close()
                return
            s.close()
            time.sleep(0.05)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def test_run_remote_returns_the_rows_the_worker_posts_back(factory, db):
    worker, _ = _live_worker(db)
    _play_worker(factory, ("done", {"rows": [{"company_id": "c1", "n": 3}]}, None))
    rows = worker_hub.run_remote(worker.id, "job-postings-crawler", "csv",
                                 {"input_csv": "company_id\r\nc1\r\n", "env": {"LLM_API_KEY": "secret"}},
                                 run_timeout=30, session_factory=factory, poll_seconds=0.05)
    assert rows == [{"company_id": "c1", "n": 3}]
    task = db.query(CrawlerTask).one()
    db.refresh(task)
    assert task.status == "done" and "env" not in task.request  # keys don't outlive the run


def test_run_remote_raises_the_workers_own_error(factory, db):
    worker, _ = _live_worker(db)
    _play_worker(factory, ("error", None, "review-crawler exited 1: boom"))
    with pytest.raises(CrawlerRunError, match="exited 1: boom"):
        worker_hub.run_remote(worker.id, "review-crawler", "csv", {"input_csv": "x"}, 30,
                              session_factory=factory, poll_seconds=0.05)


def test_run_remote_refuses_an_offline_or_unknown_worker_without_queueing(factory, db):
    worker, _ = _live_worker(db)
    worker.last_seen_at = datetime.utcnow() - timedelta(minutes=10)
    db.commit()
    with pytest.raises(CrawlerRunError, match="offline"):
        worker_hub.run_remote(worker.id, "x", "csv", {}, 30, session_factory=factory, poll_seconds=0.05)
    with pytest.raises(CrawlerRunError, match="removed"):
        worker_hub.run_remote("nope", "x", "csv", {}, 30, session_factory=factory, poll_seconds=0.05)
    assert db.query(CrawlerTask).count() == 0


def test_run_remote_gives_up_and_cancels_a_task_nobody_picks_up(factory, db, monkeypatch):
    monkeypatch.setattr(worker_hub, "CLAIM_WAIT_SECONDS", 0.3)
    worker, _ = _live_worker(db)
    with pytest.raises(CrawlerRunError, match="did not pick up"):
        worker_hub.run_remote(worker.id, "x", "csv", {}, 30, session_factory=factory, poll_seconds=0.05)
    task = db.query(CrawlerTask).one()
    db.refresh(task)
    assert task.status == "cancelled"  # so a worker that wakes up later won't run a stale crawl


# ---- the job manager carries the target through -----------------------------------------------------------

def test_the_job_manager_runs_each_company_on_its_own_target(monkeypatch):
    from crawl_jobs import CrawlJobManager
    seen, sized = {}, []
    # Sizing the slots reads the worker's row from the database; that has its own tests below.
    monkeypatch.setattr(worker_hub, "apply_worker_capacity", lambda wid, factory=None: sized.append(wid))

    def runner(cid, session_factory=None, on_step=None, phases=None):
        seen[cid] = worker_hub.current_target()
        return cid, cid, {}

    m = CrawlJobManager(run_company=runner)
    m.submit({"a": "A"}, workers=1, target="w1")
    m.wait(10)
    m.submit({"b": "B"}, workers=1)
    m.wait(10)
    assert seen == {"a": "w1", "b": None}
    assert sized == ["w1"], "only a crawl queued for a worker sizes that worker's slots"


def _worker_db(tmp_path, info):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from models import Base, CrawlerWorker
    tmp_path.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{(tmp_path / 'w.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    s = factory()
    s.add(CrawlerWorker(id="w1", name="Friend PC", token_hash="x", info=info))
    s.commit()
    s.close()
    return factory


def test_a_workers_reported_capacity_sizes_the_app_side_slots(tmp_path):
    import resource_governor
    resource_governor.reset_for_tests()
    worker_hub._capacity_checked.clear()
    worker_hub.apply_worker_capacity("w1", _worker_db(tmp_path, {"max_parallel": 2, "protocol": 1}))
    gov = resource_governor.get_governor("w1")
    assert gov.capacity("process") == 2 and gov.capacity("browser") == 2
    resource_governor.reset_for_tests()


def test_capacity_is_only_reread_once_a_minute_and_a_failure_never_raises(tmp_path, monkeypatch):
    import resource_governor
    resource_governor.reset_for_tests()
    worker_hub._capacity_checked.clear()
    factory = _worker_db(tmp_path, {"max_parallel": 3})
    worker_hub.apply_worker_capacity("w1", factory)
    assert resource_governor.get_governor("w1").capacity("process") == 3
    # Within the refresh window the database is not asked again.
    worker_hub.apply_worker_capacity("w1", lambda: pytest.fail("re-read too soon"))
    # Past it, a database that errors leaves the previous capacity in place and does not raise.
    worker_hub._capacity_checked["w1"] -= worker_hub.CAPACITY_REFRESH_SECONDS + 1

    def broken():
        raise RuntimeError("db down")

    worker_hub.apply_worker_capacity("w1", broken)
    assert resource_governor.get_governor("w1").capacity("process") == 3
    resource_governor.reset_for_tests()


def test_a_worker_that_has_reported_nothing_keeps_the_default_capacity(tmp_path):
    import resource_governor
    resource_governor.reset_for_tests()
    worker_hub._capacity_checked.clear()
    # No heartbeat yet (info is empty) or an old build that never sent max_parallel.
    worker_hub.apply_worker_capacity("w1", _worker_db(tmp_path, None))
    assert resource_governor.get_governor("w1").capacity("process") == resource_governor.default_capacities()["process"]
    worker_hub._capacity_checked.clear()
    worker_hub.apply_worker_capacity("missing-worker", _worker_db(tmp_path / "second", {}))
    resource_governor.reset_for_tests()


# ---- the setup file ---------------------------------------------------------------------------------------

@pytest.mark.skipif(not worker_installer.bundle_available(), reason="run scripts/build_worker_bundle.py first")
def test_the_setup_file_is_self_contained_and_personal_to_one_computer():
    config = worker_installer.make_config("Anna's laptop", "tok-123", "https://x.supabase.co", "sb_publishable_x")
    bat = worker_installer.build_setup_bat(config)
    text = bat.decode("ascii")  # must be pure ASCII: it is a batch file read under any code page
    assert text.startswith("@echo off\r\n") and "\n##PS1\r\n" in text and "\n##ZIP\r\n" in text

    # The stub never spells a marker out whole, or it would find itself instead of the real one.
    stub = text[:text.index("\n##PS1")]
    assert "##PS1" not in stub and "##ZIP" not in stub

    ps1 = text[text.index("\n##PS1") + 7:text.index("\n##ZIP")]
    assert "Vienna Crawler Setup" in ps1 and "Invoke-Tool" in ps1

    payload = base64.b64decode("".join(text[text.index("\n##ZIP") + 6:].split()))
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        names = set(z.namelist())
        assert {"VERSION.json", "worker.config.json", "worker/worker.mjs", "crawlers/package-lock.json"} <= names
        cfg = json.loads(z.read("worker.config.json"))
        info = json.loads(z.read("VERSION.json"))
    assert cfg["token"] == "tok-123" and cfg["name"] == "Anna's laptop"
    assert len(info["crawlers"]) == 8 and info["build"] == worker_installer.bundle_info()["build"]

    # Nothing from the Vienna app itself ships — only the crawlers and the worker.
    assert not any(n.startswith(("views/", "scrapers/", "adapters/")) or n.endswith((".py", ".db", ".env")) for n in names)
    assert not any("node_modules" in n or n.endswith(".sql") for n in names)


@pytest.mark.skipif(not worker_installer.bundle_available(), reason="run scripts/build_worker_bundle.py first")
def test_the_zip_fallback_wraps_the_same_setup_file():
    config = worker_installer.make_config("x", "t", "https://x", "k")
    with zipfile.ZipFile(io.BytesIO(worker_installer.build_setup_zip(config))) as z:
        assert z.namelist() == [worker_installer.SETUP_BAT_NAME]
        assert z.read(worker_installer.SETUP_BAT_NAME).startswith(b"@echo off")


def test_the_installer_script_is_plain_ascii():
    # It is embedded in a .bat; a stray typographic dash would corrupt under some code pages.
    worker_installer.INSTALL_PS1.read_text(encoding="ascii")


def test_the_rpc_bootstrap_is_skipped_on_sqlite_and_keyed_to_the_sql_file(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'x.db').as_posix()}")
    assert worker_hub.ensure_rpc(engine) is False  # no RPC layer, no remote workers, no error
    first = worker_hub._rpc_marker()
    assert first == worker_hub._rpc_marker() and first.startswith("rpc:")
    sql = tmp_path / "other.sql"
    sql.write_text("-- changed", encoding="utf-8")
    monkeypatch.setattr(worker_hub, "RPC_SQL_PATH", sql)
    assert worker_hub._rpc_marker() != first  # editing the SQL is what triggers a re-apply
