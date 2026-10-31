# READ FIRST: agent status and open points

Before doing anything, read this file fully. Before you finish, update it.

## A. Standing rules for every agent

1. Read this file fully before touching code. Update it before ending your session.
2. NEED and READINESS are separate scores. Never blend them.
3. The new engine in `gg_indicators/` stays side by side with the live `scoring.py`. Nothing is wired into the app, database or Target Matrix until a weight set is approved and recorded in the Decisions log.
4. Do not invent thresholds, weights or mappings. Put them in config as `TO_CALIBRATE` or `TO_MAP` and open a point for them.
5. Prefer explicit deferral over approximation. If something is undecided, document it here instead of guessing.
6. The workbook `Indicators.xlsx` is read-only for code. Its text belongs to Giorgio.
7. The VIENNA classifier is deferred. Do not implement it.
8. Verdantex is fully separate from GG. Do not mix them.
9. Items with status NEEDS_DECISION are decided by Giorgio only. An agent may add a recommendation in the notes, never resolve the item.
10. Commit and push completed repository changes by default (Giorgio, 2026-10-28: "always push and commit"). Stage only the task's changes and preserve other agents' uncommitted work; report the commit and push result.

Use `scripts/open_points.py` instead of hand-editing the points (it keeps the format parseable and touches nothing outside the point you name):
`python scripts/open_points.py list | show OP-xxx | checkout OP-xxx --agent NAME | checkin OP-xxx --status STATUS --note TEXT | note OP-xxx --text TEXT | add ... | validate`.
Protocol: git is the lock. Check out = `git pull`, run `checkout`, commit and push ONLY this file as "claim OP-xxx" BEFORE starting the work (if the push is rejected: `git pull --rebase`, re-read, pick another point if someone else now holds it, never force push). Check in = run `checkin`, commit and push as "checkin OP-xxx". Lease is 48 hours; extend it with a note if you run longer. A lease past `lease_until` with no update may be reclaimed (`checkout --reclaim`, which writes the note). Problems found while working become a NEW point (`add`), never a silent fix. Run `validate` before every commit of this file. The separate diagnosis ticket board (`scripts/board.py`, `docs/DIAGNOSIS_TICKETS.md`) is a different system with its own rules; this file does not replace it.

## B. Project status (current state only)

* Latest commit of the indicator work: `d686e86` (2026-10-25), "Add gg_indicators: Indicators.xlsx schema loader, config, and two-axis engine (DREAMY round)". Other agents have pushed on top since; this status file was created on 2026-10-26.
* Exists: package `gg_indicators/` (loader, registry, engine, weights, bridge, quality helper, questionnaire, report), its config in `gg_indicators/settings/`, the data contract `docs/indicator_contract.json` (74 variables), `docs/first_contact_questionnaire.{md,json}`, `scripts/build_indicator_contract.py`, and the full report `docs/GG_INDICATOR_INTEGRATION_REPORT.md` (conflicts, assumptions, every TO_CALIBRATE and TO_MAP item).
* Wired into the app, database or Target Matrix: nothing. The 4 DREAMY rows are not in `INDICATOR_SEED`, the database, `GAP_PLAN` or `docs/indicator_catalog.json`.
* Live engine: `scoring.py`, per-indicator database weights 0-5, gates multiply the axis score down, rank-halving redundancy dampening, no distress cap.
* New engine: `gg_indicators/`, versioned weight sets summing to 1 per axis (only the labelled placeholder "PLACEHOLDER, NOT A DECISION" exists), gates only set `qualification_flag`, four observation states with coverage, distress cap inert until calibrated.
* Mobile layer: `mobile_ui.py` (CSS only, injected from `app.py` before the login gate) makes the app usable at phone width: collapsed sidebar that closes after picking a page, 16px inputs, 44px touch targets, scrollable tabs and tables, two across metrics, non-sticky header bars, compact crawl card. No feature was removed. Follow up: OP-014.
* Detail moved out (2026-10-29): this section is the current state only (`tests/test_agent_status_file.py` caps it at 39 lines). Every dated per-change note that used to sit here (deep-dive crawl routing, Pain point picker, landing page deployments, each deep-intelligence ticket, AIDA report work, PDF work) is in `docs/STATUS_HISTORY.md`, verbatim. Add new dated notes there and keep one line here.
* Landing page `wayland.my` (separate from the Vienna app, folder `landing/`, rules in `AGENTS.md` and `landing/README.md`): LIVE on Vercel project `wayland_landingpage` (team agri-portal), built from GitHub `gmazzo98-glitch/Wayland` root directory `landing` with the Vercel MCP `create_deployment`; the project is NOT git-linked, so a push does not publish (`/update-landing`, or `create_deployment` with the new sha, does). Both forms POST to `/api/submit` (Neon schema `landing`, ntfy notification, required terms consent stored); legal pages `/privacy` and `/terms`. Edit `landing/src/page.html`, run `python landing/tools/pack.py` (`--check` must say "In sync"). Still Giorgio's: the legal placeholders visible on the live pages, the mailboxes info@ and privacy@wayland.my, rotating the `landing_writer` password (it appeared in a transcript), connecting the repo in Vercel if a push should publish.
* Deep-intelligence program (a SEPARATE board: `docs/INTEL_ROADMAP.md`, `docs/INTEL_AGENT_PROMPT.md`, `python scripts/intel_board.py`, code in `intel/`, runner `scripts/intel_run.py`, golden set `docs/intel/golden/`): collectors for site, headers, technology, Italian registry, people, events, networks, ownership, documents and certifications over an evidence ledger and a verification gate. Nothing is wired into scoring, the Target Matrix, the dossier or the live database: the ledger is the blob dataset `crawler_deep_profile` and nothing reads it. The worker bundle was not rebuilt (release ticket I90), which is why `test_committed_bundle_contains_the_current_worker_source` fails (OP-010). Early DONE tickets hard-coded the golden company and were reworked (I93 to I96, I99); the honest figures are in `docs/intel/results/`, never in the superseded history lines. Decisions G1 to G7 are OP-017.
* AIDA single-company report (Data Admin; `aida_single_report.py`, `aida_report_mapping.py`, `views/aida_single_report.py`, `docs/AIDA_SINGLE_COMPANY_REPORT_UPLOAD.md`): every sheet and cell kept as JSON, financial years by actual date, Flexible-Import style connections with reusable profiles, a new indicator for every reporting year of a line, all dates connected by default (Y-1 to Y-10) while trends keep the Latest/Y-1/Y-2 window. Existing production rows were not touched; the audit of the old generic import is OP-018.
* PDF financial statements as a data source (Flexible Data Import; `balance_sheet_pdf.py`, `financial_ledger.py`, `views/balance_sheet_upload.py`, `docs/BALANCE_SHEET_UPLOAD.md`; OP-019 closed): the AI reads the text layer into every printed row and the company details (kept only where printed on the page, numbers parsed from the text, cross-checked by the rule reader for the Bank of Italy and the OIC layouts); lines connect like any import and merge by fiscal year with AIDA into `Merged Annual Financials` (balance sheet wins by default, conflicts are the operator's, newest year = Latest, a missing fact stays missing). Year recognition (2026-10-29): the dates over the figure columns decide which year each number is, in printed order; an unreadable date is asked for and dates the columns before anything imports; the preview shows where each year lands; `tests/test_year_placement.py` checks placement and trends against an oracle for every upload order, gaps, a June year end and restatements; dossier trend charts keep time order. The reader rebuilds page text from word positions (touching digit fragments, separate minus signs, one or two figure columns, a calculated equity when no total is printed) and blocks interim periods unless confirmed (2026-10-30, from a real half-year file). Company Intelligence reads the merged record: Financials (statement, full history with sources, per-statement lines, removal with re-merge), Overview (Identity shows what the statement printed, flags and header read the new year), Data tab, valuation, signals, so Target Matrix, pain points and the assistant follow. Audit and fixes 2026-10-29 (claude): `last_accounts_year` follows the merged year, bulk-AIDA results built on a Latest the history no longer has are offered for withdrawal, headcount syncs when the statement printed one, the growth headline reads the merged record, a bulk AIDA re-import re-merges companies with statement history, OIC layout rows (section headings, `6)` items) map and agree with the AI reading. NOT verified: the hosted app, a scanned PDF (no vision key), a live model on an OIC file, a browser look. Open: OP-020 (no EBITDA from a statement, so valuation says "not valued"), OP-021 (peer cohort ignores PDFs).
* Tests, 2026-10-29: full run 1288 passed, 12 skipped (no pgserver, no Scraper repo, no raw AIDA exports here), 1 known failure: `test_worker_hub.py::test_committed_bundle_contains_the_current_worker_source` (stale worker bundle, OP-010, release ticket I90). `tests/test_app_pages.py` renders every navigation page on a database holding a PDF-built company.
* Company notes (2026-10-29): dossier section "Notes" (`views/company_notes_panel.py`, `company_notes.py`, tables `company_notes` and `company_note_presence` created by `create_all`). One row per note so several people write at once, list polls every 3 s (`st.fragment`), presence, kinds, replies, pin, edit with conflict refusal, soft delete; authors are a self-typed name (one shared app password). A note is team-only until "Send to AI"; then `assistant_llm` (`get_company_notes`, `get_company_profile.analyst_notes`), the company chat and the AI brief (evidence kind `analyst_note`) read it, always labelled "analyst note, not verified data" with author and date. Notes never touch scoring. Not verified: two real browsers, hosted app, Neon tables/RLS. Tests: `tests/test_company_notes.py`.
* Business lines (2026-10-31, OP-025): `statement_segments.py` reads table E.8 (money transfer / acquiring volumes, operations, commissions), agent balances and protection fund from the stored page text of saved PDF statements (no new table), merged by year; `views/business_lines.py` shows it at the top of dossier Financials. Italian wording is a best guess, not seen on a real file. Tests: `tests/test_statement_segments.py`.
* Run tests: `python -m pytest tests --capture=no -q -p no:cacheprovider` (never bare `pytest`; `tests/conftest.py` forces a throwaway SQLite DB).
* Regenerate the contract after editing the workbook or settings: `python scripts/build_indicator_contract.py` (`--check` to verify).
* Bridge comparison (live engine vs new engine on one company; read-only, point `DATABASE_URL` at a throwaway or real DB deliberately):
  ```python
  from database import get_db_session
  from indicators import fetch_indicator_defs
  from models import Company, SignalRecord
  from scoring import build_signal_map, calculate_company_scores
  from gg_indicators import IndicatorRegistry, score_company, format_report
  from gg_indicators.bridge import observations_from_signal_map
  db = get_db_session(); reg = IndicatorRegistry.load(); defs = fetch_indicator_defs(db)
  c = db.query(Company).first(); sigs = db.query(SignalRecord).filter_by(company_id=c.id).all()
  live = calculate_company_scores(sigs, defs)
  new = score_company(reg, observations_from_signal_map(reg, build_signal_map(sigs, defs), defs), company_id=c.id)
  print(live["need_score"], live["readiness_score"]); print(format_report(new))
  ```
  Expect differences by design (open points OP-003, OP-004): the live engine scores a company with no data as 0.0, the new one returns no score and 0% coverage.

## C. Open points

### OP-001: Row 14 (Number of Employees) role mismatch
* status: NEEDS_DECISION
* needs: decision from Giorgio
* owner: none
* checked_out_at: none
* lease_until: none
* blocked_by: none
* context: `docs/GG_INDICATOR_INTEGRATION_REPORT.md` section 1 item 7; `gg_indicators/source/Indicators.xlsx` Scoring Schema row 14; `indicators.py` key `number_of_employees`
* done_when: Giorgio has recorded in the Decisions log whether row 14 is Scored (axis READINESS) or an unscored segment filter, and the sheet or catalog is aligned to it.
* notes:
  - 2026-10-26: seeded. Sheet tag is `[READINESS]` with Role Scored; the live catalog keeps it as axis context, weight 0. Its own Indicator text calls it "primarily a segment-fit filter".

### OP-002: Row 57 (Employer review sentiment) polarity vs its comment
* status: NEEDS_DECISION
* needs: decision from Giorgio
* owner: none
* checked_out_at: none
* lease_until: none
* blocked_by: none
* context: report section 1 item 8; `gg_indicators/settings/status_mappings.json` key `kununu_rating`; workbook row 57
* done_when: Giorgio has decided whether row 57 is Negative polarity, a different Role, or stays as parsed, and the workbook tag (Giorgio edits it) and mapping agree.
* notes:
  - 2026-10-26: seeded. Tag `[READINESS, caveat]` has no NEGATIVE, so it parses as Standard (a Low rating raises READINESS), while the row's Comment says to use it as a downgrade flag. The engine keeps it as parsed. Agents must not edit the workbook.

### OP-003: Gate behaviour, live engine vs new engine
* status: NEEDS_DECISION
* needs: decision from Giorgio
* owner: none
* checked_out_at: none
* lease_until: none
* blocked_by: none
* context: report section 1 item 1; `scoring.py` `_evaluate_axis` (gate_penalty_multiplier); `gg_indicators/engine.py` gates; `gg_indicators/settings/roles.json`
* done_when: Giorgio has recorded whether a failed gate only sets `qualification_flag` (new engine) or also lowers the score (live engine), for rows 67, 69 and 70.
* notes:
  - 2026-10-26: seeded. Live: `digital_lead_role_present` (row 69) multiplies the axis score down; rows 67 and 70 are Gate in the sheet but scored (not gates) in the live catalog. New engine follows the brief: gates never change a score.

### OP-004: Distress cap is uncalibrated
* status: BLOCKED
* needs: external data
* owner: none
* checked_out_at: none
* lease_until: none
* blocked_by: none
* context: report section 1 item 2; `gg_indicators/settings/distress.json`; `gg_indicators/engine.py` `_distress`
* done_when: the two trigger numbers (and the cap value if the action is `readiness_cap`) are set from calibration data and approved by Giorgio, and the distress status is `evaluated` in a test.
* notes:
  - 2026-10-26: seeded. No distress cap exists in the repo (only prose caveats in `indicators.py` and `painpoints.py`), so there was no existing logic to keep. The first version reports candidates and applies nothing until the numbers exist. Needs outcome or peer data; see OP-005.

### OP-005: All TO_CALIBRATE numbers in settings
* status: NEEDS_DECISION
* needs: decision from Giorgio
* owner: none
* checked_out_at: none
* lease_until: none
* blocked_by: none
* context: report section 3 (table); `python -c "from gg_indicators import IndicatorRegistry as R; print(R.load().todo_markers()['TO_CALIBRATE'])"`
* done_when: every item in the notes below is either set in config with Giorgio's decision in the Decisions log, or explicitly deferred in a note.
* notes:
  - 2026-10-26: seeded, per number. Decision from Giorgio: moderator factors (`moderators.json`, 2 rules, rules stay off); `completed_high_maturity` READINESS value (DREAMY level 4-5); `systems_posture.numeric_cutoff`; gate `fail_strength_at_least` (currently 1.0, a numeric default, not a TO_CALIBRATE string); `min_years_held_for_long_held` and `min_standards_for_multi_standard`. External data: `min_peer_prevalence_for_norm` (needs real peer prevalence, e.g. Accredia counts by ATECO); the three distress numbers (OP-004). The `status: TO_CALIBRATE` on the certification table entries is covered by OP-011.

### OP-006: All TO_MAP entries in source_it
* status: OPEN
* needs: agent work
* owner: none
* checked_out_at: none
* lease_until: none
* blocked_by: none
* context: `gg_indicators/settings/source_it.json`; report section 4; `../AIDA Columns.xlsx` (outside the repo, in the Wayland folder) and `aida_import.SERIES_SPEC`
* done_when: every variable with status TO_MAP, and every inline "TO_MAP" source entry, is either mapped to a confirmed field (AIDA header, Registro Imprese, UIBM, Accredia, ...) or explicitly marked as having no Italian source, and `tests/test_gg_indicators.py` still passes.
* notes:
  - 2026-10-26: seeded. 23 rows are TO_MAP and 12 inline entries inside PARTIAL rows need a field or access confirmed. Needs the AIDA field list to start; entries that depend on UIBM or Accredia access cannot be closed by an agent and should be left TO_MAP with a note.

### OP-007: Add the 4 DREAMY rows to INDICATOR_SEED and the live catalog
* status: BLOCKED
* needs: decision from Giorgio
* owner: none
* checked_out_at: none
* lease_until: none
* blocked_by: OP-001, OP-002, OP-003, OP-013
* context: report section 1 item 10; `indicators.py` `INDICATOR_SEED`, `CATALOG_MIGRATIONS`; `scripts/indicator_gaps.py` `GAP_PLAN`; `scripts/generate_indicator_catalog.py`
* done_when: four seed rows (`system_integration`, `maintenance_digitization`, `quality_process_maturity`, `digital_maturity_self_assessment`) exist with GAP_PLAN entries, row 63's catalog text is migrated, the catalog JSON is regenerated, and Giorgio's go for the live database write is in the Decisions log.
* notes:
  - 2026-10-26: seeded. Seeding writes to the live Neon database at app start, hence the separate go (OP-013).

### OP-008: Approve a weight set
* status: NEEDS_DECISION
* needs: decision from Giorgio
* owner: none
* checked_out_at: none
* lease_until: none
* blocked_by: none
* context: `gg_indicators/settings/weight_sets/`; `gg_indicators/weights.py` (drop-in files, validation); report "What was built"
* done_when: a weight set file with status `approved`, `approved_by` and `approved_on` exists, validates, and the approval is in the Decisions log.
* notes:
  - 2026-10-26: seeded. Only the placeholder exists. Weighting method is being deliberated: expert elicitation with Best-Worst Method, objective diagnostics with CRITIC, outcome calibration later. The engine accepts further sets as files and can run two side by side (`compare_weight_sets`).

### OP-009: Wire the new engine into the app and Target Matrix
* status: BLOCKED
* needs: agent work
* owner: none
* checked_out_at: none
* lease_until: none
* blocked_by: OP-008, OP-001, OP-002, OP-003, OP-004, OP-007
* context: `gg_indicators/bridge.py`; `views/target_matrix.py`; `scoring.py`; models `Company.need_score` / `readiness_score` cache
* done_when: the Target Matrix and company pages show scores from the new engine with coverage and the weight-set label, with the live scoring path either retired or switchable, and tests prove it.
* notes:
  - 2026-10-26: seeded. Do not start before every blocker is DONE and Giorgio's go is in the Decisions log.

### OP-010: Fix the 2 failing legacy tests
* status: OPEN
* needs: agent work
* owner: none
* checked_out_at: none
* lease_until: none
* blocked_by: none
* context: `tests/test_worker_hub.py::test_committed_bundle_contains_the_current_worker_source`; `tests/test_access_control.py::test_login_form_rejects_wrong_password_and_unlocks_with_configured_password`; `scripts/build_worker_bundle.py`; `worker_dist/`, `worker_shim/app/`
* done_when: both tests pass in the full run `python -m pytest tests --capture=no -q -p no:cacheprovider`, with no change to anything else.
* notes:
  - 2026-10-29: 2026-10-29 (claude audit): state now. test_agent_status_file ordering FIXED (ids are compared sorted, and section B was cut back to the 39 line cap by moving the dated history to docs/STATUS_HISTORY.md). test_phase7_batch hook test FIXED (it asserted the order in which threads recorded their start; it now asserts the submission order). The login test passed in the last two full runs. STILL RED: test_worker_hub bundle test, because worker/worker.mjs (ticket I04, intel LLM env pass-through) changed without a rebuilt bundle; rebuilding needs the Scraper repo and npm and pushes an update to helper PCs, so it stays with release ticket I90 (OP-017 G7), not rebuilt here.
  - 2026-10-27: 2026-10-27 (deep-intelligence founder): a THIRD full-run failure exists that is not in the list above: tests/test_agent_status_file.py::test_sections_and_seed_points_exist. Cause: OP-015 was moved to the Closed log (section E) after OP-016 was added to section C, so the ids read OP-001..OP-014, OP-016, OP-015 in file order and the test demands numeric order. Fails on HEAD without any intel change. Fix the test to compare sorted ids, or keep closed points in numeric position. Not changed here.
  - 2026-10-26: seeded and corrected. The login test passes when run alone and fails only in the full run, so look for test-order state leakage, not the login code. The bundle test compares LF with CRLF bytes (`worker.mjs` on disk vs the zip): check `.gitattributes`/autocrlf before rebuilding. Caution: committing a rebuilt bundle together with `worker_shim/app` is what updates the helper PC (see `docs/CRAWLER_ROADMAP.md`); do not rebuild the bundle casually.

### OP-011: Check three calls made in the integration build
* status: NEEDS_DECISION
* needs: decision from Giorgio
* owner: none
* checked_out_at: none
* lease_until: none
* blocked_by: none
* context: report section 2; `gg_indicators/settings/acquisition_modes.json`, `expected_certifications_by_ateco.json`, `applicability.json`
* done_when: Giorgio has confirmed or changed each of the three calls in the Decisions log.
* notes:
  - 2026-10-26: seeded. (1) "Manual desk research" mapped to tier T2, not T3. (2) The ATECO prefixes attached to the certification examples (29 to IATF 16949, 30.3 to AS9100 with EN 9100 as alias, 30.2 and 25.1 to ISO 3834 or EN 15085) are the assistant's mapping. (3) For row 73 any ATECO division outside the brief's lists, or a missing code, counts as "undetermined" (stays in the denominator), neither applicable nor not applicable.

### OP-012: Landing Bay project variables are not in the catalog
* status: NEEDS_DECISION
* needs: decision from Giorgio
* owner: none
* checked_out_at: none
* lease_until: none
* blocked_by: none
* context: workbook Change Log "Open items" (backlog, work in progress, collection days, margin erosion on commessa); report section 5
* done_when: Giorgio has approved the scope in the Decisions log. No agent work before that.
* notes:
  - 2026-10-26: seeded. Order backlog, work in progress, days to collect, margin erosion per project. The sheet has no rows for them, so nothing is modelled.

### OP-013: Giorgio's go for a live database write of catalog changes
* status: NEEDS_DECISION
* needs: decision from Giorgio
* owner: none
* checked_out_at: none
* lease_until: none
* blocked_by: none
* context: `database.init_db`, `indicators.seed_indicator_definitions` (runs at app start against the live Neon database)
* done_when: Giorgio has recorded a go (or no-go) for seeding catalog rows into the live database.
* notes:
  - 2026-10-26: added while seeding because OP-007 names this go as a blocker and every blocker needs an id. Recommendation (agent): seed with weight 0 so existing scores do not move.

### OP-014: Check the mobile layer on a real phone and with a crawl running
* status: OPEN
* needs: agent work
* owner: none
* checked_out_at: none
* lease_until: none
* blocked_by: none
* context: mobile_ui.py (responsive CSS, sidebar auto close script); app.py; views/crawl_widget.py floating card; views/target_matrix.py scatter titles
* done_when: The floating crawl card, long company names and large tables have been checked at 390px with real data and any CSS fix is merged, with no feature removed.
* notes:
  - 2026-10-26: 2026-10-26: added by the mobile pass. Rendered all 9 pages at 390px on demo data, no sideways page scroll. Not checked: crawl card while a job runs, real data, text entry flows.

### OP-016: Decide EU hosting for Wayland app and worker
* status: NEEDS_DECISION
* needs: decision from Giorgio
* owner: none
* checked_out_at: none
* lease_until: none
* blocked_by: none
* context: Streamlit Community Cloud hosts all apps in the US (https://docs.streamlit.io/deploy/streamlit-community-cloud/status); Vercel vienna-worker-shim production deployment reports region iad1, which Vercel maps to Washington D.C. (https://vercel.com/docs/regions); Neon Wayland.my database is in Frankfurt
* done_when: Giorgio records whether Wayland requires EU-hosted application and worker processing and chooses a hosting plan or explicitly accepts the existing regions after appropriate review
* notes:
  - 2026-10-26: Neon database storage moved to Frankfurt, but Streamlit Community Cloud app and Vercel worker still execute in the US. Database location alone does not establish the location of all processing. This is a separate decision from the completed database migration.

### OP-017: Deep-intelligence program: decisions G1 to G7 (search API, LinkedIn, contact policy, LLM budget, subsidiary flag, score-moving fixes, bundle cadence)
* status: NEEDS_DECISION
* needs: decision from Giorgio
* owner: none
* checked_out_at: none
* lease_until: none
* blocked_by: none
* context: docs/INTEL_ROADMAP.md section 8; docs/intel/board.json (tickets I05, I14, I17, I32, I35, I42, I90 gate on these)
* done_when: Giorgio has recorded in the Decisions log an answer, or an explicit deferral, for each of G1 to G7 in docs/INTEL_ROADMAP.md section 8.
* notes:
  - 2026-10-27: 2026-10-27 (orchestrator) EVIDENCE FOR G1 (keyed search API): the free search layer is not reliable from the dev machine. DuckDuckGo times out at TCP connect, Google News RSS returns only encoded redirect links, GDELT answers 429 (one request per 5 s). The layer also hides failures (ticket I103). Recommendation unchanged but stronger: approve ONE keyed search provider (for example Brave Search API or Serper, small monthly cost) so the collectors have a source that works. Also open: G8, whether live tests may send roster company names to public search engines; and G2/G3 are still NOT answered (a results note claimed approval that is not in the Decisions log).
  - 2026-10-27: 2026-10-27 (claude-61ea, ticket I95): the I14 results note (docs/intel/results/I14.md, Decisions taken, items 1 and 2) says the user explicitly approved G2 (LinkedIn) and G3 (contact policy). There is no such entry in the Decisions log below and this point is still NEEDS_DECISION; the launch notes of the I95 session also state G1 to G7 are unanswered. Treat G2 and G3 as unanswered. The I14 and I95 collectors comply with the recommendations anyway: no LinkedIn-derived data, no authenticated crawling, only business contact points the company publishes, never a guessed personal address.
  - 2026-10-27: 2026-10-27: seeded by the deep-intelligence founder. Recommendations are in the roadmap table: free search first (G1), public LinkedIn pages only (G2), business contact points the company publishes (G3), per-company and per-day LLM cap, shortlist first (G4), flag controlled subsidiaries without changing scores (G5), approve score-moving fixes only after the before/after counts (G6), bundle rebuild once per wave (G7). Nothing blocks wave 0 and wave 1.

### OP-018: Audit data from the failed generic Mondial Bony report import
* status: OPEN
* needs: agent work
* owner: none
* checked_out_at: none
* lease_until: none
* blocked_by: none
* context: company_service.py previous _flatten_report_workbook; user reported incorrect import of Mondial Bony.xlsx on 2026-10-28
* done_when: Existing imported Mondial Bony records are inspected against the source and any repair is reviewed and applied with an audit trail
* notes:
  - 2026-10-28: Corrective implementation now provides a dedicated report preview/import and preserves every source cell. Mondial source reconciled 3751/3751 cells in disposable SQLite; nine people/ten appointments and actual years verified. Existing production data remains uninspected/unmodified; retain this point for an audited repair if a bad import was confirmed.
  - 2026-10-28: Parser diagnosis: positional years ignored actual dates (2015 absent), duplicated financial aliases, full ATECO description used as code, people left inside a JSON string, no report preview. Dedicated import replacement is being built; no production rows have been inspected or changed in this session.

### OP-020: Derive EBITDA from a PDF statement (EBIT plus amortisation) so a statement-only year can be valued
* status: NEEDS_DECISION
* needs: decision from Giorgio
* owner: none
* checked_out_at: none
* lease_until: none
* blocked_by: none
* context: balance_sheet_pdf.py _RULES (no ebitda or amortisation rule for statements), financial_ledger.py, valuation.py (needs EBITDA), tests/test_balance_sheet_import.py oic_pages (the line 'Totale ammortamenti e svalutazioni' is read and unmapped)
* done_when: Giorgio has recorded which statement lines make EBITDA (operating result plus B.10 a and b only, or also the write-downs and provisions), then the ledger derives it as a labelled observation with its source lines, tests cover it, and a statement-only year gets a valuation instead of 'Revenue and EBITDA for the latest year are not available'.
* notes:
  - 2026-10-29: 2026-10-29 (claude audit): found while running a real OIC-layout PDF end to end. A statement has revenue, EBIT and the amortisation total but no EBITDA line, so after a newer PDF the merged history has no EBITDA for Latest and the valuation says not valued, the EBITDA flags and ebitda_trend disappear. Recommendation (agent): EBITDA = operating result + total of amortisation and depreciation (B.10 a and b); leave write-downs of receivables and provisions out, say so in the evidence. Not implemented: it is a definition for Giorgio, not a guess.

### OP-021: Diagnosis peer cohort ignores PDF statements and the merged annual history
* status: NEEDS_DECISION
* needs: decision from Giorgio
* owner: none
* checked_out_at: none
* lease_until: none
* blocked_by: none
* context: diagnosis/peers.py load_cohort_dataframe reads only the dataset 'AIDA Raw Exports'; financial_ledger.py 'Merged Annual Financials'; docs/DIAGNOSIS_TICKETS.md (separate board with its own rules)
* done_when: Giorgio has decided whether a peer cohort may mix fiscal years (a company with a newer statement against peers on the older AIDA year), then the cohort reads the merged record accordingly, with a test, or the limit is documented in the peer view.
* notes:
  - 2026-10-29: 2026-10-29 (claude audit): the dossier, valuation, signals and so Target Matrix and pain points follow a PDF; the peer comparison is the one consumer that does not. A company with a FY2025 statement is compared on its FY2024 AIDA figures, or on none if it has no AIDA row. Not changed: the cohort statistics mix years once the merged record is read, which is a method choice.

### OP-022: Run the test suite automatically on every push
* status: NEEDS_DECISION
* needs: decision from Giorgio
* owner: none
* checked_out_at: none
* lease_until: none
* blocked_by: none
* context: .github/workflows/ holds only wayland-morning-journal.yml; the suite takes about 70 seconds (python -m pytest tests --capture=no -q -p no:cacheprovider) and had 3 failures nobody saw for days
* done_when: Giorgio has said yes or no to a GitHub Actions workflow that installs requirements.txt and runs the suite on push and pull request; if yes it exists and is green.
* notes:
  - 2026-10-29: 2026-10-29 (claude audit): no CI runs the tests, so a broken test sits unnoticed until an agent happens to run everything (the status file test failed for days, a phase 7 test is timing dependent). Recommendation (agent): add the workflow once the stale worker bundle test is dealt with (OP-010), otherwise it starts red. Costs Actions minutes on the repository's plan.

### OP-023: Verify the PDF import with real files in the hosted app (OCR, live model, browser)
* status: BLOCKED
* needs: external data
* owner: none
* checked_out_at: none
* lease_until: none
* blocked_by: none
* context: docs/BALANCE_SHEET_UPLOAD.md; balance_sheet_pdf.py; hosted Streamlit app needs the new commit and the CRAWLER_LLM / ASSISTANT_LLM / CRAWLER_VISION_LLM keys in its secrets and pdfplumber and pypdfium2 installed
* done_when: A real FY2025 Mondial Bony PDF, a real OIC industrial company PDF and one scanned PDF have been imported in the hosted app; the AI reading counts (agreed, AI only, mismatch, dropped) are written down; Company Intelligence Financials, Overview, Data and valuation were looked at in a browser; any defect became its own point.
* notes:
  - 2026-10-29: 2026-10-29 (claude audit): everything was verified on throwaway databases with synthetic text-layer PDFs (Bank of Italy and OIC layouts) and a fake model; never on a scan, a live OIC statement, the hosted app or a browser. This container has no model keys.

### OP-024: Handle interim (half-year) statements as interim records, not fiscal years
* status: NEEDS_DECISION
* needs: decision from Giorgio
* owner: none
* checked_out_at: none
* lease_until: none
* blocked_by: none
* context: balance_sheet_pdf.py partial_period detection, financial_ledger.py plan_statement block, docs/BALANCE_SHEET_UPLOAD.md; real file BILANCIO_RICLASSIFICATO 30/06/2026 (01/01 to 30/06/2026, one column)
* done_when: Giorgio has decided how an interim is stored (dated balance sheet snapshot without the income statement, or kept only as provenance, or annualised), and the ledger keeps it out of the annual trends while showing it in Company Intelligence.
* notes:
  - 2026-10-30: 2026-10-30 (claude): today an interim is read correctly but import is blocked unless the operator ticks that it counts as a fiscal year, which would put half-year income figures next to full years. Recommendation: store the balance sheet items as a dated snapshot (point in time values are valid), keep the income statement out of the annual series.

### OP-025: Business-line segment data from statement notes (Mondial Bony: money transfer vs acquiring/POS) and a client-engagement view
* status: NEEDS_DECISION
* needs: decision from Giorgio
* owner: none
* checked_out_at: none
* lease_until: none
* blocked_by: none
* context: Part D table E.8 in Bilancio 2023/2024 PDFs (page_text already stored by balance_sheet_pdf.py); notes 2.1/2.2, protection fund, agents receivables; MondialBony/Appunti Gio.docx and Appunti Chiamata Gio.docx
* done_when: Giorgio has chosen whether segment tables from statement notes become a stored per-company dataset plus a dossier section, and whether an engagement-question view (call topics mapped to data present / missing) is built; then an agent implements it with tests.
* notes:
  - 2026-10-31: 2026-10-31 (claude): Giorgio decided 'Yes, build it'; built in 975f42f (statement_segments.py, views/business_lines.py). Left NEEDS_DECISION only because the script cannot check in a point that was never checked out; Giorgio may close it.
  - 2026-10-30: 2026-10-30 (claude): analysis only, no code. Verified E.8 holds two lines for 2022-2024: money transfer 194.4M/263.5M/298.6M amounts, 721k/882k/955k ops, commission 5.04M/6.37M/6.64M; acquiring 3.0M/6.9M/10.0M, 78k/183k/302k ops, commission 53k/88k/120k. Proposal given in chat; nothing built.

### OP-026: Engagement question view: map call topics to data present, missing and who holds it (Mondial Bony / Naples visit)
* status: OPEN
* needs: agent work
* owner: none
* checked_out_at: none
* lease_until: none
* blocked_by: none
* context: MondialBony/Appunti Gio.docx, Appunti Chiamata Gio.docx; statement_segments.py gives the baseline for ticket, take rate, headcount, agent balances; docs/BALANCE_SHEET_UPLOAD.md
* done_when: A dossier section lists each briefing topic with the baseline figure from the statements (source cited), the data to request and the holder, editable by the team, with tests.
* notes:
  - 2026-10-31: 2026-10-31 (claude): split off from OP-025 once the segment extraction was built. Giorgio chose segments first.

## D. Decisions log

Only Giorgio's decisions. One line each: date, decision, who decided, quote or source.

* 2026-10-26 | Stay side by side until weights are approved | Giorgio | "Keep the new engine side by side for now; don't wire it into the app until weights are approved."
* 2026-10-31 | Build business-line extraction from statement notes (money transfer vs acquiring) into the dossier | Giorgio | "Yes, build it"

## E. Closed log

Done points, with the commit hash of the work. Move the whole point here when it is DONE.

### OP-015: Finish Wayland to Wayland.my Neon cutover
* status: DONE
* needs: agent work
* owner: none
* checked_out_at: none
* lease_until: none
* blocked_by: none
* context: Neon Wayland production plain-bar-03934367 in aws-us-east-2; Wayland.my production gentle-union-48468599 in aws-eu-central-1; Vercel vienna-worker-shim and main app DATABASE_URL
* done_when: Source writes are paused or redirected; a final consistent dump is restored to Wayland.my; all public tables and data fingerprints match; app and worker connections use Frankfurt; no new writes reach the US source
* notes:
  - 2026-10-26: Independent Codex audit: Neon active-project list contains only Wayland.my (gentle-union-48468599, aws-eu-central-1); recoverable-project list marks Wayland (plain-bar-03934367) deleted_at 2026-10-26T18:27:30Z and recoverable_until 2026-11-02T18:27:30Z. Frankfurt production branch is ready, neondb exists, and read-only SQL returned companies 953, signal_records 121750, company_people 7967, indicator_definitions 131, crawler_tasks 167, with crawler_workers.last_seen_at 2026-10-26T20:17:14Z. Prior Codex Streamlit Secrets editor check independently saw Frankfurt DATABASE_URL and no US host after save; hosted app booted to its password gate.
  - 2026-10-26: 2026-10-26: Giorgio confirmed in chat that the Streamlit Community Cloud DATABASE_URL secret now points to Wayland.my (not independently verified by an agent).
  - 2026-10-26: 2026-10-26 (Claude Code): Verified Wayland.my (gentle-union-48468599, Frankfurt) matches the US source: companies 953, signal_records 121750, company_people 7967, indicator_definitions 131, crawler_tasks 167; worker heartbeat now lands in Frankfurt (US last heartbeat 16:39, 0 connections since 17:40). At Giorgio's explicit request the old Neon project Wayland (plain-bar-03934367, us-east-2) was DELETED; Neon list now shows only Wayland.my. Not verified: the Streamlit Community Cloud DATABASE_URL secret - if the dashboard still pointed at the old DB it will error until the secret is set to Wayland.my. Also: Vercel project claude-design-c28005046028ef91ec2ae97877dedf31 (new landing page) exists in team agri-portal, but its deployment is behind Vercel auth and the connector got 403, so its content could not be checked.
  - 2026-10-26: Frankfurt cutover verified: consistent PostgreSQL dump restored; 21 public tables and 142018 rows matched on counts and stable content fingerprints; six later signal updates synchronized; worker functions present. Local .env, Vercel vienna-worker-shim production, and Streamlit Community Cloud DATABASE_URL now use Wayland.my Frankfurt. Streamlit app booted through init_db; source had no newer tasks or signals at final check. Old US project retained. Commit e79fe14 records the earlier cutover status; database and hosted secret changes were external. (commit e79fe14)
  - 2026-10-26: checked out by Codex
  - 2026-10-26: Read-only app smoke check through the updated local .env returned 953 companies from Frankfurt. Remaining action is Streamlit Community Cloud deployment secret and live app verification after Giorgio completes sign-in in the open browser tab; no production dashboard secret has been changed yet.
  - 2026-10-26: Giorgio confirmed the main dashboard is on Streamlit Community Cloud. Its deployment secret is not yet updated: the agent's Streamlit tab reached a sign-in dialog, and the computer-use skill requires Giorgio to sign in himself. Local .env and Vercel worker shim already use Frankfurt; after sign-in, update the Streamlit DATABASE_URL and verify it reads Wayland.my.
  - 2026-10-26: 2026-10-26 cutover: Vercel agri-portal access granted; vienna-worker-shim DATABASE_URL updated from US to Frankfurt and production redeployed as dpl_2nnUVFVgJCKb2g6ED4zkycJQveFK (READY). Local .env DATABASE_URL also changed to Frankfurt. At 16:41 UTC, source worker heartbeat remained at 16:39:32 while destination advanced to 16:41:04; 21 table counts and content fingerprints match with volatile crawler_workers.last_seen_at excluded, 142018 rows. Main app hosting remains unconfirmed, so keep this point OPEN until its production database setting is checked.
  - 2026-10-26: Fresh source snapshot restored into Frankfurt after crawler queue drained. Exact counts and two content fingerprints match on all 21 tables except crawler_workers.last_seen_at, which continues to heartbeat on the US source; all other crawler_workers fields match. Final row count 142018. Vercel team connector still returns 403 pending in-browser grant, so worker/app connection cutover remains open.
  - 2026-10-26: 2026-10-26: Initial pg_dump/pg_restore succeeded into empty Frankfurt neondb. All 21 public tables and worker functions are present; schema exports match. Live crawler activity caused six tables to differ after the snapshot (company_source_runs, crawler_tasks, crawler_workers, raw_import_records, signal_records, source_health). Vercel agri-portal team access and main app host/write pause requested from Giorgio; no cutover yet.

### OP-019: Merge dated PDF financial statements with AIDA history and roll relative years
* status: DONE
* needs: agent work
* owner: none
* checked_out_at: none
* lease_until: none
* blocked_by: none
* context: Giorgio supplied Mondial Bony 2022-2025 PDFs after a 2024 AIDA import; docs/FINANCIAL_YEAR_ROLLOVER_PLAN.md. Current uploader accepts XLSX and derives relative periods within one report, without a cross-source annual ledger.
* done_when: Reviewed PDF extraction preserves dated source evidence; per-company annual facts merge with unit conversion, comparative deduplication and conflict review; a 2025 import reanchors all consumers and connections so 2024 is Y-1 without inventing missing 2025 values; regression coverage includes out-of-order uploads and restatements.
* notes:
  - 2026-10-29: 2026-10-29 (claude audit): follow-up to the closed work, all in the repository and covered by tests: last_accounts_year now follows the merged year (the dossier no longer says FY2025 is missing beside FY2025 figures); bulk AIDA results built on a Latest the history no longer has are withdrawn like the single-report ones; headcount follows the statement; the growth headline reads the merged record; OIC-layout statements (section headings, 6) items, positive costs) now map revenue, cash, debt, equity; removal of a wrong statement with re-merge; identity block shows printed company details. Still open: OP-020, OP-021, OP-023.
  - 2026-10-28: Implemented: PDF extraction, dated merge with AIDA, conflict review (balance sheet default), anchor rollover, tests. Not verified: live OCR/AI, hosted app, valuation after rollover. See docs/BALANCE_SHEET_UPLOAD.md. (commit 653d342)
  - 2026-10-28: checked out by claude-code-bs01
  - 2026-10-28: Read-only review confirms 2025 report covers 31 December 2025 with 2024 comparatives. No PDF importer, data migration or production write performed.
