"""
Wayback Timeline — settings/status panel for the standalone, opportunistic
tool in the Scraper repo (tools/wayback_timeline/) that walks a company's
Wayback Machine history with the local Ollama model to find when a claim
(online store, sustainability commitment, named certification) first
appeared on its homepage.

Unlike Crawler Setup (views/crawler_setup.py), this is NOT the distributed,
database-backed worker system for friend PCs — that exists because a
hosted Vienna instance has no Scraper folder of its own and can't start
programs on a viewer's computer. This tool is simpler on purpose: it only
ever runs on whichever single machine has Ollama and the Scraper repo, and
that is checked honestly below, same "local_crawlers_available()" honesty
convention as crawler_setup.py — if this page is being viewed from a
different machine than the one that has them, it says so and stops, rather
than pretending Start/Stop would do anything.

Process control here is local-process control (subprocess.Popen / a STOP
file the tool already respects), not a queued job — intentionally, since
the whole point of this tool is "run it on this one machine, whenever it's
convenient," not a distributed queue.
"""
from __future__ import annotations

import csv
import ctypes
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st

# Sibling repo to this one, and the tool's own default output location
# (outside OneDrive on purpose — see tools/wayback_timeline/timeline_builder.py
# for why). Both are duplicated constants, not imported, by the same
# decoupling principle the tool itself follows: this page must still render
# a clear, honest "not available here" message even if that repo is absent.
SCRAPER_REPO = Path(r"C:\Users\gmazz\OneDrive\Documents\Altro\Wayland\Scraper")
OUTPUT_DIR = Path.home() / "WaybackTimelineOutput"
COMPANIES_CSV = SCRAPER_REPO / "tools" / "wayback_timeline" / "data" / "companies.csv"
OLLAMA_URL = "http://localhost:11434"


def _ollama_reachable() -> bool:
    try:
        urllib.request.urlopen(OLLAMA_URL, timeout=1.5)
        return True
    except Exception:
        return False


def _tool_available() -> bool:
    """Honest availability check, same convention as local_crawlers_available()
    in crawler_setup.py: both the code and the model have to be reachable
    from wherever THIS Streamlit process is actually running."""
    return SCRAPER_REPO.exists() and (SCRAPER_REPO / "tools" / "wayback_timeline").exists()


def _pid_alive(pid: int) -> bool:
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def _running_pid() -> Optional[int]:
    pid_file = OUTPUT_DIR / "running.pid"
    if not pid_file.exists():
        return None
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None
    return pid if _pid_alive(pid) else None


def _read_state() -> dict:
    state_file = OUTPUT_DIR / "state.json"
    if not state_file.exists():
        return {}
    try:
        return json.loads(state_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _read_hits() -> list[dict]:
    hits_file = OUTPUT_DIR / "timeline_hits.jsonl"
    if not hits_file.exists():
        return []
    hits = []
    for line in hits_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                hits.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return hits


def _start() -> Optional[str]:
    """Launches the tool detached, so it keeps running after this Streamlit
    page (or the whole app) is closed — same "survives the viewer leaving"
    property the crawler worker has, just via a plain local process instead
    of a background service. Returns an error string, or None on success."""
    if not COMPANIES_CSV.exists():
        return f"No company list yet at {COMPANIES_CSV} — add one below first."
    try:
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        DETACHED_PROCESS = 0x00000008
        subprocess.Popen(
            [sys.executable, "-m", "tools.wayback_timeline.timeline_builder",
             "--input", str(COMPANIES_CSV)],
            cwd=str(SCRAPER_REPO),
            creationflags=CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return None
    except OSError as e:
        return f"Couldn't start it: {e}"


def _stop() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "STOP").write_text("requested from Wayback Timeline panel", encoding="utf-8")


def render_wayback_timeline_page(db) -> None:  # noqa: ARG001 — db unused, kept for call-site consistency
    st.title("🕰️ Wayback Timeline")
    st.caption(
        "A standalone, opportunistic pass: reads a company's Wayback Machine history with the "
        "local model and finds when claims like an online store or a sustainability commitment "
        "first appeared on its homepage. Runs only on this one machine, only when you start it — "
        "nothing else in this app depends on it."
    )

    if not _tool_available():
        st.warning(
            f"The tool isn't reachable from this server — expected it at `{SCRAPER_REPO}`. "
            "This page only works when Vienna is running on the same machine that has the Scraper "
            "repo and Ollama (see Crawler Setup's own note about hosted vs local for why).",
            icon="⚠️",
        )
        return

    _render_status()
    st.markdown("---")
    _render_companies()
    st.markdown("---")
    _render_hits()


@st.fragment(run_every=4)
def _render_status() -> None:
    ollama_ok = _ollama_reachable()
    pid = _running_pid()

    cols = st.columns([2, 1])
    with cols[0]:
        if pid:
            st.success(f"🟢 Running (process {pid})", icon="🟢")
        else:
            st.info("⚪ Not running right now", icon="⚪")
        if not ollama_ok:
            st.caption("⚠️ Ollama isn't responding on localhost:11434 — a run would fail to start.")
    with cols[1]:
        if pid:
            if st.button("Stop", type="secondary", width="stretch"):
                _stop()
                st.caption("Stop requested — it finishes the snapshot it's on, saves, then exits.")
        else:
            if st.button("Start", type="primary", width="stretch", disabled=not ollama_ok):
                error = _start()
                if error:
                    st.error(error)
                else:
                    st.rerun()

    state = _read_state()
    if state:
        done = sum(1 for e in state.values() if e.get("done"))
        with_hit = sum(1 for e in state.values() if e.get("first_hit"))
        st.caption(f"{done} of {len(state)} company/claim pairs checked so far · {with_hit} confirmed")


def _render_companies() -> None:
    st.subheader("Companies")
    st.caption(f"Edited here, saved straight to `{COMPANIES_CSV.name}` — the same file the tool reads.")
    if COMPANIES_CSV.exists():
        df = pd.read_csv(COMPANIES_CSV)
    else:
        df = pd.DataFrame(columns=["company_id", "homepage_url"])

    edited = st.data_editor(df, num_rows="dynamic", width="stretch", key="wayback_companies_editor")
    if st.button("Save company list"):
        COMPANIES_CSV.parent.mkdir(parents=True, exist_ok=True)
        edited.to_csv(COMPANIES_CSV, index=False)
        st.success(f"Saved {len(edited)} companies.")


def _render_hits() -> None:
    st.subheader("Confirmed so far")
    hits = _read_hits()
    if not hits:
        st.caption("Nothing confirmed yet.")
        return
    for h in sorted(hits, key=lambda x: x.get("date", ""), reverse=True):
        with st.container(border=True):
            st.markdown(f"**{h.get('company_id')}** — {h.get('claim_id')} — first seen **{h.get('date')}**")
            st.caption(h.get("quote", ""))
            st.markdown(f"[View on Wayback Machine]({h.get('replay_url')})")
