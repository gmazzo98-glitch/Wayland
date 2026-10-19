"""
Imports AIDA's full production-cost breakdown (Conto economico B6-B14, nine lines, four fiscal years)
from the "WAYLAND_COGS_BREAKDOWN" workbook and stores every category twice: as an absolute amount in
thousand EUR and as ratios. See cost_breakdown.py for the categories and the signal keys.

Why this replaced the first version (2026-10-19): the first import took only SEVEN lines, three years,
and computed service_costs = services (new file) / revenue (read from the OLDER PL export's blob).
For about a quarter of the companies the two files are different fiscal years, so numerator and
denominator described different years and the ratio could exceed 100%. Rules now:

  * Every figure comes from this one file, so the nine lines reconcile exactly to COSTI DELLA PRODUZIONE
    and each category's SHARE of that total is always valid (shares add to 100%).
  * A revenue ratio is written only where the revenue year is PROVEN to match the cost year: the PL
    export's production-cost series is matched against this file's total by value, which pins down which
    PL year is which cost year; revenue is then taken from that same PL year. No match -> no revenue ratio
    (never estimated). Companies with a stale vintage keep their absolute and share values.
  * The old wrong service_costs rows this dataset wrote are neutralised (not_yet_checked) when no
    year-matched revenue exists, and rewritten when it does.

Identity matching (confirmed empirically, 922 of 951 rows match): "Codice fiscale" is a numeric column that
lost its leading zeros, so it is zero-padded to 11 digits, then matched to Company.registration_number or
Company.external_ref_id. Unmatched rows are reported, never created.

Numeric quirk: older-year cells often arrive as comma-thousands strings ("24,503"); _num strips the commas.
"""

import json
import math
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd
from sqlalchemy.orm import Session

import cost_breakdown as cb
from aida_import import _fold, _json_safe, normalize_header, ratio
from models import Company, RawImportRecord, SignalRecord
from utils import normalize_registration_nr

DATASET_NAME = "AIDA Cost Breakdown Supplement"
SHEET_NAME = "Risultati"
PARENT_DATASET = "AIDA Raw Exports"  # the older PL export: its revenue is used ONLY when its year provably matches

_SUFFIXES = {"ultimo anno disp.": "latest", "anno - 1": "y-1", "anno - 2": "y-2", "anno - 3": "y-3"}
_PL_PERIODS = ["latest", "y-1", "y-2"]
# Parity with the file's own rounding: a total is "the same year" if equal within 0.5 k EUR or 0.1%.
_ABS_TOL, _REL_TOL = 0.5, 0.001


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
    s = str(v).strip()
    if s.endswith(".0"):
        s = s[:-2]
    if s.isdigit() and len(s) <= 11:
        s = s.zfill(11)
    return normalize_registration_nr(s, country="Italy")


def _line_columns(df: pd.DataFrame, header: str) -> Dict[str, str]:
    """{'latest'|'y-1'|'y-2'|'y-3': column} for the line whose first header part is `header`."""
    want, out = _fold(header), {}
    for col in df.columns:
        parts = [p.strip() for p in str(col).split("|")]
        if len(parts) >= 2 and _fold(parts[0]) == want:
            suffix = _SUFFIXES.get(_fold(parts[-1]))
            if suffix:
                out[suffix] = col
    return out


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


def _same_amount(a: Optional[float], b: Optional[float]) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= max(_ABS_TOL, _REL_TOL * max(abs(a), abs(b)))


def pair_revenue(totals: Dict[str, Optional[float]], pl_blob: Optional[dict]) -> Dict[str, Optional[float]]:
    """{period: revenue (k EUR) of the SAME fiscal year as that period's cost total, or None}.
    The PL export's production_costs_<p> series is compared with this file's totals; a value match pins the
    year, and revenue is read from that PL period. Nothing is inferred when no value matches."""
    out: Dict[str, Optional[float]] = {p: None for p in cb.PERIODS}
    if not pl_blob:
        return out
    for p in cb.PERIODS:
        for q in _PL_PERIODS:
            if _same_amount(totals.get(p), _num(pl_blob.get(f"production_costs_{q}"))):
                rev = _num(pl_blob.get(f"revenue_{q}"))
                if rev is not None and rev > 0:
                    out[p] = rev
                break
    return out


def _round(v: Optional[float], nd: int = 4) -> Optional[float]:
    return None if v is None else round(v, nd)


def build_records(db: Session, df: pd.DataFrame) -> Tuple[Dict[str, dict], List[str]]:
    """{match_key: {"company_id", "legal_name", "signals": {key: draft}, "blob": {...}}}, [unmatched keys]."""
    cat_cols = {}
    for cat in cb.CATEGORY_KEYS:
        found = _line_columns(df, cb.AIDA_HEADERS[cat])
        missing = set(cb.PERIODS) - set(found)
        if missing:
            raise KeyError(f"{SHEET_NAME!r} has no {cb.AIDA_HEADERS[cat]!r} column for {sorted(missing)}")
        cat_cols[cat] = found
    total_cols = _line_columns(df, cb.TOTAL_HEADER)
    if set(total_cols) != set(cb.PERIODS):
        raise KeyError(f"{SHEET_NAME!r} has no complete {cb.TOTAL_HEADER!r} series")
    detail_cols = {key: _line_columns(df, header) for key, header in cb.DETAIL_LINES}

    all_companies = db.query(Company).all()
    by_reg = {c.registration_number: c for c in all_companies}
    by_ext: Dict[str, Company] = {}
    for c in all_companies:
        if c.external_ref_id:
            by_ext.setdefault(_normalize_match_key(c.external_ref_id), c)
    companies = {k: (by_reg.get(k) or by_ext.get(k)) for k in df.index}
    companies = {k: v for k, v in companies.items() if v is not None}
    unmatched = sorted(set(df.index) - set(companies))

    pl_blobs = {
        r.company_id: r.raw_row for r in db.query(RawImportRecord).filter(
            RawImportRecord.dataset_name == PARENT_DATASET,
            RawImportRecord.company_id.in_([c.id for c in companies.values()])).all()
    }

    records: Dict[str, dict] = {}
    for match_key, company in companies.items():
        row = df.loc[match_key]
        lines = {cat: {p: _num(row.get(col)) for p, col in cols.items()} for cat, cols in cat_cols.items()}
        totals = {p: _num(row.get(col)) for p, col in total_cols.items()}
        detail = {k: {p: _num(row.get(col)) for p, col in cols.items()} for k, cols in detail_cols.items() if cols}
        revenue = pair_revenue(totals, pl_blobs.get(company.id))

        # Does the file reconcile for this company? Kept as a flag so a dashboard can say so, never hidden.
        recon = {}
        for p in cb.PERIODS:
            parts = [lines[c][p] for c in cb.CATEGORY_KEYS]
            if totals.get(p) is not None and any(v is not None for v in parts):
                recon[p] = round(sum(v for v in parts if v is not None) - totals[p], 3)

        signals: Dict[str, dict] = {}
        tot = totals.get("latest")
        if tot is not None:
            signals[cb.TOTAL_KEY] = {"value": _round(tot, 3), "text": f"total production costs {tot:,.0f}k EUR",
                                      "basis": "COSTI DELLA PRODUZIONE, latest year, thousand EUR", "inputs": {"total": tot}}
        rev_l = revenue.get("latest")
        for cat in cb.CATEGORY_KEYS:
            v = lines[cat].get("latest")
            if v is None:
                continue
            label = cb.LABELS[cat]
            signals[cb.k_key(cat)] = {"value": _round(v, 3), "text": f"{label}: {v:,.0f}k EUR",
                                      "basis": f"AIDA '{cb.AIDA_HEADERS[cat]}', latest year, thousand EUR", "inputs": {cat: v}}
            if tot is not None and tot > 0:
                s = v / tot * 100.0
                signals[cb.share_key(cat)] = {
                    "value": _round(s), "text": f"{label}: {v:,.0f}k of {tot:,.0f}k total costs",
                    "basis": f"'{cb.AIDA_HEADERS[cat]}' / COSTI DELLA PRODUZIONE, same file and year (%)",
                    "inputs": {cat: v, "total": tot}}
            r = ratio(v, rev_l)
            if r is not None:
                signals[cb.rev_key(cat)] = {
                    "value": _round(r), "text": f"{label}: {v:,.0f}k / revenue {rev_l:,.0f}k",
                    "basis": f"'{cb.AIDA_HEADERS[cat]}' / revenue, year-matched via the PL export's cost series (%)",
                    "inputs": {cat: v, "revenue": rev_l}}
        if cb.rev_key("services") in signals:  # the scored indicator, now correct by construction
            d = dict(signals[cb.rev_key("services")])
            signals["service_costs"] = d

        blob = {"match_key": match_key,
                "matched_via": "registration_number" if by_reg.get(match_key) is company else "external_ref_id",
                "lines": lines, "total": totals, "detail": detail, "revenue_year_matched": revenue,
                "revenue_ratio_available": rev_l is not None, "reconciliation_gap": recon}
        records[match_key] = {"company_id": company.id, "legal_name": company.legal_name,
                              "signals": signals, "blob": _json_safe(blob)}
    return records, unmatched


def _aida_owned(sig: SignalRecord) -> bool:
    """A row this importer may overwrite is empty, simulated, or already written by this dataset - never a
    human entry or another producer's value."""
    return sig.status == "not_yet_checked" or bool(sig.is_simulated) or (sig.source or "").strip() == DATASET_NAME


def _differs(a, b) -> bool:
    if a is None or b is None:
        return (a is None) != (b is None)
    return abs(float(a) - float(b)) > 1e-6 * max(1.0, abs(float(a)), abs(float(b)))


MANAGED_KEYS = [cb.TOTAL_KEY] + [f(c) for c in cb.CATEGORY_KEYS for f in (cb.k_key, cb.share_key, cb.rev_key)] + ["service_costs"]


def apply_records(db: Session, records: Dict[str, dict], apply: bool = False) -> dict:
    """Conflict-safe bulk upsert of every managed signal plus one blob per company. apply=False computes the
    identical report and writes nothing. A managed signal this dataset wrote earlier but can no longer
    support (e.g. a service_costs ratio whose revenue year does not match) is withdrawn, not left stale."""
    company_ids = [rec["company_id"] for rec in records.values()]
    existing: Dict[Tuple[str, str], SignalRecord] = {}
    for i in range(0, len(company_ids), 400):
        for s in db.query(SignalRecord).filter(SignalRecord.signal_key.in_(MANAGED_KEYS),
                                                SignalRecord.company_id.in_(company_ids[i:i + 400])).all():
            existing[(s.company_id, s.signal_key)] = s
    report = {"companies": len(records), "inserted": 0, "updated": 0, "unchanged": 0, "withdrawn": 0,
              "conflicts": [], "by_key": {}}
    now = datetime.utcnow()
    inserts, updates, blobs = [], [], []

    for match_key, rec in records.items():
        cid = rec["company_id"]
        for key in MANAGED_KEYS:
            d, sig = rec["signals"].get(key), existing.get((cid, key))
            if d is None:
                if sig is not None and (sig.source or "").strip() == DATASET_NAME and sig.status == "present":
                    updates.append({"id": sig.id, "numeric_value": None, "status": "not_yet_checked", "text_value": None,
                                    "fetched_at": now, "raw_payload_ref": None})
                    report["withdrawn"] += 1
                continue
            payload = json.dumps({"dataset": DATASET_NAME, "signal_key": key, "simulated": False, "basis": d["basis"],
                                  "evidence": {"method": d["basis"], "inputs": d["inputs"]}}, default=str)
            row = {"numeric_value": d["value"], "status": "present", "text_value": d["text"], "source": DATASET_NAME,
                   "confidence": 1.0, "is_simulated": False, "fetched_at": now, "raw_payload_ref": payload}
            if sig is None:
                inserts.append(dict(row, id=str(uuid.uuid4()), company_id=cid, signal_key=key))
                report["inserted"] += 1
            elif not _aida_owned(sig):
                report["conflicts"].append({"match_key": match_key, "legal_name": rec["legal_name"],
                                            "signal_key": key, "held_by": sig.source})
            elif (sig.source or "") == DATASET_NAME and sig.status == "present" and not _differs(sig.numeric_value, row["numeric_value"]):
                report["unchanged"] += 1
            else:
                updates.append(dict(row, id=sig.id))
                report["updated"] += 1
            report["by_key"][key] = report["by_key"].get(key, 0) + 1
        blobs.append({"id": str(uuid.uuid4()), "company_id": cid, "dataset_name": DATASET_NAME,
                      "source_filename": SHEET_NAME, "raw_row": rec["blob"], "mapping_snapshot": None,
                      "imported_at": now, "updated_at": now})

    if not apply:
        return report
    if inserts:
        db.bulk_insert_mappings(SignalRecord, inserts)
    if updates:
        db.bulk_update_mappings(SignalRecord, updates)
    for i in range(0, len(company_ids), 400):
        db.query(RawImportRecord).filter(RawImportRecord.dataset_name == DATASET_NAME,
                                         RawImportRecord.company_id.in_(company_ids[i:i + 400])).delete(synchronize_session=False)
    db.bulk_insert_mappings(RawImportRecord, blobs)
    db.commit()
    return report
