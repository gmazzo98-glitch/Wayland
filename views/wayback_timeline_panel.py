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

import hashlib
import io
from typing import Optional

import pandas as pd
import streamlit as st

import worker_hub
import worker_installer
from models import SHORTLIST_STATUSES, Company, CrawlerTask, CrawlerWorker
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
    last_action = (db.query(CrawlerTask)
                   .filter_by(worker_id=worker_id, crawler="wayback_timeline")
                   .order_by(CrawlerTask.id.desc()).populate_existing().first())

    if info.get("error"):
        st.error(info["error"])
    if last_action and last_action.status == "error":
        st.error(f"Last {last_action.kind} request failed: {last_action.error or 'unknown worker error'}")
    elif last_action and last_action.status in ("queued", "running"):
        st.caption(f"{last_action.kind.capitalize()} request is {last_action.status} on the worker.")

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
        if last_log:
            label = "Latest" if running else "Last run's final line"
            st.caption(f"{label}: {last_log}")
    with cols[1]:
        if running:
            if st.button("Stop", type="secondary", width="stretch"):
                worker_hub.queue_wayback_action(db, worker_id, "stop")
                st.caption("Stop requested — the worker signals it within a few seconds; it finishes "
                          "the snapshot it's on, saves, then exits.")
        else:
            if st.button("Start", type="primary", width="stretch", disabled=info.get("pythonReady") is False):
                csv_text = st.session_state.get("wb_companies_csv")
                if not csv_text:
                    st.error("Add at least one company below first.")
                else:
                    worker_hub.queue_wayback_action(db, worker_id, "start", input_csv=csv_text)
                    st.caption("Start requested — the worker claims it within a couple of seconds.")


LIST_COLUMNS = ["company_id", "homepage_url", "name"]
DEFAULT_SHORTLIST_FILTER = ["shortlisted", "in_pilot"]


def _normalize_url(url) -> str:
    text = "" if url is None or (isinstance(url, float) and pd.isna(url)) else str(url).strip()
    if text and "://" not in text:
        text = "https://" + text
    return text


def _clean_list(df: pd.DataFrame) -> pd.DataFrame:
    df = df.reindex(columns=LIST_COLUMNS).fillna("").astype(str)
    df["company_id"] = df["company_id"].str.strip()
    df["homepage_url"] = df["homepage_url"].map(_normalize_url)
    df = df[(df["company_id"] != "") & (df["homepage_url"] != "")]
    return df.drop_duplicates("company_id", keep="first").reset_index(drop=True)


def _replace_list(df: pd.DataFrame) -> None:
    # data_editor applies its stored edits on top of whatever frame it is handed, so the
    # list can only be swapped by handing it a new frame under a new widget key.
    st.session_state["wb_base_df"] = df.reset_index(drop=True)
    st.session_state["wb_epoch"] = st.session_state.get("wb_epoch", 0) + 1
    st.rerun()


def _render_companies(db, worker_id: str) -> None:
    st.subheader("Companies")
    st.caption("Sent fresh with every Start — this computer has nowhere else to read the list from. "
               "Companies a previous run already finished are skipped automatically, so the list can "
               "keep growing between runs.")

    if "wb_base_df" not in st.session_state:
        existing_csv = _last_companies_csv(db, worker_id)
        base = pd.read_csv(io.StringIO(existing_csv)) if existing_csv else DEFAULT_COMPANIES
        st.session_state["wb_base_df"] = _clean_list(base)
        st.session_state["wb_epoch"] = 0

    edited = st.data_editor(st.session_state["wb_base_df"], num_rows="dynamic", width="stretch",
                            key=f"wb_companies_editor_{st.session_state['wb_epoch']}")
    current = _clean_list(edited)
    st.session_state["wb_companies_csv"] = current.to_csv(index=False)

    left, right = st.columns([3, 1])
    left.caption(f"{len(current)} {'company' if len(current) == 1 else 'companies'} in the list.")
    if right.button("Clear list", key="wb_clear_list", disabled=current.empty):
        _replace_list(current.iloc[0:0])

    _render_vienna_picker(db, current)


def _render_vienna_picker(db, current: pd.DataFrame) -> None:
    with st.expander("➕ Add companies from Vienna (shortlist and other columns)", expanded=True):
        rows = db.query(
            Company.registration_number, Company.legal_name, Company.website_url,
            Company.shortlist_status, Company.segment, Company.country, Company.sector_name,
            Company.need_score, Company.readiness_score,
        ).all()
        df = pd.DataFrame(rows, columns=[
            "registration_number", "legal_name", "website_url", "shortlist_status", "segment",
            "country", "sector", "need_score", "readiness_score",
        ])
        if df.empty:
            st.info("There are no companies in Vienna yet.")
            return

        c1, c2, c3 = st.columns(3)
        statuses = c1.multiselect("Shortlist status", list(SHORTLIST_STATUSES), default=DEFAULT_SHORTLIST_FILTER,
                                  key="wb_pick_status")
        segments = c2.multiselect("Segment (empty = any)", sorted(df["segment"].dropna().unique()),
                                  key="wb_pick_segment")
        countries = c3.multiselect("Country (empty = any)", sorted(df["country"].dropna().unique()),
                                   key="wb_pick_country")
        c4, c5, c6 = st.columns(3)
        sectors = c4.multiselect("Sector (empty = any)", sorted(df["sector"].dropna().unique()),
                                 key="wb_pick_sector")
        min_need = c5.number_input("Min need score (0 = ignore)", min_value=0.0, value=0.0, step=1.0,
                                   key="wb_pick_need")
        min_ready = c6.number_input("Min readiness score (0 = ignore)", min_value=0.0, value=0.0, step=1.0,
                                    key="wb_pick_ready")

        picked = df
        if statuses:
            picked = picked[picked["shortlist_status"].isin(statuses)]
        if segments:
            picked = picked[picked["segment"].isin(segments)]
        if countries:
            picked = picked[picked["country"].isin(countries)]
        if sectors:
            picked = picked[picked["sector"].isin(sectors)]
        if min_need > 0:
            picked = picked[picked["need_score"].fillna(-1) >= min_need]
        if min_ready > 0:
            picked = picked[picked["readiness_score"].fillna(-1) >= min_ready]

        has_site = picked["website_url"].fillna("").astype(str).str.strip() != ""
        no_site = int((~has_site).sum())
        picked = picked[has_site]
        already = picked["registration_number"].isin(set(current["company_id"]))
        n_already = int(already.sum())
        picked = picked[~already].reset_index(drop=True)

        notes = []
        if no_site:
            notes.append(f"{no_site} matching without a website (nothing to look up)")
        if n_already:
            notes.append(f"{n_already} already in the list")
        st.caption(f"{len(picked)} companies match and can be added" + (f" · {' · '.join(notes)}" if notes else "") + ".")

        if picked.empty:
            return

        table = pd.DataFrame({
            "Add": False, "Company": picked["legal_name"], "Reg. no.": picked["registration_number"],
            "Status": picked["shortlist_status"], "Segment": picked["segment"], "Country": picked["country"],
            "Sector": picked["sector"], "Website": picked["website_url"],
        })
        signature = hashlib.md5("|".join(picked["registration_number"]).encode()).hexdigest()[:10]
        ticked = st.data_editor(
            table, hide_index=True, width="stretch", height=320,
            disabled=[c for c in table.columns if c != "Add"],
            key=f"wb_pick_table_{st.session_state['wb_epoch']}_{signature}",
        )

        def _to_list_rows(mask) -> pd.DataFrame:
            chosen = picked[mask.to_numpy()]
            return pd.DataFrame({"company_id": chosen["registration_number"],
                                 "homepage_url": chosen["website_url"], "name": chosen["legal_name"]})

        n_ticked = int(ticked["Add"].sum())
        b1, b2 = st.columns(2)
        if b1.button(f"Add the {n_ticked} ticked", disabled=n_ticked == 0, key="wb_add_ticked"):
            _replace_list(_clean_list(pd.concat([current, _to_list_rows(ticked["Add"])])))
        if b2.button(f"Add all {len(picked)} shown", key="wb_add_all"):
            _replace_list(_clean_list(pd.concat([current, _to_list_rows(pd.Series(True, index=picked.index))])))

        st.caption("Each company is checked on its own: roughly 20 archived snapshots per claim, three claims, "
                   "one local-model call each. That is minutes per company on a free machine and hours on a "
                   "busy 8 GB one, so start with a short list and add more once the first batch is done.")
