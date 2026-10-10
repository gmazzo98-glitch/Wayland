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
| T03b | Job Postings Crawler reliability (23% error rate) | Reliability | P1 | OPEN | | | |
| T03c | Directory Listing Crawler reliability (25% error rate) | Reliability | P1 | OPEN | | | |
| T04 | Competitor Product/Catalog Benchmark → `product_differentiation` | Competitor army | P1 | OPEN | | | |
| T05 | Competitor Hiring-Velocity signal (raw data) | Competitor army | P2 | OPEN | | | |
| T06 | Competitor News/Press-Share signal (raw data) | Competitor army | P2 | OPEN | | | |
| T07 | Competitor Review/Reputation signal (raw data) | Competitor army | P2 | OPEN | | | |
| T08 | Directory plugin: ANIMA / Confindustria Meccanica Varia | New directories | P1 | OPEN | | | |
| T09 | Directory plugin: Kompass Italia | New directories | P2 | OPEN | | | |
| T10 | Directory plugin: Europages | New directories | P2 | OPEN | | | |
| T11 | `sector_pilot_precedent` via free Google News RSS route | Signal extension | P2 | OPEN | | | |
| T12 | `regulatory_compliance_exposure` curated EUR-Lex lookup table | Signal extension | P2 | OPEN (needs legal review) | | | |

Adding a new ticket yourself (T13+)? Append a row here and a matching
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

---

### T09 — Directory plugin: Kompass Italia
**Track:** New directories · **Priority:** P2

**Problem/plan:** same shape as T08, different directory. Kompass is a
general B2B company directory with real Italian manufacturer coverage —
lower sector-specificity than ANIMA but broader company coverage.

**Files to touch:** new
`Scraper/crawlers/directory-listing-crawler/src/directories/kompass.ts`;
one-line addition to `index.ts`.

**Acceptance criteria:** same as T08.

---

### T10 — Directory plugin: Europages
**Track:** New directories · **Priority:** P2

**Problem/plan:** same shape again. Europages is a pan-European B2B
directory; useful for the subset of these companies with real listings
there, and gives the crawler a plugin that isn't Italy-only (useful if/when
German companies re-enter scope).

**Files to touch:** new
`Scraper/crawlers/directory-listing-crawler/src/directories/europages.ts`;
one-line addition to `index.ts`.

**Acceptance criteria:** same as T08.

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
