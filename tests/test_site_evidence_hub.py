"""
App side of the site evidence reader (T26): which computers may start a run, what the person is told in every state
the run can be in, the audit bundle, and undoing a run from the audit trail alone. SQLite; the PL/pgSQL side is
tested on a real PostgreSQL in test_site_evidence_sql.py.
"""

import json
import textwrap
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from streamlit.testing.v1 import AppTest

import site_evidence_hub as hub
import worker_hub
from models import (Base, Company, CrawlerTask, CrawlerWorker, SignalRecord, SiteEvidenceCompany, SiteEvidenceEvent,
                    SiteEvidenceRun)

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
NOW = datetime(2026, 10, 20, 12, 0, 0)
GOOD_INFO = {"protocol": worker_hub.PROTOCOL, "available": True, "can_start": True, "blockers": [],
             "ollama": {"installed": True, "reachable": True, "models": ["qwen2.5:3b"], "model_ok": True}}


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'hub.db').as_posix()}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def computer(db, name="Office PC", reader=None, seen_ago=5, **extra):
    w, _ = worker_hub.create_worker(db, name)
    w.last_seen_at = NOW - timedelta(seconds=seen_ago)
    w.info = {"protocol": worker_hub.PROTOCOL, "checks": {}, **extra}
    if reader is not None:
        w.info["site_evidence"] = reader
    db.commit()
    return w


def company(db, reg, name=None, url="https://x.example", status="not_shortlisted"):
    c = Company(id=f"id-{reg}", legal_name=name or f"Company {reg}", registration_number=reg, country="Italy",
                segment="SME", website_url=url, shortlist_status=status)
    db.add(c)
    db.commit()
    return c


CSV = "company_id,homepage_url,name\nIT1,https://a.example,A\nIT2,https://b.example,B\n"


# ---- who can start --------------------------------------------------------------------------------------------------
def test_a_computer_without_the_local_model_is_never_offered_the_job(db):
    no_ollama = {**GOOD_INFO, "can_start": False, "blockers": ["Ollama (the local model) is not installed on this computer"]}
    bare = computer(db, "Laptop", reader=no_ollama)
    cap = hub.capability(bare, now=NOW)
    assert not cap["can_start"] and "Ollama" in " ".join(cap["reasons"])
    with pytest.raises(ValueError, match="Ollama"):
        hub.queue_start(db, bare.id, CSV, now=NOW)
    assert db.query(SiteEvidenceRun).count() == 0 and db.query(CrawlerTask).count() == 0


def test_a_worker_from_before_the_reader_existed_is_told_to_wait_for_its_update(db):
    old = computer(db, "Old", reader=None)
    cap = hub.capability(old, now=NOW)
    assert not cap["can_start"] and "does not include the reader yet" in cap["reasons"][0]


def test_an_offline_computer_cannot_start_a_run(db):
    off = computer(db, "Off", reader=GOOD_INFO, seen_ago=3600)
    cap = hub.capability(off, now=NOW)
    assert not cap["can_start"] and "Offline" in cap["reasons"][0]


# ---- queueing -------------------------------------------------------------------------------------------------------
def test_queue_start_creates_run_and_task_and_refuses_a_second_active_run(db):
    w = computer(db, reader=GOOD_INFO)
    run = hub.queue_start(db, w.id, CSV, {"retry_after_hours": 0}, now=NOW)
    task = db.get(CrawlerTask, run.task_id)
    assert run.status == "queued" and run.input_total == 2 and run.options == {"retry_after_hours": 0}
    assert (task.crawler, task.kind, task.status) == ("site_evidence", "start", "queued")
    assert task.request["run_id"] == run.id and task.request["input_csv"] == CSV
    with pytest.raises(ValueError, match="already active"):
        hub.queue_start(db, w.id, CSV, now=NOW)
    run.status = "finished"
    db.commit()
    assert hub.queue_start(db, w.id, CSV, now=NOW).id != run.id


def test_queue_start_rejects_an_empty_list_and_a_removed_computer(db):
    w = computer(db, reader=GOOD_INFO)
    with pytest.raises(ValueError, match="empty"):
        hub.queue_start(db, w.id, "company_id,homepage_url,name\n", now=NOW)
    worker_hub.revoke_worker(db, w.id)
    with pytest.raises(ValueError, match="removed"):
        hub.queue_start(db, w.id, CSV, now=NOW)


def test_a_start_nobody_picked_up_can_be_withdrawn_but_a_started_run_cannot(db):
    w = computer(db, reader=GOOD_INFO)
    run = hub.queue_start(db, w.id, CSV, now=NOW)
    assert hub.cancel_unclaimed(db, run)
    assert run.status == "stopped" and db.get(CrawlerTask, run.task_id).status == "cancelled"
    run2 = hub.queue_start(db, w.id, CSV, now=NOW)
    db.get(CrawlerTask, run2.task_id).status = "done"
    run2.status = "running"
    db.commit()
    assert not hub.cancel_unclaimed(db, run2)


def test_a_run_the_computer_never_closed_can_be_marked_ended_so_a_new_one_can_start(db):
    w = computer(db, reader=GOOD_INFO)
    run = hub.queue_start(db, w.id, CSV, now=NOW)
    run.status = "running"
    db.get(CrawlerTask, run.task_id).status = "done"
    db.commit()
    with pytest.raises(ValueError, match="already active"):
        hub.queue_start(db, w.id, CSV, now=NOW)
    hub.abandon(db, run)
    assert run.status == "failed" and run.finished_at is not None and "never reported" in run.last_error
    event = db.query(SiteEvidenceEvent).filter_by(code="run_abandoned").one()
    assert event.level == "warn" and event.source == "app"
    assert hub.queue_start(db, w.id, CSV, now=NOW).id != run.id
    with pytest.raises(ValueError, match="already ended"):
        hub.abandon(db, run)


def test_stop_diagnostics_and_resync_are_queued_as_tasks_for_that_computer(db):
    w = computer(db, reader=GOOD_INFO)
    run = hub.queue_start(db, w.id, CSV, now=NOW)
    hub.queue_stop(db, run)
    hub.queue_diagnostics(db, w.id)
    hub.queue_resync(db, w.id)
    kinds = [t.kind for t in db.query(CrawlerTask).order_by(CrawlerTask.id)]
    assert kinds == ["start", "stop", "diagnostics", "resync"]
    assert db.query(CrawlerTask).filter_by(kind="stop").one().request == {"run_id": run.id}


# ---- the company list -----------------------------------------------------------------------------------------------
def test_company_list_puts_the_shortlist_first_and_leaves_out_what_cannot_be_read(db):
    company(db, "IT3", status="not_shortlisted")
    company(db, "IT1", status="shortlisted")
    company(db, "IT2", url="")
    company(db, "IT4", url="  ")
    company(db, "IT5", status="in_pilot")
    text, n, left_out = hub.build_company_list(db)
    ids = [line.split(",")[0] for line in text.strip().splitlines()[1:]]
    assert ids == ["IT1", "IT5", "IT3"] and (n, left_out) == (3, 2)
    only, n2, _ = hub.build_company_list(db, only_shortlist=True)
    assert [line.split(",")[0] for line in only.strip().splitlines()[1:]] == ["IT1", "IT5"] and n2 == 2


def test_company_names_with_commas_survive_the_csv(db):
    company(db, "IT9", name='Rossi, Bianchi & "Figli" S.p.A.')
    text, _, _ = hub.build_company_list(db)
    import csv, io
    rows = list(csv.DictReader(io.StringIO(text)))
    assert rows[0]["name"] == 'Rossi, Bianchi & "Figli" S.p.A.' and rows[0]["company_id"] == "IT9"


# ---- what the person is told ----------------------------------------------------------------------------------------
def _run(db, w, status="running", **kw):
    fields = {"id": "11111111-1111-1111-1111-111111111111", "requested_at": NOW - timedelta(hours=2), "input_total": 900,
              "done": 0, "hits_total": 0, "signals_written": 0, "signals_skipped": 0, **kw}
    r = SiteEvidenceRun(worker_id=w.id, status=status, **fields)
    db.add(r)
    db.commit()
    return r


def live(rid, state="running", **over):
    return {**GOOD_INFO, "run": {"id": rid, "state": state, "total": 900, "done": 120, "remaining": 780, "hits_total": 31,
                                 "eta_seconds": 7200, "avg_seconds": 20,
                                 "current": {"company_id": "IT1", "name": "Acme SRL", "step": "reading pages"}, **over}}


def test_a_running_run_shows_progress_from_the_heartbeat(db):
    rid = "11111111-1111-1111-1111-111111111111"
    w = computer(db, reader=live(rid))
    r = _run(db, w, last_report_at=NOW - timedelta(seconds=30))
    v = hub.run_view(r, w, now=NOW)
    assert (v["state"], v["tone"]) == ("running", "good")
    assert v["progress"]["done"] == 120 and v["progress"]["current"]["name"] == "Acme SRL" and v["progress"]["eta_seconds"] == 7200
    assert v["alerts"] == []


def test_waiting_for_memory_and_for_the_model_are_said_in_plain_words(db):
    for i, (state, phrase) in enumerate((("waiting_ram", "free memory"), ("waiting_model", "local model"),
                                         ("paused_for_update", "update"))):
        rid = f"11111111-1111-1111-1111-11111111111{i}"
        w = computer(db, f"PC-{state}", reader=live(rid, state))
        v = hub.run_view(_run(db, w, id=rid, last_report_at=NOW - timedelta(seconds=30)), w, now=NOW)
        assert v["state"] == state and v["tone"] == "warn" and phrase in v["detail"]


def test_a_silent_computer_is_called_silent_not_running(db):
    rid = "11111111-1111-1111-1111-111111111111"
    w = computer(db, reader=live(rid), seen_ago=900)
    v = hub.run_view(_run(db, w, last_report_at=NOW - timedelta(minutes=16)), w, now=NOW)
    assert v["state"] == "worker_silent" and v["tone"] == "bad" and "15 min" in v["detail"] and "carries on by itself" in v["detail"]


def test_finished_on_the_computer_but_not_yet_in_the_database_is_delivering_not_finished(db):
    rid = "11111111-1111-1111-1111-111111111111"
    info = live(rid, "finished")
    info["reporting"] = {"backlog_outbox_bytes": 40960, "backlog_events_bytes": 0}
    w = computer(db, reader=info)
    v = hub.run_view(_run(db, w, last_report_at=NOW - timedelta(seconds=30)), w, now=NOW)
    assert v["state"] == "delivering" and "40 KB" in v["detail"]


def test_final_states_come_from_the_database_row_and_explain_themselves(db):
    w = computer(db, reader=GOOD_INFO)
    run = _run(db, w, status="finished", done=900, finished_at=NOW - timedelta(hours=1), started_at=NOW - timedelta(hours=9),
               summary={"total": 900, "done": 900, "retry_pending": 4})
    v = hub.run_view(run, w, now=NOW)
    assert (v["state"], v["tone"]) == ("finished", "good") and v["progress"]["done"] == 900
    run.status, run.last_error = "failed", "Not started: Ollama (the local model) is not installed on this computer"
    v = hub.run_view(run, w, now=NOW)
    assert (v["state"], v["tone"]) == ("failed", "bad") and "Ollama" in v["detail"]
    run.status = "stopped"
    assert "continues where it left off" in hub.run_view(run, w, now=NOW)["detail"]


def test_a_start_the_computer_refused_is_failed_not_queued_forever(db):
    w = computer(db, reader=GOOD_INFO)
    run = hub.queue_start(db, w.id, CSV, now=NOW)
    task = db.get(CrawlerTask, run.task_id)
    task.status, task.error = "error", "the start request has no company list"
    db.commit()
    v = hub.run_view(run, w, task, now=NOW)
    assert v["state"] == "failed" and "no company list" in v["detail"]


def test_a_queued_start_on_an_offline_computer_says_nothing_has_started(db):
    w = computer(db, reader=GOOD_INFO)
    run = hub.queue_start(db, w.id, CSV, now=NOW)
    task = db.get(CrawlerTask, run.task_id)
    task.created_at = NOW - timedelta(minutes=10)
    w.last_seen_at = NOW - timedelta(hours=2)
    db.commit()
    v = hub.run_view(run, w, task, now=NOW)
    assert v["state"] == "queued" and any("not online" in a["text"] for a in v["alerts"])


def test_delivery_trouble_is_surfaced_as_alerts(db):
    rid = "11111111-1111-1111-1111-111111111111"
    info = live(rid, restarts=2)
    info["reporting"] = {"failures_in_a_row": 3, "last_error": "HTTP 502", "quarantined": 2}
    w = computer(db, reader=info)
    v = hub.run_view(_run(db, w, last_report_at=NOW - timedelta(minutes=20)), w, now=NOW)
    text = " | ".join(a["text"] for a in v["alerts"])
    assert "cannot reach the database" in text and "HTTP 502" in text and "2 local record(s)" in text
    assert "restarted 2 time(s)" in text and "has not received a progress report" in text


def test_sidebar_status_shows_active_runs_and_hides_old_finished_ones(db):
    assert hub.sidebar_status(db, NOW) is None
    rid = "11111111-1111-1111-1111-111111111111"
    w = computer(db, reader=live(rid))
    run = _run(db, w, last_report_at=NOW - timedelta(seconds=30))
    s = hub.sidebar_status(db, NOW)
    assert s["state"] == "running" and s["progress"] == "120 / 900"
    run.status, run.finished_at = "finished", NOW - timedelta(hours=30)
    db.commit()
    assert hub.sidebar_status(db, NOW) is None


# ---- audit bundle and rollback --------------------------------------------------------------------------------------
RID = "11111111-1111-1111-1111-111111111111"


def _signal_event(db, run, c, key, action, previous=None, n=0):
    db.add(SiteEvidenceEvent(id=f"ev{n}{key}{c.id}"[:36], run_id=run.id, ts=NOW + timedelta(seconds=n), source="database",
                             level="info", code=f"signal_{action}", message=f"Wrote {key}",
                             data={"company_id": c.id, "registration_number": c.registration_number, "name": c.legal_name,
                                   "signal_key": key, "action": action, "value": 1, "previous": previous}))


def _signal(db, c, key, source, status="present", simulated=False, run_id=RID):
    s = SignalRecord(company_id=c.id, signal_key=key, source=source, status=status, is_simulated=simulated,
                     numeric_value=1.0, confidence=0.6, raw_payload_ref=json.dumps({"run_id": run_id}))
    db.add(s)
    db.commit()
    return s


def test_rollback_removes_what_was_created_restores_what_was_replaced_and_leaves_what_others_changed(db):
    w = computer(db, reader=GOOD_INFO)
    run = _run(db, w, status="finished")
    created, replaced, taken, later = (company(db, f"IT{i}") for i in range(4))

    s1 = _signal(db, created, "university_partnership", hub.SOURCE_NAME)
    _signal_event(db, run, created, "university_partnership", "created", None, 1)

    s2 = _signal(db, replaced, "university_partnership", hub.SOURCE_NAME)
    prev = {"id": "old", "source": "Simulated", "status": "not_yet_checked", "numeric_value": None, "text_value": None,
            "confidence": 0.1, "is_simulated": True, "fetched_at": "2026-01-01T00:00:00", "raw_payload_ref": None}
    _signal_event(db, run, replaced, "university_partnership", "replaced", prev, 2)

    _signal(db, taken, "university_partnership", "Someone Else")
    _signal_event(db, run, taken, "university_partnership", "created", None, 3)

    _signal(db, later, "university_partnership", hub.SOURCE_NAME, run_id="22222222-2222-2222-2222-222222222222")
    _signal_event(db, run, later, "university_partnership", "created", None, 4)
    db.commit()

    plan = {p["registration_number"]: p for p in hub.rollback_plan(db, run.id)}
    assert [plan[k]["action"] for k in ("IT0", "IT1", "IT2", "IT3")] == ["delete", "restore", "leave", "leave"]
    assert "changed by Someone Else" in plan["IT2"]["reason"] and "later run" in plan["IT3"]["reason"]

    done = hub.rollback_apply(db, run.id)
    assert done == {"restored": 1, "deleted": 1, "left": 2}
    assert db.query(SignalRecord).filter_by(id=s1.id).first() is None
    db.refresh(s2)
    assert (s2.source, s2.status, s2.is_simulated) == ("Simulated", "not_yet_checked", True)
    assert db.query(SignalRecord).filter_by(source="Someone Else").count() == 1
    audit = db.query(SiteEvidenceEvent).filter_by(code="run_rolled_back").one()
    assert audit.level == "warn" and audit.data == done
    # a second undo finds nothing left to do for the two it already handled
    again = {p["registration_number"]: p["action"] for p in hub.rollback_plan(db, run.id)}
    assert again["IT0"] == "nothing" and again["IT1"] == "leave"


def test_rollback_uses_the_first_event_when_a_run_touched_a_signal_twice(db):
    w = computer(db, reader=GOOD_INFO)
    run = _run(db, w, status="finished")
    c = company(db, "IT1")
    s = _signal(db, c, "external_collaboration", hub.SOURCE_NAME)
    _signal_event(db, run, c, "external_collaboration", "created", None, 1)
    _signal_event(db, run, c, "external_collaboration", "updated", {"id": s.id, "source": hub.SOURCE_NAME, "status": "present"}, 2)
    plan = hub.rollback_plan(db, run.id)
    assert len(plan) == 1 and plan[0]["action"] == "delete"


def test_audit_bundle_has_everything_and_not_the_company_list(db):
    w = computer(db, reader=GOOD_INFO)
    run = hub.queue_start(db, w.id, CSV, now=NOW)
    c = company(db, "IT1")
    db.add(SiteEvidenceEvent(id="e1", run_id=run.id, ts=NOW, source="runner", level="error", code="company_error",
                             message="boom", data={"traceback": "Traceback ..."}))
    db.add(SiteEvidenceCompany(company_id=c.id, registration_number="IT1", status="site_unreachable", last_run_id=run.id,
                               site_detail="dns", finished_at=NOW))
    db.commit()
    bundle = json.loads(hub.audit_bundle_json(db, run.id))
    assert bundle["run"]["id"] == run.id and bundle["event_counts"] == {"company_error": 1}
    assert bundle["events"][0]["data"]["traceback"] == "Traceback ..."
    assert bundle["companies"][0]["site_detail"] == "dns" and bundle["worker"]["name"] == "Office PC"
    assert bundle["tasks"][0]["request"]["input_csv"] == "<company list omitted>"
    assert "token" not in json.dumps(bundle).lower().replace("token_hash", "")
    with pytest.raises(ValueError):
        hub.audit_bundle(db, "nope")


def test_events_can_be_filtered_by_level_kind_and_text(db):
    w = computer(db, reader=GOOD_INFO)
    run = _run(db, w)
    for i, (level, code, msg) in enumerate([("info", "site_settled", "ok"), ("warn", "site_unreachable", "Acme dns"),
                                            ("error", "company_error", "boom")]):
        db.add(SiteEvidenceEvent(id=f"e{i}", run_id=run.id, ts=NOW + timedelta(seconds=i), source="runner", level=level,
                                 code=code, message=msg))
    db.commit()
    assert [e.code for e in hub.events(db, run.id)] == ["company_error", "site_unreachable", "site_settled"]
    assert [e.code for e in hub.events(db, run.id, levels=["error", "warn"])] == ["company_error", "site_unreachable"]
    assert [e.code for e in hub.events(db, run.id, codes=["site_settled"])] == ["site_settled"]
    assert [e.code for e in hub.events(db, run.id, search="acme")] == ["site_unreachable"]


# ---- the page --------------------------------------------------------------------------------------------------------
def _page(db_path: str, extra: str = ""):
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {_PROJECT_ROOT!r})
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from views.site_evidence_panel import render_site_evidence_page
        engine = create_engine({f"sqlite:///{db_path}"!r}, connect_args={{"check_same_thread": False}})
        db = sessionmaker(bind=engine)()
        render_site_evidence_page(db)
    """)
    return AppTest.from_string(script).run(timeout=30)


def _file_db(tmp_path):
    path = (tmp_path / "page.db").as_posix()
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return path, sessionmaker(bind=engine)


def test_page_with_no_computer_explains_what_to_do(tmp_path):
    path, _ = _file_db(tmp_path)
    app = _page(path)
    assert not app.exception
    assert any("No run has been started yet" in i.value for i in app.info)
    assert any("Crawler Setup" in w.value for w in app.warning)


def test_page_says_why_a_computer_cannot_run_it(tmp_path):
    path, factory = _file_db(tmp_path)
    with factory() as db:
        w = worker_hub.create_worker(db, "Laptop")[0]
        w.last_seen_at = datetime.utcnow()
        w.info = {"protocol": worker_hub.PROTOCOL, "checks": {},
                  "site_evidence": {"available": True, "can_start": False,
                                    "blockers": ["Ollama (the local model) is not installed on this computer"]}}
        db.commit()
    app = _page(path)
    assert not app.exception
    text = " ".join(c.value for c in app.caption) + " ".join(w.value for w in app.warning)
    assert "Laptop" in text and "Ollama (the local model) is not installed" in text
    assert not [b for b in app.button if "Start the run" in b.label]


def test_page_shows_a_ready_computer_a_start_button_and_the_company_count(tmp_path):
    path, factory = _file_db(tmp_path)
    with factory() as db:
        w = computer(db, reader=GOOD_INFO, seen_ago=0)
        w.last_seen_at = datetime.utcnow()
        db.commit()
        company(db, "IT1")
        company(db, "IT2", status="shortlisted")
    app = _page(path)
    assert not app.exception
    assert any("2 companies will be sent" in c.value for c in app.caption)
    start = [b for b in app.button if "Start the run" in b.label]
    assert start
    start[0].click().run(timeout=30)
    assert not app.exception
    with factory() as db:
        run = db.query(SiteEvidenceRun).one()
        assert run.status == "queued" and run.input_total == 2
        assert db.query(CrawlerTask).one().request["input_csv"].splitlines()[1].startswith("IT2,")   # shortlist first


def test_page_shows_a_failed_run_with_its_reason_and_the_way_to_diagnose_it(tmp_path):
    path, factory = _file_db(tmp_path)
    with factory() as db:
        w = computer(db, reader=GOOD_INFO)
        w.last_seen_at = datetime.utcnow()
        _run(db, w, status="failed", last_error="Not started: Python is not installed", finished_at=datetime.utcnow(),
             requested_at=datetime.utcnow() - timedelta(minutes=5))
        db.commit()
    app = _page(path)
    assert not app.exception
    assert any("Failed" in s.value for s in app.subheader)
    assert any("Python is not installed" in e.value for e in app.error)
    assert any("Diagnostics" in e.value for e in app.error)


def test_page_shows_a_finished_run_as_finished(tmp_path):
    path, factory = _file_db(tmp_path)
    with factory() as db:
        w = computer(db, reader=GOOD_INFO)
        w.last_seen_at = datetime.utcnow()
        _run(db, w, status="finished", done=900, hits_total=210, signals_written=180, signals_skipped=12,
             requested_at=datetime.utcnow() - timedelta(hours=9), started_at=datetime.utcnow() - timedelta(hours=9),
             finished_at=datetime.utcnow() - timedelta(minutes=2), summary={"total": 900, "done": 900, "retry_pending": 7})
        db.commit()
    app = _page(path)
    assert not app.exception
    assert any(s.value.endswith("Finished") for s in app.subheader)
    md = " ".join(m.value for m in app.markdown)
    assert "900 of 900" in md and "210" in md and "180" in md and "7 site(s) could not be reached" in md


def test_page_shows_a_live_run_with_progress_the_current_company_and_a_stop_button(tmp_path):
    path, factory = _file_db(tmp_path)
    rid = "11111111-1111-1111-1111-111111111111"
    with factory() as db:
        w = computer(db, reader=live(rid, "waiting_ram", waiting_for="ram"))
        w.last_seen_at = datetime.utcnow()
        _run(db, w, last_report_at=datetime.utcnow() - timedelta(seconds=20), requested_at=datetime.utcnow() - timedelta(hours=1))
        db.commit()
    app = _page(path)
    assert not app.exception
    assert any("waiting for free memory" in s.value for s in app.subheader)
    captions = " ".join(c.value for c in app.caption)
    assert "Now reading: **Acme SRL** (reading pages)" in captions and "Waiting for: free memory" in captions
    assert [b for b in app.button if "Stop the run" in b.label]
    assert any("Run stuck?" in e.label for e in app.expander)
    assert not [b for b in app.button if "Start the run" in b.label]          # one run at a time
