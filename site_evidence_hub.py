"""
App side of the site evidence reader (T26): starting it on a Crawler Worker, telling the person in plain
words what it is doing, and keeping the evidence needed to fix it when it does not go well.

How a run flows (the app holds no files of the worker's computer, and no browser needs to stay open):

    app: queue_start() ──CrawlerTask──▶ worker ──▶ supervisor ──▶ runner (local model, the company's own site)
    worker ──heartbeat (every ~15 s)──▶ crawler_workers.info['site_evidence']      what it is doing NOW
    worker ──vienna_worker_site_evidence_report──▶ SiteEvidenceRun / Event / Company, signal_records
                                                                                   what it DID, idempotent

Everything the person sees comes from those two streams, so run_view() is a pure function of rows that
can be tested for every state (running, waiting for memory, computer asleep, finished but still delivering,
failed, ...). Deliberately free of any Streamlit import.
"""

import csv
import io
import json
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from models import (Company, CrawlerTask, CrawlerWorker, RawImportRecord, SignalRecord, SiteEvidenceCompany,
                    SiteEvidenceEvent, SiteEvidenceRun, SITE_EVIDENCE_RUN_ACTIVE, SITE_EVIDENCE_RUN_FINAL)
import worker_hub

CRAWLER = "site_evidence"
SOURCE_NAME = "Site Evidence Reader"
DATASET_NAME = "crawler_site_evidence"
SIGNAL_EVENT_CODES = ("signal_created", "signal_updated", "signal_replaced")
TASK_RETENTION = timedelta(days=2)
# How long a worker may stay silent before the app says so instead of showing its last known state.
SILENT_AFTER_SECONDS = worker_hub.ONLINE_SECONDS

# state -> (label, tone). tone drives the colour of the banner: good | warn | bad | neutral.
STATES: Dict[str, Tuple[str, str]] = {
    "queued": ("Waiting for the computer to pick it up", "neutral"),
    "starting": ("Starting", "neutral"),
    "running": ("Running", "good"),
    "waiting_ram": ("Running, waiting for free memory", "warn"),
    "waiting_model": ("Running, waiting for the local model", "warn"),
    "paused_for_update": ("Paused while the worker updates itself", "warn"),
    "stopping": ("Stopping", "warn"),
    "worker_silent": ("The computer is not reporting", "bad"),
    "delivering": ("Finished on the computer, delivering the last results", "warn"),
    "finished": ("Finished", "good"),
    "stopped": ("Stopped", "neutral"),
    "failed": ("Failed", "bad"),
}


# What the reader calls the outcome for one company, in words for the person reading the audit trail.
COMPANY_STATUS_LABELS = {
    "done": "Read",
    "partial": "Partly read (time budget; retried automatically)",
    "no_website": "No website on file",
    "site_unreachable": "Site could not be reached",
    "bad_homepage": "Address is not a company site",
    "robots_blocked": "Site asks robots to stay out",
    "blocked": "Site refuses automated reading",
}


# ---- fresh rows ---------------------------------------------------------------------------------------------------
# Streamlit keeps one Session across page reruns, so a plain query can hand back a row as it was a minute ago.
# Everything the live view shows is read through these.
def fresh_worker(db, worker_id: Optional[str]) -> Optional[CrawlerWorker]:
    return db.query(CrawlerWorker).filter_by(id=worker_id).populate_existing().first() if worker_id else None


def fresh_task(db, task_id: Optional[int]) -> Optional[CrawlerTask]:
    return db.query(CrawlerTask).filter_by(id=task_id).populate_existing().first() if task_id else None


def view_of(db, run: SiteEvidenceRun, now: Optional[datetime] = None) -> Dict[str, Any]:
    return run_view(run, fresh_worker(db, run.worker_id), fresh_task(db, run.task_id), now)


def latest_diagnostics(db, worker_id: str) -> Optional[CrawlerTask]:
    return (db.query(CrawlerTask).filter_by(worker_id=worker_id, crawler=CRAWLER, kind="diagnostics")
            .populate_existing().order_by(CrawlerTask.id.desc()).first())


# ---- what a computer can do ------------------------------------------------------------------------------------
def reader_info(worker: Optional[CrawlerWorker]) -> Dict[str, Any]:
    return ((worker.info or {}).get("site_evidence") or {}) if worker is not None else {}


def capability(worker: CrawlerWorker, expected: Optional[Dict[str, Any]] = None,
               now: Optional[datetime] = None) -> Dict[str, Any]:
    """{"can_start", "reasons"}: why this computer can or cannot start a run right now, in plain words.
    A computer without a local model (or Python) is never offered the job: the Wayback panel once let a
    run be started on a PC with no Ollama, where it could only fail."""
    status = worker_hub.worker_status(worker, expected, now)
    reasons: List[str] = []
    if not status["usable"]:
        reasons.append(f"{status['label']}: {status['detail']}")
    info = reader_info(worker)
    if status["usable"] and not info:
        reasons.append("This computer's worker does not include the reader yet. Workers update themselves within a few "
                       "minutes of a new release; check back shortly.")
    elif info:
        reasons.extend(info.get("blockers") or [])
        if info.get("available") is False and not info.get("blockers"):
            reasons.append("The reader is not installed on this computer.")
    return {"can_start": not reasons and bool(info.get("can_start")), "reasons": reasons, "status": status, "info": info}


# ---- the company list ---------------------------------------------------------------------------------------------
def build_company_list(db, order: str = "shortlist_first", only_shortlist: bool = False) -> Tuple[str, int, int]:
    """(csv text, companies in it, companies left out). company_id is the registration number, the same key
    the database function and the importer use. Companies without a website or a registration number are
    left out: there is nothing to read, or nothing to attach the result to."""
    rows = db.query(Company.registration_number, Company.website_url, Company.legal_name, Company.shortlist_status).all()
    keep = [r for r in rows if (r.website_url or "").strip() and (r.registration_number or "").strip()]
    priority = ("shortlisted", "in_pilot")
    if only_shortlist:
        keep = [r for r in keep if r.shortlist_status in priority]
    keep.sort(key=lambda r: (0 if order == "shortlist_first" and r.shortlist_status in priority else 1, r.registration_number))
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["company_id", "homepage_url", "name"])
    for r in keep:
        writer.writerow([r.registration_number.strip(), r.website_url.strip(), r.legal_name])
    return buf.getvalue(), len(keep), len(rows) - len(keep)


# ---- queueing ----------------------------------------------------------------------------------------------------
def latest_run(db, worker_id: Optional[str] = None, statuses: Optional[Iterable[str]] = None) -> Optional[SiteEvidenceRun]:
    q = db.query(SiteEvidenceRun)
    if worker_id:
        q = q.filter(SiteEvidenceRun.worker_id == worker_id)
    if statuses:
        q = q.filter(SiteEvidenceRun.status.in_(tuple(statuses)))
    return q.populate_existing().order_by(SiteEvidenceRun.requested_at.desc()).first()


def active_run(db, worker_id: str) -> Optional[SiteEvidenceRun]:
    return latest_run(db, worker_id, SITE_EVIDENCE_RUN_ACTIVE)


def _task(db, worker_id: str, kind: str, request: Dict[str, Any]) -> CrawlerTask:
    task = CrawlerTask(worker_id=worker_id, crawler=CRAWLER, kind=kind, status="queued", request=request)
    db.add(task)
    db.flush()
    return task


def queue_start(db, worker_id: str, input_csv: str, options: Optional[Dict[str, Any]] = None,
                now: Optional[datetime] = None) -> SiteEvidenceRun:
    """Creates the run and its start task. Refuses a second active run on the same computer and a computer
    that cannot do it, so a click that cannot succeed fails here, with the reason, not on the worker."""
    worker = fresh_worker(db, worker_id)
    if worker is None or worker.revoked:
        raise ValueError("That computer was removed; pick another one.")
    cap = capability(worker, None, now)
    if not cap["can_start"]:
        raise ValueError("This computer cannot run it right now: " + " ".join(cap["reasons"] or ["it has not reported yet."]))
    if not (input_csv or "").strip() or input_csv.count("\n") < 2:
        raise ValueError("The company list is empty.")
    running = active_run(db, worker_id)
    if running is not None:
        raise ValueError(f"A run is already active on this computer (started {running.requested_at:%Y-%m-%d %H:%M}). "
                         "Stop it first, or wait until it finishes.")
    # Old finished tasks carry the whole company list; sweep them so the table stays small.
    db.query(CrawlerTask).filter(CrawlerTask.crawler == CRAWLER,
                                 CrawlerTask.finished_at < datetime.utcnow() - TASK_RETENTION).delete()
    options = dict(options or {})
    run = SiteEvidenceRun(id=str(uuid.uuid4()), worker_id=worker_id, status="queued", options=options,
                          input_total=max(0, input_csv.count("\n") - 1), signals_written=0, signals_skipped=0)
    db.add(run)
    task = _task(db, worker_id, "start", {"run_id": run.id, "input_csv": input_csv, "options": options})
    run.task_id = task.id
    db.commit()
    return run


def queue_stop(db, run: SiteEvidenceRun) -> CrawlerTask:
    task = _task(db, run.worker_id, "stop", {"run_id": run.id})
    db.commit()
    return task


def queue_diagnostics(db, worker_id: str) -> CrawlerTask:
    task = _task(db, worker_id, "diagnostics", {})
    db.commit()
    return task


def queue_resync(db, worker_id: str) -> CrawlerTask:
    task = _task(db, worker_id, "resync", {})
    db.commit()
    return task


def cancel_unclaimed(db, run: SiteEvidenceRun) -> bool:
    """A start that no computer ever picked up (it was off) can be withdrawn; nothing ran, nothing to undo."""
    task = db.get(CrawlerTask, run.task_id) if run.task_id else None
    if run.status != "queued" or task is None or task.status != "queued":
        return False
    task.status, task.finished_at, task.error = "cancelled", datetime.utcnow(), "withdrawn in the app"
    run.status, run.finished_at, run.last_error = "stopped", datetime.utcnow(), "Withdrawn before the computer picked it up."
    db.commit()
    return True


def abandon(db, run: SiteEvidenceRun, reason: str = "") -> None:
    """Closes a run in the database that the computer never closed (reinstalled, its folder deleted, gone for good), so a
    new run can be started. Only the database row changes; if that computer is in fact still working, its results keep
    arriving and are stored, but the run stays ended."""
    if run.status in SITE_EVIDENCE_RUN_FINAL:
        raise ValueError("This run has already ended.")
    now = datetime.utcnow()
    reason = (reason or "").strip() or "Marked as ended in the app: the computer never reported that it finished."
    task = db.get(CrawlerTask, run.task_id) if run.task_id else None
    if task is not None and task.status == "queued":
        task.status, task.finished_at, task.error = "cancelled", now, "run marked as ended in the app"
    run.status, run.finished_at, run.last_error = "failed", now, reason
    db.add(SiteEvidenceEvent(id=uuid.uuid4().hex, run_id=run.id, ts=now, source="app", level="warn", code="run_abandoned",
                             message=reason, data={"previous_status": "active"}))
    db.commit()


def task_outcome(db, task_id: Optional[int]) -> Optional[Dict[str, Any]]:
    task = fresh_task(db, task_id)
    if task is None:
        return None
    return {"id": task.id, "kind": task.kind, "status": task.status, "error": task.error, "result": task.result,
            "created_at": task.created_at, "finished_at": task.finished_at}


# ---- one clear status --------------------------------------------------------------------------------------------
def _age(then: Optional[datetime], now: datetime) -> Optional[float]:
    return (now - then).total_seconds() if then else None


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "unknown"
    seconds = int(max(0, seconds))
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 5400:
        return f"{round(seconds / 60)} min"
    hours = seconds / 3600
    return f"{hours:.1f} h" if hours < 48 else f"{hours / 24:.1f} days"


def run_view(run: SiteEvidenceRun, worker: Optional[CrawlerWorker], task: Optional[CrawlerTask] = None,
             now: Optional[datetime] = None) -> Dict[str, Any]:
    """Everything the status banner needs, as data. See module docstring for where each part comes from."""
    now = now or datetime.utcnow()
    info = reader_info(worker)
    on_worker = info.get("run") or {}
    mine = on_worker if on_worker.get("id") == run.id else {}
    summary = run.summary or {}
    progress = {
        "total": mine.get("total") or summary.get("total") or run.input_total,
        "done": mine.get("done") if mine.get("done") is not None else (summary.get("done") if summary.get("done") is not None else run.done),
        "remaining": mine.get("remaining") if mine.get("remaining") is not None else summary.get("remaining"),
        "hits_total": mine.get("hits_total") if mine.get("hits_total") is not None else (run.hits_total or 0),
        "eta_seconds": mine.get("eta_seconds") if mine else summary.get("eta_seconds"),
        "avg_seconds": mine.get("avg_seconds") or summary.get("avg_seconds_per_company"),
        "by_status": mine.get("by_status") or summary.get("by_status") or {},
        "current": mine.get("current"),
    }
    heartbeat_age = _age(worker.last_seen_at, now) if worker is not None else None
    report_age = _age(run.last_report_at, now)
    reporting = info.get("reporting") or {}
    alerts: List[Dict[str, str]] = []

    state = None
    detail = ""
    if run.status in SITE_EVIDENCE_RUN_FINAL:
        state = run.status
        detail = {"finished": "Every company that was due has been read.",
                  "stopped": "It was stopped; progress is kept and a new start continues where it left off.",
                  "failed": run.last_error or "It failed; open the audit trail and diagnostics below."}[state]
        if run.finished_at:
            detail += f" Ended {run.finished_at:%Y-%m-%d %H:%M} UTC."
    else:
        worker_state = mine.get("state")
        if task is not None and task.status == "error":
            state, detail = "failed", f"The computer refused to start it: {task.error}"
        elif task is not None and task.status == "queued" and run.status == "queued":
            state = "queued"
            waited = _age(task.created_at, now) or 0
            detail = f"Queued {format_duration(waited)} ago. The computer picks it up within seconds when it is online."
            if waited > 90 and (heartbeat_age is None or heartbeat_age > SILENT_AFTER_SECONDS):
                alerts.append({"level": "error", "text": "The computer is not online, so nothing has started. It starts by itself "
                               "when that computer is switched on and the worker connects. You can withdraw this start below."})
        elif heartbeat_age is None or heartbeat_age > SILENT_AFTER_SECONDS:
            state = "worker_silent"
            detail = ("The computer has stopped reporting (switched off, asleep or offline). The run is kept there and "
                      "carries on by itself when it is back")
            detail += f"; last heard {format_duration(heartbeat_age)} ago." if heartbeat_age is not None else "."
        elif worker_state in SITE_EVIDENCE_RUN_FINAL:
            state = "delivering"
            backlog = (reporting.get("backlog_outbox_bytes") or 0) + (reporting.get("backlog_events_bytes") or 0)
            detail = (f"The computer says it is {worker_state}; the last results ({backlog // 1024} KB) are still being "
                      "delivered to the database.")
        elif worker_state in STATES:
            state = worker_state
        else:
            state = "running" if run.status == "running" else ("stopping" if run.status == "stopping" else "starting")
        if state == "waiting_ram":
            detail = "The computer is short of free memory and waits for it, then continues by itself. Close other programs on it to speed this up."
        elif state == "waiting_model":
            detail = "The local model is not answering; the run keeps trying and restarts it automatically."
        elif state == "paused_for_update":
            detail = "The worker is installing an update; the run resumes by itself in a minute."

    if reporting.get("failures_in_a_row"):
        alerts.append({"level": "warn", "text": "The computer cannot reach the database right now "
                       f"({reporting.get('last_error') or 'no answer'}). Results are kept on the computer and delivered when it can."})
    if reporting.get("quarantined"):
        alerts.append({"level": "error", "text": f"{reporting['quarantined']} local record(s) could not be delivered and were set "
                       "aside on the computer (see the audit trail and diagnostics)."})
    if mine.get("restarts"):
        alerts.append({"level": "info", "text": f"The run was restarted {mine['restarts']} time(s) after crashes or reboots; see the audit trail."})
    if run.status not in SITE_EVIDENCE_RUN_FINAL and state not in ("worker_silent", "queued") and report_age is not None \
            and report_age > 600 and heartbeat_age is not None and heartbeat_age <= SILENT_AFTER_SECONDS:
        alerts.append({"level": "warn", "text": f"The database has not received a progress report for {format_duration(report_age)}."})

    label, tone = STATES.get(state, (state, "neutral"))
    return {"state": state, "label": label, "tone": tone, "detail": detail, "progress": progress, "alerts": alerts,
            "heartbeat_age": heartbeat_age, "report_age": report_age, "last_event": mine.get("last_event"),
            "waiting_for": mine.get("waiting_for"), "restarts": mine.get("restarts") or 0,
            "error": run.last_error or mine.get("error")}


def sidebar_status(db, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """One line for the sidebar: the latest run that is active, or ended in the last 24 hours."""
    now = now or datetime.utcnow()
    run = latest_run(db)
    if run is None:
        return None
    if run.status in SITE_EVIDENCE_RUN_FINAL and run.finished_at and now - run.finished_at > timedelta(hours=24):
        return None
    view = view_of(db, run, now)
    p = view["progress"]
    text = f"{p['done']} / {p['total']}" if p.get("total") and p.get("done") is not None else ""
    return {"label": view["label"], "tone": view["tone"], "progress": text, "run_id": run.id, "state": view["state"]}


# ---- audit and diagnostics -----------------------------------------------------------------------------------------
def events(db, run_id: Optional[str] = None, levels: Optional[Iterable[str]] = None, codes: Optional[Iterable[str]] = None,
           search: str = "", limit: int = 500) -> List[SiteEvidenceEvent]:
    q = db.query(SiteEvidenceEvent)
    if run_id:
        q = q.filter(SiteEvidenceEvent.run_id == run_id)
    if levels:
        q = q.filter(SiteEvidenceEvent.level.in_(tuple(levels)))
    if codes:
        q = q.filter(SiteEvidenceEvent.code.in_(tuple(codes)))
    if search:
        q = q.filter(SiteEvidenceEvent.message.ilike(f"%{search}%"))
    return q.populate_existing().order_by(SiteEvidenceEvent.ts.desc(), SiteEvidenceEvent.id).limit(limit).all()


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def event_dict(e: SiteEvidenceEvent) -> Dict[str, Any]:
    return {"id": e.id, "run_id": e.run_id, "ts": _iso(e.ts), "source": e.source, "level": e.level, "code": e.code,
            "message": e.message, "data": e.data}


def company_rows(db, status: Optional[str] = None, limit: int = 2000) -> List[Dict[str, Any]]:
    q = db.query(SiteEvidenceCompany, Company.legal_name).join(Company, Company.id == SiteEvidenceCompany.company_id)
    if status:
        q = q.filter(SiteEvidenceCompany.status == status)
    out = []
    for row, name in q.populate_existing().order_by(SiteEvidenceCompany.updated_at.desc()).limit(limit).all():
        out.append({"registration_number": row.registration_number, "name": name, "status": row.status, "attempts": row.attempts,
                    "pages_read": row.pages_read, "via": row.via, "seconds": row.seconds, "hits": row.hits or [],
                    "windows": row.windows, "rejected": row.rejected, "rejected_samples": row.rejected_samples,
                    "pages_failed": row.pages_failed, "site_detail": row.site_detail, "error": row.error,
                    "rules_version": row.rules_version, "finished_at": _iso(row.finished_at), "last_run_id": row.last_run_id})
    return out


def audit_bundle(db, run_id: str) -> Dict[str, Any]:
    """Everything needed to understand or reproduce a run, as one JSON-able document. This is what to attach
    when asking someone to look at a problem: the run, every audit event, every company outcome it touched,
    the tasks, and the worker's last self-report."""
    run = db.get(SiteEvidenceRun, run_id)
    if run is None:
        raise ValueError("Unknown run.")
    worker = db.get(CrawlerWorker, run.worker_id)
    all_events = db.query(SiteEvidenceEvent).filter_by(run_id=run_id).order_by(SiteEvidenceEvent.ts, SiteEvidenceEvent.id).all()
    companies = db.query(SiteEvidenceCompany).filter_by(last_run_id=run_id).all()
    tasks = db.query(CrawlerTask).filter(CrawlerTask.worker_id == run.worker_id, CrawlerTask.crawler == CRAWLER).order_by(CrawlerTask.id).all()
    by_code: Dict[str, int] = {}
    for e in all_events:
        by_code[e.code] = by_code.get(e.code, 0) + 1
    return {
        "generated_at": datetime.utcnow().isoformat(timespec="seconds"),
        "run": {c.name: _iso(getattr(run, c.name)) for c in SiteEvidenceRun.__table__.columns},
        "event_counts": by_code, "events": [event_dict(e) for e in all_events],
        "companies": [{c.name: _iso(getattr(r, c.name)) for c in SiteEvidenceCompany.__table__.columns} for r in companies],
        "tasks": [{"id": t.id, "kind": t.kind, "status": t.status, "error": t.error, "created_at": _iso(t.created_at),
                   "finished_at": _iso(t.finished_at),
                   "request": {k: ("<company list omitted>" if k == "input_csv" else v) for k, v in (t.request or {}).items()}}
                  for t in tasks],
        "worker": {"id": worker.id, "name": worker.name, "last_seen_at": _iso(worker.last_seen_at),
                   "info": worker.info} if worker else None,
    }


def audit_bundle_json(db, run_id: str) -> str:
    return json.dumps(audit_bundle(db, run_id), indent=1, default=str)


# ---- undoing a run -------------------------------------------------------------------------------------------------
def _written_by_run(row: SignalRecord) -> Optional[str]:
    """The run id the database function stamped into the signal's payload, if it can be read."""
    try:
        payload = json.loads(row.raw_payload_ref) if isinstance(row.raw_payload_ref, str) else (row.raw_payload_ref or {})
        return payload.get("run_id")
    except (ValueError, AttributeError):
        return None


def rollback_plan(db, run_id: str) -> List[Dict[str, Any]]:
    """What undoing this run's signal changes would do, from the audit trail alone. For every signal the run wrote,
    the FIRST event holds the row as it was before the run touched it: nothing (so the row is removed) or the row
    that was replaced (so it is restored). A row that someone else has changed since is left alone and reported."""
    evs = (db.query(SiteEvidenceEvent).filter(SiteEvidenceEvent.run_id == run_id, SiteEvidenceEvent.code.in_(SIGNAL_EVENT_CODES))
           .order_by(SiteEvidenceEvent.ts, SiteEvidenceEvent.id).all())
    first: Dict[Tuple[str, str], SiteEvidenceEvent] = {}
    for e in evs:
        d = e.data or {}
        first.setdefault((d.get("company_id"), d.get("signal_key")), e)
    plan = []
    for (company_id, key), e in first.items():
        d = e.data or {}
        previous = d.get("previous")
        row = (db.query(SignalRecord).filter_by(company_id=company_id, signal_key=key).first()) if company_id else None
        item = {"company_id": company_id, "registration_number": d.get("registration_number"), "name": d.get("name"),
                "signal_key": key, "wrote": d.get("value"), "event_id": e.id}
        if row is None:
            item.update(action="nothing", reason="the signal is already gone")
        elif row.source != SOURCE_NAME:
            item.update(action="leave", reason=f"changed by {row.source} since this run; not touched")
        elif _written_by_run(row) not in (None, run_id):
            item.update(action="leave", reason="rewritten by a later run; undo that run first")
        elif previous:
            item.update(action="restore", restore_to={"source": previous.get("source"), "status": previous.get("status")},
                        previous=previous, row_id=row.id)
        else:
            item.update(action="delete", row_id=row.id)
        plan.append(item)
    return plan


def rollback_apply(db, run_id: str) -> Dict[str, int]:
    done = {"restored": 0, "deleted": 0, "left": 0}
    for item in rollback_plan(db, run_id):
        if item["action"] == "delete":
            db.query(SignalRecord).filter_by(id=item["row_id"]).delete()
            done["deleted"] += 1
        elif item["action"] == "restore":
            p = item["previous"]
            row = db.get(SignalRecord, item["row_id"])
            row.source, row.status = p.get("source"), p.get("status")
            row.numeric_value, row.text_value = p.get("numeric_value"), p.get("text_value")
            row.confidence, row.is_simulated = p.get("confidence"), bool(p.get("is_simulated"))
            row.raw_payload_ref = p.get("raw_payload_ref")
            fetched = p.get("fetched_at")
            row.fetched_at = datetime.fromisoformat(fetched) if isinstance(fetched, str) else fetched
            done["restored"] += 1
        else:
            done["left"] += 1
    run = db.get(SiteEvidenceRun, run_id)
    if run is not None:
        db.add(SiteEvidenceEvent(
            id=uuid.uuid4().hex, run_id=run_id, ts=datetime.utcnow(), source="app", level="warn", code="run_rolled_back",
            message=f"Signals written by this run were undone from the app: {done['restored']} restored, "
                    f"{done['deleted']} removed, {done['left']} left alone", data=done))
    db.commit()
    return done
