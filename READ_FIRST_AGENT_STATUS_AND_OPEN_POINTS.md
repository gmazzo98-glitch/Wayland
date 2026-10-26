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
* Tests: 80 new tests in `tests/test_gg_indicators.py` pass. 2 legacy failures in the full run: `tests/test_worker_hub.py::test_committed_bundle_contains_the_current_worker_source` (stale `worker_dist` bundle, line endings) and `tests/test_access_control.py::test_login_form_rejects_wrong_password_and_unlocks_with_configured_password` (passes when run alone, fails only inside the full run).
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

## D. Decisions log

Only Giorgio's decisions. One line each: date, decision, who decided, quote or source.

* 2026-10-26 | Stay side by side until weights are approved | Giorgio | "Keep the new engine side by side for now; don't wire it into the app until weights are approved."

## E. Closed log

Done points, with the commit hash of the work. Move the whole point here when it is DONE.

_none yet_
