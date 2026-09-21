"""
crawl_jobs.CrawlJobManager: the background queue behind the floating crawl widget.
No crawler runs and no database is touched — the per-company runner is a fake whose
progress the tests control with events, so every assertion is about the manager's
own bookkeeping (queueing, parallelism, stop, error isolation, progress).
"""

import threading
import time

import pytest

from crawl_jobs import CrawlJobManager, estimate_seconds, SECONDS_PER_COMPANY_ESTIMATE, LLM_SECONDS_PER_COMPANY, MAX_WORKERS


def _wait_for(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


class GatedRunner:
    """Fake run_company: each company blocks until its gate is opened, and records
    which thread ran it. Companies without a gate finish immediately."""

    def __init__(self):
        self.gates = {}
        self.started = []
        self.threads = {}
        self.results = {}
        self.phases_seen = {}
        self._lock = threading.Lock()

    def gate(self, cid):
        self.gates[cid] = threading.Event()
        return self.gates[cid]

    def __call__(self, cid, session_factory=None, on_step=None, phases=None):
        with self._lock:
            self.started.append(cid)
            self.phases_seen[cid] = phases
            self.threads[cid] = threading.current_thread().name
        gate = self.gates.get(cid)
        if gate:
            gate.wait(5)
        result = self.results.get(cid, {"Job Postings Crawler": {"status": "success"}})
        if isinstance(result, Exception):
            raise result
        return cid, f"name-{cid}", result


def _names(*ids):
    return {i: f"Company {i}" for i in ids}


def test_runs_companies_across_workers_and_finishes_once():
    runner = GatedRunner()
    gates = {c: runner.gate(c) for c in "abcd"}
    mgr = CrawlJobManager(run_company=runner)

    res = mgr.submit(_names(*"abcd"), workers=2)
    assert res.new_job and res.added == 4 and res.already_queued == 0

    # Only two are ever in flight at once; the rest wait their turn.
    assert _wait_for(lambda: len(runner.started) == 2)
    snap = mgr.snapshot()
    assert snap.running and snap.total == 4 and snap.done == 0
    assert len(snap.in_flight) == 2 and snap.pending == 2
    assert len(set(runner.threads.values())) == 2

    for gate in gates.values():
        gate.set()
    assert mgr.wait(3)
    snap = mgr.snapshot()
    assert snap.state == "finished" and snap.done == 4 and snap.pending == 0 and not snap.in_flight
    assert snap.fraction == 1.0 and snap.clean == 4
    assert mgr.version == 1


def test_submit_while_running_appends_and_skips_duplicates():
    runner = GatedRunner()
    gate_a, gate_b = runner.gate("a"), runner.gate("b")
    mgr = CrawlJobManager(run_company=runner)
    first = mgr.submit(_names("a", "b"), workers=1)
    assert _wait_for(lambda: runner.started == ["a"])

    # "a" is in flight and "b" is queued: re-submitting them adds nothing, "c" is new.
    second = mgr.submit(_names("a", "b", "c"), workers=1)
    assert second.job_id == first.job_id and not second.new_job
    assert second.added == 1 and second.already_queued == 2
    assert mgr.snapshot().total == 3

    gate_a.set(), gate_b.set()
    assert mgr.wait(3)
    assert runner.started == ["a", "b", "c"]
    assert mgr.snapshot().done == 3


def test_append_after_earlier_workers_retired_still_gets_full_parallelism():
    """Queue runs dry while one long company is still going, so the other workers have
    retired. New arrivals must not be crawled one at a time by the single survivor."""
    runner = GatedRunner()
    long_gate = runner.gate("long")
    mgr = CrawlJobManager(run_company=runner)
    mgr.submit(_names("long", "quick1", "quick2"), workers=3)
    assert _wait_for(lambda: len(mgr.snapshot().in_flight) == 1 and mgr.snapshot().done == 2)

    late_gates = {c: runner.gate(c) for c in ("x", "y")}
    mgr.submit(_names("x", "y"), workers=3)
    # Both start together, alongside the still-running long company.
    assert _wait_for(lambda: {"x", "y"} <= set(runner.started))
    assert len(mgr.snapshot().in_flight) == 3

    long_gate.set()
    for g in late_gates.values():
        g.set()
    assert mgr.wait(3)
    assert mgr.snapshot().done == 5


def test_stop_skips_queued_companies_but_lets_in_flight_finish():
    runner = GatedRunner()
    gate_a = runner.gate("a")
    mgr = CrawlJobManager(run_company=runner)
    mgr.submit(_names("a", "b", "c"), workers=1)
    assert _wait_for(lambda: runner.started == ["a"])

    mgr.cancel()
    assert mgr.snapshot().cancel_requested and mgr.snapshot().running
    # While stopping, new work is refused rather than silently queued behind the stop.
    refused = mgr.submit(_names("z"), workers=1)
    assert refused.stopping and refused.added == 0

    gate_a.set()
    assert mgr.wait(3)
    snap = mgr.snapshot()
    assert snap.state == "cancelled" and snap.done == 1 and snap.skipped == 2
    assert runner.started == ["a"]


def test_stop_after_everything_was_claimed_is_still_a_normal_finish():
    runner = GatedRunner()
    gate = runner.gate("a")
    mgr = CrawlJobManager(run_company=runner)
    mgr.submit(_names("a"), workers=1)
    assert _wait_for(lambda: runner.started == ["a"])
    mgr.cancel()
    gate.set()
    assert mgr.wait(3)
    assert mgr.snapshot().state == "finished" and mgr.snapshot().skipped == 0


def test_a_failing_company_and_failing_crawlers_are_reported_not_fatal():
    runner = GatedRunner()
    runner.results["boom"] = RuntimeError("crawler folder missing")
    runner.results["gone"] = {"error": "company not found"}
    runner.results["partial"] = {
        "Job Postings Crawler": {"status": "success"},
        "Digital Maturity Crawler": {"status": "error", "error": "Wayback 503"},
    }
    mgr = CrawlJobManager(run_company=runner)
    mgr.submit(_names("ok", "boom", "gone", "partial"), workers=2)
    assert mgr.wait(3)

    snap = mgr.snapshot()
    assert snap.state == "finished" and snap.done == 4
    failed = dict(snap.failed)
    assert "RuntimeError: crawler folder missing" in failed["Company boom"]
    assert failed["Company gone"] == "company not found"
    assert snap.with_source_errors == (("Company partial", (("Digital Maturity Crawler", "Wayback 503"),)),)
    assert snap.clean == 1


def test_progress_counts_crawlers_finished_inside_running_companies():
    runner = GatedRunner()
    gate = runner.gate("a")

    def stepping(cid, session_factory=None, on_step=None, phases=None):
        on_step(4, 8, "Review Crawler")
        return runner(cid, session_factory, on_step, phases)

    mgr = CrawlJobManager(run_company=stepping)
    mgr.submit(_names("a", "b"), workers=1)
    assert _wait_for(lambda: mgr.snapshot().in_flight and mgr.snapshot().in_flight[0].step_total == 8)
    snap = mgr.snapshot()
    assert snap.in_flight[0].step_name == "Review Crawler"
    # 4 of a's 8 crawlers done, nothing else: 0.5 of one company out of two.
    assert snap.fraction == pytest.approx(0.25)
    gate.set()
    assert mgr.wait(3)


def test_eta_appears_after_the_first_company_and_scales_with_what_remains():
    runner = GatedRunner()
    gate_b = runner.gate("b")
    mgr = CrawlJobManager(run_company=runner)
    mgr.submit(_names("a", "b", "c"), workers=1)
    assert _wait_for(lambda: mgr.snapshot().done == 1 and runner.started[-1] == "b")
    snap = mgr.snapshot()
    assert snap.eta_seconds is not None and snap.eta_seconds >= 0
    gate_b.set()
    assert mgr.wait(3)
    assert mgr.snapshot().eta_seconds is None  # finished: nothing left to estimate


def test_a_company_requeued_after_it_finished_counts_as_a_second_run():
    """Re-submitting a finished company to a still-running job is a new run: totals and
    done-counts must agree at the end (results used to be keyed by company id, so the
    second run overwrote the first and a 3-run job reported 2 done, 66% forever)."""
    runner = GatedRunner()
    gate_b = runner.gate("b")
    mgr = CrawlJobManager(run_company=runner)
    mgr.submit(_names("a", "b"), workers=1)
    assert _wait_for(lambda: mgr.snapshot().done == 1 and runner.started[-1] == "b")

    again = mgr.submit(_names("a"), workers=1)  # "a" already finished; "b" is still running
    assert again.added == 1 and again.already_queued == 0 and not again.new_job
    gate_b.set()
    assert mgr.wait(3)
    snap = mgr.snapshot()
    assert runner.started == ["a", "b", "a"]
    assert snap.total == 3 and snap.done == 3 and snap.fraction == 1.0


def test_submitting_after_a_finish_starts_a_fresh_job():
    mgr = CrawlJobManager(run_company=GatedRunner())
    first = mgr.submit(_names("a"), workers=1)
    assert mgr.wait(3)
    second = mgr.submit(_names("a"), workers=1)  # finished ones may be crawled again
    assert second.new_job and second.job_id != first.job_id
    assert mgr.wait(3) and mgr.snapshot().total == 1
    assert mgr.version == 2


def test_nothing_to_add_does_not_start_a_job():
    mgr = CrawlJobManager(run_company=GatedRunner())
    res = mgr.submit({}, workers=3)
    assert res.job_id is None and res.added == 0 and mgr.snapshot() is None
    assert mgr.wait(0.1)  # no job -> nothing to wait for


def test_worker_count_is_clamped():
    runner = GatedRunner()
    gates = {c: runner.gate(c) for c in map(str, range(MAX_WORKERS + 4))}
    mgr = CrawlJobManager(run_company=runner)
    mgr.submit(_names(*gates), workers=999)
    assert _wait_for(lambda: len(runner.started) == MAX_WORKERS)
    time.sleep(0.05)
    assert len(runner.started) == MAX_WORKERS  # the cap, not 999
    for g in gates.values():
        g.set()
    assert mgr.wait(3)


def test_appending_while_the_last_worker_is_retiring_never_loses_a_company():
    """Hammer the retire/append race: every submitted company must be processed exactly
    once, whether the submit lands on a running job or just after it finished."""
    runner = GatedRunner()
    mgr = CrawlJobManager(run_company=runner)
    submitted = []

    def feeder(prefix):
        for i in range(40):
            cid = f"{prefix}{i}"
            submitted.append(cid)
            mgr.submit(_names(cid), workers=3)

    threads = [threading.Thread(target=feeder, args=(p,)) for p in "ABC"]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert _wait_for(lambda: len(runner.started) == len(submitted), timeout=5)
    assert sorted(runner.started) == sorted(submitted)
    assert mgr.wait(3)


def test_estimate_scales_in_batches_of_the_worker_count(monkeypatch):
    monkeypatch.delenv("CRAWLER_LLM_FALLBACK_API_KEY", raising=False)
    assert estimate_seconds(0, 3) == 0
    assert estimate_seconds(1, 3) == SECONDS_PER_COMPANY_ESTIMATE
    assert estimate_seconds(3, 3) == 3 * LLM_SECONDS_PER_COMPANY  # the LLM floor exceeds one 150s wave
    # Few LLM-free phases: purely waves of `workers`.
    assert estimate_seconds(4, 3, phases=(1,)) == 2 * 20
    assert estimate_seconds(10, 1, phases=(4,)) == 10 * 30


def test_the_llm_budget_is_a_floor_under_a_deep_crawl_estimate(monkeypatch):
    """6 real companies took 623s at 3 in flight - the waves alone would have said 300s - because the
    website crawls are serialised by the token budget, however many companies run at once."""
    monkeypatch.delenv("CRAWLER_LLM_FALLBACK_API_KEY", raising=False)
    assert estimate_seconds(6, 3) == 6 * LLM_SECONDS_PER_COMPANY
    assert estimate_seconds(6, 8) == 6 * LLM_SECONDS_PER_COMPANY, "more workers cannot beat the token budget"
    # A second provider doubles the budget (two LLM slots).
    monkeypatch.setenv("CRAWLER_LLM_FALLBACK_API_KEY", "k")
    assert estimate_seconds(6, 3) == 3 * LLM_SECONDS_PER_COMPANY
    # Phases that never touch the LLM are not held to it.
    assert estimate_seconds(6, 3, phases=(1, 4)) == 2 * 30


def test_a_companys_sources_overlap_and_all_show_as_running():
    """A company's sources run at the same time, so the snapshot must list every one that is
    running (not just the latest), and drop each when it ends."""
    runner = GatedRunner()
    gate = runner.gate("a")

    def overlapping(cid, session_factory=None, on_step=None, phases=None):
        on_step(0, 3, "Company Website Crawler")
        on_step(0, 3, "Digital Maturity Crawler")
        on_step(0, 3, "Job Postings Crawler")
        on_step(1, 3, "Job Postings Crawler", "done")
        return runner(cid, session_factory, on_step, phases)

    mgr = CrawlJobManager(run_company=overlapping)
    mgr.submit(_names("a"), workers=1)
    assert _wait_for(lambda: mgr.snapshot().in_flight and mgr.snapshot().in_flight[0].step_index == 1)
    item = mgr.snapshot().in_flight[0]
    assert set(item.running) == {"Company Website Crawler", "Digital Maturity Crawler"}
    assert item.step_index == 1 and item.step_total == 3
    # One source done of three, on the only company: a third of it.
    assert mgr.snapshot().fraction == pytest.approx(1 / 3)
    gate.set()
    assert mgr.wait(3)
    assert mgr.snapshot().in_flight == ()


def test_each_company_carries_the_phases_it_was_queued_with():
    runner = GatedRunner()
    mgr = CrawlJobManager(run_company=runner)
    mgr.submit(_names("a"), workers=1, phases=(1, 4))
    assert mgr.wait(3)
    mgr.submit(_names("b"), workers=1)
    assert mgr.wait(3)
    assert runner.phases_seen == {"a": (1, 4), "b": (7,)}


def test_phases_are_normalised_and_reported_on_the_snapshot():
    runner = GatedRunner()
    gate = runner.gate("a")
    mgr = CrawlJobManager(run_company=runner)
    mgr.submit(_names("a"), workers=1, phases=[4, 1, 4])
    assert _wait_for(lambda: mgr.snapshot() is not None)
    assert mgr.snapshot().phases == (1, 4)
    gate.set()
    assert mgr.wait(3)


def test_estimate_uses_the_slowest_phase_of_the_company(monkeypatch):
    monkeypatch.delenv("CRAWLER_LLM_FALLBACK_API_KEY", raising=False)
    assert estimate_seconds(3, 3, phases=(1, 4)) < estimate_seconds(3, 3, phases=(7,))
    assert estimate_seconds(3, 3, phases=(1, 4, 7)) == estimate_seconds(3, 3, phases=(7,))
