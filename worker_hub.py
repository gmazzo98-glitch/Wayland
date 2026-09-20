"""
Server side of the Crawler Worker: running the Node crawlers on a helper's own computer
when this app is hosted somewhere that has no Scraper/crawlers folder.

How a crawl reaches the helper's PC
-----------------------------------
scrapers/node_crawler_base.run_ts_crawler / run_node_entrypoint are the only two places
that ever spawn a crawler. When a crawl was queued for a worker, they call run_remote()
here instead: it writes a CrawlerTask row, and the worker installed on that computer
(worker/worker.mjs) claims it, runs the very same `node dist/main.js <input.csv>` command,
and posts the dataset rows back. Everything downstream of the rows (signal derivation,
SourceHealth, scoring, DB writes) is unchanged and stays on this server, which is why the
worker needs no database password — it only talks to the vienna_worker_* SQL functions
(worker/supabase_rpc.sql, applied at start by ensure_rpc()) with its own token.

Which worker a crawl uses is carried in a ContextVar (use_target), set by crawl_jobs around
each company's run. None means "run on this machine", exactly as before.

Deliberately free of any Streamlit import: it runs on crawl worker threads.
"""

import contextvars
import hashlib
import json
import secrets
import shutil
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import SCRAPER_CRAWLERS_DIR
from models import CrawlerTask, CrawlerWorker

RPC_SQL_PATH = Path(__file__).resolve().parent / "worker" / "supabase_rpc.sql"

# Bumped whenever the task format / RPC contract changes incompatibly. A worker on another
# protocol is refused (its build could misread a request); a merely older *build* of the
# same protocol is allowed to run and only flagged "update recommended".
PROTOCOL = 1

# A worker heartbeats every ~15s; missing four in a row counts as offline.
ONLINE_SECONDS = 60
POLL_SECONDS = 1.5
# How long a task may sit unclaimed before the worker is presumed unable to take it.
CLAIM_WAIT_SECONDS = 90
# Slack on top of the crawler's own timeout: claim latency + spawn + posting the rows back.
RESULT_GRACE_SECONDS = 45
TASK_RETENTION = timedelta(days=2)

# States, in the order the UI reports them.
READY, OUTDATED, BROKEN, OFFLINE, WAITING, REVOKED, INCOMPATIBLE = (
    "ready", "outdated", "broken", "offline", "waiting", "revoked", "incompatible")
USABLE_STATES = (READY, OUTDATED)


# ---- which worker a crawl runs on ---------------------------------------------------------

_target: contextvars.ContextVar = contextvars.ContextVar("crawler_target", default=None)


def current_target() -> Optional[str]:
    """The worker id the crawl running in this context should use; None = run locally."""
    return _target.get()


@contextmanager
def use_target(worker_id: Optional[str]):
    token = _target.set(worker_id)
    try:
        yield
    finally:
        _target.reset(token)


def local_crawlers_available() -> bool:
    """True when THIS machine can run the crawlers itself: the Scraper/crawlers folder is
    checked out (config.SCRAPER_CRAWLERS_DIR) and Node is installed. On a hosted deployment
    this is False, and crawls have to go to a helper's computer instead."""
    return Path(SCRAPER_CRAWLERS_DIR).is_dir() and shutil.which("node") is not None


# ---- schema / RPC bootstrap -----------------------------------------------------------------

def _rpc_marker() -> str:
    return "rpc:" + hashlib.md5(RPC_SQL_PATH.read_bytes()).hexdigest()[:12]


def ensure_rpc(engine) -> bool:
    """Creates the worker-facing SQL functions and locks the two tables down. Postgres only
    (SQLite has no RPC layer and no remote workers). Returns False — never raises — if it could
    not be applied, so a permissions hiccup can't stop the whole app from booting.

    Runs at every app start, so it must cost nothing once applied: the functions carry a
    marker comment (a hash of the SQL file), and when it matches nothing is executed at all.
    That matters because the SQL includes ALTER TABLE, which needs an exclusive lock — and
    every Streamlit session idles inside an open transaction holding a read lock on whatever
    it last queried, so DDL run needlessly would queue behind them and, worse, stall the
    workers' own queries queued behind the DDL. When it does have to apply, lock_timeout makes
    it give up after a few seconds instead of joining that queue."""
    if engine.dialect.name != "postgresql":
        return False
    try:
        marker = _rpc_marker()
        sql = RPC_SQL_PATH.read_text(encoding="utf-8")
        with engine.connect() as conn:
            cur = conn.connection.cursor()
            try:
                cur.execute("select obj_description(to_regprocedure('vienna_worker_claim(text)'), 'pg_proc')")
                applied = cur.fetchone()
            finally:
                cur.close()
            conn.rollback()
        if applied and applied[0] == marker:
            return True
        with engine.begin() as conn:
            # Straight through the DBAPI cursor: several statements, and no bind-parameter parsing
            # to trip over the $fn$ bodies.
            cur = conn.connection.cursor()
            try:
                cur.execute("set local lock_timeout = '4s'")
                cur.execute(sql)
                cur.execute(f"comment on function vienna_worker_claim(text) is '{marker}'")
            finally:
                cur.close()
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[worker_hub] could not apply {RPC_SQL_PATH.name}: {type(e).__name__}: {str(e).splitlines()[0]}")
        return False


# ---- workers ---------------------------------------------------------------------------------

def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_worker(db, name: str):
    """Registers (or re-keys) a worker and returns (worker, plain_token). The plain token
    exists only in this return value — it goes into the installer and is never stored.
    Re-installing on a computer that already has a live entry with the same name rotates
    that entry's token instead of piling up duplicates; the old install stops working."""
    name = " ".join((name or "").split())[:120]
    if not name:
        raise ValueError("Give this computer a name first.")
    token = secrets.token_urlsafe(32)
    worker = db.query(CrawlerWorker).filter_by(name=name, revoked=False).first()
    if worker:
        worker.token_hash = hash_token(token)
        worker.last_seen_at = None
        worker.info = None
    else:
        worker = CrawlerWorker(name=name, token_hash=hash_token(token))
        db.add(worker)
    db.commit()
    return worker, token


def revoke_worker(db, worker_id: str) -> None:
    worker = db.get(CrawlerWorker, worker_id)
    if worker:
        worker.revoked = True
        db.commit()


def list_workers(db, include_revoked: bool = False) -> List[CrawlerWorker]:
    q = db.query(CrawlerWorker)
    if not include_revoked:
        q = q.filter(CrawlerWorker.revoked.is_(False))
    return q.order_by(CrawlerWorker.created_at).all()


def _failed_checks(info: Dict[str, Any]) -> List[str]:
    checks = (info or {}).get("checks") or {}
    return [f"{name}: {c.get('detail') or 'failed'}" for name, c in checks.items() if not c.get("ok")]


def worker_status(worker: CrawlerWorker, expected: Optional[Dict[str, Any]] = None,
                  now: Optional[datetime] = None) -> Dict[str, Any]:
    """
    Everything the UI needs to say whether this computer can run crawls right now:
    {"state", "usable", "label", "detail", "problems", "seen_ago"}.
    `expected` is the bundle's own VERSION.json (worker_bundle.bundle_info()), so "is the
    installed scraper set the correct one" is answered by comparing build ids, not by a
    version number someone has to remember to bump.
    """
    now = now or datetime.utcnow()
    info = worker.info or {}
    seen = worker.last_seen_at
    seen_ago = (now - seen).total_seconds() if seen else None

    def result(state: str, label: str, detail: str, problems: Optional[List[str]] = None):
        return {"state": state, "usable": state in USABLE_STATES, "label": label, "detail": detail,
                "problems": problems or [], "seen_ago": seen_ago}

    if worker.revoked:
        return result(REVOKED, "Removed", "This computer was removed; its installer no longer works.")
    if seen is None:
        return result(WAITING, "Waiting for the installer",
                      "Nothing has connected yet. Run the downloaded setup file on that computer.")
    if seen_ago > ONLINE_SECONDS:
        return result(OFFLINE, "Offline",
                      "The worker isn't running (computer off, asleep, or the worker closed). "
                      "It starts by itself when that computer logs in.")
    if info.get("protocol") != PROTOCOL:
        return result(INCOMPATIBLE, "Needs re-installing",
                      "Installed with an older Vienna version that this app can no longer talk to — "
                      "download and run a fresh setup file.")
    problems = _failed_checks(info)
    if problems:
        return result(BROKEN, "Connected, but not working", "The installation's self-test failed.", problems)
    if expected and expected.get("build") and info.get("build") != expected["build"]:
        return result(OUTDATED, "Update recommended",
                      "The crawlers installed there are an older set than this app ships. "
                      "It still works; download and run a fresh setup file to update.")
    return result(READY, "Ready", "Installed, up to date and self-test passed.")


# ---- running a crawl on a worker ---------------------------------------------------------------

def _session_factory():
    from database import SessionFactory  # lazy: database imports models/config only
    return SessionFactory


def _as_dict(value):
    return json.loads(value) if isinstance(value, str) else value


def run_remote(worker_id: str, crawler: str, kind: str, request: Dict[str, Any],
               run_timeout: int, session_factory=None, poll_seconds: float = POLL_SECONDS) -> List[Dict[str, Any]]:
    """
    Queues one crawler invocation on `worker_id`, blocks until the worker posts the rows
    back, and returns them — the drop-in equivalent of the local subprocess call. Raises
    CrawlerRunError for anything that goes wrong, which the wrapper's run_adapter turns
    into the usual simulated fallback.
    """
    from scrapers.node_crawler_base import CrawlerRunError  # lazy: node_crawler_base imports this module

    factory = session_factory or _session_factory()
    db = factory()
    try:
        worker = db.get(CrawlerWorker, worker_id)
        if worker is None or worker.revoked:
            raise CrawlerRunError("The computer chosen for this crawl has been removed — pick another one.")
        status = worker_status(worker)
        if status["state"] in (OFFLINE, WAITING):
            raise CrawlerRunError(f"'{worker.name}' is {status['label'].lower()} — {status['detail']}")
        if status["state"] == INCOMPATIBLE:
            raise CrawlerRunError(f"'{worker.name}' needs to be re-installed: {status['detail']}")

        # Old finished tasks are pure clutter; sweep them here so no separate job is needed.
        db.query(CrawlerTask).filter(CrawlerTask.finished_at < datetime.utcnow() - TASK_RETENTION).delete()
        task = CrawlerTask(worker_id=worker_id, crawler=crawler, kind=kind, status="queued",
                           request={**request, "timeout": run_timeout})
        db.add(task)
        db.commit()
        task_id, worker_name = task.id, worker.name
    finally:
        db.close()

    started = time.time()
    deadline = started + CLAIM_WAIT_SECONDS + run_timeout + RESULT_GRACE_SECONDS
    while True:
        time.sleep(poll_seconds)
        db = factory()
        try:
            row = db.query(CrawlerTask.status, CrawlerTask.result, CrawlerTask.error).filter_by(id=task_id).first()
            if row is None:
                raise CrawlerRunError("The crawl task disappeared from the queue.")
            status, result, error = row
            if status == "done":
                return list((_as_dict(result) or {}).get("rows") or [])
            if status == "error":
                raise CrawlerRunError(error or f"{crawler} failed on '{worker_name}'")
            if status == "cancelled":
                raise CrawlerRunError(f"{crawler} was cancelled")

            waited = time.time() - started
            unclaimed_too_long = status == "queued" and waited > CLAIM_WAIT_SECONDS
            if unclaimed_too_long or time.time() > deadline:
                db.query(CrawlerTask).filter(CrawlerTask.id == task_id,
                                             CrawlerTask.status.in_(("queued", "running"))
                                             ).update({"status": "cancelled", "finished_at": datetime.utcnow(),
                                                       "error": "gave up waiting"}, synchronize_session=False)
                db.commit()
                if unclaimed_too_long:
                    raise CrawlerRunError(f"'{worker_name}' did not pick up the {crawler} task within "
                                          f"{CLAIM_WAIT_SECONDS}s — is the worker running there?")
                raise CrawlerRunError(f"{crawler} on '{worker_name}' did not report back within "
                                      f"{run_timeout + RESULT_GRACE_SECONDS}s")
        finally:
            db.close()

