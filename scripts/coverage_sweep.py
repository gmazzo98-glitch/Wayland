"""Drain Phase 1/4/7 crawl backlogs through the existing background queue.

Run ``python scripts/coverage_sweep.py`` for an unattended sweep, or set
``--max-minutes`` for a bounded run. Pipeline Health's SourceHealth table shows
per-source call counts and last-run times while this script works.

An empty or failed source may remain in companies_not_yet_crawled forever.
Each company/phase is therefore tried once per pass; unresolved work is retried
only after --retry-minutes, rather than spinning on the same company.
"""

import argparse
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.orm import selectinload

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from company_service import companies_not_yet_crawled, eligible_phase7_sources  # noqa: E402
from crawl_jobs import DEFAULT_WORKERS, MAX_WORKERS, estimate_seconds, get_manager  # noqa: E402
from database import SessionFactory  # noqa: E402
from models import Company, CompanySourceRun  # noqa: E402

PHASES = (1, 4, 7)


def select_backlog(db, companies, *, now=None, stale_days=60):
    """Return company id -> phases needed, including old Phase 7 runs.

    Staleness is based on the latest recorded Phase 7 attempt, regardless of
    outcome. A failed or skipped source also appears via the existing backlog
    helper; this timestamp check adds repeat crawls for otherwise completed
    companies whose data is aging.
    """
    now = now or datetime.utcnow()
    by_id = {c.id: set() for c in companies}
    for phase in PHASES:
        for company in companies_not_yet_crawled(db, companies, phase):
            by_id[company.id].add(phase)

    eligible_ids = [c.id for c in companies if eligible_phase7_sources(c)]
    latest = dict(
        db.query(CompanySourceRun.company_id, func.max(CompanySourceRun.finished_at))
        .filter(CompanySourceRun.company_id.in_(eligible_ids), CompanySourceRun.phase == 7)
        .group_by(CompanySourceRun.company_id).all()
    ) if eligible_ids else {}
    cutoff = now - timedelta(days=stale_days)
    for cid in eligible_ids:
        if latest.get(cid) is not None and latest[cid] < cutoff:
            by_id[cid].add(7)
    return {cid: tuple(sorted(phases)) for cid, phases in by_id.items() if phases}


def _remaining(db, now, stale_days):
    # The Phase 7 eligibility helper reads company.competitors; load those
    # rows in one extra query rather than one round trip per roster company.
    companies = (db.query(Company).options(selectinload(Company.competitors))
                 .order_by(Company.legal_name, Company.id).all())
    return companies, select_backlog(db, companies, now=now, stale_days=stale_days)


def sweep(*, batch_size=10, workers=DEFAULT_WORKERS, stale_days=60,
          retry_minutes=60, max_minutes=None, manager=None, session_factory=SessionFactory,
          clock=time.monotonic, pause=time.sleep):
    """Run bounded queue batches until drained or the optional wall-clock limit.

    Returns the final backlog size and number of company/phase submissions.
    The queue's own runner and resource governor enforce all existing crawler
    eligibility, credentials, policy gates and concurrency limits.
    """
    if batch_size < 1 or workers < 1 or workers > MAX_WORKERS or stale_days < 1 or retry_minutes <= 0:
        raise ValueError("batch_size, workers, stale_days and retry_minutes must be positive and workers <= MAX_WORKERS")
    if max_minutes is not None and max_minutes <= 0:
        raise ValueError("max_minutes must be positive")
    manager = manager or get_manager()
    deadline = clock() + max_minutes * 60 if max_minutes is not None else None
    attempted = set()  # (company_id, phase), reset only after the retry interval
    submitted = 0

    while True:
        if deadline is not None and clock() >= deadline:
            break
        snapshot = manager.snapshot()
        if snapshot and snapshot.running:
            manager.wait(min(5, max(0, deadline - clock())) if deadline is not None else 5)
            continue

        db = session_factory()
        try:
            companies, backlog = _remaining(db, datetime.utcnow(), stale_days)
            names = {c.id: c.legal_name for c in companies}
        finally:
            db.close()
        if not backlog:
            print("Backlog drained across phases 1, 4 and 7.", flush=True)
            break

        pending = {
            cid: tuple(p for p in phases if (cid, p) not in attempted)
            for cid, phases in backlog.items()
        }
        pending = {cid: phases for cid, phases in pending.items() if phases}
        if not pending:
            delay = retry_minutes * 60
            if deadline is not None:
                delay = min(delay, max(0, deadline - clock()))
            print(f"{len(backlog)} companies remain; retrying unresolved work after {delay:.0f}s.", flush=True)
            if delay:
                pause(delay)
            attempted.clear()
            continue

        # Drain the fast API/news phases before deep crawls. Grouping a quick
        # phase with Phase 7 would keep its backlog behind a slow Wayback run.
        phase = min(p for needed in pending.values() for p in needed)
        phases = (phase,)
        batch_ids = [cid for cid, needed in pending.items() if phase in needed][:batch_size]
        if deadline is not None and estimate_seconds(len(batch_ids), workers, phases) > deadline - clock():
            print(f"Stopping before phases {phases}: estimated batch time exceeds remaining budget.", flush=True)
            break
        batch = {cid: names[cid] for cid in batch_ids}
        result = manager.submit(batch, workers=workers, phases=phases)
        if result.stopping or result.added != len(batch):
            # Another UI client can use the same process-wide manager. Wait for
            # its job and re-read the database rather than counting duplicates.
            manager.wait(5)
            continue
        attempted.update((cid, p) for cid in batch_ids for p in phases)
        submitted += len(batch)
        print(f"Queued {len(batch)} companies for phases {phases}; {len(backlog)} companies in backlog.", flush=True)
        while not manager.wait(5):
            pass  # Complete the submitted batch even if the deadline passes.
        snap = manager.snapshot()
        if snap:
            print(f"Job {snap.id}: {snap.done}/{snap.total} finished, "
                  f"{len(snap.failed)} company failures, {len(snap.with_source_errors)} with source errors.", flush=True)

    db = session_factory()
    try:
        _, remaining = _remaining(db, datetime.utcnow(), stale_days)
    finally:
        db.close()
    return len(remaining), submitted


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--batch-size", type=int, default=10)
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    ap.add_argument("--stale-days", type=int, default=60)
    ap.add_argument("--retry-minutes", type=float, default=60)
    ap.add_argument("--max-minutes", type=float, default=None,
                    help="stop submitting after this many minutes; current work may finish afterward")
    args = ap.parse_args()
    remaining, submitted = sweep(**vars(args))
    print(f"Sweep finished: {submitted} company runs submitted; "
          f"{remaining} companies still need at least one phase.", flush=True)


if __name__ == "__main__":
    main()
