"""
Site Evidence Reader — start, watch and audit the local-model reading of companies' own websites (T25/T26).

The run itself happens on a computer with the Vienna Crawler Worker installed and a local Ollama model; it
carries on with nobody watching, this app closed, and Claude not running. This page is only the control
surface: it queues the start, then shows what the worker reports (heartbeat) and what the database has
received (run, audit events, per-company outcomes). All logic lives in site_evidence_hub.py.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st

import site_evidence_hub as hub
import worker_hub
import worker_installer
from models import SITE_EVIDENCE_RUN_ACTIVE, SITE_EVIDENCE_RUN_FINAL, CrawlerWorker, SiteEvidenceRun
from worker_hub import list_workers

_TONE = {"good": st.success, "warn": st.warning, "bad": st.error, "neutral": st.info}
_ICON = {"good": "🟢", "warn": "🟠", "bad": "🔴", "neutral": "⚪"}


def _when(value: Optional[datetime]) -> str:
    return value.strftime("%Y-%m-%d %H:%M") + " UTC" if value else "–"


def _eta(seconds: Optional[float]) -> str:
    return hub.format_duration(seconds) if seconds else "–"


# ---- page ---------------------------------------------------------------------------------------------------------
def render_site_evidence_page(db) -> None:
    st.title("📖 Site Evidence Reader")
    st.caption(
        "A local language model reads each company's own website and looks for what the company itself says about "
        "university partnerships, external collaboration, open innovation and export activity. Every quote is checked "
        "in code against the page before it counts. The run happens on a computer with the Vienna Crawler Worker and "
        "the local model installed, keeps going by itself for hours or days, and does not need this page, your browser "
        "or anyone's attention. Results go to the database and are also kept on that computer."
    )

    runs = _recent_runs(db)
    current = runs[0] if runs else None
    active = current is not None and current.status in SITE_EVIDENCE_RUN_ACTIVE
    if current is not None:
        # Refresh every few seconds while a run is live, rarely once nothing is.
        st.fragment(run_every=4 if active else 30)(_render_status)(db, current.id)
    else:
        st.info("No run has been started yet.", icon="ℹ️")

    tab_start, tab_history, tab_audit, tab_companies, tab_diag = st.tabs(
        ["▶️ Start a run", "🗂️ Runs", "🔎 Audit trail", "🏢 Per company", "🩺 Diagnostics"])
    with tab_start:
        _render_start(db, active)
    with tab_history:
        _render_history(db, runs)
    with tab_audit:
        _render_audit(db, runs)
    with tab_companies:
        _render_companies(db)
    with tab_diag:
        _render_diagnostics(db, runs)

    with st.expander("🕰️ Older tool: Wayback Timeline (superseded)", expanded=False):
        st.caption(
            "The Wayback Timeline pass that used to live on its own page found when a homepage first showed a claim. "
            "It never produced a result anything else used, so the reader above replaced it: the Wayback Machine is now "
            "used inside the reader, to read an archived copy when a site is down and to date a confirmed quote. This "
            "panel stays only for anyone who still has a list queued there."
        )
        from views.wayback_timeline_panel import render_wayback_timeline_page
        render_wayback_timeline_page(db, embedded=True)


def _recent_runs(db, limit: int = 30) -> List[SiteEvidenceRun]:
    return (db.query(SiteEvidenceRun).populate_existing().order_by(SiteEvidenceRun.requested_at.desc()).limit(limit).all())


# ---- live status --------------------------------------------------------------------------------------------------
def _render_status(db, run_id: str) -> None:
    run = db.query(SiteEvidenceRun).filter_by(id=run_id).populate_existing().first()
    if run is None:
        return
    view = hub.view_of(db, run)
    worker = hub.fresh_worker(db, run.worker_id)
    name = worker.name if worker else "(removed computer)"
    p = view["progress"]

    st.subheader(f"{_ICON[view['tone']]} {view['label']}")
    _TONE[view["tone"]](f"**{name}** · started {_when(run.requested_at)}\n\n{view['detail']}".strip(), icon=None)
    for alert in view["alerts"]:
        {"error": st.error, "warn": st.warning, "info": st.info}.get(alert["level"], st.info)(alert["text"])

    total, done = p.get("total") or 0, p.get("done") or 0
    if total:
        st.progress(min(1.0, done / total), text=f"{done} of {total} companies")
    cols = st.columns(4)
    cols[0].metric("Companies read", done)
    cols[1].metric("Quotes confirmed", p.get("hits_total") or 0)
    cols[2].metric("Signals written", run.signals_written or 0, f"{run.signals_skipped or 0} left alone" if run.signals_skipped else None,
                   delta_color="off")
    cols[3].metric("Time left", _eta(p.get("eta_seconds")) if view["state"] in ("running", "waiting_ram", "waiting_model") else "–")

    cur = p.get("current")
    if cur and run.status in SITE_EVIDENCE_RUN_ACTIVE:
        who = cur.get("name") or cur.get("company_id") or "" if isinstance(cur, dict) else str(cur)
        step = f" ({cur['step']})" if isinstance(cur, dict) and cur.get("step") else ""
        st.caption(f"Now reading: **{who}**{step}")
    if view["waiting_for"]:
        st.caption("Waiting for: " + {"ram": "free memory", "model": "the local model"}.get(view["waiting_for"], str(view["waiting_for"])))
    ev = view["last_event"]
    if ev:
        st.caption("Latest event: " + (f"{str(ev.get('ts', ''))[:19].replace('T', ' ')} · {ev.get('code')}: {ev.get('message')}"
                                       if isinstance(ev, dict) else str(ev)))
    parts = []
    if view["heartbeat_age"] is not None:
        parts.append(f"computer last heard {hub.format_duration(view['heartbeat_age'])} ago")
    if view["report_age"] is not None:
        parts.append(f"database last updated {hub.format_duration(view['report_age'])} ago")
    if p.get("avg_seconds"):
        parts.append(f"{p['avg_seconds']:.0f} s per company on average")
    if parts:
        st.caption(" · ".join(parts).capitalize())

    if p.get("by_status"):
        st.caption("Outcomes so far: " + ", ".join(
            f"{n} {hub.COMPANY_STATUS_LABELS.get(s, s or 'unknown').lower()}" for s, n in sorted(p["by_status"].items(), key=lambda kv: -kv[1])))

    if run.status in SITE_EVIDENCE_RUN_FINAL:
        _final_summary(run, view)

    if run.status in SITE_EVIDENCE_RUN_ACTIVE:
        task = hub.fresh_task(db, run.task_id)
        if run.status == "queued" and task is not None and task.status == "queued":
            if st.button("Withdraw this start", key=f"se_withdraw_{run.id}"):
                if hub.cancel_unclaimed(db, run):
                    st.rerun()
        elif run.status != "stopping":
            if st.button("⏹️ Stop the run", key=f"se_stop_{run.id}", help="Stops cleanly after the company it is on. "
                         "Everything read so far is kept and the next start continues where this one left off."):
                hub.queue_stop(db, run)
                st.rerun(scope="fragment")
        else:
            st.caption("A stop was requested; it finishes the company it is on, then ends.")
        with st.expander("Run stuck? Mark it as ended"):
            st.caption("Only if the computer is gone for good (reinstalled, wiped) and never reported that it finished: this closes "
                       "the run in the database so a new one can be started. If the computer is in fact still working, its "
                       "results keep arriving and are stored.")
            if st.checkbox("I understand", key=f"se_abandon_ok_{run.id}") and st.button("Mark this run as ended", key=f"se_abandon_{run.id}"):
                hub.abandon(db, run)
                st.rerun()

    if view["state"] == "failed":
        st.error("Open **🩺 Diagnostics** below: the audit bundle and the computer's own report hold everything needed to fix this.")


def _final_summary(run: SiteEvidenceRun, view: Dict[str, Any]) -> None:
    s = run.summary or {}
    p = view["progress"]
    lines = [f"**{p.get('done') or 0} of {p.get('total') or run.input_total or 0}** companies were read; "
             f"**{p.get('hits_total') or 0}** quotes were confirmed; **{run.signals_written or 0}** signals written to the database"
             + (f" and {run.signals_skipped} left alone because another source already had a firmer answer." if run.signals_skipped else ".")]
    pending = s.get("retry_pending")
    if pending:
        lines.append(f"{pending} site(s) could not be reached this time; a new start retries them after the waiting period.")
    if run.started_at and run.finished_at:
        lines.append(f"It ran for {hub.format_duration((run.finished_at - run.started_at).total_seconds())}.")
    st.markdown("  \n".join(lines))


# ---- starting -----------------------------------------------------------------------------------------------------
def _capable_computers(db):
    expected = worker_installer.bundle_info()
    out = []
    for w in list_workers(db):
        db.refresh(w)
        out.append((w, hub.capability(w, expected)))
    return out


def _render_start(db, active: bool) -> None:
    computers = _capable_computers(db)
    if not computers:
        st.warning("No computer is set up yet. Open **🖥️ Crawler Setup** and install the worker on the computer that has "
                   "the local model.", icon="⚠️")
        return
    ready = [(w, c) for w, c in computers if c["can_start"]]
    for w, c in computers:
        if not c["can_start"]:
            st.caption(f"❌ **{w.name}** cannot run it: " + " ".join(c["reasons"] or ["it has not reported yet."]))
    if not ready:
        st.warning("None of the computers can run the reader right now. The reasons are listed above; once that computer "
                   "has the local model and the current worker, it appears here by itself.", icon="⚠️")
        return

    names = {w.id: w.name for w, _ in ready}
    worker_id = next(iter(names)) if len(names) == 1 else st.selectbox("Run it on", list(names), format_func=names.get,
                                                                         key="se_worker_choice")
    info = hub.reader_info(next(w for w, _ in ready if w.id == worker_id))
    models = ", ".join((info.get("ollama") or {}).get("models") or [])
    st.caption(f"**{names[worker_id]}** · Python and the local model are installed" + (f" ({models})" if models else ""))

    if active:
        st.info("A run is already active. Stop it above, or wait until it has finished, before starting another.", icon="ℹ️")
        return

    c1, c2 = st.columns(2)
    only_shortlist = c1.checkbox("Only shortlisted and pilot companies", value=False, key="se_only_shortlist",
                                 help="Leave off for the full run. The shortlist is always read first either way.")
    retry_now = c2.checkbox("Retry sites that were unreachable earlier", value=False, key="se_retry_now",
                            help="Normally a site that could not be reached is retried six hours later. Tick this to retry "
                                 "everything now (for example after fixing the computer's internet connection).")
    with st.expander("Memory limits (leave as they are unless the computer struggles)"):
        m1, m2 = st.columns(2)
        to_load = m1.number_input("Free memory needed to load the model (GB)", 0.5, 16.0, 1.0, 0.5, key="se_gb_load")
        loaded = m2.number_input("Free memory needed to keep going (GB)", 0.1, 16.0, 0.3, 0.1, key="se_gb_loaded")

    csv_text, n, left_out = hub.build_company_list(db, only_shortlist=only_shortlist)
    st.caption(f"{n} companies will be sent; {left_out} have no website or no registration number and are left out. "
               "Companies already read with the current rules are skipped by the reader itself, so starting again "
               "never repeats finished work.")
    if n == 0:
        st.warning("There is nothing to read: no company has a website on file.")
        return

    if st.button(f"▶️ Start the run on {names[worker_id]}", type="primary", key="se_start"):
        options = {"retry_after_hours": 0 if retry_now else 6, "min_free_gb_to_load": float(to_load),
                   "min_free_gb_loaded": float(loaded), "only_shortlist": only_shortlist}
        try:
            hub.queue_start(db, worker_id, csv_text, options)
        except ValueError as exc:
            st.error(str(exc))
        else:
            st.rerun()


# ---- history ------------------------------------------------------------------------------------------------------
def _render_history(db, runs: List[SiteEvidenceRun]) -> None:
    if not runs:
        st.caption("Nothing here yet.")
        return
    workers = {w.id: w.name for w in list_workers(db, include_revoked=True)}
    rows = []
    for r in runs:
        ended = r.finished_at or r.last_report_at
        rows.append({"Started": _when(r.requested_at), "Computer": workers.get(r.worker_id, "?"), "State": r.status,
                     "Companies": f"{r.done or 0} / {r.input_total or 0}", "Quotes": r.hits_total or 0,
                     "Signals written": r.signals_written or 0, "Left alone": r.signals_skipped or 0,
                     "Ended / last report": _when(ended), "Run id": r.id})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")


# ---- audit trail --------------------------------------------------------------------------------------------------
def _run_label(r: SiteEvidenceRun) -> str:
    return f"{_when(r.requested_at)} · {r.status} · {r.done or 0}/{r.input_total or 0}"


def _pick_run(runs: List[SiteEvidenceRun], key: str) -> Optional[SiteEvidenceRun]:
    if not runs:
        st.caption("No runs yet.")
        return None
    by_id = {r.id: r for r in runs}
    chosen = st.selectbox("Run", list(by_id), format_func=lambda i: _run_label(by_id[i]), key=key)
    return by_id[chosen]


def _render_audit(db, runs: List[SiteEvidenceRun]) -> None:
    st.caption("Every step the run took, written on the computer and in the database: what was read, what was found, what "
               "was written, what was left alone and why, and every problem. Nothing is dropped; a problem becomes an "
               "event with the data needed to understand it.")
    run = _pick_run(runs, "se_audit_run")
    if run is None:
        return
    c1, c2, c3 = st.columns([2, 2, 3])
    levels = c1.multiselect("Level", ["error", "warn", "info"], default=["error", "warn"], key="se_audit_levels")
    known = sorted({code for (code,) in db.query(hub.SiteEvidenceEvent.code).filter_by(run_id=run.id).distinct().all()})
    codes = c2.multiselect("Kind", known, key="se_audit_codes")
    search = c3.text_input("Search in the message", key="se_audit_search")
    evs = hub.events(db, run.id, levels, codes, search, limit=1000)
    st.caption(f"{len(evs)} event(s) shown (newest first, at most 1000).")
    if not evs:
        st.success("No events match. With the default filter that means no errors and no warnings.", icon="✅")
        return
    st.dataframe(pd.DataFrame([{"Time (UTC)": e.ts.strftime("%Y-%m-%d %H:%M:%S"), "Level": e.level, "Kind": e.code,
                                "From": e.source, "What happened": e.message} for e in evs]),
                 hide_index=True, width="stretch", height=380)
    by_id = {e.id: e for e in evs[:300]}
    pick = st.selectbox("Details of one event", [""] + list(by_id), key="se_audit_event",
                        format_func=lambda i: "–" if not i else f"{by_id[i].ts:%H:%M:%S} {by_id[i].code} {by_id[i].message[:70]}")
    if pick:
        st.json(hub.event_dict(by_id[pick]))


# ---- per company --------------------------------------------------------------------------------------------------
def _render_companies(db) -> None:
    status = st.selectbox("Show", ["all"] + list(hub.COMPANY_STATUS_LABELS), key="se_company_status",
                          format_func=lambda s: "All companies" if s == "all" else hub.COMPANY_STATUS_LABELS[s])
    rows = hub.company_rows(db, None if status == "all" else status, limit=2000)
    if not rows:
        st.caption("No company has been read yet.")
        return
    st.caption(f"{len(rows)} compan{'y' if len(rows) == 1 else 'ies'}, most recently read first. "
               "“Read, nothing found” is a real answer: a company can collaborate without saying so on its own site.")
    st.dataframe(pd.DataFrame([{
        "Company": r["name"], "Reg. no.": r["registration_number"], "Outcome": hub.COMPANY_STATUS_LABELS.get(r["status"], r["status"]),
        "Found": ", ".join(r["hits"] or []) or "–", "Pages": r["pages_read"], "Source": r["via"] or "–",
        "Seconds": round(r["seconds"] or 0), "Tries": r["attempts"], "Read at": r["finished_at"] or "–"} for r in rows]),
        hide_index=True, width="stretch", height=380)
    by_reg = {r["registration_number"]: r for r in rows[:500]}
    pick = st.selectbox("Details of one company", [""] + list(by_reg), key="se_company_pick",
                        format_func=lambda i: "–" if not i else f"{by_reg[i]['name']} ({i})")
    if pick:
        row = by_reg[pick]
        if row.get("error"):
            st.error(row["error"])
        if row.get("site_detail"):
            st.caption(f"Site: {row['site_detail']}")
        st.json({k: row[k] for k in ("status", "attempts", "pages_read", "via", "rules_version", "hits", "windows", "rejected",
                                     "rejected_samples", "pages_failed", "last_run_id") if row.get(k) not in (None, [], {})})


# ---- diagnostics --------------------------------------------------------------------------------------------------
def _render_diagnostics(db, runs: List[SiteEvidenceRun]) -> None:
    st.caption("If a run fails, or does something unexpected, these two files hold everything needed to fix it. Send them "
               "to whoever is helping. They contain no keys, tokens or passwords; they do name the companies and pages "
               "the run read or had trouble with.")
    run = _pick_run(runs, "se_diag_run")
    if run is None:
        return
    st.markdown("**1 · Audit bundle** — the run, every event, every company outcome, the tasks and the computer's last report, "
                "taken from the database right now.")
    try:
        bundle = hub.audit_bundle_json(db, run.id)
        st.download_button("⬇️ Download the audit bundle (JSON)", bundle, file_name=f"site-evidence-audit-{run.id[:8]}.json",
                           mime="application/json", key="se_dl_audit")
    except ValueError as exc:
        st.error(str(exc))

    st.markdown("**2 · The computer's own report** — what the machine itself sees: memory, local model, processes, log "
                "tails and a copy of what it has not delivered yet. It works even if the run is stuck. The computer must be online.")
    c1, c2 = st.columns(2)
    if c1.button("🩺 Ask the computer for a full report", key="se_ask_diag"):
        hub.queue_diagnostics(db, run.worker_id)
        st.rerun()
    if c2.button("🔁 Re-send everything from the computer", key="se_resync",
                 help="Use after a database problem: the computer sends every result and event again. Nothing is duplicated."):
        hub.queue_resync(db, run.worker_id)
        st.success("Requested. The computer does it within a minute.")
    _render_diag_result(db, run)

    with st.expander("↩️ Undo what this run wrote"):
        _render_rollback(db, run)


@st.fragment(run_every=5)
def _render_diag_result(db, run: SiteEvidenceRun) -> None:
    task = hub.latest_diagnostics(db, run.worker_id)
    if task is None:
        return
    if task.status in ("queued", "running"):
        st.info(f"Report requested {_when(task.created_at)}: waiting for the computer…")
    elif task.status == "error":
        st.error(f"The computer could not produce the report: {task.error}")
    elif task.status == "cancelled":
        st.warning("The report request expired before the computer picked it up (is it online?).")
    elif task.status == "done":
        text = json.dumps(task.result, indent=1, default=str)
        st.success(f"Report received {_when(task.finished_at)} ({len(text) // 1024} KB).")
        st.download_button("⬇️ Download the computer's report (JSON)", text, file_name=f"site-evidence-computer-{run.id[:8]}.json",
                           mime="application/json", key=f"se_dl_diag_{task.id}")
        res = task.result or {}
        cap = res.get("capability") or {}
        if cap.get("blockers"):
            st.warning("Blocking right now: " + "; ".join(cap["blockers"]))
        if res.get("tool_error"):
            st.error(res["tool_error"])


def _render_rollback(db, run: SiteEvidenceRun) -> None:
    st.caption("Removes the signals this run created and puts back what it replaced, using only the audit trail. A signal that "
               "someone or something else has changed since is never touched. Nothing happens until you confirm.")
    plan = hub.rollback_plan(db, run.id)
    if not plan:
        st.caption("This run wrote no signals.")
        return
    counts: Dict[str, int] = {}
    for item in plan:
        counts[item["action"]] = counts.get(item["action"], 0) + 1
    st.write({"remove": counts.get("delete", 0), "restore the earlier value": counts.get("restore", 0),
              "leave alone": counts.get("leave", 0) + counts.get("nothing", 0)})
    st.dataframe(pd.DataFrame([{"Company": i["name"], "Signal": i["signal_key"], "Action": i["action"],
                                "Why": i.get("reason") or ""} for i in plan]), hide_index=True, width="stretch", height=240)
    sure = st.checkbox("I understand this changes the database", key=f"se_rb_sure_{run.id}")
    if st.button("Undo this run's signals", disabled=not sure, key=f"se_rb_go_{run.id}"):
        done = hub.rollback_apply(db, run.id)
        st.success(f"Done: {done['restored']} restored, {done['deleted']} removed, {done['left']} left alone.")


# ---- sidebar ------------------------------------------------------------------------------------------------------
def render_sidebar_status(db) -> None:
    """One line under the navigation on every page, so a run is never out of sight."""
    try:
        s = hub.sidebar_status(db)
    except Exception:  # the sidebar must never take a page down, nor leave the shared session in a failed transaction
        db.rollback()
        return
    if not s:
        return
    st.sidebar.markdown("**📖 Site evidence run**")
    st.sidebar.caption(f"{_ICON[s['tone']]} {s['label']}" + (f" · {s['progress']}" if s["progress"] else ""))
