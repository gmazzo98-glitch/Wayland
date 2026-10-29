# Audit 2026-10-29: is the PDF data wired through the app, and is it visible in Company Intelligence?

Claude Code, at Giorgio's request ("check that the info from the PDF upload is wired up correctly, especially in Company
Intelligence, where it must be visible; make a giant audit and string up loose ends").

## How it was checked
- Real PDF files (text layer, built with reportlab from invented numbers) in two Italian layouts, not page text pasted into
  the parser: the Bank of Italy layout of the Mondial Bony files and the OIC layout of industrial companies, in euro and in
  thousands. They went through `balance_sheet_pdf.parse_statement`, the mapping rules, `financial_ledger.import_statement`
  and then the dossier rendered with Streamlit `AppTest`, on throwaway SQLite databases only. Also driven through the real
  Data Admin page (Flexible Data Import) and through all nine navigation pages of `app.py`.
- The AI path ran with a fake model (no model key exists in this container).
- A baseline full test run before any change (3 failures), a static sweep (pyflakes: no undefined names), a sweep for
  orphaned modules, undeclared dependencies and TODO markers.

## What the PDF reaches (after this audit)
| Consumer | Reads the PDF data? | How |
|---|---|---|
| Company Intelligence, Financials | yes | merged annual history, source of every figure, conflicts, one expander per statement with page and connection, removal with re-merge |
| Company Intelligence, Overview | yes | KPI strip, flags, header chips, Identity block (printed share capital, REA, registered office) |
| Company Intelligence, Data and audit | yes | both records rendered as tables, signal audit finds the record behind a merged signal |
| Signals, so Target Matrix, pain points, assistant, scoring | yes | written with source "Merged annual financials" |
| Valuation | yes | prefers the dataset with the newest fiscal year; a statement-only year has no EBITDA (OP-020) |
| One-page export | yes | built from the same statement |
| Diagnosis peer cohort | **no** | reads only the bulk AIDA blob (OP-021) |

## Findings and what was done
| # | Finding (seen in the running dossier unless stated) | Fix |
|---|---|---|
| 1 | After a FY2025 statement the header chip said "Accounts FY2024" and the Overview flag "FY2025 is not in AIDA yet", beside FY2025 figures. `last_accounts_year` was never updated by the ledger. | the ledger writes the signal from the merged year; a registry value that is already later is kept |
| 2 | Bulk AIDA results built on a Latest the merged history no longer has (EBIT margin 100% from FY2024, revenue per employee, employee count) stayed as if current. The withdrawal only covered the single-report source, not the bulk export that holds nearly every company. | offered for withdrawal in the import preview like the single-report ones (the operator can keep them), withdrawn by default |
| 3 | "Is it growing?" showed AIDA 5,090k to 6,761k (+33%) beside the stored trend of -7.5%, because it took the first raw record alphabetically, and for the report and merged records compared the oldest of ten years. | reads the record behind the signal, over the Latest / Y-1 / Y-2 window, and never an AIDA blob the merged history supersedes |
| 4 | Headcount printed in the statement fed the signal but not `Company.headcount`, so the header, segment and peer filters kept the old number. | follows the signal when the statement itself printed it; a manual or AIDA headcount is not overwritten otherwise |
| 5 | Share capital, REA, registered office from the cover were only inside a collapsed statement expander. | shown in the Identity block |
| 6 | **OIC layout (most target companies)**: unnumbered headings (`B) IMMOBILIZZAZIONI`, `IV - Disponibilita liquide`, `D) DEBITI`) were glued to the next row's label, so no rule found revenue, cash, total debt or intangibles; `6)` items were not items; `TOTALE PASSIVO` was not recognised, so the balance check never ran. The tests only covered the Bank of Italy layout. | headings stay out of labels (except a label whose figures sit on the next line), `N)` items, liabilities total, new check "value of production minus costs = operating result", `_base_key` strips `6)` so AI rows meet rule rows (they would have been duplicated) |
| 7 | A mapped Incorporation Date line crashed the import on a text value. | read as a date or refused with a message |
| 8 | A wrong upload could not be undone. | `remove_statement` and a confirmed button in Company Intelligence |
| 9 | A bulk AIDA re-import left the merged record stale (the single-report import already re-merged). | `apply_records` re-merges companies that have statement history |
| 10 | Flexible Data Import shows Country and Dataset Name for a PDF, which it ignores. | a note says so |
| 11 | `.env.example` did not say that PDF reading uses the CRAWLER_LLM / ASSISTANT_LLM / vision keys. | documented |
| 12 | `tests/test_phase7_batch.py` hook test failed intermittently (asserted thread start order). | asserts the submission order |
| 13 | `tests/test_agent_status_file.py` failed for days: ids compared in file order after OP-015 moved to the Closed log, and section B had grown to 64 lines against its 39 line cap. | ids compared sorted; the dated history moved verbatim to `docs/STATUS_HISTORY.md` |

## Not fixed, and why
- `test_worker_hub::test_committed_bundle_contains_the_current_worker_source` stays red: `worker/worker.mjs` changed in ticket I04 without a rebuilt bundle. Rebuilding needs the Scraper repo and npm and ships an update to the helper PCs; it belongs to release ticket I90 (OP-010, OP-017 G7).
- EBITDA from a statement (OP-020), peer cohort (OP-021), CI for the tests (OP-022) are decisions for Giorgio. Verification with real files, a scan and the hosted app (OP-023) needs data and keys this container does not have.
- `pypdf` is imported lazily by `intel/fetch.py` but is not in `requirements.txt`; the intel layer says so and degrades. Left alone: the intel program is not wired into the app (its ledger `crawler_deep_profile` is read by nothing, by design until OP-017 is answered).
- AIDA's own "Risultato operativo" equals revenue for Mondial Bony (EBIT margin 100%); the data issue predates this audit and is not PDF related. The new withdrawal path makes it disappear for a company once a newer statement lacks an operating result.

## Not verified
The hosted app, a scanned PDF, a live model reading an OIC file, a browser look at the pages. Everything above ran on synthetic
files, a fake model and Streamlit `AppTest`.
