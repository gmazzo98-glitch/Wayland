"""
One-off repairs for data that was imported or crawled wrongly before the 2026-09-22 audit.

Every function takes `apply=False` and, in that default dry-run mode, only REPORTS what it would
change — each reported change carries the old value, so the report doubles as the backup that
scripts/repair_data.py writes before it applies anything. All of them are idempotent: once a row is
right it no longer shows up as a change.

What was wrong, and how it was found (see the audit in memory: vienna-data-audit):
  * The curated AIDA "Main" file's `cogs_*` columns are the raw export's COSTI DELLA PRODUZIONE shifted
    by one year (latest = the previous year's figure, y-2 = the personnel-cost column), and its
    `leverage_ratio_y-1`/`_y-2` are swapped (the export orders its columns Ultimo, Anno-2, Anno-1).
    Matched column-for-column against the raw exports on 100% of 953 rows.
  * `margin_compression` held an absolute k EUR fall in margin, not percentage points; `ebitda_trend`
    held the latest EBITDA level. Both are recomputed from the stored raw rows.
  * `product_age` was fed with the company's founding year (see scrapers/company_website_crawler.py).
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, Optional

from sqlalchemy.orm import Session

from company_service import DERIVATION_BASIS, compute_group_value, detect_column_groups, _to_float
from indicators import fetch_indicator_defs
from models import RawImportRecord, SignalRecord

DERIVED_KEYS = ("margin_compression", "ebitda_trend")
FOUNDING_YEAR_SUMMARY_PREFIX = "founding / first product launch year stated as"
AIDA_DATASET = "Aida Main 50-99"


def _differs(a, b) -> bool:
    if a is None or b is None:
        return (a is None) != (b is None)
    try:
        return abs(float(a) - float(b)) > 1e-6 * max(1.0, abs(float(a)), abs(float(b)))
    except (TypeError, ValueError):
        return a != b


def _payload(existing_ref: Optional[str], **extra) -> str:
    try:
        base = json.loads(existing_ref) if existing_ref else {}
    except (TypeError, ValueError):
        base = {}
    base.update(extra)
    return json.dumps(base)


# ---- 1. derived signals recomputed from the stored raw rows ----------------------------------------

def recompute_derived_signals(db: Session, keys: Iterable[str] = DERIVED_KEYS, apply: bool = False) -> dict:
    """
    Recomputes margin_compression / ebitda_trend for every company from its stored raw import row, using
    the current compute_group_value (percentage points of revenue; a real EBITDA % change).

    Returns {"checked", "unchanged", "changes": [{company_id, signal_key, old_value, old_status,
    new_value, new_status}...]}. A company whose new value can't be computed (e.g. no revenue) moves to
    not_yet_checked rather than keeping an unusable number.
    """
    keys = set(keys)
    defs = fetch_indicator_defs(db)
    keys = {k for k in keys if k in defs}
    current = {(s.company_id, s.signal_key): s
               for s in db.query(SignalRecord).filter(SignalRecord.signal_key.in_(keys)).all()}
    report = {"checked": 0, "unchanged": 0, "changes": []}
    now = datetime.utcnow().date().isoformat()

    for rec in db.query(RawImportRecord).filter(~RawImportRecord.dataset_name.like("crawler_%")).all():
        snapshot = rec.mapping_snapshot or {}
        wanted = {base: target.split(":", 1)[1] for base, target in snapshot.items()
                  if isinstance(target, str) and target.startswith("indicator:") and target.split(":", 1)[1] in keys}
        if not wanted:
            continue
        groups = detect_column_groups(list(rec.raw_row.keys()))
        for base, key in wanted.items():
            group = groups.get(base)
            if not group:
                continue
            value, status = compute_group_value(group, rec.raw_row, key, companions=groups)
            value = value if status == "present" else None
            sig = current.get((rec.company_id, key))
            report["checked"] += 1
            old_value, old_status = (sig.numeric_value, sig.status) if sig else (None, "not_yet_checked")
            if sig and not _differs(old_value, value) and old_status == status:
                report["unchanged"] += 1
                continue
            report["changes"].append({"company_id": rec.company_id, "signal_key": key, "old_value": old_value,
                                      "old_status": old_status, "new_value": value, "new_status": status})
            if apply:
                if sig is None:
                    sig = SignalRecord(company_id=rec.company_id, signal_key=key, source=rec.dataset_name,
                                       fetched_at=rec.updated_at or datetime.utcnow())
                    db.add(sig)
                sig.numeric_value, sig.status = value, status
                sig.confidence, sig.is_simulated = 1.0, False
                sig.raw_payload_ref = _payload(sig.raw_payload_ref, dataset=rec.dataset_name, signal_key=key,
                                               basis=DERIVATION_BASIS.get(key), recomputed=now)
    if apply:
        db.commit()
    return report


# ---- 2. columns of the curated AIDA file that were shifted / swapped ------------------------------------

def load_aida_corrections(data_dir: Path) -> Dict[str, Dict[str, float]]:
    """
    Reads the original AIDA exports and returns {BvD ID: {curated_column: correct_value}} for the columns
    the curated Main file got wrong. Needs the two raw files next to each other in `data_dir`.
    """
    import pandas as pd   # local: only this one-off needs the Excel readers

    def read(pattern: str):
        found = sorted(Path(data_dir).glob(pattern))
        if not found:
            raise FileNotFoundError(f"{pattern} not found in {data_dir}")
        return pd.read_excel(found[0], sheet_name="Risultati").drop_duplicates("BvD ID number").set_index("BvD ID number")

    def col(df, prefix: str, suffix: str) -> str:
        return next(c for c in df.columns if str(c).startswith(prefix) and str(c).endswith(suffix))

    pl, cap = read("WAYLAND_FINANCIAL_PL*.xls"), read("WAYLAND_FINANCIAL_CAPACITY*.xls")
    spec = [
        (pl, "COSTI DELLA PRODUZIONE", {"cogs_latest": "Ultimo anno disp.", "cogs_y-1": "Anno - 1", "cogs_y-2": "Anno - 2"}),
        (cap, "Rapporto di indebitamento", {"leverage_ratio_y-1": "Anno - 1", "leverage_ratio_y-2": "Anno - 2"}),
    ]
    out: Dict[str, Dict[str, float]] = {}
    for df, prefix, targets in spec:
        for target, suffix in targets.items():
            series = pd.to_numeric(df[col(df, prefix, suffix)], errors="coerce")
            for bvd_id, value in series.items():
                if pd.notna(value):
                    out.setdefault(bvd_id, {})[target] = float(value)
    return out


def apply_blob_corrections(db: Session, corrections: Dict[str, Dict[str, float]],
                           dataset_name: str = AIDA_DATASET, apply: bool = False) -> dict:
    """
    Sets the corrected values on each company's stored raw row (matched on its `company_id_by_aida`),
    then refreshes the `cogs` signal from the corrected latest figure.

    Returns {"checked", "unchanged", "changes": [{company_id, dataset, column, old, new}...],
             "signal_changes": [{company_id, signal_key, old_value, new_value}...]}.
    """
    report = {"checked": 0, "unchanged": 0, "changes": [], "signal_changes": []}
    cogs = {s.company_id: s for s in db.query(SignalRecord).filter_by(signal_key="cogs").all()}
    for rec in db.query(RawImportRecord).filter_by(dataset_name=dataset_name).all():
        wanted = corrections.get(rec.raw_row.get("company_id_by_aida"))
        if not wanted:
            continue
        report["checked"] += 1
        fixes = {c: v for c, v in wanted.items() if _differs(_to_float(rec.raw_row.get(c)), v)}
        if not fixes:
            report["unchanged"] += 1
            continue
        for c, v in fixes.items():
            report["changes"].append({"company_id": rec.company_id, "dataset": dataset_name, "column": c,
                                      "old": rec.raw_row.get(c), "new": v})
        sig = cogs.get(rec.company_id)
        if "cogs_latest" in fixes and sig is not None:
            report["signal_changes"].append({"company_id": rec.company_id, "signal_key": "cogs",
                                             "old_value": sig.numeric_value, "new_value": fixes["cogs_latest"]})
        if apply:
            rec.raw_row = {**rec.raw_row, **fixes}      # a fresh dict: SQLAlchemy doesn't see in-place JSON edits
            if "cogs_latest" in fixes and sig is not None:
                sig.numeric_value, sig.status = fixes["cogs_latest"], "present"
                sig.raw_payload_ref = _payload(sig.raw_payload_ref, realigned="cogs columns re-read from the AIDA export "
                                               "(COSTI DELLA PRODUZIONE); the curated file had them shifted a year")
    if apply:
        db.commit()
    return report


# ---- 3. product_age fed with founding years ----------------------------------------------------------------

def reset_founding_year_product_age(db: Session, apply: bool = False) -> dict:
    """
    Puts every product_age signal that was really a founding year back to not_yet_checked. The year itself
    stays in the crawler's stored blob (RawImportRecord 'crawler_company_website'). Returns {"changes":
    [{company_id, old_value, old_summary}...]}.
    """
    rows = db.query(SignalRecord).filter(SignalRecord.signal_key == "product_age",
                                         SignalRecord.text_value.like(FOUNDING_YEAR_SUMMARY_PREFIX + "%")).all()
    changes = [{"company_id": s.company_id, "old_value": s.numeric_value, "old_summary": s.text_value} for s in rows]
    if apply:
        for s in rows:
            s.numeric_value, s.status, s.text_value = None, "not_yet_checked", None
            s.raw_payload_ref = json.dumps({"reset": "founding year is not the age of the core product line",
                                            "simulated": False})
        db.commit()
    return {"changes": changes}
