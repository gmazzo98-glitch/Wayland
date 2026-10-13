"""
Live ATECO/NACE peer-cost benchmarking — median and percentile spread of each cost-pressure signal
across companies sharing a sector code (Company.nace_code). Replicates the *method* a one-off manual
spreadsheet analysis used (group by ATECO code, compute median/IQR/percentiles per cost metric), but
computed from live SignalRecord data and cached in SectorCostBenchmark, so it can't go stale the way
a frozen snapshot would the moment new financials land.

Before this module, the only "percentile" figures anywhere in this codebase were hardcoded prose from
a one-off manual read of "the real portfolio" (see painpoints.py's calibration notes, e.g. "machinery
makers... median 44%") — a single flat number for the whole portfolio, not broken out by sector.

    from ateco_cost_benchmarks import recompute_ateco_cost_benchmarks, compute_materials_cost_ratio_peer_gap
    recompute_ateco_cost_benchmarks(db, apply=True)
    compute_materials_cost_ratio_peer_gap(db, apply=True)

Run on demand (a script or a UI button), not on every page load — see scripts/recompute_ateco_benchmarks.py.
"""

import uuid
from datetime import datetime
from typing import Dict, Iterable, List

import pandas as pd
from sqlalchemy.orm import Session

from models import Company, SectorCostBenchmark, SignalRecord

MIN_PEERS_FOR_PERCENTILES = 5  # below this, p10/p25/p75/p90 are left None — not meaningful on a handful of peers

# The cost-pressure signals worth benchmarking by sector. Kept as an explicit list rather than "every
# signal with redundancy_group COST_PRESSURE" so a new unrelated indicator joining that group later
# doesn't silently start getting benchmarked without a decision to include it.
BENCHMARKED_SIGNALS = [
    "materials_cost", "labour_cost", "service_costs", "cogs_ratio", "logistics_cost", "energy_cost",
    "materials_cost_ratio_trend",
]


def recompute_ateco_cost_benchmarks(db: Session, signal_keys: Iterable[str] = BENCHMARKED_SIGNALS,
                                     apply: bool = False) -> Dict[str, int]:
    """
    Recomputes every (nace_code, signal_key) benchmark from live, present SignalRecord values and
    upserts SectorCostBenchmark. Returns {"groups_computed": int, "groups_too_small_for_percentiles": int}.
    dry_run (apply=False) computes the same counts and writes nothing.
    """
    rows = (
        db.query(Company.nace_code, SignalRecord.signal_key, SignalRecord.numeric_value)
        .join(SignalRecord, SignalRecord.company_id == Company.id)
        .filter(SignalRecord.signal_key.in_(list(signal_keys)), SignalRecord.status == "present",
                SignalRecord.numeric_value.isnot(None), Company.nace_code.isnot(None))
        .all()
    )
    df = pd.DataFrame(rows, columns=["nace_code", "signal_key", "numeric_value"])

    computed, too_small, upserts = 0, 0, []
    now = datetime.utcnow()
    for (nace_code, signal_key), group in df.groupby(["nace_code", "signal_key"]):
        values = group["numeric_value"]
        peer_count = len(values)
        enough = peer_count >= MIN_PEERS_FOR_PERCENTILES
        computed += 1
        too_small += int(not enough)
        upserts.append({
            "nace_code": nace_code, "signal_key": signal_key, "peer_count": peer_count,
            "median": float(values.median()),
            "p10": float(values.quantile(0.10)) if enough else None,
            "p25": float(values.quantile(0.25)) if enough else None,
            "p75": float(values.quantile(0.75)) if enough else None,
            "p90": float(values.quantile(0.90)) if enough else None,
            "computed_at": now,
        })

    if not apply:
        return {"groups_computed": computed, "groups_too_small_for_percentiles": too_small}

    existing = {(b.nace_code, b.signal_key): b for b in db.query(SectorCostBenchmark)
                .filter(SectorCostBenchmark.signal_key.in_(list(signal_keys))).all()}
    for row in upserts:
        key = (row["nace_code"], row["signal_key"])
        b = existing.get(key)
        if b is None:
            db.add(SectorCostBenchmark(id=str(uuid.uuid4()), **row))
        else:
            for field, value in row.items():
                setattr(b, field, value)
    db.commit()
    return {"groups_computed": computed, "groups_too_small_for_percentiles": too_small}


def compute_materials_cost_ratio_peer_gap(db: Session, apply: bool = False) -> Dict[str, int]:
    """
    Writes materials_cost_ratio_peer_gap = company's materials_cost signal minus its sector's cached
    materials_cost median (from SectorCostBenchmark — run recompute_ateco_cost_benchmarks first).
    Same conflict rule as the AIDA importers: only overwrites a row this exact signal already owns,
    empty, or simulated — never a human entry or another producer's value.
    """
    benchmarks = {b.nace_code: b.median for b in db.query(SectorCostBenchmark)
                  .filter_by(signal_key="materials_cost").all() if b.median is not None}
    companies = {c.id: c.nace_code for c in db.query(Company).all()}
    materials_sigs = {
        s.company_id: s.numeric_value for s in
        db.query(SignalRecord).filter_by(signal_key="materials_cost", status="present").all()
        if s.numeric_value is not None
    }
    existing_sigs = {s.company_id: s for s in db.query(SignalRecord).filter_by(signal_key="materials_cost_ratio_peer_gap").all()}

    written = skipped_no_benchmark = conflicts = unchanged = 0
    now = datetime.utcnow()
    for company_id, materials_value in materials_sigs.items():
        nace_code = companies.get(company_id)
        median = benchmarks.get(nace_code)
        if median is None:
            skipped_no_benchmark += 1
            continue
        value = round(materials_value - median, 4)
        sig = existing_sigs.get(company_id)
        if sig and not (sig.status == "not_yet_checked" or sig.is_simulated or (sig.source or "") == "Computed: ATECO peer benchmark"):
            conflicts += 1
            continue
        if sig and sig.status == "present" and sig.numeric_value is not None and abs(sig.numeric_value - value) < 1e-6:
            unchanged += 1
            continue
        written += 1
        if not apply:
            continue
        if not sig:
            sig = SignalRecord(company_id=company_id, signal_key="materials_cost_ratio_peer_gap")
            db.add(sig)
        sig.numeric_value, sig.status, sig.confidence, sig.is_simulated = value, "present", 1.0, False
        sig.source, sig.fetched_at = "Computed: ATECO peer benchmark", now
        sig.text_value = f"materials_cost {materials_value:.1f}% vs sector ({nace_code}) median {median:.1f}%"

    if apply:
        db.commit()
    return {"written": written, "unchanged": unchanged, "conflicts": conflicts, "no_benchmark_for_sector": skipped_no_benchmark}
