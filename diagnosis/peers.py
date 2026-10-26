"""
Peer engine (ticket D03): peer groups, widening, and percentiles for every financial ratio.

Answers: "Is a 2% EBITDA margin bad?" against the company's real peers.
Implements:
  1. Peer group selection with ATECO hierarchy widening (6 -> 5 -> 4 -> 3 -> 2 digits) until n >= 12.
  2. Bulk metric extraction across the cohort from AIDA raw exports and cost breakdown.
  3. Mid-rank percentile computation among peers excluding the company itself.
  4. Cached statistics table dx_peer_stats so portfolio runs are instantaneous lookups.
  5. Size cuts (headcount terciles) and secondary regional cuts.
  6. CLI interface: python -m diagnosis.peers --rebuild
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from diagnosis.contracts import Fact, GRADE_A, FACT_PRESENT
from diagnosis.factkeys import PEER_METRICS
from diagnosis.models_peers import DxPeerStats
from models import Company, RawImportRecord, SignalRecord

MIN_PEERS_DEFAULT = 12


def format_ateco_code(code: str) -> str:
    """Format an ATECO code with standard dot notation: '289300' -> '28.93.00'."""
    c = "".join(ch for ch in str(code) if ch.isdigit())
    if len(c) == 6:
        return f"{c[:2]}.{c[2:4]}.{c[4:6]}"
    elif len(c) == 5:
        return f"{c[:2]}.{c[2:4]}.{c[4]}"
    elif len(c) == 4:
        return f"{c[:2]}.{c[2:4]}"
    elif len(c) == 3:
        return f"{c[:2]}.{c[2]}"
    elif len(c) == 2:
        return c
    return str(code)


def clean_ateco_code(code: Any) -> str:
    """Extract digits from an ATECO code."""
    if code is None:
        return ""
    digits = "".join(ch for ch in str(code) if ch.isdigit())
    return digits


def resolve_peer_group(
    ateco_code: str,
    cohort_counts: Dict[str, int],
    min_peers: int = MIN_PEERS_DEFAULT,
) -> Tuple[str, str, int, str]:
    """
    Resolve peer group with hierarchy widening.
    Returns: (group_code, group_level, group_n, group_label)
    Levels:
      - '6-digit': exact code, length 6
      - '5-digit': prefix 5
      - '4-digit': prefix 4
      - '3-digit': prefix 3
      - '2-digit': prefix 2
      - 'all': whole cohort
    """
    clean = clean_ateco_code(ateco_code)
    levels = [
        ("6-digit", clean[:6] if len(clean) >= 6 else clean),
        ("5-digit", clean[:5] if len(clean) >= 5 else clean),
        ("4-digit", clean[:4] if len(clean) >= 4 else clean),
        ("3-digit", clean[:3] if len(clean) >= 3 else clean),
        ("2-digit", clean[:2] if len(clean) >= 2 else clean),
    ]

    for lvl_name, prefix in levels:
        if not prefix:
            continue
        n = cohort_counts.get(prefix, 0)
        if n >= min_peers:
            label = f"ATECO {format_ateco_code(prefix)}, 50-99 staff, Italy"
            return prefix, lvl_name, n, label

    total_n = cohort_counts.get("all", sum(cohort_counts.get(c, 0) for c in cohort_counts if len(c) == 6) or len(cohort_counts))
    return "all", "all", total_n, "All machinery makers (NACE C28), 50-99 staff, Italy"


def compute_mid_rank_percentile(value: Optional[float], peer_values: Iterable[float]) -> Optional[float]:
    """
    Compute mid-rank percentile of `value` among `peer_values`.
    Percentile = (count(peers < value) + 0.5 * count(peers == value)) / N * 100.0.
    Returns None if peer_values is empty or value is None/not finite.
    Monotone in `value`.
    """
    if value is None or not math.isfinite(value):
        return None

    peers = [v for v in peer_values if v is not None and math.isfinite(v)]
    n = len(peers)
    if n == 0:
        return None

    strictly_less = sum(1 for p in peers if p < value)
    equal_to = sum(1 for p in peers if abs(p - value) < 1e-9)
    mid_rank = strictly_less + 0.5 * equal_to
    pctile = (mid_rank / n) * 100.0
    return round(float(pctile), 1)


def compute_group_stats(values: Iterable[float]) -> Dict[str, Optional[float]]:
    """Compute summary statistics for a peer distribution."""
    valid = sorted([float(v) for v in values if v is not None and math.isfinite(v)])
    n = len(valid)
    if n == 0:
        return {
            "peer_count": 0, "median": None, "p10": None, "p25": None,
            "p75": None, "p90": None, "mean": None, "std": None,
            "min_value": None, "max_value": None,
        }

    s = pd.Series(valid)
    return {
        "peer_count": n,
        "median": float(s.median()),
        "p10": float(s.quantile(0.10)) if n >= 5 else None,
        "p25": float(s.quantile(0.25)) if n >= 5 else None,
        "p75": float(s.quantile(0.75)) if n >= 5 else None,
        "p90": float(s.quantile(0.90)) if n >= 5 else None,
        "mean": float(s.mean()),
        "std": float(s.std()) if n >= 2 else 0.0,
        "min_value": float(s.min()),
        "max_value": float(s.max()),
    }


def compute_headcount_terciles(headcounts: pd.Series) -> Dict[str, Tuple[float, float]]:
    """Compute low, medium, high tercile boundaries for headcount."""
    valid = headcounts.dropna()
    if len(valid) < 3:
        return {}
    q1 = float(valid.quantile(1.0 / 3.0))
    q2 = float(valid.quantile(2.0 / 3.0))
    return {
        "tercile_1_small": (float(valid.min()), q1),
        "tercile_2_medium": (q1, q2),
        "tercile_3_large": (q2, float(valid.max())),
    }


def load_cohort_dataframe(db: Session) -> pd.DataFrame:
    """
    Load all companies and extract financial metrics in bulk from AIDA raw exports,
    cost breakdown supplement, and SignalRecords.
    Returns a DataFrame indexed by company_id.
    """
    companies = db.query(Company).all()
    if not companies:
        return pd.DataFrame()

    c_map = {c.id: c for c in companies}
    company_ids = list(c_map.keys())

    # Bulk query AIDA raw exports (financials)
    aida_records = (
        db.query(RawImportRecord.company_id, RawImportRecord.raw_row)
        .filter(RawImportRecord.dataset_name == "AIDA Raw Exports")
        .all()
    )
    aida_map = {r.company_id: (r.raw_row or {}) for r in aida_records}

    # Bulk query Cost Breakdown records
    cost_records = (
        db.query(RawImportRecord.company_id, RawImportRecord.raw_row)
        .filter(RawImportRecord.dataset_name == "AIDA Cost Breakdown Supplement")
        .all()
    )
    cost_map = {r.company_id: (r.raw_row or {}) for r in cost_records}

    # Bulk query real signals that might exist
    signals = (
        db.query(SignalRecord.company_id, SignalRecord.signal_key, SignalRecord.numeric_value)
        .filter(
            SignalRecord.signal_key.in_([
                "service_costs", "cost_inventory_change_rev", "capex_ratio",
                "interest_coverage_ratio", "leverage_ratio"
            ]),
            SignalRecord.status == "present",
            SignalRecord.numeric_value.isnot(None),
            SignalRecord.is_simulated.is_(False),
        )
        .all()
    )
    sig_map: Dict[Tuple[str, str], float] = {(s.company_id, s.signal_key): float(s.numeric_value) for s in signals}

    rows = []
    for cid, c in c_map.items():
        r = aida_map.get(cid, {})
        cb_raw = cost_map.get(cid, {})

        rev_l = _f(r.get("revenue_latest"))
        rev_y1 = _f(r.get("revenue_y-1"))
        ebitda_l = _f(r.get("ebitda_latest"))
        ebit_l = _f(r.get("ebit_latest"))
        gm_l = _f(r.get("gross_margin_latest"))
        pers_l = _f(r.get("personnel_costs_latest"))
        mat_l = _f(r.get("materials_latest"))
        emp_l = _f(r.get("employees_latest")) or (_f(c.headcount) if c.headcount else None)
        eq_l = _f(r.get("equity_latest"))
        ta_l = _f(r.get("total_assets_latest"))
        cash_l = _f(r.get("cash_latest"))
        ic_l = _f(r.get("interest_coverage_latest"))

        # Capex calculation from raw components if signal not present
        mat_cap = _f(r.get("material_capex_latest"))
        immat_cap = _f(r.get("immaterial_capex_latest"))
        cap_parts = [x for x in (mat_cap, immat_cap) if x is not None]
        tot_capex = -sum(cap_parts) if cap_parts else None

        # Cost breakdown values (year-matched)
        cb_rev_matched = (cb_raw.get("revenue_year_matched") or {}).get("latest")
        cb_rev = _f(cb_rev_matched)
        cb_lines = cb_raw.get("lines") or {}
        inv_change_val = _f((cb_lines.get("inventory_change") or {}).get("latest"))
        services_val = _f((cb_lines.get("services") or {}).get("latest"))

        row: Dict[str, Any] = {
            "company_id": cid,
            "legal_name": c.legal_name,
            "nace_code": clean_ateco_code(c.nace_code),
            "headcount": emp_l,
            "region": c.region,
        }

        # 1. revenue_growth_pct: (rev_latest - rev_y-1) / rev_y-1 * 100.0
        row["revenue_growth_pct"] = (
            (rev_l - rev_y1) / rev_y1 * 100.0
            if (rev_l is not None and rev_y1 is not None and rev_y1 > 0)
            else np.nan
        )

        # 2. ebitda_margin_pct: ebitda / revenue * 100.0
        row["ebitda_margin_pct"] = (
            ebitda_l / rev_l * 100.0
            if (rev_l is not None and rev_l > 0 and ebitda_l is not None)
            else np.nan
        )

        # 3. ebit_margin_pct: ebit / revenue * 100.0
        row["ebit_margin_pct"] = (
            ebit_l / rev_l * 100.0
            if (rev_l is not None and rev_l > 0 and ebit_l is not None)
            else np.nan
        )

        # 4. gross_margin_pct: gross_margin / revenue * 100.0
        row["gross_margin_pct"] = (
            gm_l / rev_l * 100.0
            if (rev_l is not None and rev_l > 0 and gm_l is not None)
            else np.nan
        )

        # 5. personnel_pct_revenue: personnel / revenue * 100.0
        row["personnel_pct_revenue"] = (
            pers_l / rev_l * 100.0
            if (rev_l is not None and rev_l > 0 and pers_l is not None)
            else np.nan
        )

        # 6. materials_pct_revenue: materials / revenue * 100.0
        row["materials_pct_revenue"] = (
            mat_l / rev_l * 100.0
            if (rev_l is not None and rev_l > 0 and mat_l is not None)
            else np.nan
        )

        # 7. service_costs_pct_revenue: from signal or cost breakdown
        srv_sig = sig_map.get((cid, "service_costs"))
        if srv_sig is not None:
            row["service_costs_pct_revenue"] = srv_sig
        elif services_val is not None and cb_rev is not None and cb_rev > 0:
            row["service_costs_pct_revenue"] = services_val / cb_rev * 100.0
        else:
            row["service_costs_pct_revenue"] = np.nan

        # 8. revenue_per_employee_k: revenue / employees
        row["revenue_per_employee_k"] = (
            rev_l / emp_l
            if (rev_l is not None and emp_l is not None and emp_l > 0)
            else np.nan
        )

        # 9. equity_ratio_pct: equity / total_assets * 100.0 (rule: exclude equity <= 0)
        row["equity_ratio_pct"] = (
            eq_l / ta_l * 100.0
            if (eq_l is not None and eq_l > 0 and ta_l is not None and ta_l > 0)
            else np.nan
        )

        # 10. leverage_x: total_assets / equity (rule: exclude equity <= 0)
        lev_sig = sig_map.get((cid, "leverage_ratio"))
        if lev_sig is not None and lev_sig > 0:
            row["leverage_x"] = lev_sig
        elif ta_l is not None and eq_l is not None and eq_l > 0 and ta_l > 0:
            row["leverage_x"] = ta_l / eq_l
        else:
            row["leverage_x"] = np.nan

        # 11. interest_cover_x: interest coverage from signal or blob
        ic_sig = sig_map.get((cid, "interest_coverage_ratio"))
        row["interest_cover_x"] = ic_sig if ic_sig is not None else (ic_l if (ic_l is not None and math.isfinite(ic_l)) else np.nan)

        # 12. cash_pct_revenue: cash / revenue * 100.0
        row["cash_pct_revenue"] = (
            cash_l / rev_l * 100.0
            if (rev_l is not None and rev_l > 0 and cash_l is not None)
            else np.nan
        )

        # 13. capex_pct_revenue: capex / revenue * 100.0
        cap_sig = sig_map.get((cid, "capex_ratio"))
        if cap_sig is not None:
            row["capex_pct_revenue"] = cap_sig
        elif tot_capex is not None and rev_l is not None and rev_l > 0:
            row["capex_pct_revenue"] = tot_capex / rev_l * 100.0
        else:
            row["capex_pct_revenue"] = np.nan

        # 14. inventory_change_pct_revenue: inventory_change / revenue * 100.0
        inv_sig = sig_map.get((cid, "cost_inventory_change_rev"))
        if inv_sig is not None:
            row["inventory_change_pct_revenue"] = inv_sig
        elif inv_change_val is not None and cb_rev is not None and cb_rev > 0:
            row["inventory_change_pct_revenue"] = inv_change_val / cb_rev * 100.0
        else:
            row["inventory_change_pct_revenue"] = np.nan

        # 15-17. Planned metrics: emit nothing today
        row["dso_days"] = np.nan
        row["dio_days"] = np.nan
        row["dpo_days"] = np.nan

        rows.append(row)

    df = pd.DataFrame(rows).set_index("company_id")
    return df


def _f(v: Any) -> Optional[float]:
    """Float conversion helper."""
    if v is None:
        return None
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


@dataclass
class PeerIndex:
    """
    Cached in-memory index of peer groups, distributions, and precomputed statistics.
    Passed to BuildContext.peer_index or loaded on demand.
    """
    cohort_df: pd.DataFrame = field(default_factory=pd.DataFrame)
    cohort_counts: Dict[str, int] = field(default_factory=dict)
    min_peers: int = MIN_PEERS_DEFAULT
    # (group_level, group_code, metric) -> stats dict
    group_stats: Dict[Tuple[str, str, str], Dict[str, Any]] = field(default_factory=dict)
    # company_id -> (group_code, group_level, group_n, group_label)
    company_groups: Dict[str, Tuple[str, str, int, str]] = field(default_factory=dict)
    # (group_level, group_code, metric) -> list of (company_id, value)
    group_metric_values: Dict[Tuple[str, str, str], List[Tuple[str, float]]] = field(default_factory=dict)

    @classmethod
    def build(cls, df: pd.DataFrame, min_peers: int = MIN_PEERS_DEFAULT) -> "PeerIndex":
        """Build peer index from a cohort DataFrame."""
        if df.empty:
            return cls(cohort_df=df, min_peers=min_peers)

        # Count occurrences at every prefix level
        all_codes = [clean_ateco_code(c) for c in df["nace_code"].dropna()]
        counts: Dict[str, int] = {}
        for c in all_codes:
            for length in (6, 5, 4, 3, 2):
                if len(c) >= length:
                    p = c[:length]
                    counts[p] = counts.get(p, 0) + 1
        counts["all"] = len(df)

        company_groups: Dict[str, Tuple[str, str, int, str]] = {}
        for cid, row in df.iterrows():
            code = clean_ateco_code(row.get("nace_code", ""))
            g_code, g_level, g_n, g_label = resolve_peer_group(code, counts, min_peers)
            company_groups[str(cid)] = (g_code, g_level, g_n, g_label)

        # Assign companies to resolved groups
        # key: (group_level, group_code) -> list of company_ids
        resolved_groups: Dict[Tuple[str, str], List[str]] = {}
        for cid, (g_code, g_level, _, _) in company_groups.items():
            resolved_groups.setdefault((g_level, g_code), []).append(cid)

        # Compute distributions and statistics per resolved group & metric
        group_stats: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        group_metric_values: Dict[Tuple[str, str, str], List[Tuple[str, float]]] = {}

        for (g_level, g_code), cids in resolved_groups.items():
            sub_df = df.loc[cids]
            for metric in PEER_METRICS:
                if metric not in sub_df.columns:
                    continue
                series = sub_df[metric].dropna()
                valid_pairs = [(str(cid), float(v)) for cid, v in series.items() if math.isfinite(v)]
                vals = [v for _, v in valid_pairs]

                stats = compute_group_stats(vals)
                key = (g_level, g_code, metric)
                group_stats[key] = stats
                group_metric_values[key] = valid_pairs

        return cls(
            cohort_df=df,
            cohort_counts=counts,
            min_peers=min_peers,
            group_stats=group_stats,
            company_groups=company_groups,
            group_metric_values=group_metric_values,
        )

    def get_company_group(self, company_id: str, ateco_code: Optional[str] = None) -> Tuple[str, str, int, str]:
        """Get (group_code, group_level, group_n, group_label) for a company."""
        if str(company_id) in self.company_groups:
            return self.company_groups[str(company_id)]
        if ateco_code:
            return resolve_peer_group(clean_ateco_code(ateco_code), self.cohort_counts, self.min_peers)
        return "all", "all", self.cohort_counts.get("all", 0), "All machinery makers, 50-99 staff, Italy"

    def get_peer_values(self, group_level: str, group_code: str, metric: str, exclude_company_id: Optional[str] = None) -> List[float]:
        """Get valid values for a metric in a group, excluding the specified company."""
        key = (group_level, group_code, metric)
        pairs = self.group_metric_values.get(key, [])
        if exclude_company_id is None:
            return [v for _, v in pairs]
        exclude_str = str(exclude_company_id)
        return [v for cid, v in pairs if cid != exclude_str]

    def get_company_percentile(self, company_id: str, metric: str, company_value: Optional[float] = None) -> Optional[float]:
        """Calculate the mid-rank percentile of a company for a metric among peers excluding itself."""
        cid_str = str(company_id)
        if company_value is None:
            if not self.cohort_df.empty and cid_str in self.cohort_df.index:
                val = self.cohort_df.loc[cid_str].get(metric)
                company_value = float(val) if val is not None and math.isfinite(val) else None

        if company_value is None or not math.isfinite(company_value):
            return None

        g_code, g_level, _, _ = self.get_company_group(cid_str)
        peer_vals = self.get_peer_values(g_level, g_code, metric, exclude_company_id=cid_str)
        return compute_mid_rank_percentile(company_value, peer_vals)

    def get_peer_median(self, company_id: str, metric: str) -> Optional[float]:
        """Get the peer median for a company's group."""
        g_code, g_level, _, _ = self.get_company_group(str(company_id))
        key = (g_level, g_code, metric)
        stats = self.group_stats.get(key)
        if stats and stats.get("median") is not None:
            return stats["median"]
        return None

    def get_company_peer_facts(self, company_id: str, facts_in: Optional[Any] = None) -> List[Fact]:
        """
        Generate all peer.* Facts for a company.
        Reads company values from facts_in if available, otherwise from cached cohort.
        """
        cid_str = str(company_id)
        ateco = facts_in.value("id.ateco") if facts_in is not None else None
        g_code, g_level, g_n, g_label = self.get_company_group(cid_str, ateco)

        out: List[Fact] = [
            Fact(key="peer.group_label", value=g_label, grade=GRADE_A, status=FACT_PRESENT,
                 source="Peer engine", source_ref=f"{g_level}:{g_code}"),
            Fact(key="peer.group_n", value=g_n, unit="companies", grade=GRADE_A, status=FACT_PRESENT,
                 source="Peer engine", source_ref=f"{g_level}:{g_code}"),
            Fact(key="peer.group_level", value=g_level, grade=GRADE_A, status=FACT_PRESENT,
                 source="Peer engine", source_ref=f"{g_level}:{g_code}"),
        ]

        for metric in PEER_METRICS:
            # Check median
            key = (g_level, g_code, metric)
            stats = self.group_stats.get(key)
            if stats and stats.get("median") is not None and stats.get("peer_count", 0) > 0:
                med = stats["median"]
                out.append(
                    Fact(key=f"peer.{metric}.median", value=round(med, 2), grade=GRADE_A, status=FACT_PRESENT,
                         source="Peer engine", source_ref=f"{g_level}:{g_code}:{metric}")
                )

            # Determine company value
            comp_val = None
            if facts_in is not None:
                comp_val = _f(facts_in.value(f"fin.{metric}"))
            if comp_val is None and not self.cohort_df.empty and cid_str in self.cohort_df.index:
                val = self.cohort_df.loc[cid_str].get(metric)
                comp_val = float(val) if val is not None and math.isfinite(val) else None

            if comp_val is not None:
                peer_vals = self.get_peer_values(g_level, g_code, metric, exclude_company_id=cid_str)
                pctile = compute_mid_rank_percentile(comp_val, peer_vals)
                if pctile is not None:
                    out.append(
                        Fact(key=f"peer.{metric}.pctile", value=pctile, unit="pctile", grade=GRADE_A,
                             status=FACT_PRESENT, source="Peer engine", source_ref=f"{g_level}:{g_code}:{metric}")
                    )

        return out


_GLOBAL_PEER_INDEX: Optional[PeerIndex] = None


def get_peer_index(db: Optional[Session] = None, force_refresh: bool = False, min_peers: int = MIN_PEERS_DEFAULT) -> PeerIndex:
    """Get or load singleton PeerIndex."""
    global _GLOBAL_PEER_INDEX
    if _GLOBAL_PEER_INDEX is not None and not force_refresh:
        return _GLOBAL_PEER_INDEX

    if db is None:
        from database import get_db_session
        db = get_db_session()

    df = load_cohort_dataframe(db)
    _GLOBAL_PEER_INDEX = PeerIndex.build(df, min_peers=min_peers)
    return _GLOBAL_PEER_INDEX


def recompute_peer_stats(db: Session, min_peers: int = MIN_PEERS_DEFAULT, apply: bool = False) -> Dict[str, Any]:
    """
    Recompute peer groups, metrics, and percentiles for the whole cohort.
    If apply=True, saves/upserts statistics into dx_peer_stats.
    Returns audit summary dictionary.
    """
    t0 = time.time()
    df = load_cohort_dataframe(db)
    index = PeerIndex.build(df, min_peers=min_peers)
    t_calc = time.time() - t0

    # Summary analysis of widening
    widening_counts: Dict[str, int] = {}
    codes_widened: List[Tuple[str, str, int]] = []
    cohort_codes = set(df["nace_code"].dropna())

    for code in sorted(cohort_codes):
        g_code, g_level, g_n, _ = resolve_peer_group(code, index.cohort_counts, min_peers)
        widening_counts[g_level] = widening_counts.get(g_level, 0) + 1
        if g_level != "6-digit":
            codes_widened.append((code, f"{g_level} ({g_code})", g_n))

    total_companies = len(df)
    companies_ge_12 = sum(
        1 for _, row in df.iterrows()
        if resolve_peer_group(row.get("nace_code", ""), index.cohort_counts, min_peers)[2] >= min_peers
    )
    share_ge_12 = (companies_ge_12 / total_companies * 100.0) if total_companies > 0 else 0.0

    upsert_count = 0
    now = datetime.now(timezone.utc)
    if apply:
        # Create table if missing (SQLite / tests)
        from models import Base
        Base.metadata.create_all(bind=db.get_bind())

        existing = {s.id: s for s in db.query(DxPeerStats).all()}
        for (g_level, g_code, metric), stats in index.group_stats.items():
            record_id = f"{g_level}:{g_code}:{metric}"
            vals = [v for _, v in index.group_metric_values.get((g_level, g_code, metric), [])]
            dist_data = {
                "values": vals,
                "n": len(vals),
            }

            row_data = {
                "id": record_id,
                "group_code": g_code,
                "group_level": g_level,
                "metric": metric,
                "peer_count": stats["peer_count"],
                "median": stats["median"],
                "p10": stats["p10"],
                "p25": stats["p25"],
                "p75": stats["p75"],
                "p90": stats["p90"],
                "mean": stats["mean"],
                "std": stats["std"],
                "min_value": stats["min_value"],
                "max_value": stats["max_value"],
                "distribution_json": dist_data,
                "computed_at": now,
                "schema_version": 1,
            }

            if record_id in existing:
                stat_obj = existing[record_id]
                for k, v in row_data.items():
                    setattr(stat_obj, k, v)
            else:
                db.add(DxPeerStats(**row_data))
            upsert_count += 1

        db.commit()

    total_time = time.time() - t0
    return {
        "total_companies": total_companies,
        "min_peers_threshold": min_peers,
        "companies_with_n_ge_12": companies_ge_12,
        "share_companies_ge_12_pct": round(share_ge_12, 2),
        "distinct_ateco_codes": len(cohort_codes),
        "level_breakdown_by_code": widening_counts,
        "codes_widened_count": len(codes_widened),
        "codes_widened": codes_widened,
        "groups_computed": len(index.group_stats),
        "records_written": upsert_count if apply else 0,
        "applied": apply,
        "calc_seconds": round(t_calc, 2),
        "total_seconds": round(total_time, 2),
    }


def main():
    parser = argparse.ArgumentParser(description="Rebuild peer groups and ratio percentiles (D03).")
    parser.add_argument("--rebuild", action="store_true", help="Recompute peer groups and stats.")
    parser.add_argument("--min-peers", type=int, default=MIN_PEERS_DEFAULT, help="Minimum peer group size before widening (default 12).")
    parser.add_argument("--apply", action="store_true", help="Write cached records to dx_peer_stats table.")
    parser.add_argument("--dry-run", action="store_true", help="Compute metrics without writing to database.")
    args = parser.parse_args()

    if not args.rebuild:
        parser.print_help()
        sys.exit(0)

    from database import get_db_session
    db = get_db_session()
    apply = args.apply and not args.dry_run

    print(f"Rebuilding peer engine (min_peers={args.min_peers}, apply={apply})...")
    res = recompute_peer_stats(db, min_peers=args.min_peers, apply=apply)

    print("\n--- Peer Engine Rebuild Report ---")
    print(f"Total companies evaluated: {res['total_companies']}")
    print(f"Companies with n >= {res['min_peers_threshold']}: {res['companies_with_n_ge_12']} ({res['share_companies_ge_12_pct']}%)")
    print(f"ATECO codes widening breakdown: {res['level_breakdown_by_code']}")
    print(f"Codes widened beyond 6-digit: {res['codes_widened_count']}")
    if res['codes_widened']:
        print("Sample widened codes (first 5):", res['codes_widened'][:5])
    print(f"Groups computed across metrics: {res['groups_computed']}")
    print(f"Database write applied: {res['applied']} (rows written: {res['records_written']})")
    print(f"Total runtime: {res['total_seconds']} s (calc: {res['calc_seconds']} s)")
    print("----------------------------------\n")


if __name__ == "__main__":
    main()
