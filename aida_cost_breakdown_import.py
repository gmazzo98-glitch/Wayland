"""
Imports the cost-breakdown lines AIDA's PL export doesn't carry today: Services/B7, Third-party
assets/B8, Depreciation/B10, Inventory change/B11, Risk provisions/B12, Other provisions/B13, Other
operating/B14. See painpoints.py's PROPOSED_INDICATORS["service_costs"] ("A Need-axis indicator with
no producer today; the raw exports do not carry the line") for the exact gap this closes.

Source: a sheet named "Risultati (2)" in a supplementary AIDA export workbook — AIDA always names its
result sheet "Risultati" (aida_import.read_export() expects exactly that from the live .xls files),
so "(2)" is just Excel's own disambiguation when a second same-named export sheet is pasted into the
same workbook. Unlike the official PL/CAP/SPINE/STRUCT/SHARE bundle aida_import.py reads, this sheet
is identified by "Codice fiscale" (VAT digits, no "IT" prefix), not "BvD ID number" — so it is matched
to an existing Company, never by guessing a BvD-style id into existence. A row that doesn't match an
existing company is reported as unmatched, not created — this dataset has no legal name/sector
columns to create one from correctly.

Matching, confirmed empirically against the live DB before relying on it (see the two checks below):
  1. Excel stores "Codice fiscale" as a plain number, which silently drops any leading zero — a VAT
     is always exactly 11 digits, so a purely-numeric value is zero-padded back to 11 digits before
     normalizing. Without this, only 32 of 951 rows matched; with it, 598 did.
  2. The padded value is then looked up against Company.registration_number OR Company.external_ref_id
     (both normalized the same way) — never created, only matched. registration_number alone (after
     the 2026-10-13 VAT digit-shift fix) still missed 324 real companies whose registration_number and
     BvD-sourced external_ref_id simply hold different numbers; checking both lifted the match rate to
     922 of 951 (97%). The remaining 29 are genuinely not found under either identifier and are
     reported as unmatched, not guessed.

Of the 7 lines, only `services` is wired to a scored signal: `service_costs = services / revenue`,
filling the exact named-but-unproduced indicator slot above (revenue comes from the company's already
-imported "AIDA Raw Exports" blob, not re-derived here). The other 6 lines are stored in the
RawImportRecord blob only (`x_<name>_<period>`), same "untapped series, available for a future
indicator without re-importing" convention aida_import.py itself uses — not fabricated into
indicators nobody asked for yet.

Numeric quirk specific to this workbook (confirmed by inspecting it directly): within the same 3-year
series, "Ultimo anno disp." and "Anno - 1" come through as native numbers while "Anno - 2" frequently
comes through as a comma-thousands-separated STRING (e.g. 56556, 36103, "24,503" for the same company)
- the official raw .xls exports aida_import.py reads don't have this, so its own `_num()` isn't built
for it. _num() below strips thousands separators before parsing.
"""

import math
import re
from typing import Dict, List, Optional, Tuple

import pandas as pd
from sqlalchemy.orm import Session

from aida_import import _fold, _json_safe, normalize_header, ratio, series_columns
from models import Company, RawImportRecord, SignalRecord
from utils import normalize_registration_nr

DATASET_NAME = "AIDA Cost Breakdown Supplement"
SHEET_NAME = "Risultati (2)"
PARENT_DATASET = "AIDA Raw Exports"  # where revenue_latest already lives, needed for the service_costs ratio

# (our name, AIDA header's first part) — each has 3 years: latest, y-1, y-2
SERIES_SPEC = [
    ("services", "Servizi"),                               # B7
    ("third_party_assets", "Godimento beni di terzi"),      # B8
    ("depreciation", "TOT Ammortamenti e svalut."),         # B10
    ("inventory_change", "Variazione materie"),             # B11
    ("risk_provisions", "Accantonamenti per rischi"),       # B12
    ("other_provisions", "Altri accantonamenti"),           # B13
    ("other_operating", "Oneri diversi di gestione"),       # B14
]


def _num(v) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, str):
        v = v.strip().replace(",", "")
        if not v or v.lower() in ("n.d.", "n.a.", "-"):
            return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _normalize_match_key(v) -> str:
    """A VAT is always exactly 11 digits; Excel stores "Codice fiscale" as a plain number and
    silently drops any leading zero, so a purely-numeric value is zero-padded back to 11 digits
    before normalizing — see the module docstring for the empirical check (32 -> 598 matches)."""
    s = str(v).strip()
    if s.endswith(".0"):
        s = s[:-2]
    if s.isdigit() and len(s) <= 11:
        s = s.zfill(11)
    return normalize_registration_nr(s, country="Italy")


def read_cost_breakdown_sheet(path) -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name=SHEET_NAME)
    original_headers = {normalize_header(c): str(c) for c in df.columns}
    df.columns = [normalize_header(c) for c in df.columns]
    if len(original_headers) != len(df.columns):
        raise ValueError(f"Normalizing headers in {SHEET_NAME!r} produced duplicate column names")
    cf_col = next((c for c in df.columns if _fold(c) == "codice fiscale"), None)
    if not cf_col:
        raise KeyError(f"{SHEET_NAME!r} has no 'Codice fiscale' column")
    df = df.dropna(subset=[cf_col])
    df["match_key"] = df[cf_col].apply(_normalize_match_key)
    df = df[df["match_key"] != ""].drop_duplicates("match_key").set_index("match_key")
    df.attrs["original_headers"] = original_headers
    return df


def build_records(db: Session, df: pd.DataFrame) -> Tuple[Dict[str, dict], List[str]]:
    """{match_key: {"company_id", "legal_name", "signals": {key: draft}, "blob": {...}}},
    [unmatched match_keys]."""
    cols: Dict[str, Dict[str, str]] = {}
    for base, name in SERIES_SPEC:
        found = series_columns(df, name)
        missing = {"latest", "y-1", "y-2"} - set(found)
        if missing:
            raise KeyError(f"{SHEET_NAME!r} has no {name!r} column for {sorted(missing)}")
        cols[base] = found

    match_keys = list(df.index)
    # Matched against BOTH identifiers, not registration_number alone — confirmed empirically (see
    # module docstring) that the two fields cover different, only partly-overlapping sets of real
    # companies even after the 2026-10-13 registration_number fix. registration_number wins when a
    # match_key happens to equal both (it's the field everything else in this codebase matches on).
    all_companies = db.query(Company).all()
    by_reg = {c.registration_number: c for c in all_companies}
    by_ext = {}
    for c in all_companies:
        if c.external_ref_id:
            by_ext.setdefault(_normalize_match_key(c.external_ref_id), c)

    companies = {}
    for key in match_keys:
        companies[key] = by_reg.get(key) or by_ext.get(key)
    companies = {k: v for k, v in companies.items() if v is not None}
    unmatched = sorted(set(match_keys) - set(companies))

    # revenue already lives in each company's AIDA Raw Exports blob — needed to turn the raw
    # 'services' line into the service_costs ratio; nothing here re-derives revenue itself.
    revenue_blobs = {
        r.company_id: r.raw_row for r in db.query(RawImportRecord).filter(
            RawImportRecord.dataset_name == PARENT_DATASET,
            RawImportRecord.company_id.in_([c.id for c in companies.values()]),
        ).all()
    }

    records: Dict[str, dict] = {}
    for match_key, company in companies.items():
        row = df.loc[match_key]
        n: Dict[str, Optional[float]] = {}
        for base, found in cols.items():
            for suffix, col in found.items():
                n[f"{base}_{suffix}"] = _num(row.get(col))

        services_l = n.get("services_latest")
        revenue_l = _num((revenue_blobs.get(company.id) or {}).get("revenue_latest"))
        v = ratio(services_l, revenue_l)
        signals = {}
        if v is not None:
            signals["service_costs"] = {
                "value": round(v, 4),
                "text": f"services {services_l:,.0f}k ÷ revenue {revenue_l:,.0f}k",
                "basis": "'Servizi' ÷ revenue, latest year (%) — revenue read from the existing AIDA Raw Exports blob",
                "inputs": {"services_latest": services_l, "revenue_latest": revenue_l},
            }

        blob = {"match_key": match_key, "matched_via": "registration_number" if by_reg.get(match_key) is company else "external_ref_id",
               **{f"x_{k}": v for k, v in n.items()}}
        records[match_key] = {"company_id": company.id, "legal_name": company.legal_name,
                              "signals": signals, "blob": _json_safe(blob)}
    return records, unmatched


def _aida_owned(sig: SignalRecord) -> bool:
    """Same rule as aida_import._aida_owned, scoped to this dataset: a row this importer may overwrite
    is empty, simulated, or already written by this exact dataset — never a human entry or another
    producer's value."""
    return sig.status == "not_yet_checked" or bool(sig.is_simulated) or (sig.source or "").strip() == DATASET_NAME


def _differs(a, b) -> bool:
    if a is None or b is None:
        return (a is None) != (b is None)
    return abs(float(a) - float(b)) > 1e-6 * max(1.0, abs(float(a)), abs(float(b)))


def apply_records(db: Session, records: Dict[str, dict], apply: bool = False) -> dict:
    """
    Writes the service_costs signal (insert/update, conflict-safe) and one RawImportRecord blob per
    company. dry_run (apply=False) computes the same report and writes nothing.
    """
    import json
    import uuid
    from datetime import datetime

    company_ids = [rec["company_id"] for rec in records.values()]
    existing_sigs = {
        s.company_id: s for s in db.query(SignalRecord).filter_by(signal_key="service_costs")
        .filter(SignalRecord.company_id.in_(company_ids)).all()
    }
    report = {"companies": len(records), "inserted": 0, "updated": 0, "unchanged": 0, "conflicts": []}
    now = datetime.utcnow()
    inserts, updates, blobs = [], [], []

    for match_key, rec in records.items():
        company_id = rec["company_id"]
        d = rec["signals"].get("service_costs")
        if d is not None:
            payload = json.dumps({"dataset": DATASET_NAME, "signal_key": "service_costs", "simulated": False,
                                  "basis": d["basis"], "evidence": {"method": d["basis"], "inputs": d["inputs"]}}, default=str)
            row = {"numeric_value": d["value"], "status": "present", "text_value": d["text"], "source": DATASET_NAME,
                   "confidence": 1.0, "is_simulated": False, "fetched_at": now, "raw_payload_ref": payload}
            sig = existing_sigs.get(company_id)
            if sig is None:
                inserts.append(dict(row, id=str(uuid.uuid4()), company_id=company_id, signal_key="service_costs"))
                report["inserted"] += 1
            elif not _aida_owned(sig):
                report["conflicts"].append({"match_key": match_key, "legal_name": rec["legal_name"], "held_by": sig.source})
            elif not _differs(sig.numeric_value, row["numeric_value"]) and (sig.source or "") == DATASET_NAME:
                report["unchanged"] += 1
            else:
                updates.append(dict(row, id=sig.id))
                report["updated"] += 1
        blobs.append({"id": str(uuid.uuid4()), "company_id": company_id, "dataset_name": DATASET_NAME,
                      "source_filename": SHEET_NAME, "raw_row": rec["blob"], "mapping_snapshot": None,
                      "imported_at": now, "updated_at": now})

    if not apply:
        return report

    if inserts:
        db.bulk_insert_mappings(SignalRecord, inserts)
    if updates:
        db.bulk_update_mappings(SignalRecord, updates)
    db.query(RawImportRecord).filter(RawImportRecord.dataset_name == DATASET_NAME,
                                     RawImportRecord.company_id.in_(company_ids)).delete(synchronize_session=False)
    db.bulk_insert_mappings(RawImportRecord, blobs)
    db.commit()
    return report
