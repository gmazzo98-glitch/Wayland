# PDF financial history and fiscal-year rollover

Read-only investigation, 2026-10-28. Follow-up: OP-019 (not implemented).

## Confirmed source periods

Giorgio supplied four Mondial Bony Service SPA reports. Their internal statement
dates, rather than their filenames, establish these periods:

| File | Current period | Comparative period |
| --- | --- | --- |
| Bilancio 035 2022.pdf | 2022-12-31 | 2021-12-31 |
| Bilancio 035 2023 en.pdf | 2023-12-31 | 2022-12-31 |
| Bilancio 035 2024  en-US.pdf | 2024-12-31 | 2023-12-31 |
| MBS_Bilancio_2025.pdf | 2025-12-31 | 2024-12-31 |

All identify fiscal number 06038441215 and amounts in EUR. The previously examined
AIDA workbook uses thousand EUR for monetary statement figures. For example,
2024 net profit is EUR 203,823 in the PDFs versus 204 thousand EUR in AIDA.
Other differences require review: 2024 total assets are EUR 4,228,215 in the
PDF versus 4,229 thousand EUR in AIDA; do not silently declare these equivalent.

These are financial-intermediary statements. Preserve commission income, net
commissions and intermediation margin as distinct metrics. Industrial COGS or
EBITDA mappings must not be inferred from superficially similar labels.
The 2025 income statement repeats a before-tax label on line 280 despite showing
an after-tax amount; line 300 explicitly supplies annual net profit. Extraction
must retain the source labels and validate totals rather than trust label matching
alone. Translated reports also contain damaged headers and parentheses.

## Required behavior

1. Match the company by fiscal identifier. Extract and review PDF tables with
   actual period-end dates, reporting basis, currency, units and statement scope.
2. Preserve every extracted observation and source evidence (file, page, row label,
   original text/value, actual date, extraction and reviewed mapping). Monetary
   EUR values convert to thousand EUR for existing canonical calculations.
3. Merge observations by company, semantic metric, actual reporting period and
   reporting basis. Keep versions and provenance. Repeated comparative figures
   corroborate existing values; conflicting figures or restatements require review.
   No automatic precedence between an AIDA estimate and a PDF figure is assumed.
4. On confirmation of the newer annual period, set a company-wide anchor of 2025.
   Derive offsets from actual years, never column position or upload order:

   | Actual year | Display / relative period |
   | --- | --- |
   | 2025 | Latest / Y0 |
   | 2024 | Y-1 |
   | 2023 | Y-2 |
   | 2022 | Y-3 |
   | 2021 | Y-4 |

5. Historical observations and their saved connections stay bound to absolute
   periods. A 2024 connection must not become a 2025 observation when the anchor
   changes. Reusable upload templates may still express relative periods, resolved
   to actual dates when applied to a new file. Direct indicator connections also
   need explicit period semantics; moving numeric values between slots alone is
   insufficient.
6. Rebuild all relative canonical facts and affected derived signals together,
   including dossier labels, trends and valuation inputs, in one transaction.
   Clear obsolete source-owned derived values. Preserve other-source evidence and
   existing company/people data. Historical uploads cannot move the anchor backward.
7. Missing 2025 metrics remain missing for 2025. A 2024 headcount or cost figure
   can remain visible as dated historical evidence, but must not masquerade as
   Latest. Ratios and growth calculations must use compatible periods and units.
8. Preview new periods, the year-label rollover, reused connections, conversion,
   missing fields and conflicts before confirmation. Financial statement imports
   do not automatically approve new scoring weights or indicator-engine wiring.

## Current implementation gap

`aida_single_report.py` accepts an XLSX single-company report and replaces its
dataset record on re-import. `aida_report_mapping.py` derives relative periods
within that report; the dossier selects a report source rather than merging dated
facts across sources. `dossier_data.py` and valuation consume relative facts.
There is no reviewed PDF extractor or shared annual financial ledger in this flow.
The existing connection editor therefore does not yet provide this integration.

Implementation needs regression checks for 2024 AIDA plus 2025 PDF, historical
uploads after 2025, duplicate comparatives, restatements, mixed units, missing
latest metrics, preserved dated connections, source conflicts, rollback and coherent
consumer refresh. This review used local files only; no production data was changed,
no PDF upload feature was added and no hosted rollout was verified.
