"""
Floating deep-crawl widget — rendered from app.py on EVERY page, so a crawl started
anywhere stays visible wherever you go. Shows live progress while a job runs, then
turns into a summary card (and fires a toast once) when it ends.

The crawl itself runs on background threads (crawl_jobs.py); this file only reads
snapshots. The widget is an auto-refreshing fragment: it re-polls every couple of
seconds while a job is running without rerunning the page underneath it, and — new
in this Streamlit — a fragment refresh never interrupts a page that is mid-run.
"""

import streamlit as st

from crawl_jobs import get_manager, JobSnapshot, SubmitResult, DEFAULT_WORKERS, DEFAULT_PHASES

POLL_SECONDS = 2
MAX_LISTED_IN_FLIGHT = 3

_SEEN_VERSION = "crawl_seen_version"     # last job-completion counter this session has reconciled
_TOASTED_JOB = "crawl_toasted_job"       # job whose completion toast this session already showed
_DISMISSED_JOB = "crawl_dismissed_job"   # job whose summary card the user closed
_FLASH = "crawl_flash"                   # (message, icon) to toast at the start of the next run

_WIDGET_CSS = """
<style>
.st-key-crawl_widget {
    position: fixed; right: 1.25rem; bottom: 1.25rem; z-index: 999980;
    width: min(360px, calc(100vw - 2.5rem));
    background: #1E293B; border: 1px solid #334155; border-radius: 12px;
    padding: 0.7rem 0.9rem 0.8rem; gap: 0.4rem;
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.5);
}
.st-key-crawl_widget [data-testid="stProgress"] { margin: 0; }
.st-key-crawl_widget p { margin: 0; }
</style>
"""


# ---- session plumbing (called once at the top of every app run) -------------------------

def sync_session(db) -> None:
    """Reconcile this browser session with the background manager, before any page draws."""
    manager = get_manager()
    if _SEEN_VERSION not in st.session_state:
        # First run of this session: a job that already finished is old news — no toast for it.
        st.session_state[_SEEN_VERSION] = manager.version
        snap = manager.snapshot()
        if snap and not snap.running:
            st.session_state[_TOASTED_JOB] = snap.id
    elif st.session_state[_SEEN_VERSION] != manager.version:
        st.session_state[_SEEN_VERSION] = manager.version
        # The workers wrote through their own sessions; this page's long-lived session
        # would otherwise keep showing the pre-crawl values it already loaded.
        db.expire_all()

    flash = st.session_state.pop(_FLASH, None)
    if flash:
        st.toast(flash[0], icon=flash[1])


def flash(message: str, icon: str) -> None:
    """Leaves a message to toast at the start of the next run (safe to call before st.rerun())."""
    st.session_state[_FLASH] = (message, icon)


def queue_crawl(companies: dict, workers: int = DEFAULT_WORKERS, target=None, phases=DEFAULT_PHASES) -> SubmitResult:
    """Queues companies ({id: name}) for a background run and leaves a message for the next
    run to toast. Callers st.rerun() afterwards so the widget appears.
    `target` is the Crawler Worker id to run on (None = this server); callers get it
    from views.crawler_setup.resolve_crawl_target. `phases` is which pipeline phases to run:
    the default (7) is the deep crawl; (1,) / (4,) are the API and web/news layers, which
    used to block the page for the whole list."""
    result = get_manager().submit(companies, workers, target=target, phases=phases)
    n = len(companies)
    what = _job_label(phases).lower()
    if result.stopping:
        message, icon = "The running crawl is being stopped — try again once it has finished.", "🛑"
    elif result.added == 0:
        message, icon = f"{'It is' if n == 1 else 'Those are'} already queued for crawling.", "ℹ️"
    elif result.new_job:
        message, icon = f"{_job_label(phases)} started for {_companies(result.added)} — progress is in the widget at the bottom right.", "🕸️"
    else:
        skipped = f" ({result.already_queued} already queued)" if result.already_queued else ""
        message, icon = f"Added {_companies(result.added)} to the running {what}{skipped}.", "🕸️"
    flash(message, icon)
    return result


# ---- the widget -----------------------------------------------------------------------

def render_crawl_widget() -> None:
    snap = get_manager().snapshot()
    # Poll only while something runs. The interval is fixed for a fragment's lifetime, so
    # when the job ends _widget() asks for one full rerun, which re-creates it timer-less.
    st.fragment(_widget, run_every=POLL_SECONDS if snap and snap.running else None)()


def _widget() -> None:
    manager = get_manager()
    snap = manager.snapshot()
    if snap is None or st.session_state.get(_DISMISSED_JOB) == snap.id:
        return

    if not snap.running and st.session_state.get(_SEEN_VERSION) != manager.version:
        # The job ended while only this fragment was polling. The pages underneath still
        # show pre-crawl numbers, so hand over to a full run (sync_session refreshes the
        # DB session, then the toast below fires from that run).
        st.rerun()

    if not snap.running and st.session_state.get(_TOASTED_JOB) != snap.id:
        st.session_state[_TOASTED_JOB] = snap.id
        message, icon = _completion_message(snap)
        st.toast(message, icon=icon, duration="long")

    with st.container(key="crawl_widget"):
        st.html(_WIDGET_CSS)
        if snap.running:
            _render_running(snap, manager)
        else:
            _render_finished(snap)


def _render_running(snap: JobSnapshot, manager) -> None:
    head, action = st.columns([4, 1.4], vertical_alignment="center")
    with head:
        st.markdown(f"**🕸️ {_job_label(snap.phases)} " + ("stopping…" if snap.cancel_requested else "running") + "**")
    with action:
        if not snap.cancel_requested:
            st.button("Stop", key="crawl_stop", on_click=manager.cancel, width="stretch",
                      help="Finish the companies already being crawled and skip the rest of the queue.")

    eta = f" · about {_duration(snap.eta_seconds)} left" if snap.eta_seconds is not None else ""
    st.progress(snap.fraction, text=f"{snap.done} of {snap.total} companies{eta}")

    lines = []
    if snap.cancel_requested:
        lines.append(f"Waiting for {_companies(len(snap.in_flight))} already in progress to finish; "
                     f"{snap.pending} still queued will be skipped.")
    for item in snap.in_flight[:MAX_LISTED_IN_FLIGHT]:
        # A company's sources run side by side, so show how many are done and which are running now.
        step = ""
        if item.step_total:
            step = f" · {item.step_index}/{item.step_total} done"
            if item.running:
                step += " · " + _sources(item.running)
        lines.append(f"⏳ **{_clip(item.name)}**{step}")
    if len(snap.in_flight) > MAX_LISTED_IN_FLIGHT:
        lines.append(f"…and {len(snap.in_flight) - MAX_LISTED_IN_FLIGHT} more in progress")
    if snap.pending and not snap.cancel_requested:
        lines.append(f"{snap.pending} waiting in the queue")
    if lines:
        st.caption("  \n".join(lines))  # one block: separate captions stack with no line spacing


def _render_finished(snap: JobSnapshot) -> None:
    icon = "🛑" if snap.state == "cancelled" else ("⚠️" if snap.failed or snap.with_source_errors else "✅")
    head, action = st.columns([5, 1], vertical_alignment="center")
    with head:
        st.markdown(f"**{icon} {_job_label(snap.phases)} {'stopped' if snap.state == 'cancelled' else 'finished'}**")
    with action:
        st.button("✕", key="crawl_dismiss", help="Dismiss", width="stretch",
                  on_click=lambda job_id=snap.id: st.session_state.__setitem__(_DISMISSED_JOB, job_id))

    parts = [f"{snap.clean} of {snap.done} crawled cleanly"]
    if snap.with_source_errors:
        parts.append(f"{len(snap.with_source_errors)} with crawler errors")
    if snap.failed:
        parts.append(f"{len(snap.failed)} failed")
    if snap.skipped:
        parts.append(f"{snap.skipped} skipped")
    st.caption(" · ".join(parts))

    if snap.failed or snap.with_source_errors:
        with st.expander("What went wrong"):
            for name, error in snap.failed:
                st.markdown(f"**{name}** — failed outright: {error}")
            for name, errors in snap.with_source_errors:
                st.markdown(f"**{name}**")
                for source, error in errors:
                    st.caption(f"{source}: {error}")


# ---- wording ----------------------------------------------------------------------------

def _clip(name: str, width: int = 24) -> str:
    return name if len(name) <= width else name[: width - 1].rstrip() + "…"


def _companies(n: int) -> str:
    return f"{n} company" if n == 1 else f"{n} companies"


def _job_label(phases) -> str:
    """Phase 7 (the Node crawlers) is the 'deep crawl'; phases 1/2/4 alone are quick API/web calls."""
    return "Deep crawl" if 7 in tuple(phases) else "Source sync"


def _sources(names, limit: int = 3) -> str:
    short = [n.removesuffix(" Crawler") for n in names]
    return ", ".join(short[:limit]) + (f" +{len(short) - limit}" if len(short) > limit else "")


def _completion_message(snap: JobSnapshot):
    if snap.state == "cancelled":
        return f"{_job_label(snap.phases)} stopped — {snap.done} of {snap.total} done, {snap.skipped} skipped.", "🛑"
    problems = len(snap.failed) + len(snap.with_source_errors)
    if problems:
        return f"{_job_label(snap.phases)} finished — {_companies(snap.done)} processed, {problems} with errors.", "⚠️"
    return f"{_job_label(snap.phases)} finished — {_companies(snap.done)} processed.", "✅"


def _duration(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest:02d} min"
