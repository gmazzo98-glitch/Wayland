"""
Crawler Setup — where the Phase 7 crawlers run, and how a helper's computer gets set up to
run them.

The crawlers are Node/Playwright programs; a browser tab can't start programs on the viewer's
computer, and a hosted app has no Scraper folder of its own. So a helper installs the small
"Vienna Crawler Worker" once (one download, one double-click — see worker_installer.py), and
from then on it picks up crawls this app queues for it (worker_hub.py). This page is where that
download lives, and where the app shows whether each computer really has the right thing installed:
every worker reports its version, build id and self-test results on a heartbeat, and
worker_hub.worker_status turns that into ready / update recommended / broken / offline.
"""

from typing import Dict, Optional

import streamlit as st

import worker_hub
import worker_installer
from config import SUPABASE_ANON_KEY, SUPABASE_URL
from worker_hub import (BROKEN, INCOMPATIBLE, OFFLINE, OUTDATED, READY, REVOKED, WAITING,
                        list_workers, local_crawlers_available, revoke_worker, worker_status)

TARGET_KEY = "crawl_target_choice"
SETUP_FILE_KEY = "crawler_setup_file"
LOCAL = "local"

_BADGE = {READY: "🟢", OUTDATED: "🟡", BROKEN: "🔴", OFFLINE: "⚪", WAITING: "🕓", REVOKED: "⚫", INCOMPATIBLE: "🔴"}


def _ago(seconds: Optional[float]) -> str:
    if seconds is None:
        return "never"
    if seconds < 90:
        return f"{int(seconds)}s ago"
    if seconds < 5400:
        return f"{round(seconds / 60)} min ago"
    if seconds < 172800:
        return f"{round(seconds / 3600)} h ago"
    return f"{round(seconds / 86400)} days ago"


# ---- which computer crawls run on ------------------------------------------------------------------

def _options(db):
    """[(value, label, status-or-None)] — this machine first (when it can crawl), then every worker."""
    expected = worker_installer.bundle_info()
    options = []
    if local_crawlers_available():
        options.append((LOCAL, "This server (its own crawlers)", None))
    for w in list_workers(db):
        status = worker_status(w, expected)
        options.append((w.id, f"{w.name} — {status['label']}", status))
    return options


def resolve_crawl_target(db=None) -> Dict:
    """
    Where the next crawl will run, as {"target": None | worker_id, "label", "ok", "problem"}.
    None = this machine. The choice lives in the session (the picker below); with no choice
    yet it is this machine when that can crawl, else the first computer that is ready.
    """
    if db is None:
        from database import get_db_session
        db = get_db_session()
    options = _options(db)
    by_value = {value: (label, status) for value, label, status in options}
    choice = st.session_state.get(TARGET_KEY)
    if choice not in by_value:
        usable = [v for v, _, status in options if status is None or status["usable"]]
        choice = usable[0] if usable else None

    if choice is None:
        return {"target": None, "label": "no computer set up", "ok": False,
                "problem": "No computer is set up to run the crawlers yet. Open 🖥️ Crawler Setup to install "
                           "the worker on one."}
    label, status = by_value[choice]
    if status is None:
        return {"target": None, "label": "this server", "ok": True, "problem": ""}
    name = label.split(" — ")[0]
    if not status["usable"]:
        return {"target": choice, "label": name, "ok": False,
                "problem": f"{name} can't run crawls right now: {status['label'].lower()}. {status['detail']}"}
    return {"target": choice, "label": name, "ok": True, "problem": ""}


def render_sidebar_target(db) -> None:
    """Sidebar block: which computer crawls run on, and whether it is actually ready."""
    options = _options(db)
    st.sidebar.markdown("**🖥️ Crawls run on**")
    if not options:
        st.sidebar.caption("No computer set up yet — open **🖥️ Crawler Setup**.")
        return
    values = [v for v, _, _ in options]
    labels = {v: (f"{_BADGE.get(s['state'], '')} {l}" if s else f"🟢 {l}") for v, l, s in options}
    if st.session_state.get(TARGET_KEY) not in values:
        st.session_state[TARGET_KEY] = resolve_crawl_target(db)["target"] or values[0]
    st.sidebar.selectbox("Crawls run on", values, key=TARGET_KEY, format_func=lambda v: labels[v],
                         label_visibility="collapsed")
    resolved = resolve_crawl_target(db)
    if not resolved["ok"]:
        st.sidebar.caption(f"⚠️ {resolved['problem']}")


# ---- the page ----------------------------------------------------------------------------------------------

def render_crawler_setup_page(db) -> None:
    st.title("🖥️ Crawler Setup")
    st.caption(
        "The deep-crawl scrapers run on a computer with the **Vienna Crawler Worker** installed — this server "
        "if it has them, or a helper's own PC. Install it once; after that, crawls started in this app run there "
        "automatically."
    )
    expected = worker_installer.bundle_info()
    if expected:
        st.caption(f"Current crawler set: version **{expected['version']}** · build `{expected['build']}` · "
                   f"{len(expected['crawlers'])} crawlers")

    _render_computers(db, expected)
    st.markdown("---")
    _render_add_computer(db)


@st.fragment(run_every=4)
def _render_computers(db, expected) -> None:
    st.subheader("Computers")
    if local_crawlers_available():
        st.markdown("🟢 **This server** — has its own crawlers and Node; crawls can run right here.")
    else:
        st.caption("This server has no crawlers of its own, so crawls need a computer below.")

    workers = list_workers(db)
    if not workers:
        st.info("No helper computer yet. Add one below.")
        return
    for w in workers:
        db.refresh(w)  # the worker updates its own row; don't show this session's stale copy
        status = worker_status(w, expected)
        info = w.info or {}
        with st.container(border=True):
            head, action = st.columns([5, 1], vertical_alignment="center")
            with head:
                st.markdown(f"{_BADGE[status['state']]} **{w.name}** — {status['label']}")
                st.caption(status["detail"])
            with action:
                if st.button("Remove", key=f"rm_{w.id}", help="Stops this computer from receiving crawls."):
                    revoke_worker(db, w.id)
                    if st.session_state.get(TARGET_KEY) == w.id:
                        st.session_state.pop(TARGET_KEY, None)
                    st.rerun()
            facts = [f"last seen {_ago(status['seen_ago'])}"]
            if info.get("version"):
                facts.append(f"version {info['version']} · build `{info.get('build')}`")
            if info.get("host"):
                facts.append(f"host {info['host']}")
            if info.get("busy") is not None and status["state"] in (READY, OUTDATED):
                facts.append(f"{info['busy']} crawl(s) running now")
            st.caption(" · ".join(facts))
            for name, check in (info.get("checks") or {}).items():
                st.caption(f"{'✅' if check.get('ok') else '❌'} **{name}** — {check.get('detail')}")


def _render_add_computer(db) -> None:
    st.subheader("Add a computer")
    problems = []
    if not worker_installer.bundle_available():
        problems.append("The crawler bundle hasn't been built (`python scripts/build_worker_bundle.py`, then commit "
                        "`worker_dist/`).")
    if not (SUPABASE_URL and SUPABASE_ANON_KEY):
        problems.append("`SUPABASE_URL` and `SUPABASE_ANON_KEY` aren't configured for this app (environment or "
                        "Streamlit secrets). They're the project's public URL and publishable key.")
    if db.get_bind().dialect.name != "postgresql":
        problems.append("Helper computers need the Postgres/Supabase database; this app is running on SQLite.")
    if problems:
        for p in problems:
            st.error(p)
        return

    st.markdown(
        "1. Type a name for the computer and press **Prepare setup file**.\n"
        "2. **Download** the file on *that* computer.\n"
        "3. **Double-click it.** Everything installs by itself (about 5 minutes, ~300 MB) — no admin rights needed. "
        "If Windows says *“Windows protected your PC”*, click **More info → Run anyway**.\n"
        "4. A window says **“Everything is ready.”** This page then shows the computer as 🟢 Ready."
    )
    st.caption("Windows only. The download is personal to this computer — don't forward it; use **Remove** above "
               "to cut a computer off.")

    name = st.text_input("Name of the computer", placeholder="e.g. Anna's laptop", key="setup_computer_name")
    if st.button("Prepare setup file", type="primary", disabled=not name.strip()):
        try:
            worker, token = worker_hub.create_worker(db, name)
            config = worker_installer.make_config(worker.name, token, SUPABASE_URL, SUPABASE_ANON_KEY)
            st.session_state[SETUP_FILE_KEY] = {
                "worker_id": worker.id, "name": worker.name,
                "bat": worker_installer.build_setup_bat(config),
                "zip": worker_installer.build_setup_zip(config),
            }
        except Exception as e:  # noqa: BLE001 — surface it, don't crash the page
            st.error(f"Couldn't prepare the setup file: {e}")

    prepared = st.session_state.get(SETUP_FILE_KEY)
    if prepared:
        st.success(f"Setup file for **{prepared['name']}** is ready.")
        col_a, col_b = st.columns(2)
        with col_a:
            st.download_button("⬇️ Download setup file", prepared["bat"], file_name=worker_installer.SETUP_BAT_NAME,
                               mime="application/octet-stream", type="primary", width="stretch", on_click="ignore")
        with col_b:
            st.download_button("Browser blocked it? Download as .zip", prepared["zip"],
                               file_name=worker_installer.SETUP_ZIP_NAME, mime="application/zip",
                               width="stretch", on_click="ignore",
                               help="Same file inside a zip. Open the zip, then double-click the file inside.")
