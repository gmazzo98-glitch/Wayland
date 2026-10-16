"""
Wayback Timeline — settings/status panel for the standalone, opportunistic tool in the
Scraper repo (tools/wayback_timeline/) that walks a company's Wayback Machine history
with the local Ollama model to find when a claim (online store, sustainability
commitment, named certification) first appeared on its homepage.

Unlike Crawler Setup, there is no separate install step here: wayback_timeline rides the
SAME installed Vienna Crawler Worker, bundled alongside the Node crawlers (see
scripts/build_worker_bundle.py's collect_wayback_timeline). But it is dispatched
differently, because it runs for hours, not minutes:

  - Start/Stop are fire-and-forget tasks (worker_hub.queue_wayback_action), never
    run_remote's blocking wait-for-result — the worker claims one, acts immediately
    (spawns or signals a local process that keeps running on its own), and reports done
    right away. A multi-hour wait on a CrawlerTask would fight the 900s cap built into
    the worker's normal crawl-task path, and there is nothing to return anyway: the
    Python tool is resumable/stoppable/RAM-gated by itself (tools/wayback_timeline/
    ram_guard.py), the worker does not babysit it.
  - Status comes from the worker's own heartbeat 'info' (worker.mjs's
    waybackTimelineInfo), read the exact way crawler_setup.py already reads
    info.get('busy') etc. — not from a task result.
  - The company list has nowhere to live that both a hosted app and the worker's own
    machine can both reach, so it travels WITH every start request (request.input_csv,
    the same convention the existing crawlers already use for CSV input) rather than a
    shared file. The most recently dispatched list is read back from that task row to
    pre-fill the editor, which is simple but not a settings table — editing here always
    requires picking a worker, even if there is only one.
"""
from __future__ import annotations

import io
from typing import Optional

import pandas as pd
import streamlit as st

import worker_hub
import worker_installer
from models import CrawlerTask, CrawlerWorker
from worker_hub import list_workers, worker_status

DEFAULT_COMPANIES = pd.DataFrame(
    [{"company_id": "illy", "homepage_url": "https://www.illy.com"},
     {"company_id": "brembo", "homepage_url": "https://www.brembo.com"}]
)


def _usable_workers(db) -> list[CrawlerWorker]:
    expected = worker_installer.bundle_info()
    out = []
    for w in list_workers(db):
        db.refresh(w)
        status = worker_status(w, expected)
        if status["usable"]:
            out.append(w)
    return out


def _wb_info(w: CrawlerWorker) -> dict:
    return (w.info or {}).get("wayback_timeline") or {}


def _last_companies_csv(db, worker_id: str) -> Optional[str]:
    task = (db.query(CrawlerTask)
            .filter_by(worker_id=worker_id, crawler="wayback_timeline", kind="start")
            .order_by(CrawlerTask.id.desc()).first())
    if task and task.request and task.request.get("input_csv"):
        return task.request["input_csv"]
    return None


def render_wayback_timeline_page(db) -> None:
    st.title("🕰️ Wayback Timeline")
    st.caption(
        "A standalone, opportunistic pass: reads a company's Wayback Machine history with the "
        "local model and finds when claims like an online store or a sustainability commitment "
        "first appeared on its homepage. Runs on a computer with the Vienna Crawler Worker "
        "installed — nothing else in this app depends on it."
    )

    workers = _usable_workers(db)
    available = [w for w in workers if _wb_info(w).get("available")]
    if not available:
        if not workers:
            st.warning("No computer is set up to run crawls yet. Open **🖥️ Crawler Setup** to install the "
                      "worker on one.", icon="⚠️")
        else:
            st.warning(
                "A worker is online, but none of them have wayback_timeline yet — it ships with the crawler "
                "bundle, so it arrives on each computer's next auto-update (checked every 5 minutes) after "
                "this was deployed. Check back shortly.", icon="⚠️"
            )
        return

    names = {w.id: w.name for w in available}
    worker_id = available[0].id if len(available) == 1 else st.selectbox(
        "Computer", list(names), format_func=lambda i: names[i], key="wb_worker_choice")

    _render_status(db, worker_id, names[worker_id])
    st.markdown("---")
    _render_companies(db, worker_id)


@st.fragment(run_every=4)
def _render_status(db, worker_id: str, worker_name: str) -> None:
    w = db.get(CrawlerWorker, worker_id)
    if w is None:
        st.info("That computer was removed.")
        return
    db.refresh(w)
    info = _wb_info(w)
    running = bool(info.get("running"))

    cols = st.columns([2, 1])
    with cols[0]:
        if running:
            st.success(f"🟢 Running on **{worker_name}** (process {info.get('pid')})", icon="🟢")
        else:
            st.info(f"⚪ Not running on **{worker_name}**", icon="⚪")
        total = info.get("total") or 0
        if total:
            st.caption(f"{info.get('done', 0)} of {total} company/claim pairs checked · "
                      f"{info.get('hits', 0)} confirmed")
        last_log = info.get("lastLog")
        if last_log and not running:
            st.caption(f"Last run's final line: {last_log}")
    with cols[1]:
        if running:
            if st.button("Stop", type="secondary", width="stretch"):
                worker_hub.queue_wayback_action(db, worker_id, "stop")
                st.caption("Stop requested — the worker signals it within a few seconds; it finishes "
                          "the snapshot it's on, saves, then exits.")
        else:
            if st.button("Start", type="primary", width="stretch"):
                csv_text = st.session_state.get("wb_companies_csv")
                if not csv_text:
                    st.error("Add at least one company below first.")
                else:
                    worker_hub.queue_wayback_action(db, worker_id, "start", input_csv=csv_text)
                    st.caption("Start requested — the worker claims it within a couple of seconds.")


def _render_companies(db, worker_id: str) -> None:
    st.subheader("Companies")
    st.caption("Sent fresh with every Start — this computer has nowhere else to read the list from.")

    existing_csv = _last_companies_csv(db, worker_id)
    if existing_csv:
        df = pd.read_csv(io.StringIO(existing_csv))
    else:
        df = DEFAULT_COMPANIES

    edited = st.data_editor(df, num_rows="dynamic", width="stretch", key="wb_companies_editor")
    st.session_state["wb_companies_csv"] = edited.to_csv(index=False)
