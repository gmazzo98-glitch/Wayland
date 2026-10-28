# AIDA single company report upload

In Data Admin, open **AIDA single company report upload**, select the formatted
single-company `.xlsx` report, review it, and click **Import AIDA single company
report**. This is separate from the row-based AIDA bulk exports and uses the same
mapping targets and financial calculations as Flexible Data Import. Uploading a
recognizable report in Flexible Data Import points to the new tab.

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

`aida_single_report.py` parses and imports the report, `aida_report_mapping.py`
evaluates reviewed connections, and `views/aida_single_report.py` renders the
preview, connection editor and saved report. Company Intelligence's Data & Audit view
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
excluded from calculations and shown as warnings. Recognized financial lines supply
the default connections; reviewed mappings can connect other lines to the same
financial facts and indicator calculations. Monetary facts are normalized to
thousand EUR only for recognized EUR units; source values remain unchanged.

The report and its people, company fields and indicators save in one database
transaction. Failure rolls the whole import back. Re-import replaces only people
owned by this dataset and cannot silently overwrite indicators from other sources.
There is no automatic repair or deletion of earlier Flexible Data Import records.

## Editable connections and reusable profiles

**Connect report fields** is available in the upload preview and on the saved report
in Company Intelligence → Data & Audit. Every financial line/date is a source value,
including unrecognized lines and unavailable values. Company details and other
labelled report fields are also available. Company identity stays anchored to the
report's fiscal identifier.

For each source value, choose the same targets as Flexible Data Import: a company
field, a canonical financial fact, a direct indicator, **Create New Indicator**, or
**Ignore**. Known source labels suggest financial connections for the latest three
years. You can change those suggestions and connect older years too. Section,
period and text filters plus 20-value pages keep the full report manageable; edits
on other pages remain in the mapping. All unconnected values stay in the report.

The **Connected indicators and calculations** preview reflects the reviewed mapping.
Canonical facts use the same derivation function as Flexible Data Import. Currency
facts are normalized to thousand EUR; direct indicators take the numeric source
value in its stated unit. Explicit percentage/day/count/ratio labels override the
generic table currency in source references. A financial point cannot be mapped to
the wrong fiscal period, and different reporting dates cannot silently overwrite
one direct indicator.

The mapping profile is saved on confirmation using the existing `ColumnMappingProfile`
table under the `aida-report::` namespace. Source identifiers use section, label and
relative fiscal period, rather than Excel row or absolute year, allowing profiles to
be reused on another company or next year's report. Each saved report also keeps its
own mapping snapshot and all source values, with file/sheet/cell/date/unit evidence.

Use **Apply report mapping** on a saved report to change connections without uploading
the file again. This recalculates indicators, clears stale values owned by this
dataset when disconnected, and leaves imported people in place. Other-source
indicators remain protected. New indicators start at context axis, weight zero; they
are created only when saving, in the same transaction as report data and the profile.

Re-import still replaces this dataset's report rather than keeping successive upload
versions. Fiscal-year series within the report remain available. Connecting directly
to existing cost indicators is supported; a complete cost-analytics supplement is not
inferred from an arbitrary selection of report lines.

## Verification

The supplied `Mondial Bony.xlsx` was reconciled against all **3,751** populated
source cells and imported into disposable in-memory SQLite. The check found four
financial tables with **301** financial lines across ten dates, and **nine** people
with ten appointments. Revenue for 2014 remains at `revenue_y-10`, not `y-9`.

`tests/test_report_upload.py` exercises date alignment across tables, merged cells,
blank periods, repeated/conflicting observations, all-sheet preservation, identity,
people, read-only preview, confirmation, repeat imports, source conflicts, rollback,
and dossier integration. It does not require the user's private workbook in the repo.

Connection verification also uses `tests/test_aida_report_mapping.py`: unknown-line
connections, existing and new indicators, profile reuse on another company/next
fiscal year, remapping without replacing people, clearing stale connections, exact
source references, invalid period/direct-indicator combinations, row units and
transaction rollback. The actual Mondial file was checked with a manually connected
ROA value: 159.87%, sourced from Page 1!Q94 on 2024-12-31, with its profile saved.

Final required full-suite verification: all 25 report/mapping tests passed;
1200 tests passed overall, 2 skipped, and 2 previously tracked failures remained
(status-file point ordering and the stale worker bundle, OP-010).

The new flow does not deploy itself or modify production data during verification.
The earlier incorrect import, if confirmed in production, needs a separate audited
repair (OP-018).
