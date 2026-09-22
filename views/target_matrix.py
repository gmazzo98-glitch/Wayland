"""
Target Matrix View (Page 1): Interactive 2D Plotly Scatter Plot (Need vs. Readiness)
Section 4 of GG_Dashboard_Technical_Brief.docx

Two things this view deliberately does NOT do, both per explicit findings in
the Brief:
  - Pool Midcap and SME into one ranked list (Section 5) — each segment gets
    its own chart and leaderboard, always.
  - Let the ranked view be read "as if it were complete" with no visibility
    into pipeline/source health (Section 1's named failure mode) — a
    completeness/live-vs-simulated banner sits above everything else on this
    page, and can't be filtered away.
"""

from collections import defaultdict
from datetime import datetime
import hashlib
import re
import streamlit as st
import plotly.express as px
import pandas as pd
from sqlalchemy import func
from sqlalchemy.orm import Session
from models import Company, SignalRecord, SourceHealth
from scoring import calculate_company_scores, rank_companies, is_prime_target, PRIME_NEED_MIN, PRIME_READINESS_BAND
from indicators import fetch_indicator_defs
from crawl_jobs import get_manager, estimate_seconds, DEFAULT_WORKERS, MAX_WORKERS
from views.crawl_widget import queue_crawl, flash
from views.crawler_setup import resolve_batch_targets

SEGMENT_COLORS = {"Midcap": "#38BDF8", "SME": "#F59E0B"}
SEGMENT_ORDER = ["Midcap", "SME"]

# Company ids the user has ticked as deep-crawl candidates, held BY ID in plain session
# state. The tables' own notion of a selection can't carry this: st.dataframe's row
# selection is wiped by the frontend whenever you sort (and reports "nothing selected"),
# and it only knows row positions, which go stale on every filter change or re-rank. The
# tick column below is real cell data in an st.data_editor, so sorting can't touch it.
SELECTION_KEY = "matrix_crawl_selection"
BIG_BATCH = 50  # above this the run button needs an explicit "yes, that many" tick
TICK_COLUMN = "Crawl"

# A keyed container is wrapped in an stLayoutWrapper that is barely taller than the bar
# itself, and a sticky element can only stick within its parent — so the wrapper (whose
# parent is the whole page column) is what has to be sticky, not the container.
_SELECT_BAR_CSS = """
<style>
[data-testid="stLayoutWrapper"]:has(> .st-key-crawl_select_bar) {
    position: sticky; top: 3.75rem; z-index: 90;
}
.st-key-crawl_select_bar {
    background: #1E293B; border: 1px solid #334155; border-radius: 10px;
    padding: 0.6rem 0.9rem; margin: 0.25rem 0 0.75rem; gap: 0.5rem;
    box-shadow: 0 6px 18px rgba(0, 0, 0, 0.35);
}
.st-key-crawl_select_bar p { margin: 0; }
</style>
"""


def _table_signature(frame: pd.DataFrame) -> str:
    return hashlib.md5(pd.util.hash_pandas_object(frame, index=True).values.tobytes()).hexdigest()


def _fold_edits(selected: set, ids_in_order: list, edited_rows: dict) -> set:
    """
    Applies a data_editor's edits to the selection. `edited_rows` maps a row's position in
    the frame the editor was GIVEN (not the sorted view it may be showing) to its changed
    cells; the edits are cumulative from that frame, so applying them again is harmless.
    """
    out = set(selected)
    for pos, change in edited_rows.items():
        if TICK_COLUMN in change and 0 <= pos < len(ids_in_order):
            (out.add if change[TICK_COLUMN] else out.discard)(ids_in_order[pos])
    return out


def _widget_key(table_key: str, epoch: int) -> str:
    return f"{table_key}__{epoch}"


def _on_table_edit(table_key: str) -> None:
    """The user ticked or unticked rows in one table: fold that into the selection."""
    meta = st.session_state[f"{table_key}__meta"]
    edits = st.session_state[_widget_key(table_key, meta["epoch"])]["edited_rows"]
    selected = _fold_edits(st.session_state.get(SELECTION_KEY, set()), meta["ids"], edits)
    st.session_state[SELECTION_KEY] = selected
    meta["expected"] = selected & set(meta["ids"])  # the table already shows this


def _selectable_table(table_key: str, seg_df: pd.DataFrame, disp_df: pd.DataFrame, column_config: dict) -> None:
    """
    A table with a tick column, driven by SELECTION_KEY in both directions.

    An st.data_editor is identified by its input data: change the frame it is given and it
    starts over, dropping edits it hasn't reported. So the input is a fixed BASELINE (the
    selection at the moment it was built) that user ticks accumulate on top of, folded into
    the selection by _on_table_edit. Anything else that changes the selection or the rows —
    Clear, Add prime, a filter, a re-rank after a crawl, coming back from another page — makes
    the baseline stale, and the table is then rebuilt from the current selection under a new
    key (epoch). Sorting is view state inside the editor and touches none of this.
    """
    ids = seg_df["id"].tolist()
    signature = _table_signature(disp_df)
    selected = st.session_state.setdefault(SELECTION_KEY, set())
    live = selected & set(ids)

    meta_key = f"{table_key}__meta"
    meta = st.session_state.get(meta_key)
    stale = (
        meta is None
        or meta["ids"] != ids
        or meta["signature"] != signature
        or meta["expected"] != live                                     # changed from outside the table
        or _widget_key(table_key, meta["epoch"]) not in st.session_state  # widget state was dropped (other page)
    )
    if stale:
        meta = {"epoch": meta["epoch"] + 1 if meta else 0, "ids": ids, "signature": signature,
                "baseline": live, "expected": live}
        st.session_state[meta_key] = meta

    frame = disp_df.copy()
    frame.insert(0, TICK_COLUMN, [cid in meta["baseline"] for cid in ids])
    st.data_editor(
        frame,
        column_config={
            TICK_COLUMN: st.column_config.CheckboxColumn(
                TICK_COLUMN, width="small", default=False,
                help="Tick to queue this company for a deep crawl. Ticks survive sorting and filtering.",
            ),
            **column_config,
        },
        disabled=[c for c in frame.columns if c != TICK_COLUMN],
        num_rows="fixed",
        width="stretch",
        hide_index=True,
        key=_widget_key(table_key, meta["epoch"]),
        on_change=_on_table_edit,
        args=(table_key,),
    )


# ---- bulk selection ---------------------------------------------------------------------
# Ticking hundreds of rows by hand doesn't scale, and the sort order you see is client-side
# (the server never learns it), so bulk picks are expressed as rules the server can evaluate.
# Every rule works per segment: Midcap and SME are never pooled into one ranking.

BULK_METRICS = {
    "Need score": "need_score",
    "Readiness score": "readiness_score",
    "Data completeness": "total_completeness_pct",
}


def _with_website(frame: pd.DataFrame) -> pd.DataFrame:
    return frame[frame["website_url"].fillna("").astype(str).str.strip() != ""]


def _top_n_ids(frames: dict, column: str, n: int, ascending: bool = False, require_website: bool = False) -> set:
    """The n best rows of EACH segment frame by `column`."""
    out = set()
    for frame in frames.values():
        pool = _with_website(frame) if require_website else frame
        out |= set(pool.sort_values(column, ascending=ascending, kind="stable").head(n)["id"])
    return out


def _ids_in_score_ranges(frames: dict, need: tuple, readiness: tuple, require_website: bool = False) -> set:
    """Rows whose Need and Readiness both fall inside their (low, high) range, inclusive."""
    out = set()
    for frame in frames.values():
        pool = _with_website(frame) if require_website else frame
        hit = pool["need_score"].between(*need) & pool["readiness_score"].between(*readiness)
        out |= set(pool.loc[hit, "id"])
    return out


def _normalize_token(text: str) -> str:
    return re.sub(r"[\W_]+", "", str(text).casefold())


def _match_pasted_list(text: str, df: pd.DataFrame):
    """
    Matches a pasted list (one company per line — e.g. a column copied out of Excel — or
    separated by tabs/semicolons) against registration numbers and legal names. Exact match
    first (ignoring case, spacing and punctuation: 'HRB 670192' == 'hrb-670192'); failing that,
    a fragment counts only if exactly one company contains it, so 'agrotech 02' resolves but
    'agri' doesn't silently tick forty rows. Returns (ids, not_found, ambiguous).
    """
    by_reg = {_normalize_token(r): i for r, i in zip(df["registration_number"], df["id"]) if _normalize_token(r)}
    by_name = {_normalize_token(n): i for n, i in zip(df["legal_name"], df["id"])}
    ids, not_found, ambiguous = set(), [], []
    for token in re.split(r"[\r\n\t;]+", text or ""):
        token = token.strip()
        key = _normalize_token(token)
        if not key:
            continue
        exact = by_reg.get(key) or by_name.get(key)
        if exact:
            ids.add(exact)
            continue
        hits = {i for name, i in by_name.items() if key in name} if len(key) >= 4 else set()
        if len(hits) == 1:
            ids |= hits
        else:
            (ambiguous if hits else not_found).append(token)
    return ids, not_found, ambiguous


def _render_bulk_select(frames: dict, df: pd.DataFrame, selected: set) -> None:
    """The '⚡ Bulk' popover: pick many companies with one rule instead of one click each."""
    st.caption("Top-N and score rules apply to the companies currently shown (filters above), "
               "separately for each segment.")
    require_site = st.checkbox("Skip companies with no website on record", value=True, key="bulk_require_site",
                               help="Most crawlers need a website, so ticking these would only waste crawl time.")

    st.markdown("**Top N of each segment**")
    col_n, col_metric, col_dir = st.columns([1, 1.6, 1.4])
    n = col_n.number_input("N", min_value=1, max_value=1000, value=20, key="bulk_n")
    metric = col_metric.selectbox("Ranked by", list(BULK_METRICS), key="bulk_metric")
    order = col_dir.selectbox("Order", ["Highest first", "Lowest first"], key="bulk_order")
    top_ids = _top_n_ids(frames, BULK_METRICS[metric], int(n), ascending=(order == "Lowest first"), require_website=require_site)
    if st.button(f"Add top {int(n)} by {metric.lower()}  ({len(top_ids - selected)} new)", key="bulk_add_top",
                 disabled=not (top_ids - selected), width="stretch"):
        st.session_state[SELECTION_KEY] = selected | top_ids
        st.rerun()

    st.divider()
    st.markdown("**By score range**")
    need = st.slider("Need score", 0, 100, (50, 100), key="bulk_need")
    readiness = st.slider("Readiness score", 0, 100, (40, 85), key="bulk_readiness",
                          help="The default is the prime band's own Readiness window.")
    range_ids = _ids_in_score_ranges(frames, need, readiness, require_website=require_site)
    if st.button(f"Add all matching  ({len(range_ids)} match, {len(range_ids - selected)} new)", key="bulk_add_range",
                 disabled=not (range_ids - selected), width="stretch"):
        st.session_state[SELECTION_KEY] = selected | range_ids
        st.rerun()

    st.divider()
    st.markdown("**Paste a list**")
    pasted = st.text_area("One per line — legal names or registration numbers (e.g. a column copied from Excel)",
                          key="bulk_paste", height=110, placeholder="AgroTech 02 Italia S.p.A.\nHRB 670192\n…")
    # Never disabled: a text area only commits when it loses focus, so clicking straight after
    # pasting would hit a still-disabled button and the click would be swallowed.
    if st.button("Tick these", key="bulk_add_paste", width="stretch"):
        found, not_found, ambiguous = _match_pasted_list(pasted, df)
        if not pasted.strip():
            flash("Paste a list first — one company name or registration number per line.", "📋")
            st.rerun()
        st.session_state[SELECTION_KEY] = selected | found
        problems = []
        if not_found:
            problems.append(f"{len(not_found)} not found: " + ", ".join(not_found[:5]) + ("…" if len(not_found) > 5 else ""))
        if ambiguous:
            problems.append(f"{len(ambiguous)} ambiguous (matched several): " + ", ".join(ambiguous[:5]) + ("…" if len(ambiguous) > 5 else ""))
        flash(f"Ticked {len(found)} from the list." + (" " + "; ".join(problems) if problems else ""), "📋")
        st.rerun()


def _short_duration(seconds: int) -> str:
    minutes = max(1, round(seconds / 60))
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h {minutes % 60:02d} min"


def _render_crawl_bar(db: Session, bar, df: pd.DataFrame, frames: dict):
    """The sticky 'N selected → crawl them' strip, filled after the tables (it needs
    their selection) but placed above them. `frames` = the ranked frame of each shown segment."""
    visible_ids = {cid for frame in frames.values() for cid in frame["id"]}
    prime_visible_ids = {cid for frame in frames.values() for cid in frame.loc[frame["is_prime"].astype(bool), "id"]}

    with bar:
        st.html(_SELECT_BAR_CSS)
        by_id = df.set_index("id")
        selected = st.session_state.setdefault(SELECTION_KEY, set())
        # (a deleted company can't be crawled, hence the membership test)
        chosen = sorted((cid for cid in selected if cid in by_id.index), key=lambda cid: by_id.at[cid, "legal_name"].lower())
        snap = get_manager().snapshot()
        running = bool(snap and snap.running)
        stopping = bool(running and snap.cancel_requested)
        n = len(chosen)

        info = st.container()  # top row; filled last because its ETA needs the parallelism chosen below
        confirm_slot = st.container()
        col_prime, col_all, col_bulk, col_clear, col_workers, col_run = st.columns(
            [1.2, 0.9, 1.25, 0.95, 1.2, 2.2], vertical_alignment="center")

        with col_prime:
            if st.button("🎯 Prime", width="stretch", disabled=not prime_visible_ids,
                         help="Add every prime-band company shown in the tables below to the selection."):
                st.session_state[SELECTION_KEY] = selected | prime_visible_ids
                st.rerun()
        with col_all:
            if st.button("☑ All", width="stretch", disabled=not visible_ids,
                         help="Add every company currently shown (after the filters above) to the selection."):
                st.session_state[SELECTION_KEY] = selected | visible_ids
                st.rerun()
        with col_bulk:
            with st.popover("⚡ Bulk", width="stretch", disabled=not visible_ids,
                            help="Pick many at once: top N by a score, a score range, or a pasted list."):
                _render_bulk_select(frames, df, selected)
        with col_clear:
            st.button("Clear", width="stretch", disabled=not chosen,
                      on_click=lambda: st.session_state.__setitem__(SELECTION_KEY, set()))
        # Where the batch will run — Automatic spreads it across every computer that's ready
        # right now (see views.crawler_setup); only needed once anything is actually selected.
        where = resolve_batch_targets(db, chosen) if chosen else {
            "ok": True, "problem": "", "computers": [], "recommended_workers": None}

        with col_workers:
            if running:
                workers = snap.workers
                st.caption(f"{workers} in parallel")
            else:
                workers = int(st.number_input(
                    "In parallel", min_value=1, max_value=MAX_WORKERS, value=DEFAULT_WORKERS,
                    key="matrix_crawl_workers", label_visibility="collapsed",
                    help="Companies crawled at the same time, across every computer in use. 3 per computer is "
                         "the sweet spot on a 16 GB machine (one headless Chromium each) before rate limits "
                         "start biting."))

        with info:
            if not chosen:
                st.markdown("**🕸️ Deep crawl** — tick companies in the tables below to queue them for the crawlers.")
            else:
                seg_counts = by_id.loc[chosen, "segment"].value_counts()
                per_segment = " · ".join(f"{seg_counts[s]} {s}" for s in SEGMENT_ORDER if s in seg_counts)
                hidden = len([cid for cid in chosen if cid not in visible_ids])
                hidden_note = f" · {hidden} hidden by filters" if hidden else ""
                st.markdown(f"**🕸️ {n} selected** · {per_segment}{hidden_note}")
                names = [by_id.at[cid, "legal_name"] for cid in chosen]
                listed = ", ".join(names[:3]) + (f" +{n - 3} more" if n > 3 else "")
                notes = [listed, f"about {_short_duration(estimate_seconds(n, workers))} at {workers} in parallel"]
                computers = where["computers"]
                if len(computers) > 1:
                    notes.append(f"spreads across {len(computers)} computers: {', '.join(computers)}"
                                 + (f" (try {where['recommended_workers']} in parallel to use them all at once)"
                                    if where["recommended_workers"] and where["recommended_workers"] > workers else ""))
                elif computers:
                    notes.append(f"runs on {computers[0]}")
                no_site = int(by_id.loc[chosen, "website_url"].fillna("").astype(str).str.strip().eq("").sum())
                if no_site:
                    notes.append(f"⚠️ {no_site} with no website on record (most crawlers need one)")
                st.caption(" · ".join(notes))

        if chosen and not where["ok"]:
            st.warning(where["problem"])

        confirmed = True
        if n > BIG_BATCH:
            with confirm_slot:
                confirmed = st.checkbox(
                    f"Yes, queue all {n} — about {_short_duration(estimate_seconds(n, workers))}, and free-tier "
                    f"rate limits (Groq, Wayback) may start failing crawlers.",
                    key="matrix_crawl_confirm_big",
                )

        with col_run:
            label = "➕ Add to running crawl" if running else "▶ Crawl selected"
            if st.button(label, type="primary", width="stretch",
                         disabled=(not chosen) or stopping or not confirmed or not where["ok"],
                         help="The crawl runs in the background — keep browsing, and follow it in the widget "
                              "at the bottom right of any page."):
                queue_crawl({cid: by_id.at[cid, "legal_name"] for cid in chosen}, workers=workers,
                            targets=where["targets"], target_labels=where["target_labels"])
                st.session_state[SELECTION_KEY] = set()
                st.rerun()


# Only what the score needs; text_value / raw_payload_ref feed the per-signal
# detail view on Company Intelligence, never the bulk ranking here, and they're
# the bulk of the row's bytes over the wire.
_SCORING_SIGNAL_COLS = (
    SignalRecord.company_id, SignalRecord.signal_key, SignalRecord.status,
    SignalRecord.numeric_value, SignalRecord.fetched_at,
)


def _signals_fingerprint(db: Session):
    """One cheap aggregate query that changes whenever the pipeline writes signals."""
    return tuple(db.query(func.count(SignalRecord.id), func.max(SignalRecord.fetched_at)).one())


@st.cache_data(ttl=300, show_spinner="Scoring companies…")
def _score_all_companies(_db: Session, _companies, _indicator_defs: dict, signals_fingerprint, weights_fingerprint):
    """Scores every company from ONE bulk signal query (this used to be one query
    per company, twice per render — ~2,000 round trips to a remote Postgres).

    Cached across Streamlit reruns, so filter clicks don't recompute anything. The
    two fingerprint args are the cache key: a pipeline write or an Indicator Weights
    edit changes them and invalidates the cache immediately; the TTL is a backstop.
    The underscore args are excluded from hashing.

    Also refreshes Company's cached need_score/readiness_score/last_scored_at
    snapshot — but only for rows whose score actually changed, and only when the
    cache misses. Nothing in the app reads FROM these columns to render a score
    (every view recomputes live from SignalRecords); they exist for external
    consumers (a future BI/export query, PilotOutcome context).
    """
    signals_by_company = defaultdict(list)
    for row in _db.query(*_SCORING_SIGNAL_COLS).all():
        signals_by_company[row.company_id].append(row._asdict())

    scores_by_id = {}
    stale_rows = []
    now = datetime.utcnow()
    for comp in _companies:
        scores = calculate_company_scores(signals_by_company.get(comp.id, []), _indicator_defs)
        scores_by_id[comp.id] = scores
        if (comp.need_score != scores["need_score"] or comp.readiness_score != scores["readiness_score"]
                or comp.last_scored_at is None):
            stale_rows.append({
                "id": comp.id, "need_score": scores["need_score"],
                "readiness_score": scores["readiness_score"], "last_scored_at": now,
            })
    if stale_rows:
        _db.bulk_update_mappings(Company, stale_rows)
        _db.commit()
    return scores_by_id


def _render_completeness_banner(db: Session, companies, indicator_defs, scores_by_id):
    # Mirror how scoring.py counts signals_total: a 'need'/'readiness' indicator
    # counts once, a 'both'-axis one counts once per axis (twice), 'context' never.
    scored_count = sum(2 if d["axis"] == "both" else 1 for d in indicator_defs.values() if d["axis"] != "context")
    total_possible = len(companies) * scored_count
    total_checked = sum(scores_by_id[comp.id]["signals_checked"] for comp in companies)
    pct_run = (total_checked / total_possible * 100.0) if total_possible else 0.0

    sources = db.query(SourceHealth).all()
    live_count = sum(1 for s in sources if s.mode == "live")
    never_run = sum(1 for s in sources if s.last_status in (None, "idle"))

    if pct_run >= 90 and live_count == len(sources) and sources:
        st.success(
            f"Pipeline coverage: **{pct_run:.0f}%** of all possible signal checks are populated across "
            f"{len(companies)} companies — {live_count}/{len(sources)} sources running live.",
            icon="✅",
        )
    else:
        st.warning(
            f"Pipeline coverage: only **{pct_run:.0f}%** of possible signal checks are populated across "
            f"{len(companies)} companies. **{live_count}/{len(sources) or 0} sources are running live** "
            f"({never_run} never run) — the rest are simulated or not yet checked. "
            f"Scores below are computed only from what's actually present; check **⚙️ Pipeline & Source Health** "
            f"before treating any ranking here as final.",
            icon="⚠️",
        )


def _segment_section(seg_name: str, seg_df: pd.DataFrame):
    company_word = "company" if len(seg_df) == 1 else "companies"
    st.subheader(f"{'🏢' if seg_name == 'Midcap' else '🏬'} {seg_name} Segment ({len(seg_df)} {company_word})")

    if seg_df.empty:
        st.caption("No companies in this segment match the current filters.")
        return

    prime_count = int(seg_df["is_prime"].sum())
    prime_word = "company" if prime_count == 1 else "companies"
    prime_verb = "qualifies" if prime_count == 1 else "qualify"
    st.caption(
        f"Prime target band: Need ≥ {PRIME_NEED_MIN:.0f} and Readiness between "
        f"{PRIME_READINESS_BAND[0]:.0f}–{PRIME_READINESS_BAND[1]:.0f} (moderate-to-high, not just the top corner) "
        f"— **{prime_count}** {seg_name} {prime_word} currently {prime_verb}."
    )

    fig = px.scatter(
        seg_df,
        x="need_score",
        y="readiness_score",
        size="total_completeness_pct",
        color="is_prime",
        hover_name="legal_name",
        hover_data={
            "country": True,
            "registration_number": True,
            "need_score": ":.1f",
            "readiness_score": ":.1f",
            "total_completeness_pct": ":.1f%",
            "shortlist_status": True,
            "is_prime": False,
        },
        labels={
            "need_score": "Need Axis Score (Could Innovate: Margin, Export, Digital)",
            "readiness_score": "Readiness Axis Score (Is Innovating: R&D, IP, Grants)",
        },
        color_discrete_map={True: "#22C55E", False: SEGMENT_COLORS.get(seg_name, "#94A3B8")},
        size_max=30,
    )

    # Shade the actual prime band (high-need + MODERATE-TO-HIGH readiness), not a
    # single high+high corner — Section 4 of the Brief.
    fig.add_shape(
        type="rect", x0=PRIME_NEED_MIN, y0=PRIME_READINESS_BAND[0], x1=100, y1=PRIME_READINESS_BAND[1],
        fillcolor="#22C55E", opacity=0.12, line=dict(width=0), layer="below",
    )
    fig.add_shape(type="line", x0=PRIME_NEED_MIN, y0=0, x1=PRIME_NEED_MIN, y1=100, line=dict(color="#475569", dash="dash"))
    fig.add_annotation(x=75, y=(PRIME_READINESS_BAND[0] + PRIME_READINESS_BAND[1]) / 2, text="🎯 PRIME BAND", showarrow=False, font=dict(color="#22C55E", size=12))

    fig.update_layout(
        xaxis=dict(range=[-5, 105]), yaxis=dict(range=[-5, 105]),
        template="plotly_dark", height=480, showlegend=False,
    )
    st.plotly_chart(fig, use_container_width=True, key=f"scatter_{seg_name}")

    disp_df = seg_df[[
        "legal_name", "country", "registration_number", "need_score", "readiness_score",
        "total_completeness_pct", "signals_checked", "shortlist_status"
    ]]
    _selectable_table(
        f"matrix_table_{seg_name}", seg_df, disp_df,
        column_config={
            "legal_name": f"{seg_name} Company",
            "country": st.column_config.TextColumn("Country", width="small"),
            "registration_number": "Reg. Number",
            "need_score": st.column_config.NumberColumn("Need Score", format="%.1f"),
            "readiness_score": st.column_config.NumberColumn("Readiness Score", format="%.1f"),
            "total_completeness_pct": st.column_config.ProgressColumn("Data Completeness", format="%.1f%%", min_value=0, max_value=100),
            "signals_checked": "Checked Signals",
            "shortlist_status": "Shortlist Status",
        },
    )


def render_target_matrix_page(db: Session):
    st.title("🎯 Scored Target Matrix (Need × Readiness)")
    st.caption("2D Positioning Rubric, scored separately per segment — Midcap and SME are never pooled into one ranking (Section 5 of the Brief).")

    companies = db.query(Company).all()
    if not companies:
        st.warning("No target companies found in database. Run seed script or ingestion pipeline.")
        return

    indicator_defs = fetch_indicator_defs(db)
    scores_by_id = _score_all_companies(
        db, companies, indicator_defs,
        _signals_fingerprint(db), repr(sorted((k, sorted(d.items(), key=str)) for k, d in indicator_defs.items())),
    )
    # The score cache's write-back commit expires every loaded Company; re-query once
    # (one round trip) so reading their attributes below doesn't lazy-refresh row by row.
    companies = db.query(Company).all()
    _render_completeness_banner(db, companies, indicator_defs, scores_by_id)
    st.markdown("---")

    matrix_data = []
    for comp in companies:
        scores = scores_by_id[comp.id]
        matrix_data.append({
            "id": comp.id,
            "legal_name": comp.legal_name,
            "country": comp.country or "Germany",
            "registration_number": comp.registration_number,
            "nace_code": comp.nace_code,
            "sector": comp.sector_name,
            "segment": comp.segment,
            "website_url": comp.website_url,
            "shortlist_status": comp.shortlist_status,
            "need_score": scores["need_score"],
            "readiness_score": scores["readiness_score"],
            "total_completeness_pct": scores["total_completeness_pct"],
            "signals_checked": f"{scores['signals_checked']}/{scores['signals_total']}",
            "is_prime": is_prime_target(scores["need_score"], scores["readiness_score"]),
        })
    df = pd.DataFrame(matrix_data)

    all_segments = [s for s in SEGMENT_ORDER if s in df["segment"].unique()]
    all_segments += [s for s in sorted(df["segment"].unique()) if s not in all_segments]

    col_f1, col_f2, col_f3, col_f4 = st.columns(4)
    with col_f1:
        country_options = ["All Countries", "Germany 🇩🇪", "Italy 🇮🇹"]
        selected_country = st.selectbox("Filter Country 🌐", country_options)
    with col_f2:
        sectors = ["All Sectors"] + sorted(df["sector"].unique().tolist())
        selected_sec = st.selectbox("Filter Sector", sectors)
    with col_f3:
        min_comp = st.slider("Min Completeness %", 0, 100, 0, step=10)
    with col_f4:
        selected_segments = st.multiselect(
            "Segments (never pooled)",
            options=all_segments, default=all_segments,
        )

    filtered_df = df.copy()
    if selected_country == "Germany 🇩🇪":
        filtered_df = filtered_df[filtered_df["country"] == "Germany"]
    elif selected_country == "Italy 🇮🇹":
        filtered_df = filtered_df[filtered_df["country"] == "Italy"]

    if selected_sec != "All Sectors":
        filtered_df = filtered_df[filtered_df["sector"] == selected_sec]
    filtered_df = filtered_df[filtered_df["total_completeness_pct"] >= min_comp]

    st.markdown("---")

    # Sticky "N selected → crawl" strip. Created here so it sits above the tables (and must be
    # a direct child of the page for `position: sticky` to work), filled below once the
    # tables have reported their ticks.
    crawl_bar = st.container(key="crawl_select_bar")

    frames = {}
    for seg_name in [s for s in all_segments if s in selected_segments]:
        seg_slice = filtered_df[filtered_df["segment"] == seg_name]
        ranked_seg_df = pd.DataFrame(rank_companies(seg_slice.to_dict("records")), columns=seg_slice.columns)
        frames[seg_name] = ranked_seg_df
        _segment_section(seg_name, ranked_seg_df)
        st.markdown("---")

    _render_crawl_bar(db, crawl_bar, df, frames)
