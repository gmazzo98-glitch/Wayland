"""
Read-side analytics over the nine AIDA cost lines (cost_breakdown.py) - the single place the Cost Structure
dashboard and the assistant's get_cost_breakdown tool get their numbers, so they can never disagree.

Everything here is computed from the stored signals/blob; nothing is estimated. A figure that was not
imported (e.g. a revenue ratio whose revenue year could not be proven to match) is None, not a guess.
"""

from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

import cost_breakdown as cb
from models import Company, RawImportRecord, SignalRecord

DATASET_NAME = "AIDA Cost Breakdown Supplement"
MIN_NACE_PEERS = 5  # below this a sector median is not meaningful; fall back to the whole portfolio

_VALUE_KEYS = [cb.TOTAL_KEY] + [f(c) for c in cb.CATEGORY_KEYS for f in (cb.k_key, cb.share_key, cb.rev_key)]


def load_cost_frame(db: Session) -> pd.DataFrame:
    """One row per company that has cost data. Columns: company_id, legal_name, registration_number,
    nace_code, segment, total_k, then <cat>_k / <cat>_share / <cat>_rev for each category."""
    rows = (db.query(SignalRecord.company_id, SignalRecord.signal_key, SignalRecord.numeric_value)
            .filter(SignalRecord.signal_key.in_(_VALUE_KEYS), SignalRecord.status == "present",
                    SignalRecord.numeric_value.isnot(None)).all())
    if not rows:
        return pd.DataFrame(columns=["company_id", "legal_name", "registration_number", "nace_code", "segment", "total_k"])
    wide = pd.DataFrame(rows, columns=["company_id", "signal_key", "v"]).pivot_table(
        index="company_id", columns="signal_key", values="v", aggfunc="first")
    rename = {cb.TOTAL_KEY: "total_k"}
    for c in cb.CATEGORY_KEYS:
        rename.update({cb.k_key(c): f"{c}_k", cb.share_key(c): f"{c}_share", cb.rev_key(c): f"{c}_rev"})
    wide = wide.rename(columns=rename)
    for col in rename.values():
        if col not in wide.columns:
            wide[col] = np.nan
    ids = list(wide.index)
    comp = pd.DataFrame(
        [(c.id, c.legal_name, c.registration_number, c.nace_code, c.segment)
         for i in range(0, len(ids), 400)
         for c in db.query(Company).filter(Company.id.in_(ids[i:i + 400])).all()],
        columns=["company_id", "legal_name", "registration_number", "nace_code", "segment"])
    return comp.merge(wide.reset_index(), on="company_id", how="inner").reset_index(drop=True)


def load_blobs(db: Session, company_ids: Optional[List[str]] = None) -> Dict[str, dict]:
    q = db.query(RawImportRecord.company_id, RawImportRecord.raw_row).filter(RawImportRecord.dataset_name == DATASET_NAME)
    if company_ids is not None:
        q = q.filter(RawImportRecord.company_id.in_(company_ids))
    return {cid: raw for cid, raw in q.all()}


def _stats(values: pd.Series) -> Dict[str, Optional[float]]:
    v = values.dropna()
    if v.empty:
        return {"n": 0, "median": None, "p25": None, "p75": None, "p90": None}
    return {"n": int(len(v)), "median": float(v.median()), "p25": float(v.quantile(.25)),
            "p75": float(v.quantile(.75)), "p90": float(v.quantile(.90))}


def percentile_rank(values: pd.Series, x: float) -> Optional[float]:
    v = values.dropna()
    return None if v.empty else float((v <= x).mean() * 100.0)


def peer_group(frame: pd.DataFrame, nace_code: Optional[str]) -> Tuple[pd.DataFrame, str]:
    """The company's ATECO peers when there are enough of them, otherwise the whole portfolio."""
    if nace_code:
        same = frame[frame["nace_code"] == nace_code]
        if len(same) >= MIN_NACE_PEERS:
            return same, f"ATECO {nace_code} peers ({len(same)})"
    return frame, f"whole portfolio ({len(frame)})"


def company_breakdown(db: Session, company: Company, frame: Optional[pd.DataFrame] = None) -> Optional[Dict[str, Any]]:
    """Everything the cost-structure view and the assistant say about one company, in one dict."""
    frame = load_cost_frame(db) if frame is None else frame
    row = frame[frame["company_id"] == company.id]
    if row.empty:
        return None
    r = row.iloc[0]
    blob = load_blobs(db, [company.id]).get(company.id) or {}
    peers, peer_label = peer_group(frame, company.nace_code)
    lines, totals, revenue = blob.get("lines", {}), blob.get("total", {}), blob.get("revenue_year_matched", {})

    def f(x):
        return None if x is None or (isinstance(x, float) and np.isnan(x)) else float(x)

    cats = []
    for c in cb.CATEGORY_KEYS:
        series = lines.get(c, {})
        shares = {p: (series.get(p) / totals[p] * 100.0 if series.get(p) is not None and totals.get(p) else None)
                  for p in cb.PERIODS}
        share = f(r.get(f"{c}_share"))
        stats = _stats(peers[f"{c}_share"])
        gap = None if share is None or stats["median"] is None else share - stats["median"]
        trend = None if shares.get("latest") is None or shares.get("y-3") is None else shares["latest"] - shares["y-3"]
        cats.append({
            "category": c, "label": cb.LABELS[c], "k_eur": f(r.get(f"{c}_k")), "share_of_costs_pct": share,
            "pct_of_revenue": f(r.get(f"{c}_rev")), "peer_median_share_pct": stats["median"],
            "gap_vs_peers_pp": gap, "peer_percentile": percentile_rank(peers[f"{c}_share"], share) if share is not None else None,
            "share_change_pp_3y": trend, "k_eur_by_period": series, "share_by_period_pct": shares,
        })
    return {"legal_name": company.legal_name, "registration_number": company.registration_number,
            "nace_code": company.nace_code, "total_costs_k_eur": f(r.get("total_k")), "total_by_period_k_eur": totals,
            "revenue_k_eur_year_matched": revenue, "revenue_ratio_available": bool(blob.get("revenue_ratio_available")),
            "peer_basis": peer_label, "categories": cats,
            "units": "k_eur values are thousand EUR; *_pct fields are percent; *_pp fields are percentage points",
            "note": ("Shares are of total production costs and add to 100%. pct_of_revenue is only present where the "
                     "revenue fiscal year was verified to match the cost year.")}


def anomaly_table(frame: pd.DataFrame, basis: str = "share", top: int = 50, min_total_k: float = 0.0,
                  min_gap_pp: float = 5.0) -> pd.DataFrame:
    """Companies x categories ranked by how far a cost line sits from the sector's norm, as a robust z-score
    ((x - median) / (1.4826 * MAD)) within the company's ATECO peers (portfolio if too few). Big z = unusual cost mix.
    A line must also sit at least min_gap_pp percentage points from the median: on a tiny line (e.g. other charges,
    median 0.6%) a 2-point gap is a huge z-score but not a finding."""
    suffix = {"share": "_share", "rev": "_rev"}[basis]
    out = []
    groups = {n: g for n, g in frame.groupby("nace_code")}
    for nace, g in groups.items():
        ref = g if len(g) >= MIN_NACE_PEERS else frame
        for c in cb.CATEGORY_KEYS:
            col = f"{c}{suffix}"
            med, mad = ref[col].median(), (ref[col] - ref[col].median()).abs().median()
            if pd.isna(med) or not mad or pd.isna(mad):
                continue
            z = (g[col] - med) / (1.4826 * mad)
            for idx in z.dropna().index:
                out.append({"company_id": g.at[idx, "company_id"], "Company": g.at[idx, "legal_name"], "nace_code": nace,
                            "Category": cb.LABELS[c], "category": c, "Value %": g.at[idx, col], "Sector median %": med,
                            "Gap (pp)": g.at[idx, col] - med, "Robust z": float(z[idx]), "Total costs (k EUR)": g.at[idx, "total_k"]})
    df = pd.DataFrame(out)
    if df.empty:
        return df
    df = df[(df["Total costs (k EUR)"].fillna(0) >= min_total_k) & (df["Gap (pp)"].abs() >= min_gap_pp)]
    df["abs_z"] = df["Robust z"].abs()
    return df.sort_values("abs_z", ascending=False).head(top).drop(columns="abs_z").reset_index(drop=True)


def share_movers(blobs: Dict[str, dict], names: Dict[str, str], category: str, top: int = 25, rising: bool = True) -> pd.DataFrame:
    """Companies whose category share of production costs moved most between the oldest and latest filed year."""
    out = []
    for cid, blob in blobs.items():
        s, t = (blob.get("lines") or {}).get(category, {}), blob.get("total") or {}
        old, new = None, None
        for p in reversed(cb.PERIODS):  # oldest available first
            if s.get(p) is not None and t.get(p):
                old = (p, s[p] / t[p] * 100.0)
                break
        if s.get("latest") is not None and t.get("latest"):
            new = ("latest", s["latest"] / t["latest"] * 100.0)
        if old and new and old[0] != "latest":
            out.append({"Company": names.get(cid, cid), "From": old[0], "Share then %": old[1], "Share now %": new[1],
                        "Change (pp)": new[1] - old[1], "Costs now (k EUR)": t.get("latest")})
    df = pd.DataFrame(out)
    if df.empty:
        return df
    return df.sort_values("Change (pp)", ascending=not rising).head(top).reset_index(drop=True)


def sector_heatmap(frame: pd.DataFrame, basis: str = "share", min_companies: int = MIN_NACE_PEERS) -> pd.DataFrame:
    """Median cost-line share by ATECO code (rows) x category (columns); sectors with fewer companies than min are left out."""
    suffix = {"share": "_share", "rev": "_rev"}[basis]
    sizes = frame.groupby("nace_code").size()
    keep = sizes[sizes >= min_companies].index
    med = frame[frame["nace_code"].isin(keep)].groupby("nace_code")[[f"{c}{suffix}" for c in cb.CATEGORY_KEYS]].median()
    med.columns = [cb.LABELS[c] for c in cb.CATEGORY_KEYS]
    med.insert(0, "Companies", sizes[med.index])
    return med
