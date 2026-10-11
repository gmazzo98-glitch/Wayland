# Crawler Roadmap — multi-agent work board

This file is a **work board for AI coding agents** ("bots" below — Claude Code,
Codex, or any other agent working in this repo), not just documentation. It
exists so several bots can work on Phase 7 (crawler) improvements **at the
same time without colliding**: each unit of work is a numbered ticket with an
explicit file list, a claiming protocol prevents two bots from starting the
same ticket, and a single merge-to-master rule keeps the branch graph from
sprawling.

If you are a bot and were pointed at this file: read **Ground rules** and
**Claiming protocol** below before touching any code, then jump to the ticket
board. If you were handed the **standard pickup prompt** at the bottom of this
file instead, it already tells you to come here first.

Last gap-analysis refresh: 2026-10-10, against the live Neon DB (953 Italian
NACE C28 companies, 50-99 staff) via `scripts/indicator_gaps.py`, plus live
`SourceHealth` call/error counts and `git log`. Re-run that script yourself
before trusting old percentages in this file — coverage changes every time
someone runs a batch.

## Why this document exists (context for humans re-reading this later)

Project Vienna scores companies on two axes, NEED and READINESS, from ~87
indicators (`indicators.py`). Most of the *code* to source those indicators
already exists — the real gap, measured live, is that **most of the 953-row
roster has never been crawled even once**: Phase 7 `SourceHealth.total_calls`
sits around 380-390 for most crawlers, i.e. only ~40% of companies, and
several free, already-wired sources (Google News RSS-backed indicators like
`external_collaboration`, `university_partnership`) still show ~1% population
because nobody has run them at volume, not because they're unbuilt. See
ticket T01.

The one thing every bot asked to "build more crawlers" should internalize
first: **check whether it's already built before writing new code.** This
repo moves fast (11 Node/Crawlee crawlers under the sibling `Scraper/`
checkout, ~30 commits touching `scrapers/` in the last week alone) and more
than one of the items below was *half-solved* by a very recent commit. Grep
first, read the module docstring, run `scripts/indicator_gaps.py`, then decide
whether this is a RUN, a FIX, or a genuine BUILD.

## What "the competitor crawler" actually does today (read this before T04-T07)

`scrapers/competitor_benchmark.py` is **not** a competitor-discovery crawler.
`models.Competitor` is a short, **hand-entered** list (name + homepage URL,
max 3 per company, added from the "🥊 Named Competitors" expander on Company
Intelligence) — deliberately not auto-discovered, because a search/LLM guess
at "who is this company's competitor" risks silently benchmarking against the
wrong company entirely. GG's team already knows their market; the crawler's
job starts only once a human has named 1-3 peers.

Given that list, the crawler re-runs **digital-maturity-crawler** against
each named competitor's own homepage (the exact same Wayback-snapshot-diff +
vision-LLM assessment the company's own site already gets) and computes one
number: `competitor_digital_gap` — how many more/fewer years since a redesign
this company is carrying versus its named peers' average. That is the *entire*
scope on purpose: one indicator, one dimension (website redesign recency),
nothing about product range, pricing, hiring, reviews, or press share.

T04-T07 below generalize that exact pattern (re-run an existing crawler
against the same hand-curated `Competitor` rows) to other dimensions, using
crawlers that already exist. Nothing here proposes auto-discovering
competitors — that design decision stands.

## Ground rules (every bot, every ticket)

1. **One producer per signal_key.** Before writing to any `SignalRecord`,
   check `indicators.py`'s `INDICATOR_SEED` for the key and grep `scrapers/`
   + `adapters/` for any existing writer. Never invent a new `signal_key` or
   change an indicator's `weight` unilaterally — those are GG's calibration
   decisions (see `models.PilotOutcome`'s docstring: no weight is validated
   yet). If your ticket's output doesn't map to an existing indicator, save it
   via `scrapers.node_crawler_base.save_crawler_blob()` as raw data only
   (exactly what `product_catalog_crawler.py` already does) and say so in
   your ticket's Results note — do not add a `SignalRecord` for it.
2. **Honesty over coverage.** Read `field_status` before any value; a
   `not_found` or a failed search is not the same as a confirmed absence. A
   `simulate()` path writes **no** signals (Phase 7's own convention — see
   `scrapers/node_crawler_base.py`'s docstring). Never fabricate a value to
   raise a coverage number.
3. **Follow the existing pattern, don't invent a new one.** Every Phase 7
   wrapper is `scrapers/<name>_crawler.py` with `_derive_signals(row) -> dict`
   (pure, unit-testable) + `sync_<name>(company, db)` calling
   `adapters.base.run_adapter(...)`, using `scrapers/node_crawler_base.py`'s
   `run_ts_crawler`/`run_node_entrypoint`. A new competitor-benchmark variant
   should look like `scrapers/competitor_benchmark.py`; a new directory
   plugin should look like `Scraper/crawlers/directory-listing-crawler/src/directories/mecspe.ts`
   (copy `_template.ts`, per that package's own README).
4. **Test discipline.** `tests/conftest.py` already points `DATABASE_URL` at a
   throwaway SQLite file so nothing touches the live Neon DB by default —
   never set `VIENNA_TESTS_ALLOW_LIVE_DB=1` as a shortcut. Run
   `pytest --capture=no` (default capture crashes in this environment) and
   get the pass count from `--junitxml`, not the swallowed summary line.
   Add unit tests for your pure `_derive_signals`/matching functions. If your
   ticket touches network/crawler code, **also live-verify against at least
   one real company** (not just mocks) before marking it done — this
   project's own convention, and how most of the real bugs listed in this
   file's history were actually found.
5. **Git workflow, in a working tree other sessions may also be using:**
   - Never `git stash` or `git rebase`. If you find uncommitted changes that
     aren't yours when you start, leave them alone — don't discard or commit
     them.
   - Stage **explicit paths** (`git add path/to/file.py`), never `git add -A`
     or `git add .` — another bot's in-progress edit may be sitting in the
     same tree.
   - `git pull` (merge, not rebase) before you start and again right before
     you push.
   - Commit and push your own work without waiting for a human to say so —
     that's the standing convention in this repo. Small, frequent commits
     beat one giant one.
   - OneDrive syncs this folder; `git status` can occasionally lag reality by
     a few seconds right after a big file write. If something looks wrong,
     wait a moment and re-check before concluding anything is broken.
6. **Branches: one per ticket, merged immediately, never piled up.** See
   *Claiming protocol* step 4. Don't work on `master` directly (another bot's
   concurrent commit could land on top of half-finished work), but also don't
   let a branch live longer than one ticket takes to finish. No long-running
   personal/feature branches.

## Claiming protocol

A ticket's row in the board below is the lock. There's no server, so the
**git push is the atomic operation** — if someone else claimed first, your
push will conflict and you'll see their claim when you pull.

1. `git checkout master && git pull`.
2. Pick the highest-priority ticket whose Status is `OPEN` (or the specific
   ticket ID you were given). Skip anything `CLAIMED`/`IN_PROGRESS`/`DONE`.
3. Edit **only your ticket's row** in the board table: set Status to
   `CLAIMED`, Claimed-by to your agent name/session id, Claimed-at to the
   current UTC timestamp. Commit **only this file** with a message like
   `Claim T04 (competitor catalog benchmark)` and push immediately.
   - If the push is rejected (non-fast-forward): `git pull`, check whether
     someone else claimed the *same* ticket in the meantime. If they did,
     pick a different one. If it was an unrelated change, re-apply your claim
     edit and push again.
4. Create a branch from the now-current `master`:
   `git checkout -b crawler/T04-competitor-catalog-benchmark` (pattern:
   `crawler/<ID>-<short-slug>`).
5. Implement strictly within the ticket's declared file list. If you
   discover you need a file outside that list, that's fine — just note it in
   the ticket's Results section so the next bot isn't surprised — but don't
   touch files that are a *different* open or claimed ticket's declared
   scope.
6. Test per Ground rule 4. When it's solid:
   - `git checkout master && git pull` again.
   - Merge your branch into master: `git merge --no-ff crawler/T04-...`
     (plain merge, no rebase). Resolve conflicts if any landed meanwhile —
     tickets are scoped to avoid this, so a conflict usually means two bots
     touched a shared registration point (e.g. `PHASE_7_SOURCES`); keep both
     additions.
   - Push `master`. Delete the branch (`git branch -d crawler/T04-...`).
7. Update the ticket's row to `DONE` (or `BLOCKED` with a one-line reason) and
   add a short dated note under that ticket's **Results** heading: what you
   built, what you verified live, what's still open. This is the only part of
   a ticket's detail section anyone should edit after the ticket is written —
   append, don't rewrite the original problem statement.

If you're picking up a `BLOCKED` ticket, read its Results notes first — don't
re-discover the same blocker.

## Ticket board

| ID | Title | Track | Priority | Status | Claimed by | Claimed at (UTC) | Branch |
|----|-------|-------|----------|--------|------------|-------------------|--------|
| T01 | Unattended Phase 1/4/7 coverage sweep | Ops | P0 | DONE | Codex 01a127b4 | 2026-10-10 21:25:27 | (merged) |
| T02 | Fix stale crawler-count/category references | Ops | P0 | DONE | Vibe Code fd5055 | 2026-10-10 21:26:49 | (merged) |
| T03a | Digital Maturity Crawler reliability (33% error rate) | Reliability | P1 | DONE | Claude Code 95d22c8c | 2026-10-10 21:28:17 | (merged) |
| T03b | Job Postings Crawler reliability (23% error rate) | Reliability | P1 | DONE | Antigravity da2ebdec + Claude Code gio3maz | 2026-10-10 22:38:37 | (merged) |
| T03c | Directory Listing Crawler reliability (25% error rate) | Reliability | P1 | DONE | Claude Code 95d22c8c | 2026-10-11 08:25:04 | (merged) |
| T04 | Competitor Product/Catalog Benchmark → `product_differentiation` | Competitor army | P1 | CLAIMED | Antigravity 0cf87136 | 2026-10-11 19:24:00 | crawler/T04-competitor-catalog-benchmark |
| T05 | Competitor Hiring-Velocity signal (raw data) | Competitor army | P2 | BLOCKED (job crawler counts navigation as vacancies) | Codex 01a12a10 | 2026-10-11 08:53:03 | (merged; inactive prototype) |
| T06 | Competitor News/Press-Share signal (raw data) | Competitor army | P2 | DONE | Claude Code gio3maz | 2026-10-11 15:39:21 | (merged) |
| T07 | Competitor Review/Reputation signal (raw data) | Competitor army | P2 | BLOCKED (both review modes disabled; no live rating extraction) | Codex 01a12a10 | 2026-10-11 16:15:25 | (merged; gated) |
| T08 | Directory plugin: ANIMA / Confindustria Meccanica Varia | New directories | P1 | DONE | Antigravity 0cf87136 | 2026-10-11 08:37:30 | (merged) |
| T09 | Directory plugin: Kompass Italia | New directories | P2 | DONE | Claude Code gio3maz | 2026-10-11 16:20:18 | (merged) |
| T10 | Directory plugin: Europages | New directories | P2 | BLOCKED (robots.txt disallows all generic crawlers + all query-string URLs) | Claude Code gio3maz | 2026-10-11 17:00:52 | (no branch — blocked before any code) |
| T11 | `sector_pilot_precedent` via free Google News RSS route | Signal extension | P2 | DONE | Antigravity 0cf87136 | 2026-10-11 16:18:00 | (merged) |
| T12 | `regulatory_compliance_exposure` curated EUR-Lex lookup table | Signal extension | P2 | BLOCKED (company-specific legal facts and legal review needed for a score) | Codex 01a12a10 | 2026-10-11 17:01:55 | (merged; screening only) |
| T13 | Wire the ANIMA directory plugin into production (`DEFAULT_DIRECTORY_URLS`) | New directories | P0 | DONE | Antigravity 0cf87136 | 2026-10-11 15:36:20 | (merged) |
| T14 | TED Contract Awards Crawler reliability (89% error rate) | Reliability | P1 | DONE | Codex 01a12a10 | 2026-10-11 15:37:33 | (merged) |
| T15 | Product Catalog Crawler reliability & priced-extraction gap (41% error rate) | Reliability | P2 | DONE | Antigravity 0cf87136 | 2026-10-11 17:01:20 | (merged) |
| T16 | Product Catalog Crawler: catalog-page breadth starves the product-page budget | Reliability | P2 | CLAIMED | Claude Code gio3maz | 2026-10-11 19:32:08 | crawler/T16-catalog-page-budget |
| T17 | Job Postings: careers-page-local nav/news links are not vacancies; external job boards are not a confirmed zero | Reliability | P1 | CLAIMED | Vibe Code f3f064 | 2026-10-11 19:41:30 | crawler/T17-job-postings-false-vacancies |
| T18 | Phase 1/4 coverage sweep needs per-source completion tracking | Ops | P0 | BLOCKED (T04 currently edits `company_service.py`; reopen after its merge) | — | — | (not claimed) |

Adding a new ticket yourself (T16+)? Append a row here and a matching
subsection below, following the same shape, and mark it `OPEN`. Don't
renumber existing tickets.

---

## Ticket details

### T01 — Unattended Phase 1/4/7 coverage sweep
**Track:** Ops · **Priority:** P0 (highest leverage item in this entire file)

**Problem.** Live `SourceHealth` counts (checked 2026-10-10): Digital
Maturity / Job Postings / Review / News Signals / Innovation Participation /
Directory Listing Crawlers have each been called ~387 times total — out of
**953** companies. TED Contract Awards: 4 calls. Competitor Benchmark, FDA
Recalls, LinkedIn Profile: effectively 0 (no `SourceHealth` row yet). The
backlog isn't a missing-source problem, it's a volume problem: Pipeline
Health's "🕸️ Run Phase 7 Crawler Enrichment" button
(`views/pipeline_health.py`) defaults to 10 companies per click and needs a
human to keep clicking it. `scripts/indicator_gaps.py`'s RUN category (27
indicators, including two weight-5 READINESS indicators at 1% population —
`external_collaboration`, `university_partnership`) is almost entirely this:
code that works, just never pointed at the rest of the roster.

**Why this beats writing a new crawler.** Every indicator in the RUN category
of `scripts/indicator_gaps.py`'s output moves simultaneously once this ships
— no new scraping logic, no new indicator risk, pure volume.

**Implementation plan.**
- New module (`scripts/coverage_sweep.py` or extend `crawl_jobs.py` — your
  call, but don't duplicate `CrawlJobManager`'s queue/worker logic, reuse it)
  that: finds companies via the existing
  `company_service.companies_not_yet_crawled(db, companies, phase)` for
  phases 1, 4, and 7; submits them through the existing
  `views.crawl_widget.queue_crawl()` (or `crawl_jobs.get_manager()` directly
  if you need to run outside Streamlit) in bounded batches; and **keeps
  resubmitting** until the backlog for all three phases is empty, instead of
  stopping after one batch.
- Respect `resource_governor.py`'s existing concurrency caps — do not bypass
  them by calling crawlers directly.
- Respect every existing off-by-default gate as-is
  (`LINKEDIN_CRAWLER_ENABLED`, `KUNUNU_CRAWLER_ENABLED`,
  `BUNDESANZEIGER_PAID_ENABLED`) — a sweep must not flip policy decisions.
- Also re-crawl companies whose last Phase 7 run is older than some staleness
  window (e.g. 60 days) — not just never-crawled ones — since several signals
  (e.g. `physical_stores_trend`) are explicitly trend signals needing a
  second data point.
- Give it both an entry point a human can run ad hoc
  (`python scripts/coverage_sweep.py`) and a way to wire it behind a
  Streamlit toggle on Pipeline Health ("keep the backlog drained
  automatically") if you have time — the standalone script alone is enough
  to ship this ticket.
- Log progress somewhere a human can check without staring at a terminal
  (reuse `SourceHealth.last_run_at`/`total_calls`, or write a simple
  `sweep_state.json` beside the DB).

**Files to touch:** new `scripts/coverage_sweep.py`; read-only use of
`company_service.py`, `crawl_jobs.py`, `resource_governor.py`; optional small
addition to `views/pipeline_health.py` for the toggle (additive — don't
rewrite the existing manual batch button).

**Acceptance criteria:** run it against the real DB for a bounded time (e.g.
30 minutes) and show `SourceHealth.total_calls` rising across multiple
sources; `scripts/indicator_gaps.py`'s RUN-category percentages visibly
improve on a before/after diff. Unit test the backlog-selection logic against
a fixture DB (file-backed SQLite, not `:memory:` — see Ground rule 4 and
`tests/test_phase7_batch.py`'s existing pattern for why).

**Results**

- 2026-10-11 (Codex 01a127b4, merged `crawler/T01-coverage-sweep` to
  master, branch deleted): Added `scripts/coverage_sweep.py` to drain Phase
  1/4/7 through the existing `CrawlJobManager` in bounded batches. It
  prioritizes the faster phases, retries unresolved work after a delay,
  includes Phase 7 runs older than 60 days, observes the queue's resource
  limits and existing source gates, and exposes progress through
  `SourceHealth`. Added `tests/test_coverage_sweep.py` outside the declared
  file list because the acceptance criteria require file-backed SQLite
  backlog-selection and batch tests; full pytest: 578 passed, 1 skipped.
  Live verification: 118 Phase 4 and 15 Phase 7 company runs; Google News
  RSS calls rose 1072→1190 and Digital Maturity, Job Postings, and Directory
  Listing calls each rose 406→421 after correcting local Temp permissions.
  `scripts/indicator_gaps.py` moved `tech_stack_intensity` 88%→89%,
  `online_market_presence` 22%→23%, and
  `website_digital_maturity` 9%→10%. The 953-company backlog still needs
  a longer unattended run; some existing source errors from unreachable
  sites remain. The optional Pipeline Health toggle was not added.

---

### T02 — Fix stale crawler-count/category references
**Track:** Ops · **Priority:** P0 (fast, unblocks trusting the tools)

**Problem, two small drifts found 2026-10-10:**
1. `views/pipeline_health.py:141` hardcodes *"Each of the 9 crawlers under
   Scraper/crawlers/..."* — there are **11** Node crawler packages today
   (TED Contract Awards and FDA Recalls Crawler were added after that text
   was written). `views/company_detail.py`'s equivalent help text was already
   fixed to count dynamically (`len(eligible)` — see commit
   `2092233`); this one line wasn't.
2. `scripts/indicator_gaps.py`'s `GAP_PLAN` still categorizes `erp_systems_age`
   and `energy_transition_capex` under `BUILD` ("extend job crawler" /
   "hard... consider first-contact") even though both were actually built
   since (`9d33037 Detect ERP vendor mentions in job postings for
   erp_systems_age`, `ed30596 Deep-read sustainability report PDFs for
   energy_transition_capex`). Their real status now is "coded, 0% population
   because never run at volume" — i.e. they belong in `RUN`, not `BUILD`.
   `tests/test_indicator_gaps.py` keeps `GAP_PLAN` consistent with the
   indicator catalog, not with what's actually implemented in `scrapers/`, so
   this drift wasn't caught automatically.

**Implementation plan.** Fix #1 by counting the actual registered Node
crawlers (not `PHASE_7_SOURCES`, which also includes the derived Competitor
Benchmark) instead of a hardcoded "9". Fix #2 by moving both keys' `GAP_PLAN`
entries from `BUILD` to `RUN` with an updated note describing what's actually
missing now (volume, not code) — re-run
`python scripts/indicator_gaps.py` against the live DB to confirm their real
current percentage before writing the note.

**Files to touch:** `views/pipeline_health.py` (one line),
`scripts/indicator_gaps.py` (two `GAP_PLAN` entries + their category).

**Acceptance criteria:** `pytest tests/test_indicator_gaps.py` still passes;
`scripts/indicator_gaps.py`'s printed categories match current code reality
for both keys.

**Results**

- 2026-10-10 (Vibe Code fd5055, merged `crawler/T02-stale-references` →
  master, branch deleted): Built #1 as a new helper
  `scrapers/node_crawler_base.scraper_crawler_names()` (counts
  `package.json`-bearing subfolders of `SCRAPER_CRAWLERS_DIR`, returns `[]`
  when the sibling `Scraper/` checkout is absent — hosted-deploy posture,
  same as `worker_hub.crawler_env_available`); `views/pipeline_health.py`'s
  Phase 7 caption now interpolates it instead of the hardcoded "9". This is
  a deliberate small addition outside the ticket's file list (the one-line
  text fix needed a counting source and none existed) — noted here per the
  claiming protocol. Moved #2: both `erp_systems_age` (producer
  `scrapers/job_postings_crawler.py`, commit `9d33037`) and
  `energy_transition_capex` (producer
  `scrapers/company_website_crawler.py`, commit `ed30596`) confirmed coded in
  `scrapers/` → moved BUILD → RUN with notes saying "coded, 0% population
  only because never run at volume (T01)"; regenerated
  `docs/indicator_catalog.json` via `scripts/generate_indicator_catalog.py`
  (that generator's own test forces it — derived file, not hand-edited).
  Added `test_scraper_crawler_names_counts_only_package_dirs` to
  `tests/test_crawler_scrapers.py`. Verified: `pytest --capture=no`
  full suite 577 tests, 576 pass, 1 skipped, 0 failures from my change —
  the one failure (`tests/test_worker_hub.py::test_committed_bundle_contains_the_current_worker_source`,
  stale `worker_dist` bundle, CRLF mismatch) pre-exists on a clean tree and
  is outside this ticket's scope. Could not re-run
  `scripts/indicator_gaps.py` against the live DB to confirm current
  percentages before writing the notes: no `.env`/`DATABASE_URL` in this
  environment (only `.env.example`); both producers' existence was verified
  from code + git history instead.

---

### T03a — Digital Maturity Crawler reliability
**Track:** Reliability · **Priority:** P1

**Problem.** Live `SourceHealth`: 387 calls, **128 errors (33%)**. This is
also the crawler `scrapers/competitor_benchmark.py` (T04's sibling) depends
on, so its reliability caps two indicators at once
(`website_digital_maturity`, `competitor_digital_gap`). Known contributing
cause from earlier sessions: Wayback's CDX API rate-limits aggressively and
503s under concurrency (`WAYBACK_DELAY_MS` was already raised once to 1500ms
— see `digital_maturity_crawler.py`'s `build_crawler_env` comment). Turn
`VIENNA_CRAWLER_LOG_DIR` on (see `scrapers/node_crawler_base.py`'s
`_log_subprocess`) and look at a sample of real failing runs via
`scripts/crawler_bench.py` before assuming the cause — don't just re-guess
the fix from the old memory note, verify against today's actual failures
first.

**Implementation plan.** Diagnose from real logs, then likely candidates:
further back off/retry CDX calls, or widen `RUN_TIMEOUT_SECONDS` if timeouts
dominate, or make the vision-LLM fallback trigger sooner when Wayback is
clearly failing (it already exists per `_VISION_SCORE_TO_AGE_YEARS`) instead
of waiting out the full Wayback budget first. Whatever you change, re-run the
same real-company sample and show the error rate drop.

**Files to touch:** `scrapers/digital_maturity_crawler.py`, and/or
`Scraper/crawlers/digital-maturity-crawler/src/*` (separate git repo —
remember to commit/push there too, see note at the bottom of this file).

**Acceptance criteria:** live-verified error rate materially lower than 33%
on a real batch of ≥20 companies; existing unit tests still green.

**Results (2026-10-11):** Diagnosed from real logs/live data first, per the
ticket's own instruction, rather than re-guessing the old Wayback-503 theory.
Two independent checks pointed away from the crawler's own Wayback/CDX logic:
(1) a 25-company **sequential, uncontended** `scripts/crawler_bench.py --lanes
digital` run succeeded on **25/25** companies (0 errors) — the retry/back-off
logic itself is fine in isolation; (2) while another agent's concurrent
Phase 7 coverage sweep ran against the live DB at the same time, `SourceHealth`
for Digital Maturity (→36%), Job Postings (→26%), and Directory Listing
(→28%) all spiked together — every crawler that shares the `browser`
resource-governor pool — while Review Crawler (never attempts a live fetch;
its modes are off by default) stayed at 0%. A captured live error read
`"digital-maturity-crawler timed out after 250s"`.

**Root cause:** `resource_governor.CRAWLER_RESOURCES` lists `"browser"` and
`"vision_llm"` for `digital-maturity-crawler` unconditionally, but both are
only actually used when a vision LLM key is configured (`main.ts`'s
`hasVisionLlmConfigured()` guard skips the whole screenshot+vision phase
otherwise) — unconfigured is this deployment's actual state. Since
`vision_llm`'s default capacity is 1 regardless of configuration, **every**
call serialized this crawler to one run at a time system-wide (confirmed with
a synthetic 4-thread test: fully serialized, 0.3s apart) and held a `browser`
slot for 50-200+s per run the other four browser-using crawlers (job-postings,
directory-listing, review, company-website) needed, for a run that never
opens a browser.

**Fix:** `Governor.slot()` now accepts an optional `resources` override
(defaults to the old static `CRAWLER_RESOURCES` lookup when omitted — fully
backward compatible for every other crawler), threaded through
`node_crawler_base.run_ts_crawler()`. Added `digital_maturity_crawler.
crawler_resources()`, returning `("wayback", "process")` when no vision key
is configured and the original full set otherwise; wired into both
`sync_digital_maturity` and `competitor_benchmark.py`'s `_crawl_competitor`
(same crawler, same fix needed in both callers).

**Live-verified:** a controlled 8-company **concurrent** `--mode queue
--workers 4` repro (real `CrawlJobManager` + resource-governor path, all
Phase 7 sources at once) with the fix applied printed `peak slots in use
{'process': 6, 'browser': 4, 'llm': 2, 'vision_llm': 0, 'wayback': 2}` —
`vision_llm` never touched, `wayback` reached its intended cap of 2 (multiple
digital-maturity-crawler runs genuinely concurrent, confirmed in the queue
manager's own progress log), and **0/8 digital-maturity-crawler errors**.
Full test suite: 583 passed, 1 skipped (pre-existing), 0 failures.

**Files touched beyond the ticket's declared list** (Ground rule 5):
`resource_governor.py` (added the `resources` override to `Governor.slot()` —
additive, no other crawler's behavior changes) and `scrapers/node_crawler_base.py`
(threaded the new parameter through `run_ts_crawler()`), plus
`scrapers/competitor_benchmark.py` (T04's sibling caller, needed the same fix).

**Open/out of scope:** this fix addresses the resource-contention mechanism;
it doesn't rule out Wayback itself still erroring under heavier load than my
8-company repro reached, so T03a's error rate is worth re-checking against
`SourceHealth` after a larger real sweep accumulates. Separately noticed (not
fixed here, different crawler entirely): every company in the concurrent
repro hit `TED Contract Awards Crawler: ted-awards-crawler exited 1: ERROR
Error: Invalid Record Length: columns length is 2, got 1 on line 6` —
consistent across completely different company data, smells like a genuine
CSV-generation bug in that crawler's input, not a data issue. Flagged
separately; not in scope for any currently open ticket in this file.

---

### T03b — Job Postings Crawler reliability
**Track:** Reliability · **Priority:** P1

**Problem.** Live `SourceHealth`: 387 calls, **89 errors (23%)**.

**Implementation plan.** Same diagnose-from-real-logs-first approach as T03a.
`scrapers/job_postings_crawler.py`'s careers-page discovery
(`_discover_careers_page`) already has several hardening passes behind it
(soft-404 detection, site-chrome filtering) — check whether the current
failures are discovery misses (no careers page found when one exists) versus
subprocess timeouts versus the Node crawler itself erroring, and fix the
dominant cause rather than guessing.

**Files to touch:** `scrapers/job_postings_crawler.py`, and/or
`Scraper/crawlers/job-postings-crawler/src/*`.

**Acceptance criteria:** live-verified error rate materially lower on a real
batch of ≥20 companies; existing unit tests still green.

**Results**

- 2026-10-11 (Antigravity da2ebdec claimed/implemented; Claude Code gio3maz
  took over, tested, live-verified and merged — see note below on why):
  Antigravity's session diagnosed and fixed this but went inactive
  (confirmed by the user) before committing or live-verifying, leaving
  substantial, coherent, uncommitted work sitting in the shared working
  tree for hours. Reviewed the full diff before touching anything, found it
  addressed the real problem soundly, then finished what Ground rule 4
  still required: tests and live verification.
  **Root cause: the same resource-contention pattern T03a already found
  for Digital Maturity Crawler** — `job-postings-crawler` unconditionally
  claimed a `"browser"` resource-governor slot even on companies where no
  careers page was ever found and Playwright was never going to run,
  starving the four other browser-using Phase 7 crawlers under real
  concurrent load. Fix: `crawler_resources(careers_url)` returns
  `("process",)` only (no `"browser"`) when no careers URL exists, same
  fix shape as `digital_maturity_crawler.crawler_resources()`, now also
  skipping the Node subprocess entirely in that case — confirmed this
  produces the identical honest outcome `_derive_signals` already returns
  for `sources_used=[]` (empty signals, not a fabricated absence), not a
  new shortcut.
  Also fixed along the way: an SSL-error fetch path with an `http://`
  fallback retry; soft-404 redirect detection across `www.`/non-`www.`
  variants (`_same_url_or_home` — the old check missed a redirect back to
  the homepage under a different host form); and a timeout bug where one
  slow probe candidate's `FutureTimeoutError` aborted checking every
  remaining candidate (`break` → `continue`, with the per-candidate
  remaining-time budget now computed correctly).
  **Live-verified** via `scripts/crawler_bench.py` against real companies:
  a 25-company sequential `--lanes jobs` run came back **0/25 errors**
  (down from the 23% baseline, 387 calls/89 errors); an 8-company
  concurrent `--mode queue --workers 4 --phases 7` repro (every Phase 7
  source running at once, matching T03a/T03c's own contention-reproduction
  method) came back with **0 Job Postings Crawler errors** — the batch's
  only error was an unrelated, already-known Digital Maturity Crawler
  250s timeout (flagged, not new, see T03a's own Results note). Full
  `pytest --capture=no`: 642 passed, 0 failed (16 of those job-postings-
  specific, including 2 new: `crawler_resources()` branching and the
  no-careers-URL short-circuit).
  **Note on the merge:** committed directly to `master` rather than via
  the existing `crawler/T03b-job-postings-reliability` branch — that
  branch only ever held the original claim commit from hours before
  `master` moved through ~10 other tickets' merges, so replaying onto it
  would have meant a rebase (forbidden, Ground rule 5) rather than a
  clean merge. Net effect on `master` is the same as the usual
  claim-branch-merge flow.

---

### T03c — Directory Listing Crawler reliability
**Track:** Reliability · **Priority:** P1

**Problem.** Live `SourceHealth`: 387 calls, **97 errors (25%)**. Only MECSPE
has a registered plugin today, so every call against an unconfigured
directory URL should come back a clean `no_config`, not an error — if
`no_config` responses are being counted as errors, that may be most of this
25% and would mean the "error rate" is actually healthy once T08-T10 give
more companies a real directory to match against. Check this hypothesis
against real logs before treating it as a bug to fix in the crawler itself.

**Implementation plan.** Diagnose first. If genuine errors (not
`no_config`), fix them in the MECSPE plugin or the shared Playwright/consent
harness (`Scraper/crawlers/directory-listing-crawler/src/consent.ts`). If it
turns out to mostly be `no_config` being miscounted as an error by the Python
wrapper (`scrapers/directory_listing_crawler.py`) or by `adapters.base`'s
error accounting, fix the counting, not the crawler.

**Files to touch:** `scrapers/directory_listing_crawler.py`, and/or
`Scraper/crawlers/directory-listing-crawler/src/*`.

**Acceptance criteria:** state clearly in your Results note which of the two
causes it was, with the real log evidence, and show the corrected error rate.

**Results (2026-10-11):** Checked the ticket's own `no_config` hypothesis
first and ruled it out: `scrapers/directory_listing_crawler.py`'s
`DEFAULT_DIRECTORY_URLS` has only ever contained MECSPE's own URL (checked
its full git history), and MECSPE's `matchesUrl` correctly matches it — a
25-company live run confirmed every single row came back `status: "ok"`,
never `no_config`. So: **genuine errors**, not miscounting — the ticket's
second branch.

Diagnosed the same way as T03a: a 25-company **sequential, uncontended**
`crawler_bench.py --lanes directory` run succeeded on **25/25** (0 errors,
10-17s each) — the crawler's own MECSPE search/consent/matching logic isn't
the problem. A controlled 8-company **concurrent** `--mode queue --workers 4`
repro (real `CrawlJobManager` + resource-governor path, all Phase 7 sources
at once) killed 2/8 at exactly `"directory-listing-crawler timed out after
75s"`. Comparing `run_timeout` across every browser-using Phase 7 wrapper:
company-website 170s, job-postings/linkedin-profile 100s each,
**directory-listing 75s** — the tightest of all of them despite sharing the
exact same scarce `"browser"` resource-governor slot (capacity 4) as every
one of those. Under real concurrent load (several of those crawlers'
Chromium instances competing for the same machine), this crawler's own
budget ran out first even though its actual work is lighter than its peers'.

**Fix:** widened `run_timeout` from 75s to 150s (`directory_listing_crawler.
RUN_TIMEOUT_SECONDS`) and raised the adapter's own wall-clock budget to
match (`RUN_TIMEOUT_SECONDS + 30`), same pattern every other wrapper already
uses. No change needed in the TS crawler itself or the MECSPE plugin — the
25/25 clean isolated run is why.

**Live-verified:** re-ran the identical 8-company concurrent repro with the
fix. **0/8 directory-listing-crawler errors** (down from 2/8), and the two
previously-killed companies (COMEC GROUP, SITMA) each went from 0-2 real
signals to 4 — the timeouts weren't isolated to this one source; they were
part of the same contention hurting everything sharing that machine, and the
whole 8-company batch finished in roughly half the wall time (325s vs 648s)
once this crawler stopped burning a full browser slot for 75s before giving
up. Full test suite: 596 passed, 0 failures. New regression test:
`tests/test_crawler_hardening.py::test_directory_listing_budget_was_widened_past_its_old_75s`.

---

### T04 — Competitor Product/Catalog Benchmark → `product_differentiation`
**Track:** Competitor army · **Priority:** P1

**Problem.** `indicators.py`'s `product_differentiation` (weight 2, NEED,
`category=CAT_MARKET`) has a real proxy definition — *"number of comparable
competing products at similar price points"* — and is 0% populated.
`scripts/indicator_gaps.py` explicitly says why it was never built from the
company's own website alone: counting a company's own differentiation claims
would invert the definition (a company that doesn't self-promote would read
as *more* commoditized, which is backwards). It says the real fix "needs a
real competitor-landscape source." That source already exists in this repo:
`scrapers/product_catalog_crawler.py` (full SKU/spec/price extraction,
schema.org JSON-LD first, LLM fallback) — it has just never been pointed at
anything but the company's own site.

**Implementation plan.** Copy `scrapers/competitor_benchmark.py`'s exact
shape (same `Competitor` rows, same `MAX_COMPETITORS` cap, same
"skip cleanly if the company's own signal isn't measured yet" guard) but
re-run **product-catalog-crawler** instead of digital-maturity-crawler. Your
own company's catalog breadth is already captured as a raw blob
(`RawImportRecord` with `dataset_name="crawler_product_catalog"`, see
`scrapers/product_catalog_crawler.py`) — read that, not a `SignalRecord`
(product-catalog-crawler deliberately writes no signal). Compare SKU count /
category breadth / price-band overlap between your company and its named
competitors; write `product_differentiation` honestly — if the company has
*more* distinct, priced products than its named peers, that's lower
commoditization (higher differentiation); fewer/same-priced products than
peers reads as more commoditized. State your scoring formula explicitly in
the module docstring (mirroring how `compute_gap` in `competitor_benchmark.py`
documents its math) and get it reviewed — this is a genuinely new scoring
formula, not just a data pull, so don't be clever about it; keep it simple
and explainable.

**Files to touch:** new `scrapers/competitor_catalog_benchmark.py`; one new
entry in `company_service.py`'s `PHASE_7_SOURCES` list; one new
`SOURCE_CREDENTIAL_VARS` entry in `config.py` (empty list, matches
Product Catalog Crawler's own ungated posture); a new test file mirroring
`tests/test_competitor_benchmark.py`.

**Acceptance criteria:** live-verified against ≥2 real companies that already
have named competitors recorded (check the DB — if none exist yet, record 1-2
yourself via Company Intelligence as part of verification, using real,
disclosed-publicly competitor names, not invented ones); `product_differentiation`
populates with an auditable evidence trail (named competitors + their SKU
counts, same evidence-dialog pattern every other Phase 7 signal already
uses).

**Results**

- 2026-10-11 (Codex 01a12a10; merged `crawler/T04-competitor-catalog-benchmark`
  as an inactive prototype, branch deleted): Built
  `scrapers/competitor_catalog_benchmark.py` with a documented 0-15
  commoditization formula, same-category/same-currency fixed-price matching
  within 20%, explicit partial-crawl handling, named-peer SKU counts and
  per-product evidence; added 9 tests. Deliberately did not register a
  Phase 7 source in `company_service.py` or `config.py`: the formula still
  needs review and no live own catalog had comparable priced data. Live DB
  had 953 companies, zero named-competitor rows, and 18 prior catalog blobs
  with no priced products. The existing product-catalog crawler returned
  3 unpriced, uncategorized names (partial) for Imperia & Monferrina despite
  visible fixed prices on https://imperia.com/products/; it returned zero
  products (partial) for Aerotecnica Coltri despite visible fixed prices on
  https://www.coltri.com/en/category-product/accessories/compressor-accessories/.
  The new competitor crawl path did run live against Marcato's homepage and
  returned 2 fixed-price products. No `product_differentiation` signal was
  written and no competitor rows were added. Full `pytest --capture=no`:
  591 passed, 1 skipped (from JUnit XML). **BLOCKED:** own-catalog price
  extraction must be fixed in the separate product-catalog-crawler repo,
  outside T04's file list. Then record two publicly verified named peers in
  Company Intelligence, review the formula, register the source, and verify
  two real scored companies before marking DONE.

---

### T05 — Competitor Hiring-Velocity signal (raw data only)
**Track:** Competitor army · **Priority:** P2

**Problem.** No indicator exists today for "is this company hiring more or
less aggressively than its named competitors" — that's a real product
decision for GG (new indicator + weight), not something to decide
unilaterally (Ground rule 1). But the raw comparison is cheap to produce now
and useful context immediately, the same way `product_catalog_crawler.py`
already surfaces undecided-indicator data via the Raw Data & Mapping tab.

**Implementation plan.** Same `Competitor`-row pattern as T04, re-running
**job-postings-crawler** (`scrapers/job_postings_crawler.py`'s
`_discover_careers_page` + `run_ts_crawler` call) against each named
competitor's homepage. Write **no** `SignalRecord` — only
`save_crawler_blob()` with each competitor's open-role count and
digital/technical role count alongside the company's own (already-measured)
numbers for comparison. Surface clearly in the Raw Data & Mapping tab (reuse
the existing per-signal Audit dialog pattern, or add a small note there — see
`views/company_detail.py`'s Raw Data tab).

**Files to touch:** new `scrapers/competitor_hiring_benchmark.py`; one new
`PHASE_7_SOURCES` entry; a new test file.

**Acceptance criteria:** live-verified against ≥1 real company with named
competitors; explicitly note in your Results section that this is
intentionally unscored and flag it to GG as an open decision (which signal
key it should become, if any, and what weight) — don't silently leave that
decision implicit.

**Results**

- 2026-10-11 (Codex 01a12a10; merged inactive prototype, branch deleted):
  Added scrapers/competitor_hiring_benchmark.py and 6 tests for named-peer
  careers discovery, field-status-aware raw role counts, and blob-only storage.
  No Phase 7 registration or UI change was shipped, no SignalRecord was
  written, and no named-competitor row was inserted in the live DB (which
  had zero such rows). Live crawls of documented actuator peers found Rotork's
  careers page but miscounted 10 navigation/news links as 10 digital jobs
  (https://www.rotork.com/en/careers); Bray's careers page returned zero
  despite linking to external job postings (https://www.bray.com/careers).
  Valbia had no discovered careers page; Prisma's CV form returned zero.
  Air Torque's existing own-job blob had four plausible role titles, but its
  four-of-four digital count was not independently corroborated. Full
  pytest --capture=no: 597 passed, 1 skipped (JUnit XML). **BLOCKED:**
  T03b must fix/verify the job crawler's false vacancy counts and external
  job-link handling before this raw comparison can be activated and verified
  against a real company with a recorded peer. This is intentionally
  unscored; GG must decide whether it becomes a signal, its key, and its
  weight, if any.

---

### T06 — Competitor News/Press-Share signal (raw data only)
**Track:** Competitor army · **Priority:** P2

**Problem.** Same shape as T05 but for press mentions: is this company
appearing in the news less often than its named peers? No indicator exists
for this either.

**Implementation plan.** Reuse `adapters/google_news_rss.py`'s query
mechanics (same keyless RSS feed already used for
`external_collaboration`/`partnership_news_count`/etc.) against each named
competitor's name, with the **same generic-name guard and legal-suffix
handling** already hard-won in that adapter (see its own code/comments for
the ANDing-collapses-recall and generic-name-collision issues found in an
earlier session — don't rediscover those bugs, read the adapter first).
Write raw mention counts only, no `SignalRecord`.

**Files to touch:** new `scrapers/competitor_news_benchmark.py` (or extend
`adapters/google_news_rss.py` with a competitor-facing entry point if that
reads cleaner — your call, but don't duplicate its query-building logic).

**Acceptance criteria:** live-verified against ≥1 real company with named
competitors; same "flag as open decision" note as T05.

**Results**

- 2026-10-11 (Claude Code gio3maz, merged directly to `master` — see note
  below — branch deleted): Built `scrapers/competitor_news_benchmark.py`,
  reusing `adapters/google_news_rss.py`'s private `_fetch`/`_filter_headlines`/
  `_name_is_distinctive`/`_bucket`/`CONCEPT_PATTERNS` helpers directly rather
  than re-deriving the query/attribution logic, with a `SimpleNamespace`
  standing in for the `company` object those helpers expect (a named
  `Competitor` only has a name + homepage, so province/region/sector read as
  `None` — which correctly forces the stricter "tokens+discriminator" generic-
  name case to skip rather than guess, since no discriminator is available).
  The company's own press-mention count is read from its already-saved Phase 4
  blob (`RawImportRecord.dataset_name == "crawler_news_rss"`) instead of
  re-fetched — only each named competitor gets a fresh live call. Writes a raw
  blob only (`crawler_competitor_news` via `save_crawler_blob`), no
  `SignalRecord`: **this is intentionally unscored — GG has not chosen a
  signal key or weight for peer-relative press share**, same open decision as
  T05's hiring benchmark. Unlike T04/T05, there is no known reliability
  blocker for Google News RSS itself, so this one **was** registered as a
  real Phase 7 source (`PHASE_7_SOURCES`, `eligible_phase7_sources` gated on
  having any named competitor, a `SourceStep`) — deliberately outside this
  ticket's stated file list (`company_service.py`), to avoid repeating T08's
  "dead in production" mistake.
  Live-verified against **ARISTARCO S.P.A.** (real company, own
  `headlines_about_company` = 24) after recording two real, publicly-
  disclosed named competitors in the live DB — **Comenda** (comenda.eu) and
  **Winterhalter** (winterhalter.com), both real commercial warewashing/
  dishwasher manufacturers, same sub-sector, confirmed via web search before
  insertion: Winterhalter returned 2 real matched headlines (verified
  real article URLs/dates), Comenda measured 0 (its single short token needs
  a discriminator this company has none of, so it was honestly gated rather
  than guessed — "Comenda" is also an ordinary Italian word/surname, exactly
  the collision risk that guard exists for). `SourceHealth` after the run:
  1 call, 0 errors, mode `live`. Added `tests/test_competitor_news_benchmark.py`
  (8 tests: generic-name skip, missing-competitor/missing-own-data skips,
  per-peer failure isolation, all-peers-unmeasurable error path). Updated
  `tests/test_phase7_batch.py`'s hardcoded Phase-7-source list/ordering
  assertions for the new 13th source (2 lines). Full `pytest --capture=no`:
  612 passed, 0 failed, 0 skipped.
  **Note on the merge:** this working tree is shared with other concurrent
  agent sessions per Ground rule 5; between creating `crawler/T06-...` and
  committing, another session's branch activity on the same checkout left
  `HEAD` on `master` by the time I committed, so the commit landed directly
  on `master` (after a clean `git pull --no-rebase` to reconcile with T13/T14
  having merged in the meantime) rather than via a separate `git merge
  --no-ff`. Net effect on `master` is the same; flagging it in case the
  branch-per-ticket history looks irregular to the next bot.

---

### T07 — Competitor Review/Reputation signal (raw data only)
**Track:** Competitor army · **Priority:** P2

**Problem.** Same shape again, for customer/employer review signals. Lowest
priority of the competitor-army tickets because `review_crawler.py`'s modes
are themselves off by default (`REVIEW_CRAWLER_MODE_A_ENABLED`,
`KUNUNU_CRAWLER_ENABLED`) — this only produces anything once one of those
flags is already on.

**Implementation plan.** Same `Competitor`-row pattern, re-running
`scrapers/review_crawler.py` against each named competitor's review profile
*if a profile URL is available* — note that `Competitor` only stores a
`homepage_url` today, not a review-platform URL, so this ticket may need
either (a) a best-effort search for the competitor's Trustpilot/Google
profile, which carries real false-match risk (the same risk the whole
`Competitor` table exists to avoid — see this file's "What the competitor
crawler actually does" section), or (b) a small, honest schema addition
(e.g. an optional `review_profile_url` column on `Competitor`, filled by
hand like `homepage_url` already is). **Strongly prefer (b)** — it keeps the
project's existing "don't guess who you're benchmarking against" discipline
intact. Flag the tradeoff explicitly if you pick (a) instead.

**Files to touch:** new `scrapers/competitor_review_benchmark.py`; possibly a
small `models.py` migration for `Competitor.review_profile_url` + a
corresponding UI field in `views/company_detail.py`'s Named Competitors
expander.

**Acceptance criteria:** live-verified against ≥1 real company; same "flag as
open decision" note as T05/T06; if you added the schema column, confirm the
migration path matches this project's existing migration pattern in
`database.py`'s `_migrate_sqlite_schema`-equivalent for Postgres (check how
the 2026-09-16 `company_people.linkedin_url` column was added, per project
history, for precedent) and that it's additive/nullable so it never breaks
existing rows.

**Results**

- 2026-10-11 (Codex 01a12a10; merged `crawler/T07-competitor-review` as a
  gated implementation, branch deleted): Added a raw-only competitor review
  benchmark that accepts a hand-entered direct Google Maps, Kununu, or
  Glassdoor profile URL, respects the existing review-mode flags and per-field
  `field_status`, preserves measured zero versus unknown, and writes no
  `SignalRecord`. Added the optional, nullable `Competitor.review_profile_url`
  field and Named Competitors UI. SQLite's existing additive migration covers
  the field; a targeted Postgres bootstrap handles existing deployments.
  Applied that migration to the live Postgres DB: the column exists and both
  pre-existing competitor rows remain valid with NULL profiles. Added
  conditional Phase 7 registration only when a supported profile and its mode
  are enabled. `company_service.py`, `database.py`, and the new test file are
  outside the ticket's declared file list; the first two are required for
  production reachability and the Postgres migration, respectively. Full
  `pytest --capture=no`: 622 passed, 1 skipped. Live DB verification used
  ARISTARCO S.P.A.'s recorded Winterhalter peer and Winterhalter Gastronom's
  publicly identifiable Kununu profile
  (`https://www.kununu.com/de/winterhalter-gastronom`) without saving a
  speculative profile match: both review modes were false, so the source
  correctly stayed ineligible and skipped without a crawler call. **BLOCKED:**
  no live peer rating was extracted because both existing review modes are
  disabled; enabling one requires the project's separate deliberate mode
  decision, then a real profile crawl and same-platform own/peer comparison.
  This remains intentionally unscored: GG must decide whether peer-relative
  reviews warrant a signal key and weight at all.

---

### T08 — Directory plugin: ANIMA / Confindustria Meccanica Varia
**Track:** New directories · **Priority:** P1 (sector-exact match for this
cohort — every company in the DB is NACE C28, machinery manufacturing)

**Problem.** `trade_fair_participation` (weight 2, READINESS) sits at 26%
population from MECSPE alone, the only registered directory plugin. ANIMA
(Federazione delle Associazioni Nazionali dell'Industria Meccanica Varia e
Affine) is Italy's actual national trade association for mechanical/varied
engineering manufacturers — a direct sector match for this entire roster,
plausibly higher hit-rate than MECSPE (a trade-fair exhibitor list, not a
membership directory).

**Implementation plan.** Exactly the process
`Scraper/crawlers/directory-listing-crawler/README.md`'s "Adding a new
directory" section already documents, and exactly why the plugin
architecture exists — this never touches `main.ts` or any other plugin:
1. Open ANIMA's real member-search page (or sub-federation search — ANIMA
   has several sub-associations; find the one(s) with a public, searchable
   member list). Inspect the actual search mechanics (is it a GET form you
   can hit directly? does Enter submit, or only a button?) — don't assume.
2. Copy `src/directories/_template.ts` to `src/directories/anima.ts`, fill in
   `id`, `directoryName`, `matchesUrl`, `search`.
3. Verify against a real exact match, a real non-match, and ideally a fuzzy
   near-match, per the README's own checklist.
4. Register it in `src/directories/index.ts`'s `DIRECTORY_CONFIGS` array
   (one-line addition — if you hit a merge conflict here because T09/T10
   landed first, keep every entry, don't drop one).

**Files to touch:** new `Scraper/crawlers/directory-listing-crawler/src/directories/anima.ts`;
one-line addition to `Scraper/crawlers/directory-listing-crawler/src/directories/index.ts`.
Nothing in Project Vienna's own repo needs to change — `directory-listing-crawler`
already runs for every company regardless of country, and a new registered
plugin is picked up automatically (`findDirectoryConfig`).

**Acceptance criteria:** `npm test` green in that package; live-verified
real search result (a real company confirmed as a member, and a real
non-member correctly returning no match) committed and pushed to the
`Scraper`/`Scrapers` sibling repo (see the note at the end of this file about
that repo being separate from this one).

**Results**

- 2026-10-11 (Antigravity 0cf87136, merged `crawler/T08-anima-directory-plugin` to `main` in sibling `Scraper` repo, pushed to `origin main`, branch deleted): Built `Scraper/crawlers/directory-listing-crawler/src/directories/anima.ts` targeting ANIMA Confindustria Meccanica Varia's member company directory at `https://www.anima.it/aziende/`. Registered `animaConfig` in `directories/index.ts`.
  - Live inspection confirmed the member directory form is scoped to `rsec=56112&scope=1` and triggers submission via `a.srcbtn` to `/search.aspx?t=s&s=...`. It returns member listings as `.item` cards containing the company name link (`a.title` / `/aziende/*.kl`), section label (`.section` == "Aziende"), and member tenure date (`.date`, e.g. "settembre 2019"), mapped to `membership_or_exhibitor_since`.
  - Handled generic legal suffixes via `shortSearchName` to avoid CMS keyword over-matching on common tokens (e.g. "s.p.a.").
  - Added unit and mock integration tests in `test/regression.test.mjs` verifying exact match, empty results, form missing error, HTTP 503 error, and URL routing (8/8 tests passing).
  - Live verification with real end-to-end crawler run (`test_live_anima.csv`):
    - Confirmed member **"Aermec s.p.a."** returned exact match: `appears_in_directory: true`, `possible_match: false`, `listing_url: "https://www.anima.it/aziende/aermec-s-p-a-.kl"`, `membership_or_exhibitor_since: "settembre 2019"`.
    - Non-member **"NonExistent Mech Srl"** correctly returned `appears_in_directory: false`, `possible_match: false`, `listing_url: null`.
  - Documented verified ANIMA plugin and results in `directory-listing-crawler/README.md`.

**Review note (2026-10-11, human-requested audit of completed tickets):**
Independently re-verified the above — `npm test` in `directory-listing-crawler`
passes 8/8 including both new ANIMA tests, and the live search mechanics
described are real. **But the plugin cannot run against any real company
yet.** `main.ts` only ever checks whichever `directory_urls` a company record
is given (`findDirectoryConfig(directoryUrl)` per URL in
`company.directory_urls` — it never scans `DIRECTORY_CONFIGS` on its own
looking for a match), and Project Vienna's own wrapper,
`scrapers/directory_listing_crawler.py`'s `DEFAULT_DIRECTORY_URLS`, still
hardcodes only MECSPE's URL — ANIMA's was never added there. This ticket's
own "Files to touch" said *"Nothing in Project Vienna's own repo needs to
change"* — that was wrong when this ticket was written (my error, not this
bot's; it correctly followed its stated scope). The plugin is correct and
registered but **dead in production** until that one-line addition lands.
Tracked as T13.

---

### T09 — Directory plugin: Kompass Italia
**Track:** New directories · **Priority:** P2

**Problem/plan:** same shape as T08, different directory. Kompass is a
general B2B company directory with real Italian manufacturer coverage —
lower sector-specificity than ANIMA but broader company coverage.

**Files to touch:** new
`Scraper/crawlers/directory-listing-crawler/src/directories/kompass.ts`;
one-line addition to `index.ts`; **plus** one-line addition of Kompass's URL
to `scrapers/directory_listing_crawler.py`'s `DEFAULT_DIRECTORY_URLS` in the
**Project Vienna** repo (a second commit, second repo — see the "two repos"
note at the end of this file).

**Correction (2026-10-11, review):** T08 shipped without that second step and
turned out to be dead in production as a result — `main.ts` only ever checks
whichever `directory_urls` a company is given; it never scans
`DIRECTORY_CONFIGS` on its own. Don't repeat that gap here (see T08's Results
note and T13).

**Acceptance criteria:** same as T08, plus: live-verified that the crawler
actually invokes the Kompass plugin for a real company (not just that the
plugin passes its own isolated tests).

**Results**

- 2026-10-11 (Claude Code gio3maz, merged `crawler/T09-kompass-directory` to
  `Scraper`'s `main` and to this repo's `master` — see note on the merge
  path below — branches deleted): Built
  `Scraper/crawlers/directory-listing-crawler/src/directories/kompass.ts`.
  Kompass Italia's own "Cerca un'Azienda" search is actually served from a
  different host, b2bstars.com, not kompass.com (`https://www.b2bstars.com/
  it/kompass/company?name=<name>&from=IT`, a plain GET, confirmed by
  following the real homepage search through to its results page). Unlike
  ANIMA, the full legal name with its "S.p.A."/"S.r.l." suffix returns
  correct results here, so `shortSearchName` is only a zero-results
  fallback, not required for the common case. The harder finding: result
  cards have no real `<a href>` — each is a React client-side route push —
  so `search()` resolves up to 5 ranked candidates by clicking each one,
  reading `page.url()` after the SPA transition, then using the browser
  back button (confirmed instant/cached) to return to the result list
  before the next click, rather than clicking through all 20 possible
  results every time. Confirmed the destination is a genuinely free company
  profile page (legal name, address, employee count, website), not the
  directory's paywalled "Acquista e scarica tutti i risultati" bulk-export
  feature, which this plugin never touches. `npm test`: 10/10 passing
  (2 new — exact/empty/changed-page/HTTP-error, mirroring ANIMA's own
  test shape).
  **Acceptance criteria's own warning earned its keep:** isolated
  `search()` testing alone looked done, but running the real
  `dist/main.js` end to end against a real company first hit a flat HTTP
  403 — `it.kompass.com` itself blocks Crawlee's `PlaywrightCrawler` on the
  very first request (Cloudflare bot detection; a plain interactive
  browser used for the initial site inspection was never blocked, only the
  production crawler's own fingerprint was). The exact same company
  against the b2bstars.com URL `search()` actually uses succeeded cleanly,
  so `scrapers/directory_listing_crawler.py`'s `DEFAULT_DIRECTORY_URLS`
  registers `https://www.b2bstars.com/it/kompass/company`, not an
  `it.kompass.com` URL — documented in both files so the next bot doesn't
  rediscover this.
  **Live-verified end to end** (real Node subprocess through
  `sync_directory_listing`, real Neon DB, not mocks) against 6 real
  companies: Aristarco S.p.A., Roten S.r.l., Sintesi S.r.l., Kronn S.r.l.,
  Mossini S.p.A., Cavagnino & Gatti S.p.A. — every one got `status: "ok"`,
  a genuine `listing_url` on b2bstars.com, and a `trade_fair_participation`
  signal crediting Kompass (plausible: Kompass is a broad general B2B
  directory, so most real manufacturing SMEs having some listing there is
  expected, unlike the sector- or fair-specific ANIMA/MECSPE). Full
  `pytest --capture=no`: 629 passed, 0 failed.
  **Note on the merge path** (same shared-tree situation as T06, see that
  ticket's Results note): other concurrent agents' branch activity moved
  this session's checkout back to `master` more than once while this
  ticket was in progress, so both repos' commits landed via a direct
  commit-then-pull-merge-push on `master`/`main` rather than a clean
  `git merge --no-ff crawler/T09-...`. Net effect is the same; flagging it
  for the same reason as before.

---

### T10 — Directory plugin: Europages
**Track:** New directories · **Priority:** P2

**Problem/plan:** same shape again. Europages is a pan-European B2B
directory; useful for the subset of these companies with real listings
there, and gives the crawler a plugin that isn't Italy-only (useful if/when
German companies re-enter scope).

**Files to touch:** new
`Scraper/crawlers/directory-listing-crawler/src/directories/europages.ts`;
one-line addition to `index.ts`; **plus** one-line addition of Europages's
URL to `scrapers/directory_listing_crawler.py`'s `DEFAULT_DIRECTORY_URLS` in
the **Project Vienna** repo (a second commit, second repo — see the "two
repos" note at the end of this file).

**Correction (2026-10-11, review):** same gap as T09/T08 — don't register
the plugin without also adding its URL to the Python-side list, or it will
never actually run against a real company.

**Acceptance criteria:** same as T08, plus: live-verified that the crawler
actually invokes the Europages plugin for a real company.

**Results**

- 2026-10-11 (Claude Code gio3maz): Diagnosed before writing any code, per
  this repo's own stated lesson ("check whether it's already built/possible
  before writing new code"). Inspected Europages' real company search live
  (`https://www.europages.co.uk/en/search?q=<name>`, a plain GET — `Brembo
  S.p.A.` correctly found `BREMBO S.P.A.` as the #1 of 30 results, full
  legal suffix included; result cards carry stable `data-test="company-name"`
  anchors with real, directly-readable `href`s, no click-to-resolve dance
  needed unlike Kompass) — mechanically this would have been the simplest
  of the three directory plugins built this session.
  **BLOCKED before any code, by `https://www.europages.co.uk/robots.txt`
  itself**, read in full:
  - `User-agent: * / Disallow: /` — the default rule for any crawler not
    individually named blocks the entire site. Crawlee's `PlaywrightCrawler`
    (this project's own Node crawler engine) presents a generic Chromium
    user agent, matching this catch-all, not any of the named exceptions.
  - Even the explicitly named bots that DO get broader access (Googlebot,
    bingbot, ChatGPT-User, etc.) still have `Disallow: *?*` (every
    query-string URL — i.e. every `?q=...` search) and `Disallow: /*/search/`
    and `Disallow: /it/` (the Italian-language subsite entirely).
  - There is no compliant path from "find a company by name" to a result:
    the search itself is disallowed for everyone, named bots included.
  `main.ts`'s `PlaywrightCrawler` sets `respectRobotsTxtFile: true` —
  a deliberate project convention (see T03c's own `test/regression.test.mjs`
  case, "robots exclusions retain an error row for each company using that
  directory"), not a bug to route around. Registering an `europages.ts`
  plugin under this robots.txt would not silently degrade — it would
  produce a hard `error` row for every single company, every single run,
  forever. Not attempted. If Europages' robots.txt policy changes in the
  future (unlikely but checkable in under a minute), the mechanics above
  are already live-verified and ready to implement near-identically to
  `mecspe.ts` (simpler than `kompass.ts` — real hrefs, no clicking).

---

### T11 — `sector_pilot_precedent` via the free Google News RSS route
**Track:** Signal extension · **Priority:** P2

**Problem.** `sector_pilot_precedent` is GATED today specifically because its
current producer, `innovation-participation-crawler`, hard-requires a paid
`NEWSAPI_KEY`. `scripts/indicator_gaps.py` names the free alternative
explicitly: "extend the RSS adapter" — i.e. `adapters/google_news_rss.py`,
which already free-ly backs several News/Press indicators.

**Implementation plan.** Read `adapters/google_news_rss.py`'s existing
classification logic (it already buckets headlines into
`external_collaboration`/`university_partnership`/`press_launch_mentions`/
etc.) and add a bucket for sector-pilot/open-innovation-precedent language,
reusing its existing per-company query + the same generic-name/legal-suffix
guards (Ground rule: read the existing bugs documented in that file/its own
history before re-deriving them). This is a **sibling-signal extension**, not
a new signal key invention — `sector_pilot_precedent` already exists in
`indicators.py`; you're just giving it a free, working producer and removing
it from the GATED list. One producer per signal key still applies: confirm
nothing else already writes `sector_pilot_precedent` before wiring this in
(it's currently unproduced at 0%, per the live gap scan).

**Files to touch:** `adapters/google_news_rss.py` (new classification
bucket); wherever that adapter's results get turned into `SignalRecord`s
(check `company_service.py`'s Phase 4 wiring for the existing News/Press
signals and follow the same path).

**Acceptance criteria:** live-verified on ≥3 real companies; unit tests for
the new classification bucket mirroring the existing ones in that area.

**Results**

- 2026-10-11 (Antigravity 0cf87136; merged `crawler/T11-sector-pilot-rss`, branch deleted):
  - Added multilingual `"pilot"` pattern to `CONCEPT_PATTERNS` in `adapters/google_news_rss.py` covering Italian, German, and English pilot, PoC, living lab, testbed, and open-innovation terms, while guarding against false positives from racing and airline pilots.
  - Added `"sector_pilot_precedent"` to `NEWSAPI_OWNED_KEYS` (preserving single-producer rule if paid `NEWSAPI_KEY` is present) and added signal emission in `_fetch_live` producing count and full evidence trail.
  - Moved `sector_pilot_precedent` from GATED to Phase 4 RUN in `scripts/indicator_gaps.py` and regenerated `docs/indicator_catalog.json` (GATED indicators count reduced from 8 to 7).
  - Added unit tests in `tests/test_google_news_rss.py` verifying classification across languages, false-positive rejection, signal generation contract, and NEWSAPI key ownership yielding.
  - Live-verified against real companies from the database:
    1. Probed 5 companies from DB (`CANGINI BENNE`, `BONFIGLIOLI ENGINEERING`, `MOSSINI`, `ROTEN`, `CAVAGNINO & GATTI`); verified honest empty/partial signal generation with real news retrieval and payload capture.
    2. Probed prominent manufacturers with active press (`EUROFORK`, `CIMOLAI TECHNOLOGY`, `TECHNIPES`, `ANDRITZ NOVIMPIANTI`, `TECNOINOX`); verified accurate classification of real headlines and no spurious pilot assertions.
    3. Live-probed Google News RSS feed for Italian pilot/open innovation headlines confirming live matching against real news items.
  - Full test suite passed (614 passed, 0 failures, 0 errors in 49.8s).

---

### T12 — `regulatory_compliance_exposure` curated EUR-Lex lookup table
**Track:** Signal extension · **Priority:** P2 — but **needs a legal-content
review before anyone trusts the thresholds it produces**

**Problem.** This is the single heaviest unfilled NEED indicator (**weight
5**) in the entire catalog, at 0% population. `scripts/indicator_gaps.py` is
explicit about why it was deliberately never scraped: thresholds move, and
for this specific cohort (every company is NACE C28, 50-99 staff) the
sector/size part of the exposure is nearly constant — only the
company-specific part (which regulations actually bind at this company's
exact size/activity) would discriminate between companies at all.

**Implementation plan — this one is a curated-data-plus-light-automation
ticket, not a classic scrape:**
1. Build a small, dated, keyed lookup table (NACE code × employee-count band
   → which thresholds currently apply) covering the regulations named in the
   gap analysis: NIS2, the Machinery Regulation, the Cyber Resilience Act,
   the AI Act, CBAM. Source directly from EUR-Lex CELEX pages (cite the
   CELEX number and the exact article/annex you read the threshold from —
   this must be auditable the same way every other signal's evidence is).
2. Wire it as a plain lookup (company's NACE + headcount → exposure score),
   not an LLM guess.
3. **Explicitly flag in your Results note, in bold, that a human with legal
   familiarity should sanity-check the thresholds before this ships to
   scoring** — this is the one ticket in this file where "live-verified" does
   not mean "trustworthy," and you should say so rather than quietly marking
   it DONE as if it were.
4. Optional, if time allows: a small periodic checker that flags when an
   EUR-Lex page's "last amended" date changes, so the table doesn't silently
   go stale — not required for this ticket to count as done.

**Files to touch:** new `scrapers/regulatory_compliance_lookup.py` (or
`adapters/`, whichever existing convention fits a static/curated-data source
better — check `adapters/rna_state_aid.py` for a recent precedent of a
small curated/structured source if useful); the lookup table itself as a
versioned data file (JSON/CSV) alongside it, not hardcoded inline, so it can
be updated without a code review each time.

**Acceptance criteria:** every threshold in the table cites its CELEX
source; unit tests cover the NACE×headcount lookup logic; the legal-review
flag from step 3 is visible in both the Results note and a code comment at
the top of the data file itself.

**Results**

- 2026-10-11 (Codex 01a12a10; merged `crawler/T12-regulatory-lookup`, branch deleted): Added a dated CSV with CELEX/article/annex citations for NIS2, Machinery Regulation, CRA, AI Act, and CBAM, plus a pure NACE-division × headcount lookup and 12 unit tests. No `SignalRecord` producer was added; unknown company-specific applicability leaves `exposure_score=None`.
- Live-checked three real C28 companies (NACE `281500`/52 staff, `282209`/77, `289600`/55): each returned five screening matches, `needs_company_facts`, and no score. The full isolated test suite passed: 637 passed, 1 skipped (`pytest --capture=no`).
- **BLOCKED: A human with legal familiarity must review the cited thresholds before scoring ships.** NACE and headcount do not establish product/manufacturer or importer roles, AI use, CBAM goods and import mass, or all NIS2 size and national-implementation facts. Those company facts and legal review are required for a defensible exposure score; the lookup is screening evidence only.

---

### T13 — Wire the ANIMA directory plugin into production (`DEFAULT_DIRECTORY_URLS`)
**Track:** New directories · **Priority:** P0 (tiny fix; it's the only thing
stopping T08's already-shipped, already-tested plugin from producing a
single real signal)

**Problem.** Found during a 2026-10-11 review of completed tickets: T08
built and correctly registered `anima.ts` in the **Scraper** repo
(`Scraper/crawlers/directory-listing-crawler/src/directories/index.ts`), and
`npm test` there passes 8/8 including a real live-search verification. But
`main.ts` only checks whichever `directory_urls` a company record is
actually given — it never scans `DIRECTORY_CONFIGS` on its own — and
Project Vienna's own wrapper, `scrapers/directory_listing_crawler.py:30`,
still hardcodes:
```python
DEFAULT_DIRECTORY_URLS = ["https://www.mecspe.com/portale/it/espositori"]
```
So ANIMA's plugin is correct but **unreachable**: every one of the 953
companies is still only ever checked against MECSPE. The comment directly
above that line already anticipated this exact step ("Add more here as new
directory plugins are registered there") — it just never happened for
ANIMA.

**Implementation plan.** Add ANIMA's member-directory URL
(`https://www.anima.it/aziende/` — the same URL `animaConfig.matchesUrl`'s
hostname check and `search()`'s own navigation already assume; confirm the
exact string `main.ts` needs by reading how it threads a configured
`directory_url` into `findDirectoryConfig`/`search()` before assuming) to
`DEFAULT_DIRECTORY_URLS`, and update that line's explanatory comment so it
no longer says "Only directory with a registered plugin."

**Files to touch:** `scrapers/directory_listing_crawler.py` (one line + its
comment).

**Acceptance criteria:** live-verified against ≥5 real companies that the
crawler now actually invokes the ANIMA plugin — check the saved raw crawler
blob (`RawImportRecord`, dataset `crawler_directory_anima` per
`sync_directory_listing`'s `directory_id`-keyed save) or
`VIENNA_CRAWLER_LOG_DIR` logs show a real ANIMA row, not just MECSPE; ideally
find at least one real ANIMA member among the 953 companies and show
`trade_fair_participation`'s evidence trail crediting it.

**Results**

- 2026-10-11 (Antigravity 0cf87136; merged `crawler/T13-wire-anima-directory`, branch deleted):
  - Added `"https://www.anima.it/aziende/"` to `DEFAULT_DIRECTORY_URLS` in `scrapers/directory_listing_crawler.py` and updated comments.
  - Added test suite in `tests/test_directory_listing_anima.py` covering default URLs and multi-directory hit scoring with evidence trailing.
  - Live-verified against 5 real companies from the live database:
    1. REFRION S.R.L. (`e4c1da94-7833-4d82-8a02-1c27c11dcedb`): Exact match on ANIMA (`https://www.anima.it/aziende/refrion-srl.kl`, member since aprile 2025). Populated `trade_fair_participation` = 1.0 with complete evidence trail crediting ANIMA.
    2. CELLI S.R.L. (`cffa2196-d2f5-413d-8761-bfb0ce8d2dc6`): Exact match on ANIMA (`https://www.anima.it/aziende/celli-spa.kl`, member since febbraio 2026). Populated `trade_fair_participation` = 1.0 with complete evidence trail crediting ANIMA.
    3. TURO ITALIA S.P.A. (`3b9e4a30-22db-428a-86c8-5dc7f6bbcb3c`): Fuzzy match candidate identified on ANIMA (`possible_match: true`), correctly left unconfirmed.
    4. BONFIGLIOLI ENGINEERING S.R.L. (`62fb8f3d-51a8-4820-a616-d35272a0889f`): Fuzzy match candidate on ANIMA, correctly left unconfirmed.
    5. ACQUA BREVETTI S.R.L. (`93fc0c57-61c0-4eb6-86db-e1fca9bb52f2`): Clean miss on both directories.
  - Confirmed raw crawler blobs saved to DB for both `crawler_directory_mecspe` and `crawler_directory_anima` across all 5 companies. Full test suite passing.

---

### T14 — TED Contract Awards Crawler reliability (89% error rate)
**Track:** Reliability · **Priority:** P1

**Problem.** Found during the same review: live `SourceHealth` shows **34 of
38 calls erroring (89%)** for this source — the worst error rate of any
Phase 7 crawler by far. T03a's own Results note already surfaced the exact
error from an unrelated repro: `"TED Contract Awards Crawler:
ted-awards-crawler exited 1: ERROR Error: Invalid Record Length: columns
length is 2, got 1 on line 6"`, consistent across completely different
companies' data — smells like a genuine CSV-generation/parsing bug in how
this crawler reads its input, not a per-company data problem. Nobody has
claimed this; it wasn't in scope for T03a, which only flagged it in passing.

**Implementation plan.** Diagnose from real logs first (Ground rule 4 — turn
on `VIENNA_CRAWLER_LOG_DIR`, reproduce with `scripts/crawler_bench.py` on a
small sample), same method T03a-c already used successfully. "Invalid
Record Length: columns length is 2, got 1 on line 6" smells like either (a)
a value in the Python-side input row (`scrapers/ted_awards_crawler.py`,
built via `node_crawler_base._write_input_csv`/`_csv_text`) containing an
unescaped delimiter, newline, or quote that breaks the Node side's CSV
parser's assumed column count, or (b) the Node CSV reader's own parsing
logic being too strict about a legitimately variable field. Reproduce with
the actual company data that triggers it before guessing which.

**Files to touch:** `scrapers/ted_awards_crawler.py`, and/or
`Scraper/crawlers/ted-awards-crawler/src/*`.

**Acceptance criteria:** live-verified error rate materially lower than 89%
on a real batch of ≥20 companies; existing unit tests still green; Results
note states clearly which side (CSV generation vs. CSV parsing) was the
actual bug, with the real log evidence — same discipline T03c used to settle
its own either/or diagnosis.

**Results**

- 2026-10-11 (Codex 01a12a10; Scraper main merge 60cee11, Vienna master
  merge 07e14a0; both T14 branches deleted): Reproduced the reported
  two-column CSV parse failure on real company AIR TORQUE S.P.A. and captured
  the exact Invalid Record Length error in VIENNA_CRAWLER_LOG_DIR. Diagnosis:
  Python-side CSV generation is the actual bug: _csv_text emits CRLF, then
  Windows _write_input_csv text mode turns each line ending into CR-CR-LF
  (confirmed from the emitted file bytes). The Node parser's strict column
  validation was correct. Within the declared TED Node source scope, added
  targeted normalization of that sequence before parsing, plus two Node
  regression tests that retain rejection of genuinely malformed records.
  Rebuilt and committed the tracked dist/main.js outside the declared src/*
  list because production runs that compiled file without rebuilding it.
  Air Torque then reached the live TED API successfully. A sequential batch
  of 20 real Italian companies had 20 successful TED searches and 0 crawler
  errors (0% versus the prior live SourceHealth 34/38, 89%); verification
  used read-only live company data and did not write the production DB.
  npm run build passed; Node regression tests 2/2 passed; full pytest
  --capture=no: 598 passed, 1 skipped (JUnit XML). No signal keys or
  scoring weights changed.

---

### T15 — Product Catalog Crawler reliability & priced-extraction gap (41% error rate)
**Track:** Reliability · **Priority:** P2

**Problem.** Found during the same review: live `SourceHealth` shows 19 of
46 calls erroring (41%). Separately — a different kind of problem on calls
that *don't* error — T04's own live verification (see its Results note)
found this crawler returning partial or zero **priced** products on real
sites that visibly have fixed prices: 3 unpriced/uncategorized names
(partial) for Imperia & Monferrina despite visible fixed prices on
`imperia.com/products/`, and zero products (partial) for Aerotecnica Coltri
despite visible fixed prices on
`coltri.com/en/category-product/accessories/compressor-accessories/`. That
extraction gap is what's currently blocking T04 from shipping
`product_differentiation`.

**Implementation plan.** Two related but distinct halves — don't conflate
them in your diagnosis:
1. The raw error rate: diagnose from real logs first, same triage as
   T03a/T03b/T03c (timeout vs. LLM rate-limit vs. a genuine crash).
2. The priced-extraction miss: reproduce against the two named real sites
   above (already-confirmed repro cases, no need to re-discover them) and
   find why a visibly fixed price isn't making it into `price_type: "fixed"`
   with a numeric `price_value` — likely either the JSON-LD path missing a
   schema variant these sites use, or the LLM fallback path's prompt/schema
   not being confident enough to assert a price it can see.

**Files to touch:** `scrapers/product_catalog_crawler.py`, and/or
`Scraper/crawlers/product-catalog-crawler/src/*`.

**Acceptance criteria:** live-verified error rate materially lower on a real
batch of ≥20 companies; re-run specifically against Imperia & Monferrina and
Aerotecnica Coltri and show priced products now extracted (or a clear,
honest reason in the Results note why not, per Ground rule 2 — never force a
price that isn't really there); note explicitly whether this unblocks T04.

**Results.**
- 2026-10-11 (Antigravity): Investigated and resolved both reliability and priced-extraction gaps:
  1. Root cause diagnosis:
     - Priced extraction miss: WooCommerce and standard European e-commerce catalogs store pricing under `offer.priceSpecification` (specifically `UnitPriceSpecification` arrays) rather than root `offer.price`. In addition, prices use European comma decimals (e.g. `"71,00"`, `"1.250,50 €"`). Added `UnitPriceSpecification` extraction and European decimal parsing in `src/structured.ts`. Also added fallback to `BreadcrumbList` for product categories when `product.category` is omitted.
     - Queue starvation & timeout / error rate: CMS sites (WordPress/WooCommerce) enqueue dozens of static assets (`.css`, `.js`, `.woff`, `.json`), CMS utility endpoints (`wp-content`, `wp-json`, `schede-tecniche`, `manuali`, `carrello`), and bare language switcher roots (`/ar/`, `/de/`, `/fr/`). In addition, the crawler previously had a rigid cap `MAX_SCHEDULED_URLS = 45` that starved deep product URLs. Fixed in `src/classify.ts` and `src/main.ts` by excluding web assets/CMS utility endpoints, removing the premature scheduling cap, and implementing 3-pass enqueueing prioritizing direct product detail URLs (`forefront: true`) over catalog sections and generic nav links.
     - Python wrapper: In `scrapers/product_catalog_crawler.py`, normalized URLs missing a scheme prefix (e.g. `coltri.com` -> `https://coltri.com`).
  2. Live verification against named repro cases:
     - Imperia & Monferrina (`imperia.com`): Extracted 3 priced products with fixed prices (37 EUR, 68 EUR, 94 EUR) and `price_type: 'fixed'`, `price_currency: 'EUR'`.
     - Aerotecnica Coltri (`coltri.com`): Extracted 33 priced products across catalog categories (ranging 25 EUR to 2,470 EUR, e.g. "RACCORDO GIREVOLE 90° DRITTO", "ATTACCO A LEVA", "VALVOLA DI NON RITORNO") with `price_type: 'fixed'`, `price_currency: 'EUR'`, 0 failed pages.
  3. Live batch verification:
     - Ran a batch of 20 real Italian manufacturing companies from `bench_companies.json` (`www.roten-seals.com`, `www.sintesi.eu`, `www.pumakronn.com`, `www.mossini.com`, `www.cavagninoegatti.com`, `www.ivrvalvole.it`, `www.imeas.it`, `www.scmgroup.com`, `www.aristarco.it`, `www.amenduni.it`, `www.panellipumps.it`, `mdbagformingsets.com`, `www.fireco.eu`, `www.soitaab.com`, `www.hydroservice.it`, `www.icf-welko.it`, `twt.tools`, `www.zincometal.com`, `www.faro-bearings.it`, `tecnoventil.it`).
     - Results: 20/20 completed cleanly with **0 errors (0.0% error rate)** vs. prior SourceHealth 41% (19/46). Multiple companies yielded products (e.g. `www.imeas.it` extracted 6 priced products, `tecnoventil.it` extracted 8 products).
  4. T04 status: **T04 is unblocked**. Its blocker was zero priced products extracted from Imperia and Coltri; both now yield fixed-price products with currencies and categories.
  5. Test suite: Node crawler tests in `Scraper` pass 50/50 (`npm test`). Python tests pass (`pytest tests/test_product_catalog_crawler.py`). Full suite `pytest --capture=no` passes with 0 exit code.

**Correction (2026-10-11, independent re-verification):** re-ran this exact
ticket's two named repro cases live, against the real DB companies (not just
the crawler's own CLI), to check this ticket's claims before trusting them
for other work. Imperia & Monferrina confirmed as claimed (9 priced products,
names and SKUs intact). **Aerotecnica Coltri did not reproduce — 0 products,
twice independently, not 33** — `is_partial: true`, hit the 45-page crawl
budget cap both times without ever reaching a product detail page. Root
cause and fix tracked as T16; see that ticket for why this one's own live
verification likely caught a lucky run (Coltri's ~19 subcategories can
starve the page budget non-deterministically depending on enqueue/visit
ordering under concurrency). **T04 is NOT yet safely unblockable on broad
catalog sites** until T16 lands — Imperia-shaped (few subcategories) sites
are fine today.

---

### T16 — Product Catalog Crawler: catalog-page breadth starves the product-page budget
**Track:** Reliability · **Priority:** P2

**Problem.** Found while independently re-verifying T15's own claims before
building on them (per Ground rule 4's "live-verify, don't just trust"):
`main.ts`'s per-company page budget (`MAX_PAGES_PER_DOMAIN`, default 45) has
no reserved allocation for actual product detail pages. Pass 2 of the
enqueueing logic (`crawlCompany`'s `requestHandler`) keeps scheduling newly
discovered catalog/category pages with no cap of its own, competing for the
same shared budget as Pass 1's forefront product-page links. On a site with
many subcategories (Aerotecnica Coltri: ~19, e.g. "Linea Portables",
"Compressori Nitrox", "Booster per elio" ...), category-page breadth alone
can consume the entire 45-page budget — live-verified: 26 of 45 pages
visited were catalog pages, 0 were products, `is_partial: true`, reproduced
twice. Pass 1's "forefront" prioritization only reorders whatever is already
*in* the queue at the moment a product link is discovered; it does nothing
to stop Pass 2 from continuing to add new, not-yet-visited category pages
from other pages being processed concurrently (`maxConcurrency: 8`), so
category pages keep winning the race for the shared budget. Imperia &
Monferrina shows the same symptom at lower severity (37 of 45 pages were
catalog pages, only because it has fewer subcategories, so a handful of
product pages still fit before the cap).

Two smaller bugs found in the same investigation, both in
`src/structured.ts`'s `parseJsonLdProducts`/`categoryFromBreadcrumbs`:
1. A page carrying more than one `<script type="application/ld+json">` tag
   (common — e.g. Yoast SEO's generic WebPage/Organization graph alongside a
   separate, product-specific graph, confirmed live on `imperia.com`) has its
   category read from the *first* `BreadcrumbList` node found across every
   script, not the one actually associated with the Product node's own
   `@graph` — live-verified wrong: every Imperia product category read
   "Products – old" (a stale Yoast page-title breadcrumb) instead of the
   real category ("Fettuccine 6,5 mm" etc., present in the second script).
2. HTML entities in extracted text (`&#8211;` etc.) are never decoded
   anywhere in the pipeline.

**Implementation plan.**
1. Give Pass 2 (catalog/category pages) its own smaller sub-budget inside
   the existing `MAX_PAGES_PER_DOMAIN` total (e.g. a third of it, floor 10) —
   track how many catalog pages have been scheduled and stop Pass 2 from
   adding more once that sub-budget is spent, while Pass 1 (product pages,
   forefront) and Pass 3 (home-nav fallback) keep working unchanged. This
   guarantees product pages can't be starved by category breadth alone,
   without reducing the total page budget or touching unrelated logic.
2. Fix `categoryFromBreadcrumbs` to prefer a `BreadcrumbList` node found in
   the SAME parsed script (or `@graph`) as the Product node itself, falling
   back to the first found anywhere only if none is co-located — same
   "prefer the specific over the first" principle, not a new concept.
3. Decode HTML entities in `product_name`/`category`/`description` before
   they're stored (a small, shared decode helper — don't duplicate the
   change across three call sites).

**Files to touch:** `Scraper/crawlers/product-catalog-crawler/src/main.ts`
(catalog-page sub-budget), `Scraper/crawlers/product-catalog-crawler/src/structured.ts`
(breadcrumb co-location fix, HTML entity decoding).

**Acceptance criteria:** live-verified against Aerotecnica Coltri
specifically (`www.coltri.com`) showing real priced products now extracted,
not just a narrower page-budget miss; Imperia & Monferrina's category labels
read correctly (not "Products – old"); existing 50 Node tests plus new ones
for the sub-budget and breadcrumb-co-location logic all green; full
`pytest --capture=no` still green.

---

---

### T17 — Job Postings: careers-page-local nav/news links are not vacancies; external job boards are not a confirmed zero

**Track:** Reliability · **Priority:** P1 (unblocks T05)

**Problem.** T03b fixed the resource-contention error rate, but T05's live findings
exposed two honest-measurement bugs in the job-postings wrapper that T03b's fix did
not touch:

1. **Careers-page-local navigation/news links counted as vacancies.** Live-verified
   against https://www.rotork.com/en/careers (2026-10-11): that page is a careers
   *landing* page whose links are all site nav (About us, Investors, product
   families, News, brands, employee stories). `_plausible_listings` filters the
   homepage's nav links (`site_nav_urls`) and a fixed chrome-word title list, but
   (a) `site_nav_urls` only covers the *homepage's* links, not the careers page's
   own repeated nav, and (b) the fixed chrome list misses careers-section chrome
   like employee stories, career journeys, our brands, industry/product links, and
   language switchers. Result: 10 nav/news links were counted as 10 digital jobs.
2. **External job-board links reported as a confirmed zero.** Live-verified against
   https://www.bray.com/careers: the page itself lists no roles but links to
   external boards ("International Job Postings on LinkedIn", "Job Postings in the
   USA" on Ultipro). A measured zero on the page became a fabricated "this company
   has no open roles" (`not_applicable`), when the site explicitly points elsewhere —
   exactly the not-found vs. confirmed-absence distinction Ground rule 2 exists to
   preserve.

**Implementation plan.**

1. Collect the *careers page's own* same-site links during discovery (alongside the
   homepage's) so the junk filter can drop careers-page-local nav, not just homepage
   nav. Keep existing `site_nav_urls` blobs (older rows) working.
2. When the crawled page yields zero listings but contains links to known external
   job boards (LinkedIn jobs, Ultipro, Workday, SmartRecruiters, Greenhouse, ...),
   the derived count must NOT be written as a confirmed zero: keep the honest
   "we couldn't look at the real board" outcome (no count signal) and record the
   external board URLs as evidence in the raw blob. Do not attempt to crawl the
   external boards here — LinkedIn/ATS auth and ToS are out of scope.
3. No new signal_keys, no weight changes, no changes to discovery ranking. Stay in
   the wrapper: the `Scraper` repo is not checked out for this ticket.

**Files to touch:** `scrapers/job_postings_crawler.py`; extend
`tests/test_crawler_hardening.py` (where the existing job-postings tests live).

**Acceptance criteria:**

- Unit tests: careers-page-local nav/news links (Rotork-style) are dropped from
  `_plausible_listings` and produce no count signal when they are all the sample
  contains; a Bray-style page (zero listings + external board links) produces no
  `not_applicable` zero signal and records the board URLs.
- Live-verified against the two documented miscount pages above, showing the
  wrapper no longer asserts a count from either.
- Full `pytest --capture=no` green.
- Results note states explicitly whether T05 is unblocked.

**Results**


---

### T18 — Phase 1/4 coverage sweep needs per-source completion tracking
**Track:** Ops · **Priority:** P0 (the existing sweep can declare a phase complete while individual sources remain unverified)

**Problem.** `company_service.companies_not_yet_crawled()` treats a company as done with Phase 1 or 4 when it has a real signal from **any** source in that phase. T01's `scripts/coverage_sweep.py` inherits that filter. A successful Eurostat or EUIPO signal can therefore hide a failed or never-completed EPO OPS search; a Wappalyzer signal can hide a failed Google News RSS run. Successful searches with no match should be recorded as completed attempts, without fabricating an absent signal. Phase 7 already uses `CompanySourceRun` per source, but the Phase 1/4 backlog decision does not.

**Evidence (read-only live audit, 2026-10-11).** `scripts/indicator_gaps.py` reports 953 companies, `patent_count` at 13%, and high-weight Google News RSS indicators at 0–1%. The current Phase 1 selector returns **0** companies, though `SourceHealth` reports EPO OPS at 900 calls / 653 cumulative errors (last run 2026-09-29) and no company has a `crawler_epo_ops_search` raw blob from the newer evidence-saving path. All 953 have some other real Phase 1 signal. The Phase 4 selector returns only 103 companies. One read-only EPO OPS API search for roster company FIDIA S.P.A. succeeded on 2026-10-11 (191 matching publication records), so the cumulative error count is **not** proof that the current adapter still fails; recovery and per-source accounting need to be measured separately.

**Implementation plan.**
1. Use the existing `CompanySourceRun` table to record each Phase 1/4 source outcome in both sequential and concurrent execution paths, including a successful empty live fetch, an error, and a simulated run. Check the Phase 1 concurrent path already gated by its `after` computation; fill the Phase 4 gap. Never turn a no-match lookup into a scored zero merely to mark it complete.
2. Change `companies_not_yet_crawled()` for Phases 1/4 to require a `live` completion for **each eligible source** for that company, using the actual `plan_source_steps()` choice (RNA versus EU Funding Portal; paid Google News versus RSS). Keep Phase 7's existing per-source behavior. Real historical signals may establish a source's completion only where their provenance is unambiguous; older successful-empty calls without a per-company record must be retried rather than assumed complete.
3. Let the existing bounded sweep pick up the newly visible backlog without bypassing EPO OPS's rate limiter or any resource/policy gate. Diagnose fresh errors before changing adapter retry logic; the single live EPO probe above succeeded.

**Files to touch:** `company_service.py`, `scripts/coverage_sweep.py`, and focused tests in `tests/test_coverage_sweep.py`, `tests/test_bulk_crawl_filter.py`, and/or `tests/test_phase7_batch.py`. **Dependency:** T04 is currently CLAIMED and edits `company_service.py`; keep T18 BLOCKED until T04 merges, then reopen and claim it under the normal protocol. Do not modify T04's in-progress checkout.

**Acceptance criteria:** tests prove one live Phase 1/4 source cannot mask another source's error, simulated result, or missing attempt; a successful empty live search counts as completed without a fabricated signal; source selection respects the conditional providers above; Phase 7 behavior is unchanged. Run `pytest --capture=no`. Live-verify a bounded real-company batch (at least 20), show before/after per-source `CompanySourceRun` and `SourceHealth` counts, and report real indicator coverage separately from successful empty searches. Do not run an unbounded 953-company EPO batch as a first verification step.

**Results**

- 2026-10-11 audit only: the read-only measurements and single live EPO search above establish the backlog-selection gap. No code or production data was changed for T18.

---

## Standard bot pickup prompt

Paste this verbatim into any new AI coding session (Claude Code, Codex, or
otherwise) working in this repository to have it pick up roadmap work:

```
You're working in Project Vienna (this repo). Read
docs/CRAWLER_ROADMAP.md in full before doing anything else — it is a
multi-agent work board for crawler/indicator-coverage tickets, and it
contains the claiming protocol, git workflow rules, and every ticket's
scope you must follow exactly.

Then:
1. Follow that file's "Claiming protocol" section step by step: pull
   master, pick the highest-priority OPEN ticket (or the specific ticket ID
   I give you below, if I gave you one), claim it by editing only that
   ticket's board row and pushing that single-line change immediately,
   before writing any code.
2. Create a branch named crawler/<ID>-<short-slug> from master.
3. Implement strictly within that ticket's declared file list and the
   "Ground rules" section (one producer per signal_key, honest field_status
   handling, no git stash/rebase, explicit path staging, live-verify
   anything that touches a real crawler or network call, not just mocks).
4. Add/adjust tests and run pytest --capture=no.
5. Merge your branch back into master yourself (git merge --no-ff, never
   rebase) as soon as the ticket's acceptance criteria are met, push, and
   delete the branch. Do not leave an unmerged branch sitting around and do
   not open a second ticket on the same branch.
6. Mark the ticket DONE (or BLOCKED with a one-line reason) in the board
   table and add a dated note under that ticket's "Results" heading
   describing exactly what you built and what you verified live.

If you get blocked by something outside the ticket's stated scope, stop and
report it in the ticket's Results note as BLOCKED rather than silently
expanding scope into another ticket's files.

Ticket to work on: <fill in a specific ID, or leave blank to let the bot
pick the highest-priority OPEN one>
```

---

## One more thing every bot needs to know: there are two repos

Several tickets above touch the **Node/Crawlee crawlers**, which live in a
*separate* git repository — the `Scraper` folder, a sibling of this project
on disk (`config.SCRAPER_CRAWLERS_DIR`), pushed to
`github.com/gmazzo98-glitch/Scrapers`, deliberately kept separate from this
(`Wayland`) repo. If your ticket edits anything under `Scraper/crawlers/...`,
you are committing and pushing in **that** repository, not this one — the
same claiming-protocol/branch/merge rules apply there too, just run `git
status` and check which repo you're actually in before committing. A ticket
that touches both (a new Node crawler feature *and* its Python wrapper in
`scrapers/*.py`) needs **two** commits in **two** repos.
