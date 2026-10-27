# Deep Intelligence Roadmap: from "numbers about 953 companies" to a dossier a broker can open a call with

*Written 2026-10-27 by Claude Code after a head-to-head test: a human-style manual research of one company (ASCO Filtri S.p.A.) against what this tool holds and what its crawlers produce on the same company.*

This is the single coordination file for the **collection-depth** program. Several AI agents work on it at the same time through a check-in / check-out board. If you were handed the prompt in [INTEL_AGENT_PROMPT.md](INTEL_AGENT_PROMPT.md), you are in the right place: read sections 5 to 7 (rules, protocol, boundaries), then `python scripts/intel_board.py status`, then claim a ticket.

Companion files: [INTEL_AGENT_PROMPT.md](INTEL_AGENT_PROMPT.md) (the one prompt for every agent) · `docs/intel/board.json` (who holds what; changed only by `scripts/intel_board.py`) · `docs/intel/golden/asco_filtri.json` (the human-found ground truth) · `scripts/intel_gap.py` (the scorer) · `scripts/intel_run.py` (run one collector live) · `intel/` (the shared code) · `docs/intel/results/` (every agent's trace).

---

## 0. In one page

**The goal.** Open any of the 953 companies and find, without a human researcher, what an expert would have found in 90 minutes: who really owns it, its history and recent events, what it makes and for whom, its certifications, who runs it and how to reach them, where it operates, which networks and programmes it belongs to, how it presents itself online, what it publishes, and a clear list of what is still unknown, every item with a source link and a quote.

**The test.** On ASCO Filtri S.p.A. (Binasco, 68 staff) a human found 42 checkable facts with a browser and public APIs ([golden file](intel/golden/asco_filtri.json)). The tool, on the same company:

| What is measured | Result |
|---|---|
| Facts the tool holds anywhere (database, AIDA imports, crawler output) | **11 of 42, 23% weighted by importance** |
| Facts that came from the crawlers/web layer itself | **2 of 42, 3%** |
| A fresh run of every crawler on ASCO (about 6 minutes) | 5 signal values, **2 golden facts** |
| Contact, products, certifications, events timeline, foreign footprint, networks, peers, documents | **0%** |

**What the human found that the tool does not have**, in order of what matters for a call: ASCO is a **100% subsidiary of Mott Corporation (US), itself bought by IDEX Corporation (NYSE: IEX) in September 2024**, after a 2019 private-equity buyout, so it is not an owner-managed Mittelstand-type firm and decisions sit abroad. Its real management (a General Manager, an export manager) is not the US executives listed on the registry board. It holds ISO 9001/45001, PED module H/H1, ASME U-Stamp and Achilles supplier credentials, publishes 21 PDFs, has subsidiaries in Abu Dhabi and India, a hydrogen programme listing, and a 2025 factory claim. The switchboard number, e-mail and PEC are published. None of this is in the tool's crawled layer.

**Why.** Not a lack of crawlers: eleven exist. The root causes are structural (section 3): the website crawler reads **2,000 characters of each of up to 15 pages into a fixed 12-field retail-shaped form** (store count, store locator), because it was sized for a free LLM tier of 8,000 tokens per minute; there is **no web-research layer** (nothing searches the open web about the company); there is no place to store a free-form, cited fact; three crawlers have been **simulated forever** (news, reviews, innovation); and the user's many LLM keys cannot even reach the helper PCs (the worker drops numbered fallbacks).

**The plan.** A new package `intel/` with a **citation-bound evidence ledger** and a **one-file collector contract**, 34 tickets in three waves checked in and out through `scripts/intel_board.py`, and a **golden benchmark** that turns "is it as good as a human?" into a number every ticket must move. Targets: **at least 70% weighted web-only coverage on ASCO and at least 60% on companies the developers never saw**, with at least 95% of stored facts supported by their own quote. Multi-PC operation is preserved by design (section 4).

**Already built (tickets I01, I02, done):** the golden file and scorer, the ledger and collector contract, a polite fetcher, a single-collector runner, this board, and a working reference collector that fixes one real false negative.

---

## 1. The test in detail: a human against the tool

### 1.1 How the human research was done
Public web search and page fetches only, plus the public APIs of GLEIF and SEC EDGAR, plus HTTP headers: about 25 searches and 15 page reads, no paid database. Every finding is in the golden file with its URL, a grade (A filed/official, B the company's own page, C a third party, D inference) and a weight (3 decisive for approaching the company, 1 nice to have).

### 1.2 Result by category (live database, everything the tool holds)

| Category | Facts | Held | Comment |
|---|---:|---:|---|
| identity | 5 | 3 | VAT, incorporation date, ATECO yes; Binasco address and share capital no |
| contact | 2 | **0** | Switchboard, e-mail, PEC: nothing. For a tool whose output is "who do we approach" this is the biggest hole |
| ownership | 6 | 2 | AIDA holds Mott and IDEX; nothing turns it into a warning, no deal history, no 2019 PE buyout |
| events | 3 | **0** | No timeline at all: 2019 buyout, 2022 Mott, 2024 IDEX, new factory, leadership change |
| financials | 3 | 2 | Three AIDA years only; the 2019-2024 history a human found on a public page is missing |
| people | 7 | 2 | Registry directors and signatories yes; the General Manager, ex-MD, export manager no |
| products | 4 | **0** | The crawler stored process fluids ("syngas", "ink jet") as "product lines" |
| certifications | 3 | **0** | ISO, PED, ASME, Achilles: no collector exists |
| locations | 1 | **0** | Abu Dhabi, India, France, Mexico entities |
| networks | 2 | **0** | H2IT, IPCEI Hydrogen listing, ACHEMA |
| digital | 4 | 2 | LinkedIn page and "no careers page" yes; the IIS/ASP.NET stack no (false "absent") |
| peers, documents | 2 | 0 | |

### 1.3 What the crawlers did on the same company (fresh run, throwaway database)

| Lane | Seconds | Result | Verdict |
|---|---:|---|---|
| Company website | 90 | 17 "product lines" (12 on the previous run) | Non-deterministic; the items are applications and fluids, not products |
| Product catalog | 47 | 0 products | Honest: engineered products, no catalog |
| Job postings | 7 | 0 roles, "no careers page found" | Honest absence |
| Reviews | 34 | **error**: "could not search any configured source" | Never live |
| Directory listing | 89 | `trade_fair_participation = 1` | **Wrong**: it is a Kompass business-directory listing, not a trade fair |
| Digital maturity | 57 | LinkedIn page found; redesign unknown; `cms_platform` null | Thin |
| News signals, Innovation | 0 | simulated (no key) | Never live |
| Google News RSS | 1 | 5 items, none about the ownership history | Recency-window search, not research |
| Wappalyzer | 1 | `tech_stack_intensity = 0, absent` | **False negative**: site is IIS 10 + ASP.NET; the detector knows 18 signatures (WordPress, Shopify ...) |
| Own-site scrape (management) | 17 | **error**: "No About/Team page found at common paths" | The site's page is `/Home/Company`; paths are guessed |

### 1.4 Portfolio-level evidence (live database, 953 companies)
* **31 indicators have no real value for any company; 17 more are populated for fewer than 18 companies.** Financial indicators are about 100% (AIDA); everything qualitative is near zero.
* Measured error rates of the existing sources (`source_health`, calls / errors): Own-Site Scrape 1192 / 921 (**77%**), EPO OPS 900 / 653 (**73%**), Wappalyzer 1192 / 344 (29%), TED 541 / 122 (23%), Digital Maturity 924 / 194 (21%), Directory Listing 936 / 171 (18%), Job Postings 925 / 124 (13%), Product Catalog 556 / 68 (12%), Company Website 928 / 89 (10%). News Signals, Innovation and Review crawlers run in simulated mode.
* **Ownership**: 533 of 953 companies have a corporate shareholder holding 50% or more and 139 are 50%+ foreign-owned (caveat: a corporate holder can be a family holding, which is why ownership must be *classified*, not just counted). A broker who calls a controlled subsidiary as if it were an owner-managed firm wastes the call.

---

## 2. Gap analysis: what is missing, grouped by root cause

Each root cause carries the evidence and the tickets that close it.

| # | Root cause | Evidence | Tickets |
|---|---|---|---|
| R1 | **The website crawler is a score-field extractor, not a profile builder.** 2,000 characters per page, at most 15 pages, one LLM call per page into a 12-field form built for retailers (`store_count`, `store_locator_present`). Sized for Groq's free 8,000 tokens/minute. | `Scraper/crawlers/company-website-crawler/src/llm.ts` (MAX_EXCERPT_CHARS), `types.ts`; ASCO: certifications, history, entities, contacts all on the pages, none extracted | I10, I11 |
| R2 | **There is no web-research layer.** Nothing searches the open web about the company. DuckDuckGo is used once, to find the homepage. Google News RSS is a 24-month recency search (5 noisy hits for ASCO). NewsAPI needs a key that does not exist. | Section 1.3; golden OW-02..05, EV-*, PE-02..05 all came from searches | I05, I13, I34 |
| R3 | **No cited free-form fact store.** `SignalRecord` is numeric-indicator-shaped; crawler blobs are per-crawler JSON. Nothing holds "fact + source URL + quote + date + confidence", detects conflicts (66 / 68 / 74 employees; founded 2002 vs "40 years"), or says "looked and found nothing". | Section 1.2; the ledger (I02) is the fix | I02 (done), I06, I40 |
| R4 | **Facts held but not acted on.** AIDA knows ASCO is 100% Mott and IDEX is level-2 controller; the dossier lists the table; no flag, no warning in the Target Matrix, no use in outreach. | Golden OW-03, OW-06; 533 / 139 portfolio counts | I12, I42 |
| R5 | **The LLM layer cannot use many keys.** Primary + one fallback reach remote PCs; `llm.ts` supports five numbered fallbacks but `worker.mjs` ENV_ALLOWLIST forwards only the unnumbered one. News and innovation crawlers call Anthropic directly. No routing by task, no budgets, no usage ledger. | `worker/worker.mjs` ENV_ALLOWLIST; `config.py` `_numbered_llm_fallbacks`; `tests/test_config_llm_fallbacks.py` | I04 |
| R6 | **Documents are not read.** ASCO publishes 21 PDFs (9 certificates, 12 manuals); the file names carry validity dates (ISO 9001 certificate to 2026-06-29, now past). Only sustainability reports are ever opened. | Golden CE-01..03, PR-03, DC-01 | I10, I11, I15 |
| R7 | **No people and contact intelligence.** Only the registry board (here: US executives of the parent). The operating General Manager, ex-MD, export manager and the published switchboard / e-mail / PEC are absent. LinkedIn crawler is off by default. | Golden CT-01/02, PE-02..05 | I14, I20 |
| R8 | **No event timeline.** "recent_ma_activity" is populated for 15 of 953 companies. No dated list of deals, plants, subsidiaries, leadership changes. | Golden EV-*, OW-02, OW-04 | I13, I31 |
| R9 | **Existing producers are partly wrong, and wrong silently.** Kompass counted as a trade fair; fluids counted as product lines; 12 vs 17 on consecutive runs; Wappalyzer 18 signatures; retail fields on B2B; path-guessing for About pages. | Section 1.3 | I18, I33, I35 |
| R10 | **Three crawlers never ran live; two others fail most of the time.** | Section 1.4 | I19, I33, I34 |
| R11 | **Nothing learns whether the output is right.** No golden set, no precision audit, no regression check on the crawled layer. | This document is the first | I03, I43 |
| R12 | **Research is one-shot and linear.** Each crawler runs once with a fixed plan. A human follows leads: owner found, then search the owner; name found, then search the person. | Manual session | I30, I31 |
| R13 | **What is fetched is not always shown, and some shown items are mislabelled.** The dossier renders `crawler_*` blobs section by section; there is no evidence/provenance view, no conflicts panel, no coverage meter, Kompass appears under trade fairs. | `views/company_detail.py`, `views/company_dossier.py` | I40, I35 |

---

## 3. Target architecture

```
                    +------------------ orchestrator (I30) -------------------+
 company row -----> |  plan from checklist -> run collectors -> verify -> merge |-----> ledger
 + AIDA blobs       |  <- follow-up queries for gaps (iterative deepening)  ->  |        (RawImportRecord
                    +-----------------------------------------------------------+         crawler_deep_profile)
   collectors (one file each, intel/collectors/*.py)          shared services (intel/)
   site-headers (I02)  deep-site (I10)  documents (I11)       ledger.py   Fact/Evidence/Ledger, quote rule
   ownership (I12)  events (I13)  people/contacts (I14)       fetch.py    polite fetch, robots, PDF   (I05)
   certifications (I15)  networks (I16)  hiring (I17)         search.py   many providers              (I05)
   tech-stack (I18)  ip (I19)  registry-it (I20)              llm_pool.py many keys, routing, budgets (I04)
   customers (I21)  peers (I22)                               verify.py   entity match + grounding     (I06)
```

**The ledger** (`intel/ledger.py`, built). One blob per company in `raw_import_records` (dataset `crawler_deep_profile`, no schema change). A *Fact* has a key (`group.name`), a value, a grade A-D, a confidence, `as_of`, the collector that produced it and one or more *Evidence* items (URL, verbatim quote, retrieval time, content hash). Rules enforced in code: a web fact without a URL and a quote of at least 12 characters is refused; the collector must check the quote against the page it fetched (`quote_in_text`), so a model's paraphrase never gets in; an unverified inference cannot claim confidence above 0.5; `absent` means "actively looked, found nothing" while a missing fact means "never looked"; single-valued keys with two values surface as a **conflict**, never silently resolved. The same fact found twice merges its evidence. It holds no score.

**The collector contract** (`intel/collector.py`, built). A collector is one new file `intel/collectors/<name>.py` exposing `COLLECTOR = Collector(name, ticket, produces=("group.",), requires, run)`. `run(ctx)` receives the company, the ledger so far, a budget, and injected `fetch`, `search`, `llm`; it returns facts and the keys it checked and found absent. It never touches the database or `company_service.py`, so ten agents can add ten collectors without a merge conflict, and it is unit-testable with recorded pages. `scripts/intel_run.py` runs one collector live and `scripts/intel_gap.py` scores it.

**The verification gate** (I06). Entity match (is this page about *this* company: legal name or brand plus a discriminator such as VAT, city, domain or a known person; name collisions such as other firms called ASCO), quote grounding, date sanity, contradiction against the ledger, optional LLM judge with citations. Only verified facts reach the dossier by default.

**The orchestrator** (I30). Starts from a checklist (the golden categories), runs applicable collectors in parallel under the existing resource governor, measures coverage, asks an LLM planner for follow-up queries for the gaps, and stops on coverage target, budget or no new facts. Resumable and idempotent: every step is a unit any PC can run.

### 3.1 Multi-PC invariants (do not break; they are why the tool works on several computers)
1. Node crawlers run on any Crawler Worker; the worker holds **no database password** and only talks to the token-checked `vienna_worker_*` functions through the shim. A crawler gets everything through its **input file and allowlisted environment variables**, never from the app server's disk or database.
2. **Every new environment variable a crawler needs must be added to `ENV_ALLOWLIST` in `worker/worker.mjs`** or it is silently stripped on remote PCs. This has happened before (the vision-phase variables). I04 adds a parity test so it cannot recur.
3. Crawler entry points honour `SOFT_DEADLINE_SECONDS` and return what they have instead of being killed.
4. A new Node crawler package lives in the separate **Scraper repository** (`github.com/gmazzo98-glitch/Scrapers`, sibling folder `../Scraper`), is listed in the worker's `VERSION_INFO.crawlers`, and reaches helper PCs only when the **release captain** rebuilds and commits `worker_dist/vienna-crawler-bundle.zip` together with `worker_shim/app/`. Ticket agents do **not** rebuild the bundle (binary conflicts, and it updates the helper PC); wave release tickets I90/I91/I92 do, once per wave.
5. Python collectors that only call APIs (EDGAR, GLEIF, registries) may run where the orchestrator runs. **Heavy fetching (whole-site crawls, many pages) goes through the Node workers**, because the hosted app has no Node, no browsers and a US IP.
6. Do not assume the machine you develop on is the machine that runs the code: no absolute paths, no assumption Node or Playwright browsers exist, bounded memory and time.
7. Protocol number in `worker_hub.PROTOCOL` changes only for an incompatible task format; additive output fields do not need it.

---

## 4. Free and keyed sources verified in this study

| Source | Result of the test (2026-10-27) | Use |
|---|---|---|
| Company website + HTTP headers | works; headers revealed IIS 10, ASP.NET, Plesk | I10, I18 |
| SEC EDGAR full-text search (`efts.sec.gov/LATEST/search-index`, needs a descriptive User-Agent) | **works**; found IDEX's FY2024 and FY2025 10-K Exhibit 21 listing "Asco Filtri SpA, Italy" | I12 (parent filings) |
| GLEIF LEI API | ASCO and Mott **have LEIs but no parent relationship records** (direct/ultimate parent returned "not found"): do not rely on it alone | I12 (identity, LEI), I20 |
| GDELT DOC API | answers but throttles to one request per 5 seconds | I05 (news, multilingual) |
| Google News RSS | works; 5 items for ASCO, mostly noise; recency window | I05 (one of several) |
| DuckDuckGo HTML | used by the discovery crawler; needs politeness | I05 |
| PR Newswire, BeBeez, Filtsep, Hartford Business | readable pages with the deal facts | I13 |
| Atoka / Top Aziende public pages | revenue 2019-2024, headcount, "similar companies"; shareholders and balance sheets are paywalled | I20, I22 |
| IPCEI Hydrogen directory, H2IT | public entry with address and membership | I16 |
| Accredia, INI-PEC, Registro Imprese, UIBM, WIPO, ANAC | **not tested**; each has a feasibility step in its ticket (captchas and paywalls are likely) | I15, I19, I20 |

---

## 5. Ground rules (every agent, every ticket)

1. **Read `READ_FIRST_AGENT_STATUS_AND_OPEN_POINTS.md` first and update it before you finish** (repo rule). Edit its open points only through `python scripts/open_points.py`.
2. **Evidence or it did not happen.** Every fact in the ledger has a URL and a verbatim quote that your code verified with `quote_in_text()` against the text it fetched. No quote, no fact. Never emit a simulated value; "not found" is `checked_absent`, not an invented value.
3. **No scores, no signal changes, no new weights.** Collectors write ledger facts only. Existing `SignalRecord` producers change only through ticket I35, with GG's approval where a score moves. The NEED / READINESS axes are never blended (standing rule).
4. **One producer per fact key.** Reuse the names in `diagnosis/factkeys.py` where they exist (`own.*`, `prof.*`, `pub.*`, `reach.*`, `trg.*`). Your collector may only emit under its declared `produces` prefixes (enforced by `check_result`).
5. **Do not write to `company_people`** from the web. Management-age and diversity signals are computed from that table; web-found people go in the ledger (`people.*`).
6. **Do not game the benchmark.** Never hard-code ASCO, its VAT, its names or its pages in code or prompts. A fix must work on companies you have not seen. Every ticket is accepted on the golden company *and* on at least three others examined by eye (the hold-out set from I03 is reserved for release captains).
7. **Be polite and lawful.** Honour robots.txt (the fetcher does), identify yourself, respect per-host delays, no login walls, no CAPTCHA solving, no credentials of the user's accounts. Business contact points the company itself publishes only; never guess personal e-mails or phone numbers. Open `docs/intel/SECURITY_AND_SOURCES.md` (I44) before adding a source that is not in section 4.
8. **Secrets.** Never print, log, commit or paste a key or the `.env` contents. Show env var *names* only.
9. **Tests.** `python -m pytest tests --capture=no -q -p no:cacheprovider` (never bare `pytest`). `tests/conftest.py` points the database at a throwaway SQLite file; never set `VIENNA_TESTS_ALLOW_LIVE_DB=1` to make a test pass. Unit tests use recorded pages, not the network. The two known legacy failures (`test_worker_hub` bundle line endings, `test_access_control` order leakage) are OP-010, not yours.
10. **Live verification is part of done.** Run your collector on the golden company and on at least three others with `scripts/intel_run.py`, read the facts by eye, and record counts and your precision judgement in your results note.
11. **Git in a shared tree.** Work in your own git worktree and branch `intel/<ID>-<slug>`; never switch branches or edit files in the shared main checkout; never `git stash`, `rebase` or force-push; stage explicit paths; `git pull` (merge) before you push; merge to `master` yourself with `--no-ff` when the acceptance holds. The user's apps auto-reload from the main checkout.
12. **Scraper repo.** Changes under `../Scraper/crawlers/...` are committed and pushed **in that repository**, on a branch of the same name; a ticket that touches both repos makes two commits in two repos. Declare `@scraper-repo/<package>` in your write scope (the board already does).
13. **Stay inside your write scope.** The board locks the files a claimed ticket declares. If you need a file outside it, record why in your results note; if that file belongs to a claimed ticket, stop and `block` or coordinate through a note instead.
14. **Problems you find become tickets.** `python scripts/intel_board.py add --title ... --files ... --why ...`, never a silent fix outside your scope.
15. **Decisions that are GG's stay GG's.** Tickets marked `gg_input` / `gg_approval` need an answer in the session before the step they gate. Do not guess; ask, and write the answer into your results note and, if it is a standing decision, into the Decisions log of the status file.

---

## 6. Protocol (check in / check out)

State lives in `docs/intel/board.json` and changes only through `scripts/intel_board.py`, which is `scripts/board.py` (the diagnosis program's engine) pointed at its own file. Every change is a compare-and-swap push to `origin/master`, so two agents cannot both win a ticket; a claim also locks the files in the ticket's write scope, and `@bundle`, `@live-db-write`, `@scraper-repo/<package>` are exclusive resources.

```
set INTEL_AGENT=claude-7f3a           (PowerShell: $env:INTEL_AGENT="claude-7f3a")   name = tool + 4 hex
python scripts/intel_board.py preflight            can this session really do the work? run it BEFORE claiming
python scripts/intel_board.py status               who holds what, stale claims
python scripts/intel_board.py next                 what you could claim now, and why not the rest
python scripts/intel_board.py claim [ID]           no ID = it picks one for you (priority, then wave, spread across agents)
python scripts/intel_board.py heartbeat ID         at least hourly; a claim silent for 8 hours can be taken over
python scripts/intel_board.py done ID --result docs/intel/results/ID.md     after your work is on origin/master
python scripts/intel_board.py release ID --note "..."    give a ticket back
python scripts/intel_board.py block ID --reason "..."    stop and explain
python scripts/intel_board.py add --title "..." --files a b --depends-on I05 --why "..."
```

**Prove it before you check in:** `python scripts/intel_gap.py --golden docs/intel/golden/asco_filtri.json --db --require-ticket Ixx` for registry-backed facts, or score a fresh `intel_run.py --out run.json` with `--corpus-file run.json --web-only`. It exits non-zero until every golden fact assigned to your ticket (listed in `board.json` under `golden`) is captured. That is the floor; your ticket's acceptance list says what else must hold.

**The results note** (`docs/intel/results/<ID>.md`, the trace every other agent reads) has these headings: *What I built* (files, one line each), *Verified live* (companies, commands, counts, your precision judgement from reading the facts), *Golden score before / after*, *Decisions taken and why*, *Not done / open points*, *New tickets proposed*, *What the next agent must know*. Append only to your own note.

**Nothing in the status file's open-points list is edited by hand**; use `scripts/open_points.py`. This program's board does not replace it.

---

## 7. Boundaries with the Diagnosis program

`docs/DIAGNOSIS_MASTERPLAN.md` (38 tickets, board `docs/diagnosis/board.json`) builds the **synthesis**: hypotheses, call packs, tiering. This program builds the **collection depth** it needs. They meet at the fact keys.

| This program produces | The diagnosis ticket that consumes it | Coordination rule |
|---|---|---|
| Ledger facts, all collectors | D02 (readers B) should add a reader for `crawler_deep_profile` | I02 defines the shape; D02 reads it, never rewrites it |
| `prof.*`-style business profile (I10) | D11 business profile extractor (local model, site-evidence hub) | Check D11's status first. If DONE, reuse its corpus and extend; if OPEN, I10 emits ledger facts and D11 reads them. One site read, not two |
| `ownership.*` (I12) | D01 (`own.*`), D27 archetypes | Reuse the `own.*` names already declared |
| `people.*`, `contact.*` (I14) | D23 reachability | D23 ranks and routes; I14 finds and verifies |
| `events.*` (I13, I31) | D16 trigger engine, D17 news verifier | I13/I06 verify *collection*; D16/D17 type and date *for diagnosis*. Do not build the verifier twice: read D17's result note |
| `peers.*` (I22) | D32 competitor suggestion | I22 collects candidates, D32 ranks, a human confirms |
| thin / JavaScript-only sites (I05 browser fallback) | D33 | Share the Playwright fallback |
| registry feasibility (I15, I20) | D10 free-source feasibility study, D34 enrichers | Read `docs/diagnosis/results/D10.md` if it exists; do not repeat the study |
| golden set + precision audits (I03, I43) | D08 golden set, D25 truth audit | Use one format where possible; link, do not fork |

Files both programs may touch (`views/company_dossier.py`, `views/target_matrix.py`, `worker/*`): the boards cannot see each other's locks. Keep edits small and additive, `git pull` right before you push, and name the other ticket in your commit message.

---

## 8. Decisions for GG (nothing blocks the first waves; these gate specific tickets)

| # | Decision | Needed by | Recommendation |
|---|---|---|---|
| G1 | Allow one **keyed search API** (Brave, Serper, Tavily or SerpAPI) on top of the free providers? Roughly tens of USD per month at the planned volume. | I05 (optional extension) | Start free; add one provider only if the free layer misses the golden facts |
| G2 | **LinkedIn**: public company pages read through search snippets only, or authenticated crawling with a session cookie (breaches LinkedIn's terms, account-ban risk)? | I14, I17 | Public pages and snippets only; keep `LINKEDIN_CRAWLER_ENABLED=false` |
| G3 | **Contact data policy**: confirm collecting only business contact points a company itself publishes, with source and date, no guessed personal addresses, with a suppression list. | I14 | Confirm |
| G4 | **Daily LLM budget** and whether the full 953-company run may start. | I32 | Cap per company and per day; run the shortlist (48) first |
| G5 | Should a **controlled subsidiary of a large group** be flagged, de-prioritised, or excluded in the Target Matrix? (Flag only, no score change, is what I42 builds.) | I42 | Flag now; decide exclusion after seeing the numbers |
| G6 | Approve **score-moving fixes** to existing producers (Kompass is not a trade fair; tech-stack false absents; fluids as product lines). | I35 | Approve after seeing the before/after counts the ticket produces |
| G7 | **Rollout cadence** of crawler bundle updates to the helper PC (release captains rebuild once per wave). | I90 | Once per wave |

---

## 9. The tickets

State is on the board, not here. `python scripts/intel_board.py status` shows it. Every ticket's write scope is in `board.json`; the golden facts a ticket must capture are listed there under `golden`.

| Wave | Tickets |
|---|---|
| 0 Foundations | I01 golden + scorer (done), I02 ledger + contract + board (done), I03 more golden companies, I04 LLM pool, I05 web research layer, I06 verification gate |
| 1 Collectors | I10 deep site reader, I11 documents, I12 ownership, I13 events, I14 people and contacts, I15 certifications, I16 networks, I17 hiring, I18 tech stack, I19 IP, I20 Italian registries, I21 customers, I22 peers, I93 verification gate v2, I90 release captain |
| 2 Orchestration and reliability | I30 orchestrator, I31 freshness, I32 portfolio batch, I33 own-site reliability, I34 live reviews / innovation / news, I35 producer audit, I91 release captain |
| 3 Surfaces and quality | I40 dossier UI, I41 research brief, I42 qualification flags, I43 evaluation CI, I44 security and sources, I45 worker fleet, I92 release captain |

**Targets the whole program is measured against** (computed by `intel_gap.py` on golden companies, web-only, hold-out companies only for the second line): weighted coverage on ASCO at least 70% (registry plus web at least 85%); on hold-out companies at least 60%; precision at least 95% of stored facts supported by their own quote (audit of 30 random facts per company); no run exceeds its token and time budget; a run killed half-way resumes without duplicates; the same input run twice gives at least 90% the same facts.

### Ticket details

#### Wave 0: foundations

### I01 — Golden benchmark and gap scorer (ASCO Filtri)  *(DONE, founder)*
**Built:** `docs/intel/golden/asco_filtri.json` (42 cited facts, weights, closing tickets), `scripts/intel_gap.py` (database, bench-report or run-file input; `--web-only`; `--require-ticket`), `tests/test_intel_gap.py`. **Baseline:** 11 of 42 held (23% weighted); crawlers alone 2 of 42 (3%). **Honesty notes:** the scorer proves the tool *holds* a fact by regex, not that it is shown or right; three of its own patterns were too loose and were tightened (see the regression test), so treat a pattern edit as a code change. Fixes to the format go through a new ticket.

### I02 — Evidence ledger, collector contract, polite fetcher, runner, board, reference collector  *(DONE, founder)*
**Built:** `intel/ledger.py` (the fact rules in section 3), `intel/collector.py` (contract and `discover()`), `intel/fetch.py` (robots-aware, per-host delay, HTML and PDF to text, headers), `scripts/intel_run.py`, `scripts/intel_board.py`, the reference collector `intel/collectors/site_headers.py` (live on ASCO: IIS, ASP.NET, Plesk; closes golden DG-02), tests. **Know:** `requirements.txt` does not list bs4, lxml, pypdf or trafilatura (installed locally; I05 decides what the hosted app needs). The ledger is stored but nothing reads it yet: I40 shows it, D02 reads it.

### I03 — Golden set: six more companies chosen to break the tool (hold-out set)
**Why:** one golden company teaches the tool to be good at ASCO. ASCO has a decent site and a rich press trail; the 953 are mostly smaller. Without a held-out set every ticket will overfit.
**Do:** pick six companies from the live roster (read-only) that differ: (a) owner-managed family firm with a thin site; (b) a company whose `website_url` is empty or wrong; (c) a South Tyrol or otherwise German-named, bilingual company; (d) a subsidiary of an Italian group; (e) a name-collision case (another firm or a person with the same name, the T20 problem); (f) a JavaScript-only site (D33 says about 15%). Research each exactly as ASCO was: 90 minutes, public sources only, every fact with URL, grade and weight, and also write down what you looked for and could **not** find. Same JSON format and categories (add categories only if the checker allows it). Fill `closed_by` from this roadmap. Mark two as **development** (agents may look at them) and four as **hold-out** (only release captains and I43 score against them): put that in `docs/intel/golden/README.md` and in each file's `"split"` field.
**Accept:** six files load with `scripts/intel_gap.py` (`load_golden`); at least 30 facts each across at least 8 categories; patterns tested against a deliberately wrong text so they are not trivially true; the README states the split and how each company was chosen; a results note with the current tool's baseline score for each (`--db` and a `crawler_bench.py` run, web-only).
**Notes:** `scripts/crawler_bench.py` writes only to a throwaway SQLite file; never run research code against the live `DATABASE_URL`.

### I04 — LLM provider pool: many keys, per-task routing, token budgets, and the worker env allowlist
**Why (R5):** the user has many AI keys. Today the Node website crawler uses a primary and up to five numbered fallbacks (`llm.ts`), `config.py` forwards them locally, but `worker/worker.mjs` ENV_ALLOWLIST lets only the unnumbered `LLM_FALLBACK_*` through, so remote PCs use at most two providers, and the vision phase has the same limit. News and innovation crawlers call Anthropic directly. Nothing routes cheap high-volume work to small models and synthesis to the best, nothing enforces a budget, nothing records usage.
**Do:** (1) `intel/llm_pool.py`: providers declared in the environment as `INTEL_LLM_<n>_{NAME,API_KEY,BASE_URL,MODEL,TIER,TASKS,TPM,RPM}` plus backward-compatible reading of the existing `CRAWLER_LLM_*` settings; task classes `triage` (cheap, high volume), `extract` (long-context JSON), `synthesize` (best model), `vision`; per-provider token-bucket and cool-down from `retry-after`; failover; a JSON-mode / tool-call normaliser with repair and one retry; temperature 0; a usage ledger per company and run; hard enforcement of `Budget.max_llm_tokens`; `complete(task, system, user, schema=None)` as the single entry point. (2) `worker/worker.mjs`: add the new names and the numbered `LLM_FALLBACK2..5_*` and `VISION_LLM_FALLBACK2..5_*` to ENV_ALLOWLIST. (3) A **parity test**: every environment variable name that `scrapers/*.py` or `config.py` forwards to a crawler must appear in the allowlist. (4) `.env.example`: names only, never values.
**Accept:** unit tests with a fake HTTP layer (429 failover, budget stop, JSON repair, routing); the parity test; a live check with at least three real providers and 20 parallel calls showing no stall beyond the longest `retry-after`; a usage report in the results note. Check how task environment travels to the worker (see I44) and do not widen what is stored in database rows. **Do not rebuild the worker bundle**; I90 does.
**Notes:** model names change; verify against each provider's `/models` before hard-coding anything (the repo already got bitten by a 404 on a vanished model).

### I05 — Web research layer: search, hardened fetch, readability, query planner
**Why (R2):** the human research was ~25 searches. The tool has none. The deal history of ASCO (2019 buyout, 2022 Mott, 2024 IDEX) is on PR Newswire, BeBeez, Filtsep and in SEC filings, reachable only by searching.
**Do:** (1) `intel/search.py`: `SearchClient.search(query, kind="web"|"news", lang, n)` over pluggable providers: free (DuckDuckGo HTML through the polite fetcher, Google News RSS in it/en/de, GDELT DOC with its 5-second spacing, SEC EDGAR full-text, Wayback CDX, Common Crawl index) and optional keyed (Brave, Serper, Tavily, SerpAPI) chosen by environment; canonical-URL de-duplication; a deny list (business-directory aggregators outrank real sources; keep parity with `BLOCKED_DOMAINS`) and an allow list for registries and press; snippet and date capture; response caching. (2) `intel/queryplan.py`: a query pack per company and topic (ownership, people, events, certifications, awards, tenders, hiring) in Italian, English and German, with name variants (S.p.A. / SpA / S.r.l., brand, VAT, city), rules first and an LLM planner for follow-ups. (3) Harden `intel/fetch.py`: disk cache with ETag, charset handling, `trafilatura` readability, optional Playwright fallback for JavaScript-only pages (shares D33), a shared per-domain rate limit. (4) Decide with `requirements.txt` which new libraries the hosted app needs (it has no Node and may not need all).
**Accept:** unit tests on recorded results; live: the ownership pack for ASCO returns pages mentioning Mott, IDEX and Red-Fish in the top 10 without a keyed provider; politeness test (no host hit faster than its delay); no network in unit tests; a results note listing every provider tested, its limits and its failure modes.

### I06 — Verification gate: entity match, quote grounding, contradiction handling
**Why (R3, T20):** news matching was about half wrong; a model will happily attribute another company's acquisition to this one ("ASCO" is also the name of a well-known valve brand owned by a larger group, and a patent in the ASCO search results belonged to MP Filtri). Nothing may reach a dossier unverified.
**Do:** `intel/verify.py`: `verify(fact, page_text, company, ledger) -> Verdict` with: quote grounding; entity match (legal name or brand, plus at least one discriminator: VAT, city or province, the company's own domain, a known person); name-collision detection from the search results themselves; date sanity (not future, not before the company existed); numeric sanity against AIDA (employees within a factor of three, revenue within a factor of two unless a different year is stated); contradiction against existing ledger facts (flag, never overwrite); an optional LLM judge ("same company? answer with a quote") through the pool, cached. Sets `verified`, adjusts `confidence`, returns reasons a human can read. Never raises.
**Accept:** 20 labelled cases (including the Emerson-ASCO-style collision, a surname collision and a patent by a similarly named applicant) at precision at least 0.95 with recall reported; unit tests; documented thresholds as constants, not buried.

<!-- TICKETS-W0-END -->

#### Wave 1: collectors. Each is one new file under `intel/collectors/`, all claimable now (they take `fetch`, `search` and `llm` from the context, so they do not wait for I04 to I06; use `scripts/intel_run.py` for live proof and switch to the real services when those land)

### I10 — Deep site reader: whole-site and PDF digest into profile facts (new Node crawler plus collector)
**Why (R1):** the current crawler reads 2,000 characters of up to 15 pages into 12 retail-shaped fields. On ASCO it produced twelve process fluids as "product lines" and nothing about history, entities, certifications, contacts or documents, all of which are on the site (`/Home/Company`, `/Home/Contatti`, footer legal block, `/Home/Download`).
**Do:** a new package `Scraper/crawlers/deep-profile-crawler` (copy the structure and `llm.ts` provider loading of `company-website-crawler`), a wrapper `scrapers/deep_profile_crawler.py` (the `run_ts_crawler` pattern) and the collector `intel/collectors/deep_site.py`. Steps: (1) **page inventory** from `sitemap.xml`, robots and breadth-first link discovery up to about 120 pages on the registrable domain, classifying pages by URL and title (about, history, products, applications, downloads, contacts, news, careers, quality/certifications, sustainability, legal notice, team); **never guess paths** (the own-site scrape fails exactly this way); (2) full page text, chunked to what the model takes, not 2,000 characters; (3) two passes through the pool: a cheap triage ("what is on this page") and a stronger extraction of identity (legal block: VAT, REA, share capital, PEC), history events with years, locations and entity types (HQ, plant, branch, subsidiary), group or parent mentions ("a Mott Company"), products, product families and applications in the company's own words, certifications and standards with scope, named customers and projects, named people and roles, published contact channels, memberships and awards, social links; each fact carries a quote the Python side re-checks against the returned text with `quote_in_text`; (4) a **document inventory** (every PDF with title, type, size) handed to I11; (5) deterministic runs: temperature 0, stable ordering, prompt and model versions recorded.
**Accept:** `--require-ticket I10` passes on ASCO (web-only); the same input run twice shares at least 90% of facts; a precision audit of 30 random facts on each of three other companies finds at least 93% supported by their quote; at most 8 minutes and the token budget per site; honours robots and `SOFT_DEADLINE_SECONDS`; new env vars added to the allowlist (coordinate with I04); runs on a remote worker through the existing queue (test with the worker harness). **No bundle rebuild** (I90). Reuse the site-evidence corpus if D11 has it.
**Notes:** keep the old crawler running; it feeds live indicators. Nothing here changes a signal.

### I11 — Document reader: certificates, manuals, brochures, annual reports (PDF, OCR fallback)
**Why (R6):** ASCO's 21 PDFs hold the real answers: certificate standards, issuing bodies, validity (the ISO 9001 file name says 2026-06-29, which is past), twelve product-family manuals.
**Do:** `intel/pdf.py` (pypdf, pdfplumber; OCR only if available, otherwise mark "scanned, not read") and `intel/collectors/documents.py`: take document URLs from the ledger (I10's inventory) or discover them from the downloads page; parse certificates (standard, number, issuer, scope, issue and expiry date, file-name dates), manuals (family code and title), reports (year, headline numbers); page selection so a 200-page report never goes to the model whole; every fact quoted and grounded. A certificate whose date has passed becomes a *flag* fact with grade D until I15 verifies it in a register.
**Accept:** `--require-ticket I11` on ASCO; cost cap per document; unreadable PDFs reported, not failed.

### I12 — Ownership classification and group flag: who really controls the company
**Why (R4):** AIDA already holds the control chain for ASCO (Mott level 1, IDEX CORP level 2) and the dossier lists it, but nothing classifies or flags it. Across the 953, 533 have a corporate holder of 50% or more and 139 are 50%+ foreign-owned; a family holding is not a foreign group, so the owner must be *typed*.
**Do:** `intel/ownership.py`, a pure classifier over the AIDA blob (`shareholders`, `controlling_shareholders`, `immediate_shareholders`, types, countries, percentages, `group_size`): ultimate owner type among natural person or family, family holding, private equity or fund, industrial group, listed group, public body, foreign subsidiary, unknown; a controlled-subsidiary boolean with its basis. Then `intel/collectors/ownership.py` for corporate owners: SEC EDGAR full-text and Exhibit 21 (verified: IDEX's FY2024 and FY2025 10-Ks list "Asco Filtri SpA"), GLEIF (identity and LEI only: **verified that ASCO and Mott have no parent relationship records**), Wikipedia and Wikidata, deal announcements; facts `ownership.ultimate_owner`, `.owner_type`, `.parent_listed`, `.parent_size`, `.acquired_on`, `.previous_owners` using the `own.*` names in `diagnosis/factkeys.py`. Set `Company.parent_company_id` only when the parent is itself a company in the database (the T24 behaviour); otherwise ledger only.
**Accept:** `--require-ticket I12` on ASCO; the classifier run over all 953 AIDA blobs (offline) produces a distribution report in the results note and reproduces the 533 / 139 raw counts; accuracy at least 90% against 30 hand-labelled companies; no database writes except the ledger.

### I13 — Corporate events timeline: deals, plants, subsidiaries, leadership changes, awards
**Why (R8):** a human reconstructed ASCO's life in minutes: 2002 founding, 2013 move, 2019 PE buyout (FY2018 revenue EUR 19.9M, EBITDA 1.9M), 2022 Mott, 2023 India, 2024 IDEX, 2025 factory, a possible MD departure. The tool has none of it.
**Do:** `intel/events.py` (event model: type from `diagnosis.contracts.TRIGGER_KINDS`, date, parties, amount, sources) and `intel/collectors/events.py`: query packs from I05 (company, owner, people, "acquisizione / acquired / investimento / nuovo stabilimento / nomina"), press-release sites, sector trade press (the agent builds an ATECO to trade-press list), the company's own news page and its Wayback history, EDGAR 8-K for parents; verification through I06; **merge the same event across outlets into one event with several sources**; a date disagreement (2021 vs 2022 for the Mott deal) is recorded as a conflict, not resolved; "searched, nothing recent" is an `absent` fact with the queries used.
**Accept:** `--require-ticket I13` on ASCO; precision audit of 30 events on three other companies at least 95% truly about the company; coordinate with D16 / D17 (they type and rank, you collect and verify).

### I14 — People and contact finder: who to call and the published ways to reach them
**Why (R7):** ASCO's registry board is US executives of the parent; the people to call (General Manager, export manager) appear only in deal stories and directories; the switchboard, e-mail and PEC are published but not stored.
**Do:** `intel/people.py`, `intel/collectors/people.py`, `intel/collectors/contacts.py`. Sources: the company's team, contact and legal-notice pages, deal and press stories, public search snippets for name plus title plus company, association member pages, the registry roster already in `company_people`. Facts `people.<role>` (name, role, level, since, source) and `contact.switchboard`, `contact.generic_email`, `contact.pec`, `contact.contact_page`, `contact.language`; classify **local operating management vs parent-company directors**; rank who can sponsor a pilot (MD or GM, operations or technology, export or commercial, purchasing). Only contact points the company itself publishes, each with source and date; never guess an e-mail pattern for a person.
**Accept:** `--require-ticket I14` on ASCO; the same on three other companies read by eye; **no writes to `company_people`** (management signals are computed from it); decision G2 and G3 answered before any LinkedIn-derived data is stored.

### I15 — Certification and compliance register lookups (Accredia, notified bodies, ASME, Achilles)
**Why:** a claim on a website is grade B; a register entry is grade A and carries the real expiry. ASCO claims ISO 9001 and 45001, PED module H/H1 (notified body CE 1370), ASME U-Stamp, National Board, Achilles Silver.
**Do:** a **feasibility table first** (which registers allow scripted, login-free, captcha-free lookups by VAT or name: Accredia certificate database, IAF CertSearch, NANDO notified-body list, National Board and ASME directories, TUV, DNV and Bureau Veritas lookups; Achilles is paywalled, accept certificate PDFs only), written into the results note and reused by D10 / D34; then `intel/collectors/certifications.py` for the feasible ones. Facts `certifications.*` with `verified_in_register` versus `claimed_on_site`, expiry, and a mismatch flag.
**Accept:** golden CE-01..CE-03 captured and, where a register is feasible, upgraded to grade A; infeasible registers recorded honestly as such.

### I16 — Trade fairs, associations and programmes (exhibitor lists, member directories, IPCEI, CORDIS)
**Why:** the directory crawler knows three directories (ANIMA, Kompass, MECSPE). ASCO's real footprint: H2IT membership, the EU IPCEI Hydrogen matchmaking directory (entry updated 2026-07-08), ACHEMA 2015 with Mott.
**Do:** `intel/collectors/networks.py`: fair exhibitor lists chosen by ATECO and end market (ACHEMA, Hydrogen Technology Expo, Filtech, SPS Italia, Ipack-Ima, MECSPE, ...), found by search and read by the fetcher; association member directories (ANIMA, UCIMA, UNACEA, ACIMAC, Federmacchine, H2IT, Assolombarda, VDMA, sector associations); programmes (IPCEI Hydrogen directory, CORDIS projects, the Italian state-aid register adapter `adapters/rna_state_aid.py` that exists but may be unwired, regional calls). Each source is a small plugin function with its own test. A Kompass listing is a *directory* fact, never a trade-fair fact.
**Accept:** `--require-ticket I16` on ASCO; plugin authoring guide in the results note.

### I17 — Hiring and employer-brand intelligence (careers, job boards, reviews, LinkedIn page)
**Why:** ASCO has no careers page and no postings (correctly recorded as absent); other companies do, and job text names ERP, MES and automation tools. The review crawler currently errors with "could not search any configured source".
**Do:** `intel/collectors/hiring.py`: careers discovery through ATS patterns and the I10 inventory, public job-board pages (Indeed, InfoJobs, Randstad, Adecco, LinkedIn Jobs public pages), roles by function, software and ERP names in job text, employer ratings where a public page allows it, LinkedIn company page public statistics through search snippets. Respect each site's terms: where scraping is prohibited use the official feed or skip and say so (decision G2).
**Accept:** honest absence on ASCO; positive detection on at least three companies known to have careers pages; share the review work with I34.

### I18 — Technology and digital footprint v2 (extends the header collector; ledger only, no signal changes)
**Why:** `scrapers/wappalyzer_local.py` knows 18 signatures; ASCO scored "absent". The reference collector fixes headers; the rest of the footprint is open.
**Do:** extend `intel/collectors/site_headers.py` and add `tech_stack.py`: HTML and script fingerprints from a real signature set (evaluate the licence of any community technology database before importing it), cookies and meta generator, analytics, marketing and CRM tags, e-commerce, customer or IoT portals, configurators and CPQ, security headers and TLS, domain registration age (RDAP), Wayback first-seen (the tool has it), **site-quality checks** (placeholder text such as "John Doe" or lorem ipsum, counters stuck at 0, broken links, copyright year, mixed languages), social accounts inventory with last public activity. Do **not** touch `tech_stack_intensity` (I35 owns score-moving changes).
**Accept:** `--require-ticket I18` on ASCO; false-positive review on five other sites.

### I19 — Patents and trademarks v2: EPO reliability, group applicants, designs
**Why:** EPO OPS errored on 653 of 900 calls (73%); ASCO's two patents were found, but patents are often filed by the parent or an affiliate, and name collisions are real (a design patent from a different Italian filter maker appeared in ASCO searches).
**Do:** read `source_health.last_error_message` and the adapter to find the real cause (quota, token expiry, query form, rate limit) and fix `adapters/epo_ops.py`; add applicant-name variants and group applicants (from I12); INPADOC families; WIPO PATENTSCOPE and Google Patents public pages; EUIPO live (credentials exist; mode shows simulated); UIBM for Italian marks; designs. Facts `ip.*`.
**Accept:** error rate under 5% over 100 companies; ASCO's records agree with Espacenet by hand; collision test: a same-name different-company patent is rejected by I06.

### I20 — Italian registry and institutional lookups (INI-PEC, Registro Imprese open data, UIBM, ANAC, RNA)
**Why:** PEC, REA, capital, object, branches and multi-year accounts are institutional facts; ASCO's PEC and REA were on its own footer and a directory.
**Do:** a **feasibility table first** (INI-PEC lookup by VAT, Registro Imprese and InfoCamere free endpoints, IPA, ANAC open data by tax code, MEPA, UIBM, the state-aid register), then `intel/collectors/registry_it.py` for what works without a paid account. Facts `identity.*`, `contact.pec`, `financials.*` (grade A only), local units. Record every paywall plainly; do not buy anything.
**Accept:** `--require-ticket I20`; items found infeasible are listed with the reason, not silently skipped.

### I21 — Customers, references and channel position (OEM, EPC, distributor, end user)
**Do:** `intel/collectors/customers.py`: named customers, projects and case studies from the site, press and tenders; classify the company's position in its chain (OEM supplier, supplier to EPC contractors, distributor, end-user supplier, aftermarket); supplier-register memberships imply customer types (Achilles implies oil and gas operators). Quote-grounded and verified. ASCO has no named customers on its pages: use the I03 companies for acceptance.

### I22 — Peer and competitor candidates (suggest-only, a human confirms)
**Do:** `intel/collectors/peers.py`: candidates from the peer engine (`diagnosis/peers.py`), "similar companies" lists on public directories (ASCO's listed MP Filtri, Stilmas, Unidro, Filtrex, Brofind, Cannon Artes, Pentair Manufacturing Italy), mentions on the company's own site and association member lists; each with its basis. **Never** creates `Competitor` rows (those are hand-entered by design); D32 ranks and a human confirms.
**Accept:** `--require-ticket I22` on ASCO.

### I93 — Verification gate v2: generic name-collision detection and a harder unseen-company benchmark
**Why (R3, T20; proposed by claude-f41b while re-checking I06):** `intel/verify.py` `KNOWN_COLLISION_RULES` is a per-brand table keyed on `asco`, `emmegi`, `pomini` and `corrada` (the golden company and the I03 development and hold-out companies), with exception patterns holding the golden company's real owners and town. It is the only collision detector, so it breaks the ground rule "never hard-code the golden company, its names or its pages" and contaminates the Pomini and Emmegi hold-outs. It does not generalise: a namesake page of the shape "Emerson {BRAND} solenoid valves ... distributor in Milano" for a Milano company is rejected as a collision for ASCO but verified for an unseen brand. The 24-case benchmark (precision 1.0) does not show generalisation either: the five collision pages contain no discriminator, so the generic "no discriminator" rule already rejects them without the table, and no case is a namesake page that also carries one discriminator (the company's city, a known person).
**Do:** replace the table with detection from the page and the search results themselves: a second entity with the same brand token but another legal form, city, domain or group name; a page about a larger or different entity that only mentions the discriminator in passing; a brand token that is a common surname or common word; a page host that belongs to another company. Any lexical hints live in a config file as data and contain no company names. Rebuild the benchmark with at least 20 hard negatives on companies not named anywhere in the code, including namesake pages that contain one discriminator, and report precision and recall on a split the author did not tune on. Keep `verify(fact, page_text, company, ledger, llm) -> Verdict` and its never-raises contract unchanged.
**Accept:** `grep -ri "asco\|emmegi\|pomini\|corrada\|mott\|idex" intel/verify.py` (and any new config) finds no rule; precision at least 0.95 on the new hard set with recall reported; the 24 existing cases still pass; the probe above gives the same decision for ASCO and for an unseen brand.

### I94 — Repair the I03 golden set: loose patterns inflate the baseline, unverifiable and non-existent sources, no not-found lists
**Why (proposed by claude-e9be, independent audit of I03):** the six I03 files were claimed and done in 25 minutes. Of 237 cited web sources 172 were bare home pages and 50 returned an error (404, DNS failure, 403), and 161 of 189 facts cited no readable page deeper than a home page; an earlier-draft phone number, a VAT, an ATECO, a certification list and a capital figure were wrong or unsupported; patterns such as `vic`, `qualit`, `2007`, `brochure`, `subsidiary of` matched any text (Pomini PR-01 was "held" through the signal key `cost_services_k`); none of the files had the "what I looked for and could not find" list the ticket asked for.
**Do:** rebuild each of the six files with only facts that were checked: each fact has a `quote` that appears verbatim in a page that was fetched (text or raw HTML) or in the AIDA or roster text held in the tool, tight `match` patterns (checked by a script against the quote, a generic blurb and the other companies' quotes), and a top-level `not_found` list; keep the `subject` block valid for `tests/test_intel_no_golden_leak.py`.
**Accept:** all six files pass the checker with 0 failing checks (at least 30 facts, 8 categories, split field, not_found list); corrected baseline for each company in `docs/intel/results/I94.md`. **Know:** `corrada.it` redirects to a server-rendered group page (not a JavaScript-only case, see I102); `eurosicma.it` redirects to the IMA brand page; `emmegi-heat-exchangers.com` redirects to `emmegi.group`; the Pomini DB field is empty and the real site is a JavaScript-only single-page app.

### I101 — Golden-set gate: promote the I94 verifier into scripts/ plus a test, and rework the ASCO golden file against it
**Why:** the I94 checker (quote grounding, self-test, hostile texts, pattern lint) lives only as a script attached to the I94 results note, and the ASCO file (I01) fails the same checks (no quotes, no not_found list, loose patterns such as `subsidiary of`, `9001`, `2.5 m`, FI-01 held by unrelated signal text). **Do:** `scripts/golden_verify.py` (network use only when asked, cache on disk), `tests/test_intel_golden_set.py` (offline half: schema, split field, at least 30 facts, 8 categories, lint, hostile texts, own-pattern self-test; the fetch half stays a script), and rework `asco_filtri.json` so it passes. Coordinate with I43 (evaluation CI).

### I102 — Golden set: add a genuinely JavaScript-only development company
**Why:** I94 found that Corrada is server-rendered, so the development set has no JavaScript-only case; the only one is the HF Group site behind the Pomini hold-out file. **Do:** find a roster company whose own site answers any path with the same empty shell (check with a nonsense path, as in the I94 note), research it as the others (quote-grounded, not_found list), mark it `development`, update the README.
### I100 — Verifier: compound-entity detector reads a line break as a space
**Why:** `intel/verify.py` `_compound_entity_hits()` matched '{Capitalized} {brand}' with `\s+`, which spans a line break, so a page whose headline ends in a capitalised word and whose next line starts with the company name was read as a foreign compound entity and the fact was rejected as a name collision. Headline-then-body is the normal shape of extracted press text, so ordinary true facts were silently lost (the reject direction: safe, but costly in recall).
**Do:** do not let a compound cross a newline or a sentence end (horizontal whitespace only between the two tokens), keep every genuine same-line compound detected, and add the repro as a regression test.
**Accept:** the ticket's minimal repro (invented 'Meccanica Brambilla') is no longer a collision and verifies; real same-line compounds still hit; the I93 TUNE/HOLDOUT precision and recall splits still pass unchanged.

### I90 — Release captain, wave 1
**Before you start:** run `python scripts/intel_board.py status` and read every ticket proposed after a ticket of this wave was marked DONE (the rework tickets that reviews raised, for example I93 to I96). The board cannot add dependencies to a claimed ticket, so you are the check: **do not ship, and do not report the wave's coverage, while a rework ticket on a wave-1 deliverable is open**, or record in the acceptance note exactly which numbers are inflated by it. Run `tests/test_intel_no_golden_leak.py` and report its baseline list.
**Do:** integrate the wave on `master`; run the full suite and report only failures beyond the two known legacy ones; check each new collector registers (`python scripts/intel_run.py --list`); add the new crawler package(s) to the worker `VERSION_INFO`, then `python scripts/build_worker_bundle.py` **once** and commit `worker_dist/vienna-crawler-bundle.zip` with `worker_shim/app/` (read T22 and T23 in `docs/CRAWLER_ROADMAP.md` first: this is what updates the helper PC; do not rebuild casually and fix the OP-010 line-ending question before you do); score every golden company, development and hold-out, and write `docs/intel/WAVE1_ACCEPTANCE.md` with the table; update the status file; list decisions G1 to G7 that are now due.

<!-- TICKETS-W1-END -->

#### Wave 2: orchestration and reliability

### I30 — Deep-profile orchestrator: iterative research loop, budgets, resumable, multi-PC
**Why (R12):** a human follows leads. Owner found, then search the owner; a person named in a deal story, then search the person. Today each crawler runs once with a fixed plan.
**Do:** `intel/orchestrator.py` and `intel/budget.py`. Loop: (1) plan from the checklist (golden categories) and the collectors whose `requires` are met; (2) run them in parallel under `resource_governor.py`, Node steps through the existing queue (`crawl_jobs`, `worker_hub.use_target`) so they run on helper PCs, Python API steps in-process; (3) load the ledger, merge and save **per collector** (no stale-copy overwrite); (4) verify through I06; (5) compute coverage and unknowns; (6) an LLM planner proposes follow-up queries for the gaps (depth and budget limited); (7) stop at coverage target, budget or no new facts. Record each run in `ledger.runs` and as a `CompanySourceRun` row so the existing widget and Pipeline Health show it. Defaults, configurable: about 250k tokens and 15 minutes per company.
**Accept:** end-to-end on ASCO reaches at least 70% weighted web-only golden coverage within budget; runs on three other golden companies; kill the process mid-run and resume with no duplicate facts; a task claimed by worker B when worker A is offline (follow the existing worker tests); no collector can exceed its declared prefixes; a results note with a timing and cost table.

### I31 — Freshness and change detection: re-check what can change, emit trigger events
**Do:** `intel/freshness.py`: a time-to-live per category (ownership months, events weeks, hiring days), cheap probes (HEAD / ETag / content hash of the key pages, news since the last run, EDGAR filings list), a "what changed since last visit" diff, events emitted for D16. **Accept:** change detected on a recorded before/after page pair; a no-change re-check costs no LLM tokens.

### I32 — Portfolio batch runner over all 953 companies (resumable, cost-capped, fleet-aware)
**Do:** `scripts/intel_batch.py --companies shortlist|all|<file> --budget-eur X --resume`: order (shortlisted and Target Matrix selections first), progress file, retry policy, per-day budget, a summary report with golden-style coverage per category over the processed companies. Coordinate with D13 and D29. **Needs** G4 and the `@live-db-write` lock; it writes only ledger blobs, as crawlers already do. **Accept:** dry-run on 20 companies; interrupted and resumed; budget stop proven.

### I33 — Own-Site Scrape reliability: 77 percent errors from guessing About/Team paths
**Why:** `management_diversity` errored on 921 of 1,192 calls; on ASCO with "No About/Team page found at common paths" while the page is `/Home/Company`.
**Do:** replace path guessing with link discovery from the homepage navigation and sitemap (reuse I10's inventory when present); separate "this site has no team page" (a confirmed absence) from a failure. **Accept:** error rate measured on 100 random companies before and after (read-only roster, throwaway database), target under 15%; an absence is never recorded as a failure.

### I34 — Make Reviews, Innovation and News crawlers live without paid keys (they are simulated today)
**Why:** News and Innovation crawlers have been simulated for all 920+ runs (NewsAPI and Anthropic keys); the Review crawler reports "could not search any configured source".
**Do:** drive them from I05 search and the I04 pool; keep their existing flags and output field names; produce real values or honest absences; no indicator semantics change. Changes in the Scraper repo are scoped per package (`@scraper-repo/...`). **Accept:** each runs live on ASCO and on three companies, with raw output attached; no more silent simulation.

### I35 — Semantic audit of existing producers
**Why (R9):** known defects, each with evidence: Kompass listing counted as `trade_fair_participation` (ASCO = 1); `product_types` are process fluids and `product_portfolio_diversity` counts them (12 stored, 17 on re-run); retail-shaped fields applied to B2B machinery makers; Wappalyzer's 18 signatures and false "absent"; a stale news payload. Fixing them moves scores.
**Do:** `docs/intel/AUDIT_EXISTING_PRODUCERS.md`: for each defect, the evidence, the proposed fix, and a **read-only before / after count** of how many of the 953 companies and which indicators would change and by how much. Add the defects other agents report. **Make no score-moving change without GG's approval (G6).** Approved fixes go in with tests.
**Accept:** the audit exists with numbers; only approved fixes are merged.

### I91 — Release captain, wave 2
As I90, for wave 2; additionally run the orchestrator on the golden companies and publish the coverage and cost table in `docs/intel/WAVE2_ACCEPTANCE.md`.

<!-- TICKETS-W2-END -->

#### Wave 3: surfaces and quality

### I40 — Dossier UI v2: the ledger shown with sources, conflicts and a coverage meter
**Why (R13):** nothing displays provenance or conflicts; Kompass appears as a trade fair; some crawled blobs have no renderer.
**Do:** `views/company_intel.py` plus one registration line in `views/company_dossier.py`: a section "Profile and evidence" with an identity and **group-chain card (a banner when the company is a controlled subsidiary)**, a dated timeline, people and contacts (copyable), certifications (registry-verified versus claimed, expiry badges), products and applications, locations, documents, footprint; every fact with its source link, quote tooltip, grade badge and verified tick; a conflicts panel; a **coverage meter against the checklist** (known / absent / never looked); lazy loading like the other sections; mobile layout. Audit every `crawler_*` dataset for a renderer and list the gaps. Tests with Streamlit's `AppTest` as in `tests/test_company_dossier.py`.
**Accept:** screenshot of ASCO's page in the results note; no section slower than the existing ones.

### I41 — Research brief and contact sheet export (facts only)
**Do:** `intel/brief.py`, `intel/export.py`: a one-page research brief (who they are, who owns them, what they make, recent events, certifications, who to contact and how, what is unknown) in Markdown, Excel and PDF, every line with its source. **Facts only, no hypotheses and no pitch**: those are the Diagnosis call pack (D28); this feeds it. **Accept:** ASCO's brief reads correctly to someone who has not seen the tool.

### I42 — Qualification flags in the Target Matrix (no score change)
**Do:** `intel/qualification.py`: flags derived from the ledger and AIDA (controlled subsidiary of a listed group, held by private equity, dissolved or in procedure, relocated, not a manufacturer), shown as badges and a filter in `views/target_matrix.py`. **Never alters NEED or READINESS** (OP-003 and OP-008 are still open); G5 decides whether a flag ever excludes. **Accept:** `--require-ticket I42` (golden OW-06); counts per flag over the 953 in the results note.

### I43 — Evaluation CI: recorded-page regression, nightly live golden run, precision audit
**Do:** record the pages behind each golden company once (`tests/golden/`), run collectors against them offline in the test suite and fail on a coverage drop; a documented nightly or on-demand live run over the golden set; a sampling script that prints 30 random facts per company with their quotes for human review and stores the verdicts; link with D08 and D25. **Accept:** the offline regression runs in under a minute; a deliberately broken collector fails it.

### I44 — Security, privacy and source register
**Do:** `docs/intel/SECURITY_AND_SOURCES.md` and `intel/policy.py`. (1) Establish exactly how API keys and environment reach helper PCs (task rows in the shared database, retention `TASK_RETENTION`) and what is exposed if a task row is read; propose encryption or a token-fetched secret; (2) GDPR for people and contacts found on the web: purpose, source, date, retention, suppression list, no sensitive data; (3) a **source register**: every source, method, authentication, robots or terms status, personal data yes/no, GG decision, owner ticket; (4) per-collector kill switches; (5) the rotation of the `landing_writer` password already flagged in the status file. Needs GG's answers G2 and G3. **Accept:** the register covers every source in section 4 and every collector merged so far.

### I45 — Worker fleet v2: capability routing, shared per-domain throttle, safe update rollout
**Do:** tasks routed by worker capability (browser present, bandwidth, region), a per-domain rate limit shared across PCs, health and quarantine of a misbehaving worker, staged rollout of bundle updates with automatic rollback (read T22 and T23 first). **Accept:** two real workers sharing one domain stay under its delay; a bad update does not take the fleet down. Touches `worker/*`; coordinate with I04.

### I92 — Release captain, wave 3
Live pilot on 12 companies (the shortlist plus the golden set), acceptance with GG, rollout notes, `docs/intel/WAVE3_ACCEPTANCE.md` with the final coverage and precision numbers against the section 9 targets.

<!-- TICKETS-W3-END -->
