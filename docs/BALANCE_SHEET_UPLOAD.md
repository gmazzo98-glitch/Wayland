# Balance sheet (PDF) upload and merged annual history

Data Admin, tab "Balance sheet upload (PDF)". Implements OP-019 (see `docs/FINANCIAL_YEAR_ROLLOVER_PLAN.md`).

## Flow
1. **Read** (`balance_sheet_pdf.py`): the PDF text layer; pages with no text go to OCR through the vision model configured as
   `CRAWLER_VISION_LLM_*` (no key means the page is reported as unread, never guessed). Only the primary statements are parsed
   (assets, liabilities and equity, income statement, comprehensive income); every page's text is kept as evidence.
   Dates, Italian/English headings, `1.234.567` and `1,234,567` numbers, bracketed negatives (also unclosed), wrapped labels and
   scattered-letter labels are handled. Arithmetic checks (assets = liabilities + equity, result in both statements, pre-tax + tax
   = after-tax) are shown; a failed check is a warning.
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
