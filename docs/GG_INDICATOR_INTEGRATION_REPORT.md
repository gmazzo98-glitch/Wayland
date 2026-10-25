# GG indicator integration report (Indicators.xlsx, DREAMY round)

Scope: the brief "integrate the updated indicator sheet into the GG scoring engine". Everything lives in the new
package `gg_indicators/` plus `scripts/build_indicator_contract.py`, `docs/indicator_contract.json`,
`docs/first_contact_questionnaire.{md,json}` and `tests/test_gg_indicators.py` (80+ tests, all passing).
No tracked file was modified. `scoring.py`, `indicators.py`, `models.py`, the database and `Sheet1` are untouched.

**The headline decision for you:** the new engine is *not wired into the app*. It runs beside the live engine
(`scoring.calculate_company_scores`), because the brief's rules contradict the live engine in several places
(section 1) and switching silently would have changed live Target Matrix scores. A bridge
(`gg_indicators/bridge.py`) lets the new engine run over today's SignalRecords so the two can be compared.

## What was built

| Brief item | Where |
|---|---|
| Loader: Sheet1 + Scoring Schema -> schema + JSON data contract (74 rows incl. the 4 new keys and changed row 63) | `gg_indicators/schema.py`, `contract.py`, `docs/indicator_contract.json` |
| Tag parser (accepts `READINESS - NEGATIVE` and `READINESS, NEGATIVE`; case-sensitive axis rule; cross-checked against the sheet's cached formula results, loader refuses to load on a mismatch) | `schema.parse_tag` |
| Stable `variable_key` with a persisted override table (label match, then row match, so a renamed label keeps its key) | `settings/key_overrides.json` |
| Config: roles/gates, applicability by ATECO, expected certifications by ATECO, redundancy groups, `source_it`, moderators, distress, interactions, status mappings, acquisition tiers, weight sets (placeholder only) | `gg_indicators/settings/` |
| Four states `observed / inferred / unknown / not_applicable`, per-axis coverage next to every score | `engine.py` |
| Gates -> `qualification_flag` only; moderators off by default; BOTH variables via one observation and two mappings | `engine.py`, `settings/status_mappings.json` |
| Versioned weight sets, validation, side-by-side comparison, `weight_set_id` + grouping id/hash on every output, placeholder label on every report | `weights.py`, `engine.compare_weight_sets`, `report.py` |
| 63 x 72 `systems_posture`; QUALITY group carried by row 74 alone when row 29 is N/A; row 74 sector norm helper | `engine._posture`, `weights.effective_weights`, `quality.py` |
| First-contact questionnaire generated from the Proxy text of rows 11, 12, 32, 65, 70, 72, 73, 75 | `questionnaire.py` |

NEED and READINESS are never blended and the VIENNA classifier is not implemented (a test asserts neither appears in the output).

## 1. Conflicts with prior work (not silently resolved)

The contract lists 61 of the 70 sheet rows that already existed in `INDICATOR_SEED` as differing in at least one attribute
(`catalog_differences` per row in `docs/indicator_contract.json`). The ones that matter:

1. **Gates.** Live: `digital_lead_role_present` is a gate that *multiplies the axis score down* (`gate_penalty_multiplier`).
   Brief: a failed gate must leave the score unchanged and set `qualification_flag`. Also live-scored but Gate in the sheet:
   `prior_open_innovation_usage` (row 67) and `approval_chain_depth` (row 70). New engine follows the brief; live engine unchanged.
2. **No distress cap exists in the repo.** The brief says "keep the existing logic", but `scoring.py` has none; only prose
   caveats in `indicators.py` (EBIT, interest coverage) and `painpoints.py`. `settings/distress.json` is the first explicit version and is
   uncalibrated, so it reports candidates and `status: not_calibrated` and applies nothing until two numbers are set.
3. **Weights.** Live: one editable 0-5 weight per indicator in the database, normalised by the sum of checked weights. Brief: versioned sets,
   per axis, summing to 1. The sheet's two weight columns have no home in `IndicatorDefinition` (it has one `weight`); they map to weight-set files.
4. **Redundancy dampening.** Live: rank-halving by weight (1, 1/2, 1/4...). Brief placeholder: equal weight per group unit. Both are supported:
   `equal_per_group_unit` for the placeholder, and `explicit` sets reuse the *existing* `_apply_redundancy_dampening` function unchanged (`rank_halving`, default) or `none`.
5. **Signal states.** Live `present / absent / not_yet_checked / stale` vs the brief's four. Bridge mapping: present/stale -> observed;
   not_yet_checked, simulated, low confidence -> unknown; absent -> observed with live value 0 (as today), except rows 73 and 75 where absent -> unknown.
   The live engine scores a company with no data as `0.0`; the new engine returns `None` with 0% coverage.
6. **Redundancy group names** differ throughout (sheet `MGMT_AGE_TENURE`, `COST_STRUCTURE`... vs catalog `MGMT_PROFILE`, `COST_PRESSURE`...). The sheet's proposal is loaded as the active grouping, the catalog's is untouched.
7. **Row 14 (Number of Employees):** sheet tag is `[READINESS]`, Role Scored; catalog keeps it as an unscored context segment filter. Its own Indicator text says "primarily a segment-fit filter". Needs a decision.
8. **Row 57 (Kununu):** tag `[READINESS, caveat]`, no NEGATIVE, so it parses as Standard polarity (Low rating *raises* READINESS), but the row's Comment says "use as a downgrade flag". Kept as parsed and noted in `status_mappings.json`; likely needs a sheet fix (Negative) or a Role change.
9. **Automation tier** disagreements after the mode mapping: rows 15, 20, 22, 63, 71 (e.g. row 63 is T3 in the catalog, "Web or job board scrape" -> T2 in the sheet).
10. **The four DREAMY rows are not in `INDICATOR_SEED`,** the `IndicatorDefinition` table, `GAP_PLAN` or `docs/indicator_catalog.json`, and the catalog text of `erp_systems_age` still describes the pre-change row 63. Adding them seeds rows into the live (Neon) database at app start, so I left that as a deliberate follow-up. It is mechanical: add four seed rows with weight 0, four `GAP_PLAN` entries, a `CATALOG_MIGRATIONS` text update for row 63, regenerate the catalog.

Row 30's bare `[CONTEXT]` tag (no `/ MODERATOR`) is also covered by the parser tests.

## 2. Assumptions I made

* **Acquisition mode -> tier:** Registry -> T1, Web/job board scrape -> T2, **Manual desk research -> T2** (not T3: existing T3 means "only observable at first contact"), First contact -> T3. One line to change in `settings/acquisition_modes.json`.
* **Status direction:** the Sheet1 *Status* text is the "trigger" state. Standard polarity: trigger raises the axis; Negative: lowers it (rows 49, 68; row 70 is a gate so only fails it). Written out per variable in `status_mappings.json` and validated against the tag.
* **Row 73 applicability (flagged, as asked):** applicable ATECO divisions 28, 29, 30, 33, 35; not applicable 41, 42, 43, 71; *every other division or a missing ATECO is "undetermined"*: it stays in the denominator and is listed in `applicability_undetermined`. Row 73 and 75 treat absence of evidence as `unknown`.
* **B2B applicability:** driven by company attributes (`business_model: pure_b2b`, `has_customer_reviews: false`, `has_employer_reviews: false`) or by an explicit `not_applicable` observation. Nothing in the repo supplies those attributes yet.
* **Row 74 mapping:** gap vs norm -> NEED only; meets norm -> NEED 0; long-held multi-standard -> NEED 0 and READINESS 1. States that map to one axis only leave the other axis uncovered for that company (never scored low). Row 74's second Status clause ("inspector-driven, undocumented quality") is treated as a NEED trigger label.
* **Row 75:** levels 1-3 = `completed_low_to_medium` (from the Status text); levels 4-5 assumed to be `completed_high_maturity`, value `TO_CALIBRATE`, so unknown until decided.
* **Gate failure** = trigger fully present (strength 1.0 or the categorical trigger). 1.0 is a calibration choice, listed below.
* **Caveat flag** is set when a `[NEED, readiness caveat]` variable (50, 51, 72) is scored with any trigger strength above 0, and 72 is included in the distress check.
* **Confidence bars** reused from the live engine (0.60 scorable, 0.75 for gates); simulated observations are unknown.
* The ATECO prefixes attached to the brief's certification examples (29 -> IATF 16949, 30.3 -> AS9100 with EN 9100 as alias, 30.2 and 25.1 -> ISO 3834 / EN 15085) are **my mapping**; please confirm.
* A copy of the workbook was added at `gg_indicators/source/Indicators.xlsx` so the loader and tests are reproducible; the loader opens it read-only (a test hashes the file before and after).

## 3. Every `TO_CALIBRATE` item

| Where | What and why it is needed |
|---|---|
| `distress.json` trigger.variable_trigger_strength_at_least | How strongly a financial variable's status must be present to count as distress |
| `distress.json` trigger.min_variables_triggered | How many financial variables must agree before distress is declared |
| `distress.json` action.readiness_cap_value | Maximum READINESS for a distressed company, only if the action is `readiness_cap` instead of `qualification_flag` |
| `moderators.json` product_type_cost_relevance.effect.factor | Weight multiplier on the cost variables for physical goods (rule off) |
| `moderators.json` family_ownership_turnover_reading.effect.factor | Weight multiplier on management turnover for family firms (rule off; enabling it with this unset fails loudly) |
| `expected_certifications_by_ateco.json` thresholds.min_peer_prevalence_for_norm | Peer prevalence that makes a certification a "norm". No prevalence figure was invented |
| ... thresholds.min_years_held_for_long_held, min_standards_for_multi_standard | What "long held, multi-standard" means; until set, a certified firm is `certified_meets_norm` |
| ... generic_baseline and the three sector norms (`status`) | ATECO prefix -> standard mapping to be confirmed |
| `interactions.json` systems_posture.numeric_cutoff | Turning a graded strength (strictly between 0 and 1) into old/new and siloed/integrated |
| `status_mappings.json` digital_maturity_self_assessment.states.completed_high_maturity.READINESS | Value of a completed assessment at DREAMY level 4-5 |
| `roles.json` gates.*.fail_strength_at_least (= 1.0, each carries a calibration note) | Currently only a fully present trigger fails a gate |

## 4. Every `TO_MAP` item (`settings/source_it.json`)

Of 74 rows, 14 are MAPPED to a concrete wired AIDA header, 37 PARTIAL (system known, exact field or access not confirmed) and 23 TO_MAP.
`Sheet1`'s Source column is untouched.

* **TO_MAP rows** (Sheet1 rows): 4, 6, 7, 10, 11, 12, 13, 16, 17, 22, 23, 24, 26, 31, 32, 39, 40, 61, 65, 70, 72, 73, 75.
* **Inline TO_MAP inside PARTIAL rows** (field or access to confirm): 12 entries, notably Accredia bulk access (row 74), UIBM patent and trademark registers (rows 44, 45), Registro Imprese unita locali (rows 19, 20), and the AIDA header for R&D (row 43).
* Italian equivalents of the German-only wired sources (Bundesanzeiger -> AIDA) are only listed where this repo already imports the field.

## 5. Not implemented, or only partly

* **Not wired into the app, database, or Target Matrix** (see headline). No DB migration, no UI.
* **Four DREAMY rows not added to the live catalog** (conflict 10).
* **No producers.** Nothing yet emits observations for the four new rows or labels for the multi-state rows; `quality.classify_quality_process` is the only helper (needs certification data, e.g. Accredia).
* **Distress cap is inert until calibrated**; moderator numeric rules are off until calibrated (note-only M&A rule is on and changes no number).
* **Weights:** only the placeholder exists, by design.
* The bridge maps live numbers to graded strengths but cannot supply named labels (moderators, rows 74/75), so those stay unknown through the bridge.
* VIENNA classification: deferred as instructed. The sheet's row 67 comment suggests using it as a VIENNA input later.
* Change Log open item "Landing Bay project variables (backlog, WIP, collection days, margin erosion on commessa)" is not in the sheet yet, so not modelled.

## How to use

```python
from gg_indicators import IndicatorRegistry, Observation, CompanyContext, score_company, format_report
reg = IndicatorRegistry.load()
res = score_company(reg, [Observation.trigger("ebit_trend"),
                          Observation.with_label("erp_systems_age", "legacy"),
                          Observation.with_label("system_integration", "siloed")],
                    CompanyContext(ateco_code="28.99.9"))
print(format_report(res))
```

Regenerate the contract after editing the workbook or settings: `python scripts/build_indicator_contract.py` (`--check` in CI; `--persist-new-keys` after adding a sheet row).
Run the tests with `python -m pytest tests/test_gg_indicators.py -q -p no:cacheprovider`.
