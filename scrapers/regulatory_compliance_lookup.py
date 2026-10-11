"""Dated EUR-Lex screening lookup for regulatory_compliance_exposure.

This module deliberately does not produce a SignalRecord. NACE and headcount
identify provisions to investigate, but do not establish legal applicability
or the existing indicator's 0-3 score. A legal review and company-specific
facts are required before a scoring producer can be added.
"""

from __future__ import annotations

import csv
import re
from functools import lru_cache
from pathlib import Path


TABLE_DATE = "2026-10-11"
TABLE_PATH = Path(__file__).with_name(f"regulatory_thresholds_{TABLE_DATE}.csv")
REQUIRED_COLUMNS = {
    "regime", "nace_division", "employees_min", "employees_max",
    "screening_status", "threshold", "missing_facts", "celex",
    "provisions", "source_urls",
}


@lru_cache(maxsize=1)
def _rules() -> tuple[dict[str, str], ...]:
    with TABLE_PATH.open(encoding="utf-8", newline="") as source:
        rows = (line for line in source if not line.startswith("#"))
        reader = csv.DictReader(rows)
        if not reader.fieldnames or not REQUIRED_COLUMNS.issubset(reader.fieldnames):
            raise ValueError("Regulatory table is missing required columns")
        rules = tuple(reader)
    for rule in rules:
        if not all(rule.get(key) for key in ("celex", "provisions", "source_urls")):
            raise ValueError(f"Missing EUR-Lex provenance for {rule.get('regime')}")
        celex_ids = rule["celex"].split(";")
        urls = rule["source_urls"].split(";")
        if len(celex_ids) != len(urls) or any(
            f"CELEX:{celex}" not in url for celex, url in zip(celex_ids, urls)
        ):
            raise ValueError(f"Mismatched CELEX URL for {rule['regime']}")
    return rules


def _division(nace_code: str | None) -> str | None:
    if nace_code is None:
        return None
    match = re.fullmatch(r"(?:C\s*)?(\d{2})(?:[.\s-]?\d{2,4})?", str(nace_code).strip(), re.IGNORECASE)
    return match.group(1) if match else None


def lookup_regulatory_exposure(nace_code: str | None, headcount: int | None) -> dict:
    """Return auditable candidates, leaving the indicator score unknown.

    A matching row means the regime needs fact gathering, never that the
    company is compliant, noncompliant, or legally within its scope.
    """
    division = _division(nace_code)
    if division is None or not isinstance(headcount, int) or isinstance(headcount, bool) or headcount < 0:
        return {
            "table_date": TABLE_DATE,
            "field_status": "missing_input",
            "screening_matches": [],
            "exposure_score": None,
        }

    matches = []
    for rule in _rules():
        if rule["nace_division"] != division:
            continue
        minimum = int(rule["employees_min"])
        maximum = int(rule["employees_max"]) if rule["employees_max"] else None
        if headcount < minimum or (maximum is not None and headcount > maximum):
            continue
        matches.append({
            "regime": rule["regime"],
            "screening_status": rule["screening_status"],
            "threshold": rule["threshold"],
            "missing_facts": rule["missing_facts"],
            "celex": rule["celex"].split(";"),
            "provisions": rule["provisions"],
            "source_urls": rule["source_urls"].split(";"),
        })

    return {
        "table_date": TABLE_DATE,
        "field_status": "needs_company_facts" if matches else "not_covered",
        "screening_matches": matches,
        "exposure_score": None,
    }
