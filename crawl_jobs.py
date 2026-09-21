"""
Background execution of Phase 7 deep crawls.

A Streamlit page runs on the session's script thread, and any click on another
tab or widget interrupts that run. A crawl started from a button used to run
inside the page's own run, so it stopped being visible (and its progress bar
died) the moment you looked at another page — and it froze the page meanwhile.
Crawls now run on plain daemon threads owned by one process-wide manager; the UI
only READS snapshots of the manager's state and never blocks on the work.

Deliberately free of any Streamlit import: worker threads have no script-run
context and must never call st.*, and this keeps the whole thing unit-testable
with a fake per-company runner.

One job at a time, but it is a queue: submitting while a job is running appends
the new companies to it (skipping any already queued or in flight) and tops the
worker pool back up, instead of refusing or starting a second competing job.

Two levels of concurrency, deliberately separate. `workers` is how many COMPANIES are in
flight at once. Inside one company, its sources (the 8 crawlers, or the APIs of phases 1/4)
run side by side as well - company_service.run_company_phases - so a company takes as long
as its slowest source, not the sum. What may really run together is bounded by the resource
governor (resource_governor.py): Chromium instances, the LLM token budget, the Wayback rate
limit. So raising `workers` keeps the pipeline full without over-subscribing any of them.

Each company remembers WHERE its crawlers run: None = on this machine, otherwise the id
of a Crawler Worker (worker_hub.py) — a helper's own computer. That choice is set around
the company's run so the subprocess layer can route it, and lets one queue mix targets.
"""

import math
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

DEFAULT_WORKERS = 3
MAX_WORKERS = 8
# Wall-clock for ONE company's Phase 7 pass with its crawlers running together: about as long as
# the slowest one (see company_service.run_company_phases). Measured 2026-09-21: 148s for a real
# company. Used only for the "about how long will this take" hint shown before a run starts -
# once companies finish, the widget's ETA uses the durations actually observed.
SECONDS_PER_COMPANY_ESTIMATE = 150
# Phase 1 (APIs) and Phase 4 (web / news) are seconds of network calls per company.
SECONDS_PER_COMPANY_BY_PHASE = {1: 20, 2: 5, 4: 30, 7: SECONDS_PER_COMPANY_ESTIMATE}
DEFAULT_PHASES = (7,)
# The company-website crawler is limited by the LLM's tokens-per-minute budget, not by how many run
# at once: ~19k tokens per company against a bucket refilling at 8k/min is ~105s of budget each
# (measured: 6 companies took 623s, i.e. 104s each, however many were in flight). So a batch can
# never finish faster than n x this / LLM slots, whatever the parallelism.
LLM_SECONDS_PER_COMPANY = 105


def estimate_seconds(n_companies: int, workers: int, phases=DEFAULT_PHASES) -> int:
    """Rough wall-clock for n companies at `workers` in flight. A company's phases run together, so
    its time is that of its slowest phase; companies go in waves of `workers`. For phase 7 the
    serialised LLM budget is a floor under that (see LLM_SECONDS_PER_COMPANY)."""
    if n_companies <= 0:
        return 0
    per_company = max((SECONDS_PER_COMPANY_BY_PHASE.get(p, SECONDS_PER_COMPANY_ESTIMATE) for p in phases),
                      default=SECONDS_PER_COMPANY_ESTIMATE)
    estimate = math.ceil(n_companies / max(1, workers)) * per_company
    if 7 in phases:
        from resource_governor import default_capacities
        estimate = max(estimate, math.ceil(n_companies * LLM_SECONDS_PER_COMPANY / max(1, default_capacities()["llm"])))
    return int(estimate)


@dataclass(frozen=True)
class InFlight:
    name: str
    step_index: int = 0          # sources already FINISHED for this company
    step_total: int = 0
    step_name: str = ""          # the source that started most recently
    running: Tuple[str, ...] = ()  # every source running for this company right now (they overlap)


@dataclass(frozen=True)
class JobSnapshot:
    """An immutable copy of a job's state, safe to hand to the UI thread."""
    id: str
    state: str                   # "running" | "finished" | "cancelled"
    workers: int
    total: int
    done: int
    skipped: int                 # still queued when a stop was requested; never ran
    pending: int
    in_flight: Tuple[InFlight, ...]
    failed: Tuple[Tuple[str, str], ...]                       # (company, error) — its whole run blew up
    with_source_errors: Tuple[Tuple[str, Tuple[Tuple[str, str], ...]], ...]  # (company, ((crawler, error), ...))
    started_at: float
    finished_at: Optional[float]
    cancel_requested: bool
    fraction: float              # 0..1, counts crawlers finished inside in-flight companies too
    eta_seconds: Optional[float]
    phases: Tuple[int, ...] = DEFAULT_PHASES   # every phase any company in this job runs

    @property
    def running(self) -> bool:
        return self.state == "running"

    @property
    def clean(self) -> int:
        """Companies whose run completed with every crawler reporting success."""
        return self.done - len(self.failed) - len(self.with_source_errors)


@dataclass(frozen=True)
class SubmitResult:
    job_id: Optional[str]
    added: int
    already_queued: int
    new_job: bool
    stopping: bool = False       # nothing added: the running job is being stopped


@dataclass
class _Outcome:
    name: str
    error: Optional[str] = None
    source_errors: Tuple[Tuple[str, str], ...] = ()
    seconds: float = 0.0


@dataclass
class _Job:
    id: str
    workers: int
    started_at: float
    names: Dict[str, str] = field(default_factory=dict)
    targets: Dict[str, Optional[str]] = field(default_factory=dict)  # company id -> worker id (None = local)
    phases: Dict[str, Tuple[int, ...]] = field(default_factory=dict)  # company id -> phases to run
    pending: deque = field(default_factory=deque)
    in_flight: Dict[str, InFlight] = field(default_factory=dict)
    results: List[_Outcome] = field(default_factory=list)  # one per RUN — a company re-queued after it finished has two
    total: int = 0
    skipped: int = 0
    alive_workers: int = 0
    state: str = "running"
    cancel_requested: bool = False
    finished_at: Optional[float] = None
    finished: threading.Event = field(default_factory=threading.Event)


def _summarize(name: str, results, seconds: float) -> _Outcome:
    if not isinstance(results, dict):
        return _Outcome(name, error="the run returned no result", seconds=seconds)
    # Per-source results are keyed by crawler name, never "error", so a top-level
    # "error" is a whole-company failure (see company_service.run_phase7_for_company).
    if results.get("error"):
        return _Outcome(name, error=str(results["error"]), seconds=seconds)
    source_errors = tuple(
        (source, str(r.get("error") or "unknown error"))
        for source, r in results.items()
        if isinstance(r, dict) and r.get("status") == "error"
    )
    return _Outcome(name, source_errors=source_errors, seconds=seconds)


class CrawlJobManager:
    def __init__(self, run_company: Callable = None, session_factory=None):
        # run_company(company_id, session_factory, on_step, phases) -> (company_id, name, results)
        self._run_company = run_company
        self._session_factory = session_factory
        self._lock = threading.Lock()
        self._job: Optional[_Job] = None
        self._version = 0

    # ---- reading ---------------------------------------------------------------

    @property
    def version(self) -> int:
        """Bumps every time a job finishes or is stopped — a cheap "did anything
        just complete?" check for sessions that only poll."""
        return self._version

    def snapshot(self) -> Optional[JobSnapshot]:
        with self._lock:
            return self._snapshot_locked(self._job)

    def wait(self, timeout: float = None) -> bool:
        """Blocks until the current job has finished (tests / scripts; the UI never waits)."""
        job = self._job
        return True if job is None else job.finished.wait(timeout)

    # ---- control ---------------------------------------------------------------

    def submit(self, companies: Dict[str, str], workers: int = DEFAULT_WORKERS,
               target: Optional[str] = None, phases=DEFAULT_PHASES) -> SubmitResult:
        """
        Queues `companies` ({company_id: display name}). Starts a new job when none is
        running; otherwise appends to the running one. Never blocks on the crawl.
        `target` is the Crawler Worker id to run them on, or None for this machine (it only
        matters for phase 7, the Node crawlers). `phases` is which pipeline phases to run.
        """
        phases = tuple(sorted({int(p) for p in phases})) or DEFAULT_PHASES
        workers = max(1, min(int(workers), MAX_WORKERS))
        with self._lock:
            job = self._job
            running = job is not None and job.state == "running"
            if running and job.cancel_requested:
                return SubmitResult(job.id, 0, 0, new_job=False, stopping=True)

            queued = set(job.pending) | set(job.in_flight) if running else set()
            fresh = {cid: name for cid, name in companies.items() if cid not in queued}
            already = len(companies) - len(fresh)
            if not fresh:
                return SubmitResult(job.id if running else None, 0, already, new_job=False)

            if not running:
                job = _Job(id=uuid.uuid4().hex[:8], workers=workers, started_at=time.time())
                self._job = job
            job.names.update(fresh)
            job.targets.update({cid: target for cid in fresh})
            job.phases.update({cid: phases for cid in fresh})
            job.pending.extend(fresh)
            job.total += len(fresh)
            # A running job whose earlier workers already retired (queue ran dry while one
            # long company was still going) must not crawl the new arrivals one at a time.
            for _ in range(min(job.workers - job.alive_workers, len(job.pending))):
                self._spawn_worker_locked(job)
            return SubmitResult(job.id, len(fresh), already, new_job=not running)

    def cancel(self) -> None:
        """Stops taking new companies. Ones already crawling finish (a crawler's
        subprocess tree isn't safely interruptible), so this is 'stop after these'."""
        with self._lock:
            job = self._job
            if job and job.state == "running":
                job.cancel_requested = True

    # ---- internals -------------------------------------------------------------

    def _spawn_worker_locked(self, job: _Job) -> None:
        job.alive_workers += 1
        threading.Thread(target=self._worker, args=(job,), daemon=True,
                         name=f"crawl-{job.id}-{job.alive_workers}").start()

    def _worker(self, job: _Job) -> None:
        retired = False
        try:
            while True:
                with self._lock:
                    # The "queue is empty" decision and this worker retiring are ONE locked
                    # step: submit() appends only while alive_workers > 0, and every live
                    # worker re-checks the queue before retiring, so an append can never
                    # land after the last worker has decided to exit.
                    if job.cancel_requested or not job.pending:
                        retired = True
                        self._retire_locked(job)
                        return
                    cid = job.pending.popleft()
                    name = job.names.get(cid, cid)
                    job.in_flight[cid] = InFlight(name)
                self._execute(job, cid, name)
        finally:
            if not retired:  # only reachable if the loop body itself died
                with self._lock:
                    self._retire_locked(job)

    def _execute(self, job: _Job, cid: str, name: str) -> None:
        def on_step(index: int, total: int, step_name: str, event: str = "start") -> None:
            # A company's sources overlap, so this is called from several threads at once, both
            # when a source starts and when it ends; `index` is how many had finished by then.
            with self._lock:
                current = job.in_flight.get(cid)
                if current is None:
                    return
                running = [r for r in current.running if r != step_name]
                if event != "done":
                    running.append(step_name)
                job.in_flight[cid] = InFlight(name, index, total,
                                              current.step_name if event == "done" else step_name, tuple(running))

        started = time.time()
        try:
            run_company = self._run_company or _default_run_company()
            target = job.targets.get(cid)
            phases = job.phases.get(cid, DEFAULT_PHASES)
            if target:
                from worker_hub import use_target, apply_worker_capacity  # lazy: only remote crawls need the DB-backed hub
                apply_worker_capacity(target, self._session_factory)
                with use_target(target):
                    _, _, results = run_company(cid, self._session_factory, on_step, phases)
            else:
                _, _, results = run_company(cid, self._session_factory, on_step, phases)
        except Exception as e:  # noqa: BLE001 — a crashing runner must not kill the worker
            results = {"error": f"{type(e).__name__}: {e}"}
        outcome = _summarize(name, results, time.time() - started)
        with self._lock:
            job.in_flight.pop(cid, None)
            job.results.append(outcome)

    def _retire_locked(self, job: _Job) -> None:
        job.alive_workers -= 1
        if job.alive_workers > 0:
            return
        job.skipped = len(job.pending)
        job.pending.clear()
        # "cancelled" only if the stop actually cost work; one that arrived after every
        # company was already claimed changed nothing.
        job.state = "cancelled" if job.skipped else "finished"
        job.finished_at = time.time()
        self._version += 1
        job.finished.set()

    def _snapshot_locked(self, job: Optional[_Job]) -> Optional[JobSnapshot]:
        if job is None:
            return None
        outcomes = list(job.results)
        done = len(outcomes)
        durations = [o.seconds for o in outcomes if not o.error]
        remaining = len(job.pending) + len(job.in_flight)
        eta = None
        if job.state == "running" and durations and remaining:
            eta = (sum(durations) / len(durations)) * remaining / max(1, min(job.workers, remaining))
        partial = sum(f.step_index / f.step_total for f in job.in_flight.values() if f.step_total)
        return JobSnapshot(
            id=job.id, state=job.state, workers=job.workers, total=job.total, done=done,
            skipped=job.skipped, pending=len(job.pending),
            in_flight=tuple(job.in_flight.values()),
            failed=tuple((o.name, o.error) for o in outcomes if o.error),
            with_source_errors=tuple((o.name, o.source_errors) for o in outcomes if o.source_errors and not o.error),
            started_at=job.started_at, finished_at=job.finished_at,
            cancel_requested=job.cancel_requested,
            fraction=min(1.0, (done + partial) / job.total) if job.total else 0.0,
            eta_seconds=eta,
            phases=tuple(sorted({p for ph in job.phases.values() for p in ph})) or DEFAULT_PHASES,
        )


def _default_run_company() -> Callable:
    from company_service import run_company_phases  # lazy: pulls in every adapter

    def run(company_id, session_factory, on_step, phases):
        return run_company_phases(company_id, list(phases), session_factory, on_step)
    return run


_manager: Optional[CrawlJobManager] = None
_manager_lock = threading.Lock()


def get_manager() -> CrawlJobManager:
    """The process-wide manager. Streamlit re-executes app.py on every interaction but
    keeps imported modules, so this survives reruns, tab switches and closed browser
    tabs — the crawl carries on and a reopened page finds it still running."""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = CrawlJobManager()
        return _manager
