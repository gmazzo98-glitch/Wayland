"""
The nine AIDA production-cost lines (Conto economico B6-B14) as one shared, import-free definition.

Imported by the importer (aida_cost_breakdown_import.py), the indicator catalog (indicators.py), the
dashboards and the assistant, so a category's key, label and AIDA header are written down exactly once.

The nine lines reconcile exactly to "COSTI DELLA PRODUZIONE" (checked on the live file: 951 of 951 rows),
so a category's share of that total is a true partition - the shares add to 100% by construction.

Three signals exist per category, all for the LATEST filed year (a SignalRecord holds one value):
  cost_<cat>_k      absolute amount, thousand EUR
  cost_<cat>_share  % of total production costs (same file, same year: always comparable)
  cost_<cat>_rev    % of revenue - only where the revenue year provably matches the cost year
and one total, cost_total_k. All four years live in the RawImportRecord blob for trends.
"""

from typing import Dict, List, Tuple

# key, label, AIDA header (first header part), AIDA line, one-line meaning
CATEGORIES: List[Tuple[str, str, str, str, str]] = [
    ("materials", "Raw materials & consumables", "Materie prime e consumo", "B6",
     "Purchases of raw materials, components and consumables"),
    ("services", "Purchased services", "Servizi", "B7",
     "Third-party services: subcontracting, utilities, maintenance, transport, consulting"),
    ("personnel", "Personnel", "Totale costi del personale", "B9",
     "Wages, social charges, severance and pension accruals"),
    ("third_party_assets", "Leases & rentals", "Godimento beni di terzi", "B8",
     "Rents, leases and royalties for assets the company does not own"),
    ("depreciation", "Depreciation & write-downs", "TOT Ammortamenti e svalut.", "B10",
     "Depreciation, amortisation and write-downs of fixed assets and receivables"),
    ("inventory_change", "Change in inventories", "Variazione materie", "B11",
     "Change in raw-material stock (negative = stock built up)"),
    ("risk_provisions", "Risk provisions", "Accantonamenti per rischi", "B12",
     "Provisions set aside for expected risks and losses"),
    ("other_provisions", "Other provisions", "Altri accantonamenti", "B13",
     "Other provisions"),
    ("other_operating", "Other operating charges", "Oneri diversi di gestione", "B14",
     "Taxes other than income, losses, miscellaneous operating charges"),
]
CATEGORY_KEYS = [c[0] for c in CATEGORIES]
LABELS: Dict[str, str] = {c[0]: c[1] for c in CATEGORIES}
AIDA_HEADERS: Dict[str, str] = {c[0]: c[2] for c in CATEGORIES}
MEANINGS: Dict[str, str] = {c[0]: c[4] for c in CATEGORIES}
TOTAL_HEADER = "COSTI DELLA PRODUZIONE"

# Sub-lines kept in the blob only (no signal): they explain the headline lines above.
DETAIL_LINES: List[Tuple[str, str]] = [
    ("wages", "Salari e stipendi"), ("social_charges", "Oneri sociali"), ("severance", "Tratt. fine rapporto"),
    ("pension", "Tratt. di quiescenza"), ("other_personnel", "Altri costi"),
    ("amort_intangible", "Amm. Immob. Immat."), ("amort_tangible", "Amm. Immob. Mat."),
    ("other_writedowns", "Altre svalut. Immob."), ("receivable_writedowns", "Svalut. crediti"),
    ("operating_grants", "Contributi in conto esercizio"),
]

PERIODS = ["latest", "y-1", "y-2", "y-3"]  # newest first


def k_key(cat: str) -> str:
    return f"cost_{cat}_k"


def share_key(cat: str) -> str:
    return f"cost_{cat}_share"


def rev_key(cat: str) -> str:
    return f"cost_{cat}_rev"


TOTAL_KEY = "cost_total_k"

# unit text per signal key - consumed by assistant_llm.SIGNAL_UNITS so a ratio is never called "thousands".
SIGNAL_UNITS: Dict[str, str] = {TOTAL_KEY: "thousand EUR (total production costs, latest year)"}
for _cat in CATEGORY_KEYS:
    SIGNAL_UNITS[k_key(_cat)] = f"thousand EUR ({LABELS[_cat]}, latest year)"
    SIGNAL_UNITS[share_key(_cat)] = f"% of total production costs ({LABELS[_cat]})"
    SIGNAL_UNITS[rev_key(_cat)] = f"% of revenue ({LABELS[_cat]})"


def indicator_seed_rows(category: str, source_system: str) -> List[dict]:
    """Catalog rows for every cost-breakdown signal. All context axis, weight 0: they are evidence the
    dashboards, the assistant and the pain-point rules read - they never move a Need/Readiness score."""
    rows = []
    common = dict(automation_tier="T1", category=category, axis="context", weight=0.0, phase=3,
                  source_system=source_system, freshness_days=365, example_status="-")
    rows.append(dict(common, key=TOTAL_KEY, label="Total Production Costs (k EUR)",
                     proxy="Total costs of production (B), thousand EUR, latest filed year",
                     rationale="The denominator of every share below, and the size of the cost base in absolute terms.",
                     comment="Informational. The nine category lines add up to this figure exactly.",
                     source_description="AIDA: COSTI DELLA PRODUZIONE"))
    for cat in CATEGORY_KEYS:
        lab, line = LABELS[cat], next(c[3] for c in CATEGORIES if c[0] == cat)
        rows.append(dict(common, key=k_key(cat), label=f"{lab} (k EUR)",
                         proxy=f"{lab} (B{line[1:]}) in thousand EUR, latest filed year",
                         rationale=f"{MEANINGS[cat]}. Absolute size, so a large company's spend can be compared in money terms.",
                         comment="Informational. Absolute amounts scale with company size - compare shares across companies.",
                         source_description=f"AIDA: {AIDA_HEADERS[cat]}"))
        rows.append(dict(common, key=share_key(cat), label=f"{lab} (% of costs)",
                         proxy=f"{lab} as % of total production costs, latest filed year",
                         rationale="Cost structure: where the money goes. Same file and year as the denominator, so always comparable "
                                   "and the nine shares add up to 100%.",
                         comment="Can be negative for 'Change in inventories' (stock built up). Informational, not scored.",
                         source_description=f"Computed: AIDA {AIDA_HEADERS[cat]} / COSTI DELLA PRODUZIONE"))
        rows.append(dict(common, key=rev_key(cat), label=f"{lab} (% of revenue)",
                         proxy=f"{lab} as % of revenue, latest filed year",
                         rationale="Cost intensity against sales - the margin-pressure reading. Only computed where the revenue figure "
                                   "is verified to be from the same fiscal year as the cost.",
                         comment="Left empty (never estimated) when revenue and cost come from different filings.",
                         source_description=f"Computed: AIDA {AIDA_HEADERS[cat]} / revenue, year-matched"))
    return rows
