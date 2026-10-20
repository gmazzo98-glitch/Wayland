"""
vienna_worker_site_evidence_report, tested against a REAL PostgreSQL (the function is PL/pgSQL, which
SQLite cannot run), through the real bootstrap path (worker_hub.ensure_rpc) and the real shim route.

These tests never touch Neon: they start a throwaway local PostgreSQL with the `pgserver` package and
skip themselves where it is not installed. To run them:

    python -m venv .pgvenv && .pgvenv\\Scripts\\pip install pgserver psycopg2-binary sqlalchemy pytest \\
        python-dotenv fastapi httpx pydantic requests
    .pgvenv\\Scripts\\python -m pytest tests/test_site_evidence_sql.py

What they pin down is what makes an unattended run safe to leave alone: the same batch sent twice changes
nothing, one bad company never blocks the others, a real value from another source is never overwritten,
and every change the database makes is audited with the row it replaced.
"""

import json
import runpy
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

pgserver = pytest.importorskip("pgserver")

import worker_hub  # noqa: E402
from models import (Base, Company, CrawlerTask, RawImportRecord, SignalRecord, SiteEvidenceCompany,  # noqa: E402
                    SiteEvidenceEvent, SiteEvidenceRun, SourceHealth)

SOURCE = "Site Evidence Reader"
RUN = "11111111-1111-1111-1111-111111111111"
NOW_ISO = "2026-10-20T10:00:00+00:00"


@pytest.fixture(scope="module")
def pg(tmp_path_factory):
    server = pgserver.get_server(str(tmp_path_factory.mktemp("pgdata")))
    uri = server.get_uri().replace("postgresql://", "postgresql+psycopg2://", 1)
    engine = create_engine(uri)
    Base.metadata.create_all(engine)
    assert worker_hub.ensure_rpc(engine) is True          # the same path the app takes at start
    yield engine, uri
    engine.dispose()
    server.cleanup()


@pytest.fixture
def env(pg):
    """A clean database with one worker (token returned) and three companies."""
    engine, uri = pg
    with engine.begin() as conn:
        conn.execute(text("truncate site_evidence_events, site_evidence_runs, site_evidence_companies, signal_records, "
                          "raw_import_records, source_health, crawler_tasks, crawler_workers, companies cascade"))
    db = sessionmaker(bind=engine)()
    worker, token = worker_hub.create_worker(db, "Test PC")
    for i, reg in enumerate(("IT001", "IT002", "IT003")):
        db.add(Company(id=f"c{i + 1}", legal_name=f"COMPANY {i + 1} SRL", registration_number=reg))
    db.commit()
    yield type("Env", (), {"engine": engine, "uri": uri, "db": db, "worker": worker, "token": token})
    db.close()


def call(env, report, token=None):
    with env.engine.begin() as conn:
        cur = conn.connection.cursor()
        cur.execute("select vienna_worker_site_evidence_report(%s, %s::jsonb)",
                    (token if token is not None else env.token, json.dumps(report)))
        return cur.fetchone()[0]


def signal(key="university_partnership", value=1.0, conf=0.6, summary="own site documents a university partnership"):
    return {"signal_key": key, "value": value, "status": "present", "confidence": conf, "summary": summary,
            "evidence": {"quote": "Collaboriamo con il Politecnico di Torino su progetti di ricerca.",
                         "source_urls": ["https://acme.it/ricerca"], "page_read": "live"}}


def item(reg="IT001", signals=None, status="done", finished=NOW_ISO, rules=4, **record):
    hits = [{"claim": "university_partnership", "quote": "Collaboriamo con il Politecnico di Torino su progetti."}] if signals else []
    rec = {"company_id": reg, "name": f"NAME {reg}", "status": status, "attempts": 1, "pages_read": 7, "via": "live",
           "seconds": 12.5, "hits": [h["claim"] for h in hits], "windows": {"university_partnership": 2},
           "rejected": {"rules": 1}, "rejected_samples": [{"claim": "x", "reason": "rules", "quote": "q"}],
           "pages_failed": [], "site_detail": ""}
    rec.update(record)
    return {"v": 1, "company_id": reg, "name": f"NAME {reg}", "finished_at": finished, "rules_version": rules,
            "record": rec, "hits": hits, "signals": signals or []}


def report(companies=(), events=(), run=None, run_id=RUN):
    return {"run_id": run_id, "task_id": None, "run": run or {"status": "running"}, "events": list(events),
            "companies": list(companies)}


def event(i, level="info", code="runner_started", message="hello", source="runner", data=None):
    return {"id": f"{i:032x}", "ts": NOW_ISO, "source": source, "run_id": RUN, "level": level, "code": code,
            "message": message, "data": data or {}}


def signals_for(env, company_id):
    env.db.expire_all()
    return env.db.query(SignalRecord).filter_by(company_id=company_id).all()


def events_of(env, code=None):
    env.db.expire_all()
    q = env.db.query(SiteEvidenceEvent).filter_by(run_id=RUN)
    if code:
        q = q.filter_by(code=code)
    return q.order_by(SiteEvidenceEvent.ts, SiteEvidenceEvent.id).all()


# ---- who may call it and with what --------------------------------------------------------------------
def test_unknown_or_revoked_tokens_and_malformed_reports_are_refused_without_writing(env):
    assert call(env, report(), token="nope") == {"ok": False, "reason": "unknown_or_revoked"}
    assert call(env, [1, 2]) == {"ok": False, "reason": "bad_report"}
    assert call(env, {"events": []})["reason"] == "missing_run_id"
    assert env.db.query(SiteEvidenceRun).count() == 0
    worker_hub.revoke_worker(env.db, env.worker.id)
    assert call(env, report())["reason"] == "unknown_or_revoked"


def test_a_run_belongs_to_one_worker(env):
    other, other_token = worker_hub.create_worker(env.db, "Other PC")
    assert call(env, report())["ok"] is True
    assert call(env, report(), token=other_token) == {"ok": False, "reason": "run_belongs_to_another_worker"}


# ---- the run row -------------------------------------------------------------------------------------
def test_run_progress_status_and_timestamps_follow_the_workers_reports(env):
    r = call(env, report(run={"status": "running", "started_at": NOW_ISO, "total": 937, "done": 10, "hits_total": 1,
                              "summary": {"eta_seconds": 3600}, "info": {"tool_build": "abc123def456"}}))
    assert r["ok"] and r["error_count"] == 0
    run = env.db.get(SiteEvidenceRun, RUN)
    assert run.status == "running" and run.input_total == 937 and run.done == 10 and run.finished_at is None
    assert run.started_at == datetime(2026, 10, 20, 10, 0, 0) and run.summary == {"eta_seconds": 3600}
    assert run.info["tool_build"] == "abc123def456" and run.last_report_at is not None

    call(env, report(run={"status": "finished", "done": 937, "finished_at": "2026-10-21T04:00:00+00:00"}))
    env.db.expire_all()
    run = env.db.get(SiteEvidenceRun, RUN)
    assert run.status == "finished" and run.finished_at == datetime(2026, 10, 21, 4, 0, 0) and run.done == 937

    # Late results of an ended run (a resync, a batch in flight) say "running"; they must never reopen it.
    call(env, report(run={"status": "running", "done": 937}))
    env.db.expire_all()
    run = env.db.get(SiteEvidenceRun, RUN)
    assert run.status == "finished" and run.finished_at == datetime(2026, 10, 21, 4, 0, 0)

    call(env, report(run={"status": "failed", "last_error": "crash loop"}))   # an end can still be corrected to another end
    env.db.expire_all()
    run = env.db.get(SiteEvidenceRun, RUN)
    assert run.status == "failed" and run.last_error == "crash loop" and run.finished_at is not None
    call(env, report(run={"status": "banana"}))              # an unknown status is ignored, never stored
    env.db.expire_all()
    assert env.db.get(SiteEvidenceRun, RUN).status == "failed"


# ---- events ----------------------------------------------------------------------------------------------
def test_events_are_idempotent_and_a_bad_one_does_not_block_the_batch(env):
    batch = [event(1), event(2, "warn", "ram_waiting"), {"id": "x" * 40 + "z", "level": "info", "ts": "not a date"},
             event(3, "banana", "weird_level")]
    r = call(env, report(events=batch))
    assert r["events"] == 3 and r["error_count"] == 1 and r["errors"][0]["scope"] == "event"
    again = call(env, report(events=batch))
    assert again["events"] == 0                                # nothing duplicated
    assert len(events_of(env)) == 3
    assert {e.level for e in events_of(env)} == {"info", "warn"}   # an unknown level is stored as info
    ev = events_of(env, "ram_waiting")[0]
    assert ev.source == "runner" and ev.message == "hello" and ev.ts == datetime(2026, 10, 20, 10, 0, 0)


# ---- per-company outcomes ---------------------------------------------------------------------------------
def test_company_outcome_is_stored_even_when_nothing_was_found_and_older_results_never_win(env):
    call(env, report(companies=[item("IT001", status="site_unreachable", site_detail="dns: no such host",
                                     pages_failed=[{"url": "https://x.it/", "kind": "dns"}])]))
    env.db.expire_all()
    row = env.db.query(SiteEvidenceCompany).one()
    assert row.status == "site_unreachable" and row.site_detail == "dns: no such host" and row.registration_number == "IT001"
    assert row.pages_failed == [{"url": "https://x.it/", "kind": "dns"}] and row.last_run_id == RUN
    assert env.db.query(SignalRecord).count() == 0               # checked is not a signal

    call(env, report(companies=[item("IT001", status="done", finished="2026-10-20T12:00:00+00:00")]))
    call(env, report(companies=[item("IT001", status="partial", finished="2026-10-20T09:00:00+00:00")]))  # older
    env.db.expire_all()
    assert env.db.query(SiteEvidenceCompany).one().status == "done"


def test_unknown_company_is_reported_and_audited_while_the_rest_of_the_batch_is_stored(env):
    r = call(env, report(companies=[item("IT999"), item("IT001", signals=[signal()]), item("IT002")]))
    assert r["companies"] == 2 and r["error_count"] == 1
    assert r["errors"][0] == {"scope": "company", "company_id": "IT999",
                              "error": "no company has this registration number"}
    assert [e.code for e in events_of(env, "company_unmatched")] == ["company_unmatched"]
    assert env.db.query(SiteEvidenceCompany).count() == 2


def test_one_poison_record_rolls_back_only_that_company(env):
    poison = item("IT002", signals=[signal()], attempts="not a number")
    r = call(env, report(companies=[item("IT001", signals=[signal()]), poison, item("IT003", signals=[signal()])]))
    assert r["companies"] == 2 and r["error_count"] == 1 and r["errors"][0]["company_id"] == "IT002"
    assert len(signals_for(env, "c1")) == 1 and len(signals_for(env, "c3")) == 1
    assert signals_for(env, "c2") == []                          # its half-written work was rolled back
    assert env.db.query(SiteEvidenceCompany).filter_by(registration_number="IT002").count() == 0
    assert events_of(env, "company_report_failed")[0].level == "error"


# ---- signals: the guard ---------------------------------------------------------------------------------
def test_a_signal_is_created_updated_in_place_and_carries_its_evidence(env):
    r = call(env, report(companies=[item("IT001", signals=[signal(summary="first")])]))
    assert r["ok"]
    rows = signals_for(env, "c1")
    assert len(rows) == 1
    sig = rows[0]
    assert (sig.signal_key, sig.source, sig.status, sig.is_simulated, sig.numeric_value, sig.confidence) == (
        "university_partnership", SOURCE, "present", False, 1.0, 0.6)
    payload = json.loads(sig.raw_payload_ref)
    assert payload["run_id"] == RUN and payload["simulated"] is False and payload["evidence"]["quote"].startswith("Collabor")
    assert sig.text_value == "first"

    call(env, report(companies=[item("IT001", signals=[signal(summary="second", conf=0.45)],
                                     finished="2026-10-20T11:00:00+00:00")]))
    rows = signals_for(env, "c1")
    assert len(rows) == 1 and rows[0].text_value == "second" and rows[0].confidence == 0.45
    assert [e.code for e in events_of(env) if e.code.startswith("signal_")] == ["signal_created", "signal_updated"]


def test_the_same_batch_twice_changes_nothing(env):
    batch = report(companies=[item("IT001", signals=[signal()])], events=[event(1)])
    call(env, batch)
    snapshot = lambda: (len(signals_for(env, "c1")), len(events_of(env)), env.db.query(RawImportRecord).count())
    before = snapshot()
    call(env, batch)
    assert snapshot() == before
    env.db.expire_all()
    assert env.db.get(SiteEvidenceRun, RUN).signals_written == 1     # derived from the audit trail, not incremented


def test_a_real_signal_from_another_source_is_never_overwritten_and_the_skip_is_audited(env):
    env.db.add_all([
        SignalRecord(company_id="c1", signal_key="university_partnership", source="Google News RSS", status="present",
                     is_simulated=False, numeric_value=1.0, text_value="a headline"),
        SignalRecord(company_id="c2", signal_key="university_partnership", source="Other Source", status="absent",
                     is_simulated=False, numeric_value=0.0),
    ])
    env.db.commit()
    r = call(env, report(companies=[item("IT001", signals=[signal()]), item("IT002", signals=[signal()])]))
    assert r["ok"] and r["error_count"] == 0
    assert [(s.source, s.text_value) for s in signals_for(env, "c1")] == [("Google News RSS", "a headline")]
    assert [(s.source, s.status) for s in signals_for(env, "c2")] == [("Other Source", "absent")]
    skipped = {e.data["registration_number"]: e for e in events_of(env, "signal_skipped")}
    assert skipped["IT001"].level == "warn" and "already supplied by Google News RSS" in skipped["IT001"].message
    assert "recorded this as absent" in skipped["IT002"].data["reason"]
    env.db.expire_all()
    assert env.db.get(SiteEvidenceRun, RUN).signals_skipped == 2 and env.db.get(SiteEvidenceRun, RUN).signals_written == 0


def test_a_simulated_placeholder_is_replaced_and_the_audit_keeps_the_row_it_replaced(env):
    env.db.add(SignalRecord(company_id="c1", signal_key="university_partnership", source="News/Press",
                            status="not_yet_checked", is_simulated=True, numeric_value=None, confidence=0.5))
    env.db.commit()
    call(env, report(companies=[item("IT001", signals=[signal()])]))
    rows = signals_for(env, "c1")
    assert len(rows) == 1 and rows[0].source == SOURCE and rows[0].status == "present"
    ev = events_of(env, "signal_replaced")[0]
    previous = ev.data["previous"]
    assert previous["source"] == "News/Press" and previous["status"] == "not_yet_checked" and previous["is_simulated"] is True
    assert ev.data["action"] == "replaced" and ev.data["signal_key"] == "university_partnership"


def test_only_the_five_existing_indicators_with_sane_values_and_current_rules_are_ever_written(env):
    r = call(env, report(companies=[
        item("IT001", signals=[signal("brand_new_indicator"), signal("international_sales_volume", value=5000),
                               signal("years_international_activity", value=-3),
                               signal("international_sales_volume", value=70.0)]),
        item("IT002", signals=[signal()], rules=3),
    ]))
    assert r["error_count"] == 0
    assert [(s.signal_key, s.numeric_value) for s in signals_for(env, "c1")] == [("international_sales_volume", 70.0)]
    assert signals_for(env, "c2") == []
    reasons = sorted(e.data["reason"] for e in events_of(env, "signal_skipped"))
    assert any("not one of the indicators" in x for x in reasons) and any("out of range" in x for x in reasons)
    assert any("older than the required" in x for x in reasons)


def test_hits_are_kept_as_a_blob_only_when_there_are_any_and_source_health_is_updated(env):
    call(env, report(companies=[item("IT001", signals=[signal()]), item("IT002")]))
    env.db.expire_all()
    blobs = env.db.query(RawImportRecord).all()
    assert [(b.company_id, b.dataset_name) for b in blobs] == [("c1", "crawler_site_evidence")]
    assert blobs[0].raw_row["run_id"] == RUN and blobs[0].raw_row["hits"][0]["claim"] == "university_partnership"
    assert blobs[0].raw_row["coverage"]["pages_read"] == 7
    health = env.db.get(SourceHealth, SOURCE)
    assert health.phase == 7 and health.mode == "live" and health.last_status == "success" and health.total_calls == 2
    call(env, report(companies=[item("IT003")]))
    env.db.expire_all()
    assert env.db.get(SourceHealth, SOURCE).total_calls == 3


# ---- bootstrap and the real shim route -----------------------------------------------------------------
def test_ensure_rpc_is_idempotent_and_the_function_is_locked_to_workers(pg):
    engine, _ = pg
    assert worker_hub.ensure_rpc(engine) is True                 # marker matches: nothing is re-applied
    with engine.connect() as conn:
        rls = dict(conn.execute(text("select relname, relrowsecurity from pg_class where relname like 'site_evidence_%' "
                                     "and relkind = 'r'")).fetchall())
        public_can_execute = conn.execute(text(
            "select has_function_privilege('public', 'vienna_worker_site_evidence_report(text, jsonb)', 'execute')")).scalar()
    assert rls == {"site_evidence_runs": True, "site_evidence_events": True, "site_evidence_companies": True}
    assert public_can_execute is False


def test_the_shim_forwards_the_new_function_and_nothing_else_new(env, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("DATABASE_URL", env.uri.replace("+psycopg2", ""))
    shim = runpy.run_path(str(Path(__file__).resolve().parents[1] / "worker_shim" / "app" / "main.py"))
    client = TestClient(shim["app"])
    body = {"p_token": env.token, "p_report": report(companies=[item("IT001", signals=[signal()])], events=[event(7)])}
    res = client.post("/rpc/vienna_worker_site_evidence_report", json=body)
    assert res.status_code == 200 and res.json()["ok"] is True and res.json()["companies"] == 1
    assert len(signals_for(env, "c1")) == 1
    assert client.post("/rpc/vienna_worker_site_evidence_report", json={"p_token": env.token}).status_code == 400
    assert client.post("/rpc/drop_everything", json={}).status_code == 404
    assert "vienna_worker_site_evidence_report" in shim["FUNCTIONS"] and len(shim["FUNCTIONS"]) == 4
