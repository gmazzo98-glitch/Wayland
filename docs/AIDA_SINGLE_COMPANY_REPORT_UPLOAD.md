# AIDA single company report upload

In Data Admin, open **AIDA single company report upload**, select the formatted
single-company `.xlsx` report, review it, and click **Import AIDA single company
report**. This is separate from the row-based AIDA bulk exports and Flexible Data
Import. Uploading a recognizable report in Flexible Data Import points to the new tab.

The preview shows company identity, financial tables with their actual reporting
dates, people and appointments, and the complete report text with source references.
It writes nothing until confirmation. Existing indicators from other sources are
listed for comparison and kept unless their replacement is explicitly selected.
Re-uploading an existing report requires selecting its replacement.

## What went wrong in the previous implementation

- The generic mapper was given one synthetic row with approximately 150 columns,
  including a JSON string containing the whole report. Storing that string did not
  make its contents usable as company information or a readable report.
- Financial years were assigned by populated-cell position. A blank value could
  shift a year, and a missing year in the source was given an incorrect relative
  year label. The example report contains 2014 and 2016, with no 2015.
- Date headers were collected globally and effectively ignored, despite merged
  cells placing dates and amounts at different physical starting columns.
- Repeated summary and statement figures became duplicate fields instead of
  corroborating observations. People were never written to `CompanyPerson`.
- The entire ATECO description was offered as the sector code, and monetary units
  were not checked. The tests covered a small invented layout rather than the
  report's date alignment, database effects, and confirmation flow.

That parser has been removed.

## Storage and integration

`aida_single_report.py` parses and imports the report. `views/aida_single_report.py`
renders the preview and the saved report. Company Intelligence's Data & Audit view
shows the report, and its financial views can use the imported statement.

The dataset is **AIDA Single Company Report**. Its `RawImportRecord.raw_row` holds
canonical financial facts and a native JSON object `_aida_report` containing:

- every populated cell on every worksheet, with coordinates, data, number format,
  formula/cache information, comments and hyperlinks when present;
- worksheet dimensions and merged ranges, retaining the positions of blank cells;
- the source filename and SHA-256;
- every report section, including notes, unavailable modules, and ownership text;
- four financial tables for the supplied example, all source lines and dates;
- people, their source IDs, all appointments and source rows;
- canonical observations and their exact source cells, dates, labels, values and units.

Each financial table uses its own dates. A value's merged column span identifies
the date it belongs to. Blank cells remain blank; `n.d.` remains visible in the
report and becomes `None` for calculations. Conflicting repeated observations are
excluded from calculations and shown as warnings. Only explicitly recognized
financial lines enter the existing AIDA formulas. Monetary facts are normalized to
thousand EUR only for recognized EUR units; source values remain unchanged.

The report and its people, company fields and indicators save in one database
transaction. Failure rolls the whole import back. Re-import replaces only people
owned by this dataset and cannot silently overwrite indicators from other sources.
There is no automatic repair or deletion of earlier Flexible Data Import records.

## Current mapping limits

All financial lines and dates are retained and displayed, but only labels in the
parser's explicit `_METRICS` table become canonical financial facts. Those facts
feed the existing AIDA indicator formulas, with file/sheet/cell/date/unit lineage
and a stored mapping snapshot. Other financial lines remain report data and are
not automatically connected to indicators or the cost-breakdown analytics.

This flow does not yet provide Flexible Data Import's editable source-to-fact
mapping controls or saved reusable mapping profiles: the saved-report view bypasses
that editor. Re-import replaces this dataset's report rather than keeping a history
of successive upload versions. Fiscal-year series within the report are retained.

## Verification

The supplied `Mondial Bony.xlsx` was reconciled against all **3,751** populated
source cells and imported into disposable in-memory SQLite. The check found four
financial tables with **301** financial lines across ten dates, and **nine** people
with ten appointments. Revenue for 2014 remains at `revenue_y-10`, not `y-9`.

`tests/test_report_upload.py` exercises date alignment across tables, merged cells,
blank periods, repeated/conflicting observations, all-sheet preservation, identity,
people, read-only preview, confirmation, repeat imports, source conflicts, rollback,
and dossier integration. It does not require the user's private workbook in the repo.

Final verification: all 13 report tests plus the existing Data Admin tab test passed
together (14 checks). The required full suite finished with 1042 passed, 2 skipped,
and the three previously tracked failures: intermittent access-control login state,
status-file point ordering, and the stale worker bundle (OP-010).

The new flow does not deploy itself or modify production data during verification.
The earlier incorrect import, if confirmed in production, needs a separate audited
repair (OP-018).
