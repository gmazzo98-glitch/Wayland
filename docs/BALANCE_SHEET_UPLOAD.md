# Balance sheet (PDF) upload and merged annual history

Data Admin, **Flexible Data Import** (upload a `.pdf` next to csv/xlsx; there is no separate tab any more, see "The PDF is a
carrier" below). Implements OP-019 (see `docs/FINANCIAL_YEAR_ROLLOVER_PLAN.md`).

## Flow
1. **Read** (`balance_sheet_pdf.py`): the PDF text layer; pages with no text go to OCR through the vision model configured as
   `CRAWLER_VISION_LLM_*` (no key means the page is reported as unread, never guessed). Only the primary statements are parsed
   (assets, liabilities and equity, income statement, comprehensive income); every page's text is kept as evidence.
   Dates, Italian/English headings, `1.234.567` and `1,234,567` numbers, bracketed negatives (also unclosed), wrapped labels and
   scattered-letter labels are handled. Two Italian layouts are read: the Bank of Italy one (items `10.`, costs in brackets, as in
   the Mondial Bony files) and the OIC one of industrial companies (items `6)`, unnumbered section headings such as
   `B) IMMOBILIZZAZIONI` or `9) per il personale:`, costs printed positive, `Totale ...` lines). Section headings are kept out of the
   row labels, otherwise the first row under each heading would read "B) IMMOBILIZZAZIONI I - Immobilizzazioni ... Totale ..." and
   no mapping rule would find revenue, cash or debt. Arithmetic checks (assets = liabilities + equity, result in both statements, pre-tax + tax
   = after-tax, value of production minus production costs = operating result) are shown; a failed check is a warning.
2. **Map**: each line gets a year-independent id (statement + item number or label), so a saved profile
   (`ColumnMappingProfile` prefix `balance-sheet::`) carries over to next year's PDF. Rules propose facts (total assets, cash,
   net result, personnel costs, intangibles, revenue ...); a financial intermediary's commission income is proposed as revenue at
   60% with a note, never as EBITDA or COGS. "Ask the AI" adds proposals for open lines through the crawler LLM; they are
   validated against the allowed fact list and applied only when the operator picks them or accepts those above 80%.
   Targets are the Flexible Data Import ones: financial fact, direct indicator, new zero-weight indicator, ignore.
3. **Merge** (`financial_ledger.py`): observations are keyed by fact and actual fiscal year. AIDA figures are thousand EUR and
   rounded, statements are EUR (converted); a difference within rounding is corroboration, not a conflict
   (203,823 EUR = 204 k EUR). AIDA prints whole thousands with a dot as thousands separator ("Totale Attivo 4.229" = 4,229 k EUR =
   4,229,000 EUR) and can differ from the filed euro figure by up to one thousand (the filed 4,228,215 EUR vs 4,229 k), so a
   difference of at most 1 k EUR against an AIDA figure counts as the same figure and the exact statement value is stored;
   anything larger is a conflict. The newest year anywhere is the anchor (Latest);
   Y-1, Y-2 ... count down from it. An older upload never moves the anchor back; a fact a source does not report stays missing
   (2025 headcount, EBIT: the 2024 value stays as Y-1 history and the indicators that used it as Latest are withdrawn).
4. **Conflicts**: balance sheet wins by default (newest statement among statements); every conflicting fact/year and every
   indicator whose stored value would change has an operator choice. Choices are stored in the merged record.
5. **Import** is one transaction: company (created from the fiscal code if absent), the statement record
   (`Balance Sheet PDF <period end>`, with line, page, printed value and mapping), the merged record (`Merged Annual Financials`,
   same `<fact>_latest/_y-n` shape as an AIDA blob, so Company Intelligence reads it) and the signals (source
   "Merged annual financials", payload with file/page/line evidence). An AIDA report imported later triggers the same re-merge.

## Y-n in the AIDA single-company report
Default connections now cover every reporting date in the file, counted down from the latest year by actual date (a missing
year stays a gap: 2014 is Y-10). Trends and ratios keep the three-point window (Latest, Y-1, Y-2) so connecting history does not
turn a two-year growth into a ten-year one.

## Not done
Several PDFs in one go (upload one at a time, any order); cash-flow and equity-movement statements; notes to the accounts;
consolidated vs separate statements as distinct bases; OCR quality has not been verified live (no vision key in the test run).

## The PDF is a carrier, the data is the point (update)
- **Entry point:** Data Admin, Flexible Data Import accepts a PDF next to csv/xlsx (the separate tab is gone). The document flow
  below then behaves like any other import: connect, review, confirm.
- **AI reads first:** the OCR/text layer goes to the configured language model (`CRAWLER_LLM_*`), which extracts every printed row
  (statements, sub-lines, figures outside the statements such as average headcount) and the company details (legal form, share
  capital, REA ...). Each item is kept only if its label and printed numbers are on that page; numbers are always parsed from the
  printed text, never taken from the model's arithmetic. The rule-based reader runs alongside as a cross-check: lines both read are
  confirmed, lines only the AI read are added and flagged, numeric disagreements keep the printed-text parse and say so. No model
  configured = rules only, with a notice.
- **Flexible-import style mapping:** each line connects to a company field (e.g. legal form, headcount), a financial fact for every
  year of the line (incl. employees, not divided by 1000), an existing indicator, a new indicator, or ignore; reusable profile.
- **One database:** the extracted facts join the same annual history as AIDA (merged by fiscal year, conflicts decided by the
  operator, balance sheet default), feed the same signals, company fields, dossier, valuation and audit. The stored statement is only
  provenance (page, printed value, connection).

## After the import: where the data shows up (2026-10-29 audit)
- **Company Intelligence, Financials:** the statement is titled "merged annual history" when the merged record is the newest; the
  "Full annual history" expander lists every year and fact with its source and the open conflicts; one expander per uploaded
  statement shows every printed line with page and connection and has **Remove statement and re-merge** (tick the box first). A
  removal re-merges what is left (an older AIDA year becomes Latest again) and withdraws what rested only on that statement.
- **Company Intelligence, Overview:** the Identity block shows the company details the statement printed that the Company table has
  no column for (share capital, REA, registered office ...). The "Is it growing?" headline reads the record behind the stored trend
  signal (the merged history), over the same Latest / Y-1 / Y-2 window the trend uses, never an older AIDA blob.
- **Year signal:** `last_accounts_year` follows the newest year of the merged history, so the stale-accounts flag and header chip
  stop saying "FY2024, FY2025 is not in AIDA yet" once a FY2025 statement is in. A registry value that is already later is kept.
- **AIDA bulk results:** indicators the bulk export computed from a Latest that the merged history no longer has (EBIT margin when the
  newest statement has no operating result, revenue per employee without a headcount) are offered for withdrawal in the import
  preview like the single-report ones (the operator can keep them) and withdrawn by default.
- **Headcount:** when `number_of_employees` is written from the statement the company's headcount, tier T1 and segment follow it,
  as the AIDA report import does. Keeping the old signal value keeps the old headcount.
- **Re-merge on every source:** an AIDA single-company report import and a bulk AIDA export (`aida_import.apply_records`) both
  re-merge a company that has statement history (the report says `re_merged`), so the merged record never lags behind a source.
- **Dates:** a statement line connected to Incorporation Date must read as a date (ISO or dd/mm/yyyy), otherwise the plan says so.
- **Valuation** reads the merged record when it has the newest fiscal year. A statement-only year has no EBITDA, so the valuation
  says "Revenue and EBITDA for the latest year are not available" until EBITDA exists (OP-020).
- **Not following the PDF yet:** the diagnosis peer cohort (`diagnosis/peers.py`) still reads only the bulk AIDA blob (OP-021).

## How a new statement is given its year (and its place in the trends)
1. **Each figure column is dated.** The dates printed over the columns (`STATO PATRIMONIALE ATTIVO 31-12-2025 31-12-2024`) are the
   evidence of which year each number belongs to, in the order the PDF prints them (a statement that prints the older year first
   is read correctly). If only years are printed (`Esercizio 2025 Esercizio 2024`) the month and day come from the cover or from
   the operator. A date elsewhere in the text (cover, approval date) is only the fallback; when it contradicts the column
   headings the headings win and the page says so. Italian dates in words (`31 dicembre 2025`) are read.
2. **No readable date:** the columns stay placeholders and the page asks for the closing date of the newest column (prefilled with
   31 December of the year the headings print, empty when none). Entering it dates the columns (`balance_sheet_pdf.apply_period_end`);
   the import is refused while columns are undated, so a placeholder can never reach the history.
3. **Year of a column = the calendar year of its closing date**, the same rule AIDA's close date gives, so a 30 June year end is the
   year it closes in and meets AIDA's figures by that year.
4. **The preview shows the placement** ("Where this statement's years land"): per fiscal year, what it becomes in the history
   (Latest, Y-1 ...), how many figures are new, confirm another source or differ from it. Implausible years (a future one, one before
   1990, a jump of more than three years) are warned about before anything is saved.
5. **The history is rebuilt from dated observations**, never from the order of the uploads: the newest year anywhere is Latest, every
   other year counts down from it by actual year, a missing year stays a gap, a restated comparative is a conflict the newest
   statement wins by default and the operator can overrule. The trend indicators compare Latest with two years back, else one year
   back, and are not computed when neither exists (a gap is never bridged). `tests/test_year_placement.py` checks all this against
   an oracle computed from the inputs, over every upload order and with gaps.
6. **Charts and tables** of the dossier name the fiscal years (FY2023, FY2024 ...) and keep time order; a year with no value is a gap.
7. **Left out, and said so:** a dataset that holds Latest / Y-n figures but no closing date cannot be placed in a year; the preview
   names it.
Limits: the reader expects two figure columns (current and previous year). A company that changes its year end can have two
statements closing in one calendar year, which shows as a conflict for that year.
