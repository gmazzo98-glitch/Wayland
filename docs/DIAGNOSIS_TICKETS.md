# Diagnosis build: protocol, rules and tickets

This is the work board for the diagnosis program described in [DIAGNOSIS_MASTERPLAN.md](DIAGNOSIS_MASTERPLAN.md).
Several AI agents work from it at the same time. Read **Part 1 (protocol)** and **Part 2 (ground rules)** in full,
then only your own ticket in **Part 7**.

The state of the board (who holds what) is **not** in this file. It is in `docs/diagnosis/board.json` and is changed
only by `python scripts/board.py ...`. This file is the static specification and is edited only by the planner
(or by an agent adding the section for a ticket it proposed with `board.py add`).

```
python scripts/board.py status           # the whole board
python scripts/board.py next --agent X   # what you can claim right now, and why the rest cannot be claimed
```

---

## Part 1. The check-in / check-out protocol

Why it works this way: the agents are launched in the same minute from the same prompt, share one repository, and
cannot talk to each other. So (a) every claim is an atomic compare-and-swap on `origin/master` (git decides who wins,
not a human or a race), (b) a claim also locks the files the ticket may write, and (c) each agent works in its own
git worktree, so nobody's checkout is ever changed under them.

**0. Name yourself.** Pick a unique agent name: your tool plus four random hex characters (`claude-7f3a`, `codex-c01e`).
Use the same name for the whole session.

PowerShell: `$env:DIAG_AGENT = "claude-7f3a"`    bash: `export DIAG_AGENT=claude-7f3a`

**1. Get your own worktree (once per session, before claiming anything).** If `git rev-parse --git-dir --git-common-dir`
prints two different paths you are already in a linked worktree (the app often starts sessions in one): use it. Otherwise:

```
git fetch origin
git worktree add .claude/worktrees/diag-<agent> --detach origin/master
cd .claude/worktrees/diag-<agent>
```

This worktree is yours alone, so you may switch branches in it. Never `git checkout` anything inside the shared main
checkout: other agents and the user's running app are using it. `scripts/board.py` works from any worktree and never
changes your files.

**2. Check out a ticket.**

```
python scripts/board.py claim --agent $DIAG_AGENT --jitter 20      # let the board choose for you
python scripts/board.py claim D07 --agent $DIAG_AGENT              # or name a ticket
```

- `claim` works from any branch or worktree. It reads the board from `origin/master`, refuses a ticket that is taken,
  whose dependencies are not DONE, or whose write scope overlaps a ticket someone else holds, and pushes the change
  atomically. If git rejects the push because someone else pushed first, it retries on the new state by itself.
- Without a ticket id it picks the highest-priority claimable ticket (random among equals, so agents launched together
  spread out). With `--jitter 20` it first sleeps a random 0-20 s.
- You may hold **one** ticket at a time. Finish (`done`), or `release`, before claiming another.
- If it says nothing is claimable, run `next`. If the list is empty because everything is claimed or waiting, you are
  finished: report that and stop. Do not invent work. Do not take someone else's ticket.
- A claim silent for 8 h (no heartbeat) shows as STALE and may be taken over with `claim <ID> --take-over`. Read the
  previous holder's branch (`git branch -r | findstr diag/<ID>`) and continue from it instead of starting over.

**3. Create the ticket's branch** in your worktree, from the current master (your tree must be clean; if it is not,
commit or discard *your own* leftovers first, never anyone else's):

```
git fetch origin
git checkout -b diag/<ID>-<slug> origin/master      # the claim output prints the exact name
```

Environment for a fresh worktree: `.env` is git-ignored, so a new worktree has none. If your ticket has the `live_db_read`
need, copy it once from the main checkout (same machine): `copy "<main checkout>\.env" .env`. Never commit it, never
print its values, never paste a key into a file, a note or a commit message. Tests never use it (see Part 2, rule 8).

**4. Read, in this order:** the masterplan sections your ticket cites; `diagnosis/contracts.py`;
`diagnosis/factkeys.py` (the keys you produce or consume); your ticket below; the existing code it names. If the
ticket touches crawlers, also the ground rules of `docs/CRAWLER_ROADMAP.md` (they still apply).

**5. Work inside your write scope.** The `files` list on the board is your lock. Create and edit only those paths
(plus your results note). If you need something outside it:

- a *new* file in a package you do not own, whose name cannot collide (a new `diagnosis/readers/xxx_*.py`): not allowed
  unless in scope. Add a ticket instead.
- an edit to a *hot file* (Part 3) or another ticket's file: do not. Record it under "Seam requests" in your results
  note, and if the ticket cannot be finished without it, `board.py block` it with the reason.

Commit early and often, with explicit paths: `git add <path> <path>`. Never `git add -A` or `git add .`.

**6. Heartbeat** at least once an hour while you work: `python scripts/board.py heartbeat <ID> --agent $DIAG_AGENT`.

**7. Land your work** (no rebase, no stash, no force, ever):

```
git fetch origin
git merge --no-ff origin/master        # bring master INTO your branch; resolve conflicts yourself; keep both sides' additions
python -m pytest <your test files> --capture=no -p no:cacheprovider
git push origin HEAD:master            # if rejected: fetch + merge again, re-test what changed, push again
```

Run the full suite once before your final push (Part 2, rule 8). Never stage `docs/diagnosis/board.json`: the board
script owns it and merging `origin/master` brings you the current copy.

**8. Write your results note** `docs/diagnosis/results/<ID>.md` (template in Part 5), commit it, include it in the push
above.

**9. Check in.**

```
python scripts/board.py done <ID> --agent $DIAG_AGENT --result docs/diagnosis/results/<ID>.md
```

`done` refuses unless your HEAD and the results note are on `origin/master`. Then run `next` and, if something is
claimable and you have used fewer than three tickets this session, go to step 2 (new branch from a fresh `origin/master`). Otherwise stop and report.

**10. If you are stuck:** `python scripts/board.py block <ID> --agent $DIAG_AGENT --reason "..."` (the reason must say what
is needed and from whom), write what you learned in the results note, push it, stop. If you are not going to finish:
`release <ID> --note "..."` so someone else can. A silently abandoned claim blocks everyone downstream for 8 hours.

**11. If you find a gap nobody has a ticket for:** `python scripts/board.py add --agent $DIAG_AGENT --title "..." --files ... --depends-on ... --why "..."`
and add a `### D36 ...` section here in your branch. Do not quietly widen your own ticket.

---

## Part 2. Ground rules (every agent, every ticket)

1. **Honesty over coverage.** A fact you cannot source is not a fact. Never fabricate a value to raise a coverage number.
   *Unknown* (key absent) and *confirmed absent* (`status="absent"`) are different and must stay different. A page that
   failed to load is unknown, not absent.
2. **No simulated data, ever.** Rows with `SignalRecord.is_simulated = True` (about 50,000 placeholders in the live
   database) are not evidence. Readers skip them and count them in `src.simulated_ignored`. Several sources are
   simulated today (EUIPO trademarks, Kununu, most "Company Website"/"News/Press" placeholders): do not read them.
3. **Every fact carries a grade and provenance.** A official/filed; B the company's own publication read directly;
   C a third party about the company; D inference or estimate. Source label, `source_ref` and `as_of` are mandatory
   where they exist. Anything you compute from other facts takes the weakest grade among its inputs.
4. **Verbatim or nothing for extracted text.** Anything read out of a web page by a model is accepted only if the quoted
   text is found on the page by code (the T25 site-evidence rule). A model never supplies a number, a name or a date
   that is not in the source text. A model may *classify and phrase*; it may not *invent*.
5. **One producer per fact key and per signal key.** The diagnosis layer writes **Facts** and its own `dx_*` tables. It
   never writes a `SignalRecord`, never changes an indicator weight, never writes `Company.need_score`/`readiness_score`.
   If your output deserves to become an indicator, say so in your results note: that is GG's decision
   (`docs/CRAWLER_ROADMAP.md`, ground rule 1).
6. **Never blend NEED and READINESS**, and never fold the diagnosis into either. Hypotheses, triggers, reachability,
   fit and evidence quality are separate dimensions; the call tier is a set of explicit rules over them, not a sum.
7. **Peer-relative before absolute.** Judge a ratio against the company's peers (`peer.*.pctile`), not against a number
   you remember. Absolute thresholds only for hard limits (negative equity, interest cover below 1.5x). Where you set a
   threshold, print the live distribution (read-only) and put it in the module docstring, as `painpoints.py` does.
8. **Tests.** `tests/conftest.py` points every test at a throwaway SQLite file: never set `VIENNA_TESTS_ALLOW_LIVE_DB`.
   Run `python -m pytest <files> --capture=no -p no:cacheprovider`. No test touches the network. Several existing tests
   are timing-sensitive on a loaded machine (`test_access_control::test_login_form_*`, two in `test_phase7_batch`,
   `test_worker_hub::...local_budget`): if the *only* failures are these and they pass alone, say so in your note, do not
   "fix" them. Your own tests must be deterministic and fast (a few seconds).
9. **The live database is read-only** unless your ticket lists `@live-db-write` AND `gg_approval`. Read with SQLAlchemy
   through `database.get_db_session()`. If a network call fails in the Bash tool, retry it in PowerShell (a known
   difference on this machine). Writing to the live database means: dry run first on a copy, show GG the dry run, get an
   explicit yes in chat, then write, with a rollback path.
10. **LLM use.** Read provider settings fresh each call (`config.assistant_llm_settings()`, `config.crawler_llm_settings()`),
    never module-level constants. Temperature 0.1 or lower. Always have a deterministic fallback so the pipeline works
    with no key. Cache by input fingerprint. Never send personal data beyond what a business call needs (director names
    from the public registry are fine; ages and private addresses are not). The local model (Ollama, `qwen2.5:3b`) exists
    on one PC only.
11. **Privacy and policy.** LinkedIn, Kununu direct scraping and Google Maps stay off (`config` gates). Obey robots.txt.
    Collect only *business* contact data that the company itself publishes. Never put a director's age or personal
    details in text meant to be said or sent to a third party; the call pack may show them to GG's analyst only.
12. **Windows details.** The consoles are cp1252: set `PYTHONIOENCODING=utf-8` and keep emoji out of `print`. Paths contain
    spaces: quote them. The repo is inside OneDrive: `git status` can lag a few seconds after a large write.
13. **Reuse, then build.** Grep before writing. These already exist and must be used, not re-implemented: `dossier_data`
    (statement, ratios, flags, ownership), `painpoints` (17 pain points), `scoring`, `valuation_service`, `cost_breakdown`,
    `ateco_cost_benchmarks`, `company_brief.collect_evidence`, `scrapers/*`, `adapters/*`, `site_evidence_hub`,
    `resource_governor`, `crawl_jobs`.
14. **Small and finished beats large and half-done.** If your ticket turns out to be more than twice its size,
    split it: deliver the useful half, `board.py add` the rest.
15. **Do not narrate success you did not verify.** Your results note says what you ran, what you saw, and what you did not
    check. A failing test is reported with its output, not hidden.

## Part 3. Write scopes, hot files and shared resources

- A ticket's `files` list is its lock. Globs are supported (`diagnosis/readers/core_*.py`). Two tickets whose scopes
  could name the same file cannot be held at the same time (the board enforces this).
- **Hot files** (shared by everything; not in any ticket's scope unless named): `models.py`, `config.py`,
  `company_service.py`, `indicators.py`, `painpoints.py`, `scoring.py`, `utils.py`, `views/company_detail.py`,
  `views/pipeline_health.py`, `worker/**`, `worker_shim/**`, `docs/CRAWLER_ROADMAP.md`, `diagnosis/contracts.py`,
  `diagnosis/registry.py`. `app.py`, `views/company_dossier.py` and `dossier_nav.py` belong to D30 only.
- **Named resources** (`@name` in `files`): `@scraper-repo` (the separate Scraper checkout: one ticket at a time),
  `@bundle` (the committed worker bundle `worker_dist/**` + `worker_shim/app/**`: a binary, rebuilt only by the release
  captain R02 or by a ticket that holds it), `@live-db-write` (anything that writes to the live database: one at a time,
  with GG's approval).
- **New tables** go in `diagnosis/models_<area>.py` (auto-imported by `database.init_db`). New tables only, prefix
  `dx_`, never a new column on an existing table (SQLite migrates columns, the live Postgres does not).
- **New configuration** is read with `os.getenv` inside your module; do not edit `config.py`. Document the variable in
  your results note; R01/R02 add it to `.env.example`.
- The Scraper repo (`../Scraper`, remote `gmazzo98-glitch/Scrapers`) is a different repository with its own git history.
  Only tickets holding `@scraper-repo` touch it, and they commit there separately. A worktree of this repo does not
  contain it: set `SCRAPER_CRAWLERS_DIR` to the main checkout's sibling `Scraper/crawlers` when you need it.

## Part 4. Changing a contract

`diagnosis/contracts.py` and `diagnosis/factkeys.py` are the interface between agents who never speak. Rules:

- **Adding a fact key** in your own namespace: put it in `diagnosis/factkeys_ext_<ID>.py` with `declare_keys([...])`. It is
  auto-imported. Allowed in any ticket for keys that ticket owns.
- **Adding an optional field** to a dataclass, or a new enum value: needs a ticket whose scope is `diagnosis/contracts.py`.
  Propose it with `board.py add`, depend nothing on it until it lands.
- **Renaming, removing or changing the meaning of anything**: a contract change. Bump `SCHEMA_VERSION`, migrate every
  consumer in the same ticket, and tell GG in the results note. Avoid.

## Part 5. Results note template (`docs/diagnosis/results/<ID>.md`)

```
# <ID> <title>
Agent: <name>   Branch: diag/<ID>-<slug>   Merged: <date, commit>
## What I built            (files, one line each)
## How I verified it       (commands run, sample sizes, numbers; live read-only checks; what the output looked like)
## Numbers                 (coverage / precision / counts that someone can compare later)
## What it does NOT do     (known limits, false-positive classes, data that is still missing)
## Seam requests           (edits I needed in files outside my scope, with the exact change)
## Decisions for GG        (anything that is GG's call: indicator keys/weights, thresholds, policy)
## Proposed follow-ups     (each as a one-line board.py add suggestion)
```

---

## Part 6. Pilot tag vocabulary (frozen; D06 may add, never rename)

Hypotheses (D18-D21) name pilots by tag; the pilot library (D06) implements one concept per tag.

| Tag | Pilot concept | Tag | Pilot concept |
|---|---|---|---|
| `quote_automation` | AI-assisted RFQ triage and quoting (configure-price-quote) | `configurator` | Customer-facing product configurator, 3D digital catalog |
| `design_automation` | Design automation, BOM generation for configure-to-order | `servitization` | Usage-based / pay-per-use service model and billing |
| `predictive_maintenance` | Condition monitoring and predictive maintenance (retrofit IIoT kit) | `esg_carbon` | Carbon footprint / LCA / ESG data for customer requests |
| `remote_service` | Remote assistance and AR field service | `additive_spares` | Additive manufacturing for spares and on-demand parts |
| `spare_parts_portal` | Spare-parts e-commerce and aftermarket portal | `robotics_cell` | Cobot / robot cell for repetitive own-production tasks |
| `digital_twin` | Digital twin, virtual commissioning to cut FAT and site time | `back_office_ai` | Document intelligence for orders, invoices, back office |
| `scheduling_mes` | Shop-floor scheduling and MES-lite | `incentives_copilot` | Incentives and grants copilot (iperammortamento, regional calls) |
| `energy_monitoring` | Plant energy monitoring and optimisation | `supply_chain_visibility` | Supply-chain visibility and logistics optimisation |
| `procurement_analytics` | Procurement analytics, should-cost, supplier risk | `machine_data_platform` | Machine data platform / OEE dashboards for customers |
| `working_capital` | Receivables/payables automation, cash forecasting | `green_redesign` | Energy-efficient redesign (drives, heat recovery partners) |
| `inventory_optimisation` | Inventory, WIP and spares forecasting | `open_innovation` | Open-innovation scouting with universities and competence centres |
| `compliance_docs` | Technical-file and conformity automation (Machinery Regulation) | `warranty_analytics` | Warranty and claims analytics, quality feedback loop |
| `ot_cybersecurity` | OT/IT security baseline (NIS2 readiness) | `talent_platform` | Technician recruiting, skills and upskilling platforms |
| `product_cybersecurity` | Product security and SBOM for connected machines (CRA) | `knowledge_capture` | Capture of retiring experts' know-how, assisted work instructions |
| `vision_inspection` | Vision-AI quality inspection in own production | `export_leadgen` | Export lead generation and distributor discovery |

---

## Part 7. Tickets

Format of every ticket: **Goal / Why (the gap) / Inputs / Outputs / Steps / Acceptance / Don't.** Size S is a few hours of
agent work, M about a day, L two or three. Dependencies are enforced by the board. `needs` tells you what the environment
must provide; if you lack it, `release` the ticket with a note instead of improvising.

### Wave 1: foundations (all of these can run at the same time)

### D01 Fact readers A: identity, financials, people, ownership, existing scores and pain points
**Size L, P0. Needs: live_db_read. Scope:** `diagnosis/readers/core_*.py`, `tests/test_diagnosis_readers_core.py`.

**Goal.** Readers (`@reader`) that turn the already-collected structured data into Facts for every `D01` key in
`diagnosis/factkeys.py` whose state is `exists`: `id.*`, `fin.*` (except the planned working-capital keys), `mgmt.*`,
`own.*`, `pp.*`, `score.*`, `src.layers_present`, `src.simulated_ignored`.

**Why.** Today every page re-reads the database its own way (`dossier_data`, `painpoints`, `company_brief`,
`valuation_service`). The diagnosis needs one fact base with grades and provenance so that hypotheses are written once.

**Inputs.** `Company`; `RawImportRecord` dataset `AIDA Raw Exports` (3 fiscal years, 953/953); real `SignalRecord` rows
(`is_simulated=False`, `status in present/absent`); `CompanyPerson` (7,967 rows); `painpoints.py` detection and
`scoring.py` results, run read-only.

**Outputs.** Facts, grade A for AIDA/registry data (`as_of` = fiscal year end), with `source`, `source_ref`.
Suggested modules: `core_identity.py`, `core_financial.py`, `core_people.py`, `core_ownership.py`, `core_scores.py`,
`core_coverage.py`.

**Steps.** (1) Reuse `dossier_data.financial_statement`/`ownership`/`flags` and the signal keys named in each key's
`hint`; do not recompute what exists. (2) CEO selection from the roster: prefer *Amministratore Delegato*,
*Presidente*, *Legale rappresentante*, *Direttore generale*, sole director; keep the whole roster in `mgmt.roster`.
(3) `own.type` rules: individuals with the same surname as managers and total share >= 50% -> `family`; a holding
company or fund as controlling shareholder -> `holding`/`private_equity`; foreign controlling owner -> `foreign_group`.
Say `unknown` rather than guess. (4) `pp.*` and `score.*` are context facts; mark their notes "derived from the same
indicators: do not use as independent evidence". (5) Skip simulated signals and count them.

**Acceptance.** For the 48 shortlisted and 30 random companies (read-only live check): every `exists` key in your
namespace has a reader or a documented reason; zero facts originate from `is_simulated` rows; `build_facts(..., only=[...])`
runs in under 200 ms per company; no reader error on any of the 78; unit tests on a fixture SQLite DB cover a complete
company, a company with one AIDA year missing, a company with no roster, and an open distress procedure.

**Don't.** Do not recompute valuation, scores or pain points in your own way. Do not emit `fin.receivables_k` and the
other planned keys (D35 owns them).

### D02 Fact readers B: every crawled layer
**Size L, P0. Needs: live_db_read. Scope:** `diagnosis/readers/crawled_*.py`, `tests/test_diagnosis_readers_crawled.py`.

**Goal.** Readers for `dig.*`, `prod.*`, `net.*`, `pub.*`, `rev.*`, `news.headlines` from the `crawler_*`
`RawImportRecord` blobs and real signals: `crawler_company_website` (263 companies), `crawler_job_postings` (301),
`crawler_digital_maturity` (290), `crawler_product_catalog` (173), `crawler_directory_{mecspe,anima,kompass}` (~300/167/164),
`crawler_ted_contract_awards` (158), `crawler_news_rss` (952), `crawler_reviews_*` (8), `crawler_site_evidence`,
`crawler_website_discovery`, Wappalyzer (848 real), EU funding (693 real checks), EPO (250 real checks), `Competitor` rows.

**Why.** These are the only evidence about *what the company does and how it presents itself*. Their quality is
uneven and some are misleading (see "Don't"). The reader is where that unevenness is made explicit.

**Rules that matter.**
- Read `field_status` before any value. `not_found`, `failed` and "page unreadable" are **unknown**, never absent.
  Absent (`status="absent"`) only where the crawler states it checked and found nothing (e.g. a confirmed directory
  search with no match, a careers page read with zero listings).
- Grades: own website/jobs/catalog -> B; directory listings and reviews and news -> C; TED/EU funding/state aid -> A
  (official register); EPO -> A. `news.headlines` is grade **D** and its note says "unverified: about this company only
  if D17 confirms" (the headline-match path was audited at roughly half wrong).
- Directories: emit only *confirmed* listings (`appears_in_directory and not possible_match`).
- Do not read EUIPO trademark signals (simulated). Do not treat TED "no award found" as proof of no public customers.
- `dig.open_roles_*` come from the T17-fixed job wrapper; a careers page that hands off to an external board is unknown.
- Dates: `as_of` is the crawl date or the page's own date, whichever is truer; `observed_at` is the crawl time.

**Acceptance.** Run on the 48 shortlisted + 30 random companies read-only: each present key traces to a dataset name in
`source_ref`; per-layer counts reported (how many companies get each key); a test per layer using fixture blobs copied
from real shapes; a test that an unreadable-site blob yields *no* absent facts.

**Don't.** Do not call any crawler or the network. Do not write anything. Do not emit `dig.iiot_signals` (D11).

### D03 Peer engine: peer groups and percentiles for every ratio
**Size L, P0. Needs: live_db_read. Scope:** `diagnosis/peers.py`, `diagnosis/readers/derive_peers.py`, `diagnosis/models_peers.py`,
`tests/test_diagnosis_peers.py`.

**Goal.** For each company and each metric in `factkeys.PEER_METRICS`, the company's **percentile among its peers**, the
peer median and the group used, emitted as `peer.*` facts by a `@deriver`. A cached table `dx_peer_stats` holds group
statistics so a portfolio run is a lookup, not a recomputation.

**Why.** "Is a 2% EBITDA margin bad?" has no answer without peers; this is the single most reusable analytical primitive
for D18-D21. `ateco_cost_benchmarks.py` already does it for cost lines only.

**Steps.** (1) Peer group = same ATECO 6-digit; widen to 5, 4, 3 digits until n >= 12 (configurable); record
`peer.group_level`. The cohort is 50-99 staff so size band is constant today; build the size cut anyway (headcount
terciles inside a wider group) and use it when the cohort grows. Region is a *secondary* cut reported separately, never
silently mixed in. (2) Compute metrics in bulk from the AIDA blobs (pandas/SQL), not by running 953 readers. Exclude a
peer's value when the ratio's own definition fails (revenue <= 0, equity <= 0 for equity ratio). (3) Percentile = mid-rank
among peers excluding the company itself. (4) `inventory_change_pct_revenue` from the cost breakdown. DSO/DIO/DPO only
when their `fin.*` inputs exist (they do not yet: emit nothing). (5) `python -m diagnosis.peers --rebuild` recomputes
the table; document it.

**Acceptance.** Percentile is monotone in the value on a synthetic cohort; the group widens correctly on a sparse code;
at least 95% of the live cohort gets a group with n >= 12 for the core metrics (report the exact figure and the codes
that needed widening); results match `ateco_cost_benchmarks` medians where they overlap; rebuild of all 953 takes under
60 s.

### D04 Sector playbooks
**Size L, P1. Needs: web, live_db_read. Scope:** `diagnosis/sectors.py`, `diagnosis/data/sectors/**`, `tests/test_diagnosis_sectors.py`.

**Goal.** One researched, sourced playbook per ATECO code that has at least 10 companies in the live cohort (query it:
top codes today are 289300 (97), 282999 (74), 282500 (63), 282930 (63), 281400 (62), 284100 (37), 282209 (37),
289600 (36), 283090 (35), 289999 (31) and a tail). Check whether AIDA's codes are ATECO 2007 or 2025 before titling them.

**Schema** (JSON per code, loader `sectors.get(code)` falling back to the 4-digit parent): official title; what the
machines are and do; typical products (Italian and English terms, used later for product matching); end-markets with
rough weights; value-chain position; business-model mix (engineer-to-order vs series); aftermarket importance;
demand drivers and cyclicality; key inputs (steel, aluminium, electronics, energy); trade bodies and the statistics they
publish (name, URL, cadence); main trade fairs (name, city, cadence, next date); relevant regulations (ids from D05);
typical pains (hypothesis ids from the masterplan, section 6); typical margin/growth ranges *with source*;
industrial districts by province; `search_queries` in Italian and English for the market radar (D14); sources.

**Why.** The cohort is one NACE division but not one business: a food-processing machinery maker, a valve maker and a
packaging-line builder have different customers, cycles and pains. The diagnosis must speak to the right one.

**Rules.** Every claim has a source URL and an `as_of`. "Unknown" is a valid value. `review_status: "agent_draft"` until
GG reviews. Do not copy text: summarise in your own words. Spot-check five claims per playbook by re-reading the source.

**Acceptance.** Playbooks for every code >= 10 companies; each has >= 6 sourced claims, >= 4 search queries per
language, >= 1 statistics source with a working URL; loader tests; a list of codes with no usable public source.

### D05 Regulatory and incentive clock
**Size M, P0. Needs: web. Scope:** `diagnosis/regulatory.py`, `diagnosis/data/regulatory.json`,
`diagnosis/readers/derive_regulatory.py`, `diagnosis/triggers/regulatory.py`, `tests/test_diagnosis_regulatory.py`.

**Goal.** A curated, dated, source-linked table of the rules and money that change what an Italian machinery SME should
do now, an applicability engine over the company's facts, and the `reg.*` facts and dated trigger events it yields.

**Seed list to verify against primary sources (EUR-Lex, Gazzetta Ufficiale, ACN, MIMIT):** Machinery Regulation
(EU) 2023/1230, applies 20 Jan 2027; Cyber Resilience Act (EU) 2024/2847, reporting obligations from 11 Sep 2026 and the
main obligations from 11 Dec 2027; NIS2 (EU) 2022/2555 as transposed by D.Lgs. 138/2024 (confirm whether NACE C28
sits in Annex I or II, the medium-size thresholds, and the ACN registration/categorisation calendar); AI Act
(EU) 2024/1689 and the current status of the proposed delay of high-risk obligations; CBAM (EU) 2023/956 definitive
period from 2026 for steel/aluminium inputs; F-gas Regulation (EU) 2024/573 (refrigeration, code 28.25); Pressure Equipment
and ATEX where valves/pumps apply; Packaging and Packaging Waste Regulation (EU) 2025/40 as a *customer-side* driver
for packaging machinery; Energy Efficiency Directive audit duties; the **iperammortamento 2026-2028** (Legge
199/2025 art. 1 cc. 427-436, DL 38/2026, MIMIT implementing decree of 7 May 2026: investments from 1 Jan 2026 to
30 Sep 2028, reservation platform open since 12 Jun 2026, markups 180% / 100% / 50% by band) and its interaction with
Transizione 5.0 (closed 7 Nov 2025); regional calls in Lombardia, Emilia-Romagna, Veneto, Piemonte (find the live ones).
These were read from secondary sources on 2026-10-23: **verify every date against the primary text and record the
verification.**

**Schema per entry:** id, title, legal instrument and citation, applies_from, key_dates[], applicability
(`nace_prefixes`, size conditions, product-feature conditions in plain words), reading for this cohort (what it means for a
typical company, with the caveat), what it demands (<= 3 bullets), pilot_tags, source_urls[], `verified_on`,
`legal_review: "none"`.

**Outputs.** `reg.*` facts per company where the applicability rule fires; where the rule needs a product feature the
profile (D11) has not provided, emit the obligation with `applies: "depends"` and the missing feature as a question.
Trigger events (`regulatory_deadline`, `incentive_window`) with `due_on`.

**Acceptance.** Every entry has a primary-source URL and `verified_on`; every date in the table matches the primary text;
the applicability engine is unit-tested on companies with and without the feature flags; nothing writes a
`SignalRecord`; the file states plainly that this is screening, not legal advice.

**Don't.** Do not decide NIS2 category for a company; say "in scope if ...". Do not touch the BLOCKED indicator
`regulatory_compliance_exposure` (weight 5): GG owns that decision.

### D06 Pilot library
**Size L, P0. Needs: web. Scope:** `diagnosis/pilots.py`, `diagnosis/data/pilots.json`, `tests/test_diagnosis_pilots.py`.

**Goal.** One `PilotConcept` per tag in Part 6 (30 concepts), each grounded in real analogous cases.

**Per concept:** the problem in the customer's words (Italian and English one-liner); solution types (kinds of startup
solution, never named vendors unless they are the real case); typical duration (weeks); a defensible customer-side budget
band (EUR) with the reasoning; 2-4 KPIs a pilot would measure with how; prerequisites (data, asset, champion, IT access);
`not_when`; **at least two real references** (a published case, a VDMA/Fraunhofer/Digital Innovation Hub/EU project write-up,
a vendor case study) with URLs; how it ties to incentives (flag, do not assert eligibility).

**Why.** The diagnosis is only useful if it ends in something GG can propose on the call. Today `pilot_angle` is one free-text
line per pain point.

**Acceptance.** 30 concepts, each with >= 2 working references (checked), a budget band with reasoning, KPIs, and
prerequisites; loader `pilots.get(tag)`, `pilots.all()`, a validator test (tags unique and cover Part 6; durations and
budgets sane); a short list of tags you would add.

**Don't.** Do not invent customer names or results. Do not quote prices of specific vendors.

### D07 Discovery-question and talk-track bank
**Size M, P1. Scope:** `diagnosis/questions.py`, `diagnosis/data/questions.json`, `tests/test_diagnosis_questions.py`.

**Goal.** What to ask and say, written the way a good consultant would, Italian first and English second.

**Contents.** (1) For every hypothesis id in the masterplan section 6: 3-5 open, non-leading questions, what a strong
and a weak answer reveal, and a follow-up. (2) A general pilot-qualification set: budget, authority, need, timing, data
access, champion, IT constraints, past pilots and why they stopped. (3) Openers per persona: owner-entrepreneur, hired CEO,
technical/R&D director, CFO. (4) The twelve most likely objections from small Italian machinery makers ("we have no time",
"we already have a management system", "we are too small", "what if the startup disappears", "who owns the IP", "we are
not ready for AI", "it costs too much", "our customers do not ask for it"...) with a reply that answers the concern, not the
tone. (5) A "do not say" list: never cite a figure you cannot source; never mention a person's age; never imply we have
more data than we do; never promise a result.

**Acceptance.** Every seed hypothesis id has questions in both languages; a validator test; a one-page style guide. Questions
must be answerable by a person on a call in under a minute and must not reveal private data.

### D08 Golden set and evaluation harness
**Size M, P0. Needs: live_db_read, gg_input. Scope:** `diagnosis/eval/**`, `docs/diagnosis/golden/**`, `tests/test_diagnosis_eval.py`.

**Goal.** A way to know whether the diagnosis is any good, before anyone relies on it.

**Steps.** (1) Choose 12 companies, stratified: 4 shortlisted with rich data, 4 random, 2 distressed, 2 thin-data; write the
choice and the reason. (2) For each, produce an *analyst worksheet* (Markdown + CSV) with the facts a human analyst needs to
form their own view (financials, peers, roster, site excerpts) and blank fields: "top three needs you would test on a call",
"would you call? y/n", "what would you pitch", "what is missing". GG (or a human analyst) fills them: that is the
`gg_input`. (3) A harness `evaluate()` that runs the pipeline on the golden companies and reports: overlap of the engine's
top-3 with the analyst's; share of engine claims the analyst accepts; citation validity (every cited fact key exists and
supports the sentence); no simulated data used; determinism (same facts, same output); regression snapshot diff. (4) Until
labels exist, the harness runs the structural checks only and says so.

**Acceptance.** Worksheets for 12 companies; harness with tests on synthetic labels; thresholds written down for "good enough
to show GG" (initial: top-3 overlap >= 60%, citation validity 100%, zero simulated facts).

### D09 Data shopping list
**Size S, P1. Needs: live_db_read. Scope:** `docs/diagnosis/DATA_SHOPPING_LIST.md`.

**Goal.** The exact extra columns GG should pull in the next AIDA export (and any other one-off data purchase) to make the
diagnosis sharp, in priority order, with the Italian AIDA label, the reason, the hypotheses it feeds, and what the importer (D35)
must do with it. Candidates: trade receivables, inventories (raw/WIP/finished), trade payables, bank debt, TFR, tangible fixed
assets, depreciation, R&D / development costs, revenue by geography or export flag if the licence has it, five years of
history instead of three, the registry's detailed activity description (*oggetto sociale* / *attivita prevalente*), published
website/email/PEC/telephone, legal representatives with roles, any credit rating or Z-score the licence includes, certifications
if available. Compare against what is already imported (`aida_import.py` `SERIES_SPEC` and the `x_*` extras) and against
`Wayland/AIDA Columns.xlsx` so nothing already held is asked for twice. Say which are blocked by the licence and what to ask the
vendor.

**Acceptance.** A table ordered by diagnostic value per cost, each row with label, reason, hypotheses fed, already-held? y/n;
a half-page "how to pull it" for GG.

### D10 Free-source feasibility study
**Size M, P1. Needs: web. Scope:** `docs/diagnosis/SOURCE_STUDY.md`.

**Goal.** Find and rank the free, legal, machine-readable sources that fill diagnosis gaps, so D34 builds the best three, not
the first three. Assess at least: Registro Imprese lists of *PMI innovative* and *startup innovative*; Accredia certified
organisations (ISO 9001/14001/45001/50001); ANAC and OpenCoesione open data (public contracts, grants); CORDIS (EU projects);
UIBM patents and trademarks; association member directories (Federmacchine, ANIMA, UCIMA, ACIMAC, UCIMU, ACIMIT, UNACEA: members
are both a product signal and a warm route); trade-fair exhibitor lists (SPS Italia, Ipack-Ima, Cibus Tec, Plast, MECSPE, Tecna,
Marmomac, Simei, Eima, Lamiera, A&T); ISTAT and Eurostat production and orders series; Banca d'Italia and Confindustria Centro
Studi publications; World Bank / GME price series for metals and energy; Wikidata.

**Per source:** terms of use and robots verdict; format; join key to a company (VAT, name, domain); coverage of this cohort
(test on 30 companies: how many are found); freshness; cost; which hypotheses and triggers it feeds; build effort. End with a
ranked recommendation and the three to build.

**Acceptance.** >= 15 sources assessed with evidence, the 30-company coverage test for the top eight, a ranked table.

### D11 Business profile extractor
**Size L, P0. Needs: scraper_repo, local_model, live_db_read. Scope:** `@scraper-repo`, `diagnosis/readers/profile_*.py`,
`scripts/export_profile_input.py`, `scripts/import_business_profile.py`, `tests/test_diagnosis_profile.py`.

**Goal.** Know what each company makes, for whom, and how it sells: the `prof.*` keys. This is the single largest gap: the
tool knows a company's margins but not whether it builds filling lines or ball valves.

**Approach.** Extend the existing Scraper tool `tools/site_evidence/` (T25/T26: standard-library only, resumable, supervisor,
RAM gate, verbatim-quote verification, Wayback rescue) with a *profile* mode: claims for machine types, end-markets,
business model cues (`su misura`, `progettazione personalizzata`, `chiavi in mano` vs catalogue), aftermarket/service/spares,
remote assistance, export markets, named customers and partners, certifications claimed, R&D claimed, software developed in-house,
positioning sentence, sustainability claims. Same safety rule: a model's answer is accepted only if its quote is found on the page
by code and passes the claim's own rules (a customer must be a named organisation; an export market must be a country or
region; `prof.export_share_pct` only if the page states a percentage of sales). Use the D04 vocabulary of machine types and
end-markets when it exists, free text with a note when it does not.

**Vienna side.** `scripts/export_profile_input.py` (company list with domains), `scripts/import_business_profile.py` (dry run by
default; stores `RawImportRecord(dataset_name="crawler_business_profile")` only, no `SignalRecord`; backs up what it replaces),
and `diagnosis/readers/profile_reader.py` turning the blob into grade-B facts.

**Acceptance.** A pilot on 20 companies including the shortlisted ones; by-hand audit of every accepted claim: precision >= 85%
(report per claim type); the measured share of sites the reader cannot see (expect ~15% JavaScript-only/thin: that is D33);
speed and RAM behaviour; the full run is *prepared* (command, estimate) but started only with GG's approval (see D13).
Commit in the Scraper repo separately and say so. Do not touch `worker/`, the bundle or `worker_shim/`.

### D12 Hypothesis engine core
**Size M, P0. Scope:** `diagnosis/engine.py`, `diagnosis/grading.py`, `tests/test_diagnosis_engine.py`.

**Goal.** The machinery every hypothesis author uses, so that 30 rules written by four agents behave consistently.

**`grading.py` (the rubric, versioned).** Helpers `strength_from_percentile(p, direction, lo=50, hi=95)`,
`ev(fact_key, role, text)`, `coverage(facts, spec)` and `missing(facts, spec)`; `confidence(...)` from: best evidence grade,
number of independent sources (distinct `Fact.source`), freshness of `as_of`, share of `requires` known, contradictions, and
(later) the layer reliability scores from D25; `status(strength, confidence, coverage, contradicted)` implementing:
*supported* needs strength >= 60, >= 1 supporting evidence of grade A/B (or two independent C), coverage >= 0.6, none contradicting;
*plausible* strength >= 40 and coverage >= 0.4; *weak* strength >= 20; *contradicted* when contradicting evidence outweighs;
*not_assessable* when coverage < 0.3 or a required fact is unknown. Confidence high needs >= 2 independent sources, best grade
A/B and freshest input <= 18 months; medium one A/B or two C; low otherwise. A claim resting on grade D alone is never above weak.

**`engine.py`.** `diagnose(facts, ctx) -> Diagnosis`: runs `registry.run_hypotheses`, ranks (supported > plausible > weak, then
`strength x confidence weight`; positive hypotheses in their own list), collects `unknowns` ranked by value of information
(how many ranked hypotheses a missing fact would sharpen x their strength x how cheaply it is obtained: crawl vs call), and writes
`src.coverage_pct`. Output is JSON-serialisable and versioned (`engine_version`, `rubric_version`).

**Acceptance.** Table-driven tests for every status/confidence boundary; a synthetic-facts scenario for each of the four states;
determinism; the rubric is a plain data structure at the top of `grading.py` so GG's calibration (D24) can propose changes.
The engine imports nothing from `scoring` and never reads `score.*` as evidence.

### D13 Coverage drain
**Size M, P1. Needs: live_db_write, gg_approval, local_model. Scope:** `@live-db-write`.

**Goal.** Move the crawler coverage from ~30% of the cohort to the whole of it, for the layers the diagnosis uses, using only
tools that exist: `scripts/coverage_sweep.py` (T01) for Phase 1/4/7, and the Site Evidence Reader (T26) from its Vienna page.
This is an *operations* ticket: no new code.

**Steps.** (1) Record the before state with `scripts/indicator_gaps.py` and the live dataset counts. (2) Ask GG for the go-ahead in
chat, naming the machine that will run it and the expected duration. (3) Order: shortlisted first, then by NEED descending.
Sources that matter for the diagnosis: Company Website, Job Postings, Digital Maturity, Product Catalog, Directory Listing, TED.
Do **not** run the paid/simulated paths (Review, News Signals, Innovation Participation) and do not flip any gate flag.
(4) Monitor and resume; report errors by source; do not "fix" crawler code here (open a ticket). (5) Record the after state.

**Acceptance.** Before/after table for each dataset in the results note; the run can be resumed by the next agent from the note
alone; nothing outside the existing tools was run.

### R01 Release captain, wave 1
**Size S, P0. Scope:** `README.md`, `docs/diagnosis/WAVE1_ACCEPTANCE.md`.

**Goal.** Turn wave 1 into one coherent, tested whole before wave 2 builds on it. **Steps.** Merge state check
(`git fetch`, nothing stale on `diag/*` branches); run the full suite and report the count from `--junitxml`; run
`registry.discover(force=True)` and the board-consistency test; diff `contracts.py`/`factkeys.py` against the wave-0 commit
and confirm changes are additive; run `build_facts` over all 953 read-only and report per-reader coverage, time, errors;
read every results note, turn each "seam request" into a decision or a ticket; add the `diagnosis/` row and the three docs to
the README repo map and the new env vars to `.env.example` (ask first if an env var is a secret name); write
`WAVE1_ACCEPTANCE.md` with go / no-go for wave 2 and the open issues. **Acceptance.** The note exists, numbers are real,
wave 2 can start from it.

### Wave 2: the diagnosis itself

### D14 Market radar
**Size L, P0. Needs: web. Depends: D04. Scope:** `diagnosis/market/**`, `diagnosis/models_market.py`, `scripts/market_radar.py`,
`tests/test_diagnosis_market.py`.

**Goal.** A continuously refreshable store of dated, sourced items about the sub-sectors, end-markets, rules, input costs and
trade conditions that move Italian machinery makers, plus the statistical series behind them.

**Collectors (keyless, legal, rate-limited):** Google News RSS for the D04 search queries; association press pages and RSS
(UCIMU, UCIMA, ACIMIT, ANIMA, Federmacchine, ACIMAC, Confindustria Centro Studi); ISTAT/Eurostat series (industrial production,
turnover and new orders for NACE C28 and the 4-digit codes where published); Banca d'Italia and sector reports that are public;
EU Commission and Gazzetta feeds for regulation; World Bank Pink Sheet and GME for metals and energy. Skip anything paywalled.

**Storage.** `dx_market_items` (kind news|stat|reg|price, sector tags, end-market tags, theme: demand/orders/regulation/incentive/
technology/competition/input_cost/trade_geopolitics/labour/finance, direction -1/0/+1 with `direction_method`, headline, excerpt
<= 300 characters verbatim, url, published_on, source, grade, hash for dedupe) and `dx_market_series`. Direction is set by rule
when there is a number (sign of the change) and by a classifier with a quote when there is not; the method is stored.

**Acceptance.** For each of the top 10 ATECO codes >= 20 items from the last 90 days across >= 3 themes; every item has url and
date; deduplicated; a by-hand audit of 40 items: sector tag correct >= 90%, direction correct >= 85%; `mkt.orders_index` available
for at least the sub-sectors that publish one (UCIMA packaging, UCIMU machine tools, ACIMIT textile are known to). Include the
known recent readings in the results note as a sanity check (UCIMU Q2 2026 orders -25.8%; UCIMA H1 2026 orders -2.5%;
ACIMIT Q2 2026 orders 46.6 index, H1 -4%) **after confirming them at source**.

### D15 Company to market exposure
**Size M, P0. Depends: D14, D04. Scope:** `diagnosis/market_exposure.py`, `diagnosis/readers/derive_market.py`,
`tests/test_diagnosis_market_exposure.py`.

**Goal.** The step the user named: *cross the company's product types with the news.* For each company, `mkt.headwinds`,
`mkt.tailwinds`, `mkt.exposure_end_markets`, `mkt.orders_index`, `mkt.input_cost_trend`, `mkt.district`.

**Method.** Sub-sector from ATECO + the playbook; end-markets and machine types from `prof.*` when D11 has run, else the playbook's
defaults marked grade D ("typical for this code, not confirmed for this company"); match items by sector and end-market tags
and product keywords; keep at most five per side with the reason each matched; wording is "relevant to", never "caused".
`explain()` returns why an item matched. **Acceptance.** Precision audit of 50 matches >= 85% judged relevant; a company with
no profile gets only sector-level items and says so; unit tests for the matching rules.

### D16 Trigger engine
**Size L, P0. Depends: D01, D02. Needs: live_db_read. Scope:** `diagnosis/triggers/events_*.py`, `diagnosis/models_triggers.py`,
`tests/test_diagnosis_triggers.py`.

**Goal.** "Why now": verified, dated events from what we already collect, as `Trigger`s and `trg.*` facts.

**Event families.** Leadership: appointments/resignations in the roster in the last 12 months, new CEO. Accounts: new filing
(`last_accounts_year` moved, accounts close date). Money: state-aid and EU funding awards by date; TED awards by date. Hiring:
a jump in open roles between two crawls (only with two snapshots). Ownership/group change from the AIDA blob. Certification and
trade-fair appearances from directories. Verified news events from D17 once it exists (until then none: do not use unverified
headlines). Regulatory deadlines and incentive windows from D05. Closing events: distress procedure, layoffs/short-time work,
litigation in verified news.

**Method.** `dx_trigger_events` stores each event once (id, kind, text, dates, source, url, grade, weight, polarity,
first_seen); recency decay by kind (half-life documented); `trg.*` facts derived from the table. Incremental: re-running adds only
new events. **Acceptance.** On the 48 shortlisted every event has a source and a date; by-hand audit of 40 events: >= 90% correct
and about the right company; tests for each family, including the empty case.

### D17 Company news verifier
**Size M, P1. Depends: D02. Needs: live_db_read, llm. Scope:** `diagnosis/news_verify.py`, `diagnosis/models_news.py`,
`diagnosis/readers/verified_news.py`, `tests/test_diagnosis_news_verify.py`.

**Goal.** Only headlines proven to be about *this* company, typed by event. The audit in T25 read the 15 present rows produced
from headlines: 5 clearly right, 2 right company but not the indicator's meaning, 8 about someone else (Columbia University,
an arrested deputy). That is not usable as evidence.

**Method.** Candidate headlines from `crawler_news_rss` and fresh queries. Verify the entity with several independent cues:
legal-name tokens with suffix handling, province/city, the company's domain in the URL or text, product words from the profile;
reject homonyms (study the surname/person-mention filters in `adapters/google_news_rss.py` and T20). Classify the event with a
closed taxonomy (`contracts.TRIGGER_KINDS`) using the headline and snippet only, with a confidence; accept only above a
threshold. Store verified events and rejections with the reason.

**Acceptance.** By-hand audit of 100 candidate headlines: "about this company" precision >= 95% on accepted ones, recall
reported; the baseline of the old path measured on the same sample; zero fabricated entities; deterministic when the model is
unavailable (then only the rule-verified subset).

### D18 Hypotheses: commercial & growth, profitability & cost
**Size L, P0. Depends: D12, D03. Needs: live_db_read. Scope:** `diagnosis/hypotheses/com_*.py`, `diagnosis/hypotheses/prf_*.py`,
`tests/test_diagnosis_hyp_com_prf.py`. Implements COM-01..06 and PRF-01..06 of the masterplan section 6.

**For each hypothesis:** the `HypothesisSpec` (statement as it would be said on a call, `requires`, `helpful`, `disconfirm`,
`pilot_tags` from Part 6, `questions` ids from D07), a rule built on `grading` helpers and peer percentiles, evidence sentences
that quote the value and the comparison ("EBITDA margin 2.1%, 9th percentile of 62 ATECO 28.93 peers"), the contradicting-evidence
logic, and honest `not_assessable` when facts are missing.

**Calibration.** Run each rule over the live cohort read-only and report the share `supported/plausible/weak/not_assessable`. A
hypothesis supported for more than 40% of companies does not discriminate: tighten it or explain. One at 0% is either unreachable
data or a wrong rule: say which. Put the distribution in the module docstring. **Acceptance.** Unit tests per state per hypothesis;
the calibration table; a hand-check of the top five supported companies for each hypothesis against their raw numbers.

### D19 Hypotheses: operations & engineering, product & service model
Same shape and acceptance as D18. **Depends: D12, D03. Scope:** `diagnosis/hypotheses/ops_*.py`,
`diagnosis/hypotheses/prd_*.py`, `tests/test_diagnosis_hyp_ops_prd.py`. Implements OPS-01..05 and PRD-01..05. Most of these lean
on planned facts (`prof.*`, `dig.iiot_signals`, `fin.dio_days`): write the rules against the declared keys now; they will
report `not_assessable` until the data lands, which is correct and is itself the list of what to collect.

### D20 Hypotheses: finance & resilience, people & organisation, plus capacity-to-pay
Same shape and acceptance as D18. **Depends: D12, D03. Scope:** `diagnosis/hypotheses/fin_*.py`, `diagnosis/hypotheses/ppl_*.py`,
`diagnosis/readers/derive_pay.py`, `tests/test_diagnosis_hyp_fin_ppl.py`. Implements FIN-01..06 and PPL-01..05, and the `pay.*`
derivers: `pay.free_cash_proxy_k` = EBITDA - capex - interest (a proxy, say so), `pay.pilot_budget_band`, `pay.affordability`
with reasons. **Special care:** PPL rules use a manager's age; the evidence text shown to a user may say "management team
average age" but any text destined for the call script must not.

### D21 Hypotheses: regulation, risk & sustainability
Same shape and acceptance as D18. **Depends: D12, D05. Scope:** `diagnosis/hypotheses/reg_*.py`, `tests/test_diagnosis_hyp_reg.py`.
Implements REG-01..06 on top of the `reg.*` facts. Near-universal obligations (Machinery Regulation) discriminate only through
visible readiness and exposure features; do not mark every company "supported".

### D22 Pilot matcher
**Size M, P1. Depends: D06, D12. Scope:** `diagnosis/pilot_match.py`, `tests/test_diagnosis_pilot_match.py`.

**Goal.** From ranked hypotheses to a short list of `PilotConcept`s to pitch, each with why it fits, which prerequisites are met or
unknown (unmet ones become call questions), whether the budget band fits `pay.affordability`, and any `not_when` that applies. Also
validates that every `pilot_tags` used by a registered hypothesis exists in the library. **Acceptance.** Table-driven tests; no
concept is recommended when `not_when` fires; a cash-constrained company is steered to the low-budget or incentive-assisted concepts
and told why.

### D23 Reachability
**Size M, P1. Depends: D01, D02. Needs: live_db_read. Scope:** `diagnosis/reach.py`, `diagnosis/readers/derive_reach.py`,
`tests/test_diagnosis_reach.py`.

**Goal.** Who to call, how, in which language, and the warm route: `reach.*`. Decision-maker from the roster (role priority;
owner-manager detection by shared surname with the shareholders); published business contact channels from the crawled site
(contact page URL, generic company email, switchboard) read from existing blobs; language from the site languages and ownership;
warm routes = shared directories, associations, trade fairs, a parent company.

**Privacy.** Business contacts the company publishes only. No personal emails or phone numbers. No LinkedIn. **Acceptance.** On the
48 shortlisted: decision-maker identified >= 90%, a contact channel >= 70% (report the real figures), a by-hand check of 20.

### D24 Call debrief and calibration loop
**Size L, P1. Depends: D12. Scope:** `diagnosis/debrief.py`, `diagnosis/models_debrief.py`, `views/call_debrief.py`,
`tests/test_diagnosis_debrief.py`.

**Goal.** Make the tool learn. After a call GG records: outcome (no answer, gatekeeper, spoke, meeting booked, pilot interest, not
interested), and for each hypothesis in the pack the verdict (confirmed, partly, refuted, unknown), what was learned (becomes a
manual fact, grade B "stated by the company", with date), and which pilot concept resonated. Tables `dx_call_debriefs`,
`dx_hypothesis_reviews`, `dx_fact_corrections`. `render_debrief(db, company)` is a Streamlit form (wired into the dossier by D30).

**Calibration report.** Precision per hypothesis and per archetype (confirmed / (confirmed + refuted)), by tier; call-to-meeting
conversion; threshold suggestions; **no model fitting under 10 scored calls** (as `calibration.py` already says), only counts and
warnings. Link to `PilotOutcome` when a pilot starts. **Acceptance.** AppTest of the form; a report on synthetic data; a correction
to a fact changes the next pack.

### D25 Truth audit of the crawled layers
**Size L, P0. Depends: D02. Needs: live_db_read. Scope:** `scripts/truth_audit.py`, `docs/diagnosis/TRUTH_AUDIT.md`,
`diagnosis/data/layer_reliability.json`, `tests/test_diagnosis_truth_audit.py`.

**Goal.** Measure how often each crawled layer is right, so the diagnosis can weight it. Layers: company website, jobs, digital
maturity, product catalog, directories (confirmed vs possible), TED, news RSS, EPO, EU funding, Wappalyzer.

**Method.** Sample 40 companies per layer (stratified); build an *evidence card* for each claim (claim, source URL, the snippet that
supports it); verify programmatically where possible (fetch the URL, find the snippet) and mark the rest for human review in a
review sheet; compute precision per layer with a confidence interval; write `layer_reliability.json` (consumed by `grading` as a
confidence modifier) and list the top defects. Each defect worth fixing becomes a `board.py add` proposal. **Acceptance.** A
precision figure for every layer with its sample size; the review sheet for GG; the JSON; the defect list.

### D26 Synthesis engine
**Size L, P0. Depends: D12. Needs: llm. Scope:** `diagnosis/synthesis.py`, `tests/test_diagnosis_synthesis.py`.

**Goal.** Turn the structured diagnosis (facts, ranked hypotheses with evidence, triggers, pilot matches, reach, market items) into
the readable parts of the call pack: headline, "the company in 30 seconds", "why this might matter now", the three needs with their
evidence and their honest caveats, what we do not know, how to open. Italian and English.

**Rules.** (1) The model receives a closed set of numbered evidence items and may cite only those; every sentence carries its
`evidence_ids`; uncited sentences are dropped. (2) Every number in the output must appear in a cited item (tolerance for
rounding); otherwise the sentence is dropped. (3) A forbidden-claims filter: no individual's age, no revenue/headcount guesses,
no causal claims about the market, no legal conclusions. (4) A second *red-team* call lists what would make the main claims wrong
and what the evidence does not show; overclaiming is flagged. (5) A deterministic template renders the same sections from the
structured data with no model at all; that is the fallback and the test oracle. (6) Cache on facts fingerprint + prompt version;
cap tokens; use the provider chain in `config.assistant_llm_settings()` with its fallbacks, re-reading settings each call. Reuse the
patterns in `company_brief.py` (citation validation, JSON parsing).

**Acceptance.** On 20 diagnoses: zero uncited sentences, zero numbers absent from the evidence, the red-team section present;
fallback output identical in structure; tests with a fake provider for cited, uncited, wrong-number, forbidden-claim and
provider-down cases.

### R02 Release captain, wave 2
**Size S, P0. Scope:** `@bundle`, `docs/diagnosis/WAVE2_ACCEPTANCE.md`. As R01 for wave 2, and: run the whole pipeline (facts ->
hypotheses -> triggers -> pilots -> synthesis fallback) over the 48 shortlisted companies offline; list the distribution of
hypothesis states; produce five sample packs as Markdown for GG to read; rebuild and commit the worker bundle **only if** a
crawler or `worker/*` changed in this wave (`python scripts/build_worker_bundle.py`); state go / no-go for wave 3.

### Wave 3: the product

### D27 Company archetypes and call tiering
**Size M, P0. Depends: D18, D19, D20, D21, D16, D23. Scope:** `diagnosis/tiering.py`, `diagnosis/archetypes.py`,
`tests/test_diagnosis_tiering.py`.

**Goal.** (a) Archetypes (masterplan section 7) assigned by explicit rules over hypotheses and facts, each with its pitch template.
(b) The call tier A / B / C / HOLD / EXCLUDE as explicit rules over six separate dimensions: NEED, READINESS (from `score.*`, read
as context), evidence of need (supported hypotheses and their grade), timing (triggers), reachability, pilot fit, and evidence
quality (coverage). **No arithmetic blend** of NEED and READINESS or of anything else. Thresholds live in
`diagnosis/data/tiering.json` so GG can edit them; every tier comes with `tier_reasons` in plain words. HOLD means "enrich first"
and names the crawl or fact that would unlock a decision. EXCLUDE reasons: open distress procedure, dissolved, a subsidiary whose
parent decides.

**Acceptance.** Cohort distribution report (how many A/B/C/HOLD/EXCLUDE; aim for A to be a call list a person can actually work,
tens not hundreds); boundary tests; a company moves tier when its facts change and says why.

### D28 Call Pack assembly and renderers
**Size L, P0. Depends: D26, D22, D16, D07, D27. Scope:** `diagnosis/callpack.py`, `diagnosis/render/**`, `tests/test_diagnosis_callpack.py`.

**Goal.** Assemble a `CallPack` per masterplan section 8 and render it: a structured dict for the app, a self-contained one-page-plus-
appendix HTML (style of `dossier_export.py`, prints to PDF from any browser), Markdown, and DOCX if `python-docx` is importable
(otherwise skip and say so). Every claim in the render carries a footnote id resolving to a fact or URL ("sources" section). The
"unknowns" section lists what to ask and why it matters; the "do not say" box comes from D07; stakeholder data shows the role, never
the age. Italian and English.

**Acceptance.** Golden-file tests for three synthetic companies; the HTML is self-contained and escapes every dynamic string;
a pack for a company with almost no data is short, honest and says what to collect first.

### D29 Portfolio runner
**Size M, P0. Depends: D28. Needs: live_db_write, gg_approval, llm. Scope:** `diagnosis/portfolio.py`, `diagnosis/models_runs.py`,
`scripts/diagnose_portfolio.py`, `tests/test_diagnosis_portfolio.py`, `@live-db-write`.

**Goal.** Diagnose the whole cohort cheaply and repeatably. Stages: *screen* (facts + hypotheses + tier: deterministic, free, all
953), *diagnose* (add triggers, pilots, reach), *pack* (LLM narrative, only for tier A/B up to a cap, default 60). Results in
`dx_diagnoses` (company, facts fingerprint, engine/rubric/spec versions, JSON, tier, archetype, model, created_at) and
`dx_call_packs`. Incremental by fingerprint and version; resumable; honours `resource_governor` for model calls; `--dry-run` against
a SQLite copy first. **Acceptance.** Dry run on a copy of the cohort with timings and the tier distribution; a second run does no
work; `--max-llm-calls` is enforced; the live write happens only after GG sees the dry run and says yes.

### D30 Origination UI
**Size L, P0. Depends: D28, D29, D27, D24. Scope:** `views/origination.py`, `views/callpack_view.py`, `app.py`,
`views/company_dossier.py`, `dossier_nav.py`, `tests/test_diagnosis_ui.py`. (The only ticket allowed to edit these files.)

**Goal.** (1) A **Call Pack** section in the Company Intelligence dossier: the pack on screen, language toggle, regenerate, export
buttons, evidence drill-down (click a footnote -> the fact, its grade and source), the debrief form (D24). (2) An **Origination**
view: the portfolio list with tier, archetype, top-3 hypotheses, why-now, reach and evidence quality; filters by tier, archetype,
hypothesis, region, ATECO, shortlist status; select a company -> open its dossier on the Call Pack; export the call list to
Excel/CSV. (3) Respect the "one home per feature" decision (commit `32a7fc9` cut the sidebar from 12 pages to 9): propose in the
results note how this folds into the existing navigation, and GG decides before you add a page.

**Gotchas** (learned the hard way, see the project memory): Streamlit 1.56 drops dataframe selection on sort; `data_editor`
identity; a sticky header needs its wrapper; the preview needs a restart per edit; use `AppTest` for gated pages. **Acceptance.**
`AppTest` coverage of both views with a seeded SQLite; no page exceeds a few seconds on the live cohort (read from the stored
`dx_*` tables, not recomputed); existing UI tests still pass.

### D31 Startup roster import and matching
**Size M, P2. Depends: D22. Needs: gg_input. Scope:** `diagnosis/roster.py`, `diagnosis/models_roster.py`, `scripts/import_roster.py`,
`tests/test_diagnosis_roster.py`. GG's startup list as CSV (name, URL, category, stage, geography, references, pricing model); matching
to pilot tags; the pack lists "startups we could bring" per pilot. If GG has no roster, the ticket waits: `block` it with that reason.

### D32 Competitor suggestion
**Size M, P2. Depends: D03. Needs: live_db_read. Scope:** `diagnosis/competitors.py`, `tests/test_diagnosis_competitors.py`.
`suggest(db, company, n)` returns likely direct peers from the cohort by ATECO 6-digit, product-term overlap (`prod.product_types`,
`prof.machine_types`), size and region, labelled **suggested**; `confirm(db, company, suggestion)` writes a `Competitor` row and is
called only from an explicit human action. The competitor crawlers still run only for confirmed rows (the design decision in
`docs/CRAWLER_ROADMAP.md` stands). **Acceptance.** A by-hand check of 20 suggestions: >= 70% judged plausible direct competitors.

### D33 Thin and JavaScript-only websites
**Size L, P2. Depends: D11. Needs: scraper_repo. Scope:** `@scraper-repo`. For the ~15% of sites the standard-library reader cannot
see, add a browser-rendered fallback (reuse the Playwright harness of the digital-maturity and job crawlers; respect robots; polite
rate) whose text feeds the same reader. **Acceptance.** Measured coverage gain on the unreadable set; no change in precision on the
readable set; commit in the Scraper repo; if the worker bundle must change, hand that to R02 rather than rebuilding it here.

### D34 Source enrichers
**Size L, P1. Depends: D10. Needs: web, live_db_read. Scope:** `diagnosis/enrich/**`, `tests/test_diagnosis_enrich.py`. Build the three
sources D10 ranked highest, as importers into `RawImportRecord` blobs plus readers (or new `dx_*` tables), with a join on VAT/domain,
a match-quality grade, and fixtures-based tests. **Acceptance.** Coverage on the cohort reported; false-join rate measured on 30.

### D35 Extra-column importer
**Size M, P1. Depends: D09, D01. Needs: gg_input, live_db_read. Scope:** `scripts/import_extra_columns.py`,
`diagnosis/readers/extra_*.py`, `tests/test_diagnosis_extra_columns.py`. After GG pulls the extra AIDA columns (D09), import them by
header name (never by position, as `aida_import.py` does), dry run by default, backup what is replaced, store in the raw blob, and
add readers for `fin.receivables_k`, `fin.inventories_k`, `fin.payables_k`, `fin.bank_debt_k`, `fin.rd_costs_k`, `fin.export_revenue_pct`
and the derived `fin.dso_days`, `dio`, `dpo`, `ccc`. Waits (`block` with the reason) until the file exists.

### R03 Release captain, wave 3
**Size M, P0. Needs: live_db_write, gg_approval. Scope:** `docs/diagnosis/WAVE3_ACCEPTANCE.md`, `@live-db-write`. The live pilot: run the full
pipeline on 12 companies (6 shortlisted, 6 random), GG reads the packs, and the debrief form is tried on a real call if one is
scheduled. Measure: time to prepare a call, the share of top-3 hypotheses GG judges right, citation validity, anything misleading.
Write the rollout notes (how to run it weekly, what to refresh, known limits), update the project memory file list, and list what wave 4
(the calibration loop on real calls) should be.
