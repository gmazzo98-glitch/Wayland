"""
resource_governor: how many crawler processes may run at once, per scarce resource. Everything here
is threads and sleeps — no crawler, no network, no database.
"""

import threading
import time

import pytest

import resource_governor as rg
from resource_governor import Governor, WaitClock


@pytest.fixture(autouse=True)
def _fresh_registry():
    rg.reset_for_tests()
    yield
    rg.reset_for_tests()


def _run_all(fn, n):
    threads = [threading.Thread(target=fn, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not any(t.is_alive() for t in threads), "a task never finished (deadlock?)"


def test_a_resource_never_has_more_holders_than_its_capacity():
    gov = Governor({"browser": 2, "process": 6, "llm": 1, "wayback": 2})
    running, peak, lock = [0], [0], threading.Lock()

    def task(_):
        with gov.slot("job-postings-crawler"):  # browser + process
            with lock:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            time.sleep(0.05)
            with lock:
                running[0] -= 1

    _run_all(task, 8)
    assert peak[0] == 2, "browser capacity is 2"
    assert gov.stats()["browser"]["in_use"] == 0 and gov.stats()["process"]["in_use"] == 0


def test_the_llm_slot_serialises_website_crawls_but_not_other_crawlers():
    gov = Governor({"browser": 6, "process": 8, "llm": 1, "wayback": 2})
    website, other, lock = [0], [0], threading.Lock()
    peak = {"website": 0, "other": 0}

    def task(i):
        name = "company-website-crawler" if i % 2 == 0 else "directory-listing-crawler"
        bucket = website if i % 2 == 0 else other
        key = "website" if i % 2 == 0 else "other"
        with gov.slot(name):
            with lock:
                bucket[0] += 1
                peak[key] = max(peak[key], bucket[0])
            time.sleep(0.05)
            with lock:
                bucket[0] -= 1

    _run_all(task, 8)
    assert peak["website"] == 1
    assert peak["other"] > 1, "the LLM limit must not throttle crawlers that don't use the LLM"


def test_mixed_crawlers_under_tight_capacity_never_deadlock():
    """Every kind of crawler at once, capacities of 1-2: each holds several resources, so an
    inconsistent acquisition order would deadlock here."""
    gov = Governor({"browser": 1, "process": 2, "llm": 1, "wayback": 1})
    names = ["company-website-crawler", "job-postings-crawler", "digital-maturity-crawler",
             "directory-listing-crawler", "news-signals-crawler"]

    def task(i):
        for _ in range(3):
            with gov.slot(names[i % len(names)]):
                time.sleep(0.005)

    _run_all(task, 15)
    assert all(s["in_use"] == 0 and s["waiting"] == 0 for s in gov.stats().values())


def test_waiters_are_served_first_come_first_served():
    gov = Governor({"process": 1})
    order, lock = [], threading.Lock()
    gate = threading.Event()

    def holder():
        with gov.slot("news-signals-crawler"):
            gate.wait(5)

    h = threading.Thread(target=holder)
    h.start()
    time.sleep(0.05)

    def waiter(i):
        with gov.slot("news-signals-crawler"):
            with lock:
                order.append(i)

    threads = []
    for i in range(5):
        t = threading.Thread(target=waiter, args=(i,))
        t.start()
        threads.append(t)
        time.sleep(0.03)  # arrival order is 0..4
    gate.set()
    h.join(5)
    for t in threads:
        t.join(5)
    assert order == [0, 1, 2, 3, 4]


def test_a_failure_inside_the_block_releases_the_slots():
    gov = Governor({"browser": 1, "process": 1, "llm": 1, "wayback": 1})
    with pytest.raises(RuntimeError):
        with gov.slot("company-website-crawler"):
            raise RuntimeError("crawler blew up")
    assert all(s["in_use"] == 0 for s in gov.stats().values())


def test_a_crawler_that_declares_no_resources_is_not_throttled():
    gov = Governor({"browser": 1, "process": 1, "llm": 1, "wayback": 1})
    with gov.slot("some-python-adapter"), gov.slot("some-python-adapter"):
        assert all(s["in_use"] == 0 for s in gov.stats().values())


def test_capacity_can_be_raised_while_tasks_are_waiting():
    gov = Governor({"process": 1})
    started, lock = [], threading.Lock()
    release = threading.Event()

    def task(i):
        with gov.slot("news-signals-crawler"):
            with lock:
                started.append(i)
            release.wait(5)

    threads = [threading.Thread(target=task, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    time.sleep(0.1)
    assert len(started) == 1
    gov.configure(process=3)
    time.sleep(0.1)
    assert len(started) == 3, "resizing must wake the waiters"
    release.set()
    for t in threads:
        t.join(5)


def test_every_shipped_crawler_is_known_to_the_governor():
    assert set(rg.CRAWLER_RESOURCES) == {
        "company-website-crawler", "job-postings-crawler", "directory-listing-crawler", "review-crawler",
        "linkedin-profile-crawler", "digital-maturity-crawler", "news-signals-crawler", "innovation-participation-crawler",
    }
    assert all("process" in resources for resources in rg.CRAWLER_RESOURCES.values())
    assert set(rg.ACQUIRE_ORDER) >= {r for rs in rg.CRAWLER_RESOURCES.values() for r in rs}


def test_default_capacities_follow_the_machine_and_the_environment(monkeypatch):
    for name in ("CRAWL_MAX_PROCESSES", "CRAWL_BROWSER_SLOTS", "CRAWL_LLM_SLOTS", "CRAWL_WAYBACK_SLOTS", "CRAWLER_LLM_FALLBACK_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert rg.default_capacities(8) == {"process": 6, "browser": 4, "llm": 1, "wayback": 2}
    assert rg.default_capacities(2)["browser"] == 2 and rg.default_capacities(2)["process"] == 3
    monkeypatch.setenv("CRAWLER_LLM_FALLBACK_API_KEY", "k")
    assert rg.default_capacities(8)["llm"] == 2, "a second provider doubles the LLM budget"
    monkeypatch.setenv("CRAWL_BROWSER_SLOTS", "1")
    monkeypatch.setenv("CRAWL_LLM_SLOTS", "3")
    caps = rg.default_capacities(8)
    assert caps["browser"] == 1 and caps["llm"] == 3
    monkeypatch.setenv("CRAWL_BROWSER_SLOTS", "not-a-number")
    assert rg.default_capacities(8)["browser"] == 4, "a bad value falls back to the default"


def test_each_target_has_its_own_governor_and_a_worker_sizes_its_own():
    local, remote = rg.get_governor(None), rg.get_governor("worker-1")
    assert local is rg.get_governor(None) and local is not remote
    rg.configure_remote("worker-1", 2)
    assert remote.capacity("process") == 2 and remote.capacity("browser") == 2
    rg.configure_remote("worker-1", 8)
    assert remote.capacity("process") == 8 and remote.capacity("browser") == rg.default_capacities()["browser"]


# ---- the timeout budget must not be spent queueing ----------------------------------------------------

def test_wait_clock_counts_only_time_spent_waiting():
    clock = WaitClock()
    assert clock.seconds() == 0
    clock.begin()
    time.sleep(0.1)
    assert 0.08 < clock.seconds() < 0.5, "an in-progress wait is already counted"
    clock.end()
    settled = clock.seconds()
    time.sleep(0.1)
    assert clock.seconds() == pytest.approx(settled, abs=0.02), "time after the wait is not"


def test_queue_time_is_not_charged_to_the_adapters_timeout():
    """A crawler that waited longer than its whole budget for a slot must still get to RUN for its
    budget. Before this, `timeout` started at submission: the adapter gave up on a crawl that had
    never started, and its process then launched anyway, orphaned."""
    from adapters.base import _call_with_hard_timeout

    rg.reset_for_tests()
    gov = rg.get_governor(None)
    gov.configure(process=1)
    release = threading.Event()

    def blocker():
        with gov.slot("news-signals-crawler"):
            release.wait(5)

    t = threading.Thread(target=blocker)
    t.start()
    time.sleep(0.1)

    def crawl(company):
        with rg.get_governor(None).slot("news-signals-crawler"):  # waits for the blocker
            time.sleep(0.3)  # then runs for well inside its budget
            return "done"

    threading.Timer(1.5, release.set).start()  # the wait alone (1.5s) is longer than the budget (1s)
    started = time.time()
    assert _call_with_hard_timeout(crawl, object(), timeout=1) == "done"
    assert time.time() - started > 1.4
    t.join(5)


def test_a_fetch_that_really_runs_too_long_still_times_out():
    from adapters.base import _call_with_hard_timeout

    with pytest.raises(TimeoutError):
        _call_with_hard_timeout(lambda company: time.sleep(3), object(), timeout=1)


def test_a_crawler_that_never_gets_a_slot_is_not_treated_as_running_out_of_time_either():
    """The wait is not counted, but neither does it hide a genuinely stuck fetch: once the slot is
    granted, the budget is measured from then."""
    from adapters.base import _call_with_hard_timeout

    rg.reset_for_tests()
    gov = rg.get_governor(None)
    gov.configure(process=1)
    release = threading.Event()
    # Hold the only slot from a helper thread, then release it after a short wait.
    holder_started = threading.Event()

    def hold():
        with gov.slot("news-signals-crawler"):
            holder_started.set()
            release.wait(5)

    t = threading.Thread(target=hold)
    t.start()
    holder_started.wait(2)
    threading.Timer(0.5, release.set).start()

    def stuck(company):
        with rg.get_governor(None).slot("news-signals-crawler"):
            time.sleep(3)  # granted after 0.5s, then hangs past its 1s budget

    with pytest.raises(TimeoutError):
        _call_with_hard_timeout(stuck, object(), timeout=1)
    t.join(5)
