"""
Resource governor: how many crawler processes may run at once, per scarce resource.

Why this exists. A company's crawlers are independent of each other, so they can run side by
side — but they are not free to run without limit. Measured on real companies:
  * Chromium (company-website, job-postings, directory-listing, review) costs 300-500 MB and a
    core each, and this machine has ~3 GB of headroom;
  * company-website's LLM extraction is capped by the free tier's tokens-per-minute (a leaky
    bucket: running two crawls at once does not extract faster, it just makes both hit 429s);
  * digital-maturity's Wayback Machine lookups are rate-limited per IP (71% of CDX calls failed
    when several ran at once).
Running "as many as possible" is therefore slower and less reliable than running the right
number, so every crawler declares which resources it uses and takes a slot for each while it runs.

Slots are per TARGET: the crawlers of a company queued for a helper's computer (worker_hub) run
there, so they draw on that computer's capacity, not this one's.

Deliberately free of any Streamlit or database import: it is used from crawl worker threads.
"""

import contextvars
import os
import threading
import time
from contextlib import contextmanager
from typing import Dict, Iterable, Optional, Tuple

# The resources a crawler holds while its process runs. Anything not listed holds none (it is
# not throttled), which is right for the in-process Python adapters.
CRAWLER_RESOURCES: Dict[str, Tuple[str, ...]] = {
    "company-website-crawler": ("browser", "llm", "process"),
    "job-postings-crawler": ("browser", "process"),
    "directory-listing-crawler": ("browser", "process"),
    "review-crawler": ("browser", "process"),
    "linkedin-profile-crawler": ("browser", "process"),
    "digital-maturity-crawler": ("wayback", "process"),
    "news-signals-crawler": ("process",),
    "innovation-participation-crawler": ("process",),
}

# Slots are always taken narrowest first. One fixed global order means two tasks can never each
# hold what the other is waiting for, and the narrow resource (one LLM slot) is never held idle
# while its owner queues for a broad one.
ACQUIRE_ORDER = ("llm", "wayback", "browser", "process")


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, ""))
        return value if value > 0 else default
    except ValueError:
        return default


def default_capacities(cpu_count: Optional[int] = None) -> Dict[str, int]:
    """Capacities for THIS machine. Every value is overridable from the environment."""
    cpus = cpu_count or os.cpu_count() or 4
    return {
        # Total Node processes at once. One core is left for Streamlit and the DB writes.
        "process": _env_int("CRAWL_MAX_PROCESSES", min(6, max(3, cpus - 2))),
        # Headless Chromium instances. Each is 300-500 MB.
        "browser": _env_int("CRAWL_BROWSER_SLOTS", min(4, max(2, cpus // 2))),
        # One extraction run at a time: the token budget is the limit, not concurrency. Raise it
        # only with a second provider configured (CRAWLER_LLM_FALLBACK_API_KEY).
        "llm": _env_int("CRAWL_LLM_SLOTS", 2 if os.getenv("CRAWLER_LLM_FALLBACK_API_KEY") else 1),
        # archive.org rate-limits per IP.
        "wayback": _env_int("CRAWL_WAYBACK_SLOTS", 2),
    }


# ---- wait accounting ---------------------------------------------------------------------------------

class WaitClock:
    """
    Time a fetch spent QUEUED for a resource slot, as opposed to running.

    adapters.base gives every fetch a wall-clock budget. Without this, a crawler that waited two
    minutes behind another for its LLM slot would use up its whole budget waiting and be
    abandoned before it had run for a second — while its process then started anyway, orphaned.
    The budget is spent only while the fetch is actually running.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._total = 0.0
        self._since: Optional[float] = None

    def begin(self) -> None:
        with self._lock:
            self._since = time.monotonic()

    def end(self) -> None:
        with self._lock:
            if self._since is not None:
                self._total += time.monotonic() - self._since
                self._since = None

    def seconds(self) -> float:
        with self._lock:
            return self._total + (time.monotonic() - self._since if self._since is not None else 0.0)


_wait_clock: contextvars.ContextVar = contextvars.ContextVar("crawl_wait_clock", default=None)


def use_wait_clock(clock: Optional[WaitClock]) -> contextvars.Token:
    return _wait_clock.set(clock)


# ---- the governor -----------------------------------------------------------------------------------------

class _Pool:
    """A counting semaphore that can be resized and reports how many are waiting. Waiters are
    woken first-in-first-out (Condition keeps its waiters in a queue), so a task that has waited
    longest is served first and none starves."""

    def __init__(self, capacity: int) -> None:
        self.capacity = max(1, capacity)
        self.in_use = 0
        self.waiting = 0
        self._cond = threading.Condition()

    def acquire(self) -> None:
        with self._cond:
            self.waiting += 1
            try:
                while self.in_use >= self.capacity:
                    self._cond.wait()
            finally:
                self.waiting -= 1
            self.in_use += 1

    def release(self) -> None:
        with self._cond:
            self.in_use -= 1
            self._cond.notify()

    def resize(self, capacity: int) -> None:
        with self._cond:
            self.capacity = max(1, capacity)
            self._cond.notify_all()


class Governor:
    def __init__(self, capacities: Optional[Dict[str, int]] = None) -> None:
        self._pools = {name: _Pool(cap) for name, cap in (capacities or default_capacities()).items()}

    def configure(self, **capacities: int) -> None:
        for name, cap in capacities.items():
            if name in self._pools:
                self._pools[name].resize(cap)
            else:
                self._pools[name] = _Pool(cap)

    def capacity(self, resource: str) -> int:
        return self._pools[resource].capacity

    def stats(self) -> Dict[str, Dict[str, int]]:
        return {n: {"capacity": p.capacity, "in_use": p.in_use, "waiting": p.waiting} for n, p in self._pools.items()}

    @contextmanager
    def slot(self, crawler: str):
        """Holds one slot of every resource `crawler` uses for the duration of the block, waiting
        for them if need be. The wait is reported to the current WaitClock, if any."""
        wanted = [r for r in ACQUIRE_ORDER if r in CRAWLER_RESOURCES.get(crawler, ()) and r in self._pools]
        clock = _wait_clock.get()
        held = []
        if clock:
            clock.begin()
        try:
            for resource in wanted:
                self._pools[resource].acquire()
                held.append(resource)
        except BaseException:
            for resource in reversed(held):
                self._pools[resource].release()
            raise
        finally:
            if clock:
                clock.end()
        try:
            yield
        finally:
            for resource in reversed(held):
                self._pools[resource].release()


_governors: Dict[Optional[str], Governor] = {}
_registry_lock = threading.Lock()


def get_governor(target: Optional[str] = None) -> Governor:
    """The governor for one target: None = this machine, otherwise a Crawler Worker id."""
    with _registry_lock:
        if target not in _governors:
            _governors[target] = Governor()
        return _governors[target]


def configure_remote(target: str, max_parallel: int) -> None:
    """A helper's computer runs at most `max_parallel` crawler processes at once (its own
    limit, reported in its heartbeat). Queuing more than that only makes tasks wait unclaimed
    until the app gives up on them, so the app-side slots are sized to match."""
    n = max(1, int(max_parallel))
    get_governor(target).configure(process=n, browser=min(n, default_capacities()["browser"]))


def reset_for_tests() -> None:
    with _registry_lock:
        _governors.clear()
