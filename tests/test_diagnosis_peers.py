"""
Tests for peer engine (ticket D03):
  - Monotonicity of mid-rank percentile
  - Peer group resolution and ATECO widening
  - Self-exclusion in peer mid-rank ranking
  - Size cuts and headcount terciles
  - Integration with diagnosis deriver and fact keys
  - In-memory database persistence of dx_peer_stats
  - Live cohort acceptance (runtime < 60s, >= 95% companies with n >= 12, median overlap with ateco_cost_benchmarks)
"""

import math
import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from diagnosis.contracts import CompanyFacts, Fact, GRADE_A, FACT_PRESENT
from diagnosis.factkeys import FACT_KEYS, PEER_METRICS
from diagnosis.models_peers import DxPeerStats
from diagnosis.peers import (
    clean_ateco_code,
    compute_group_stats,
    compute_headcount_terciles,
    compute_mid_rank_percentile,
    format_ateco_code,
    load_cohort_dataframe,
    PeerIndex,
    recompute_peer_stats,
    resolve_peer_group,
)
from diagnosis.readers.derive_peers import derive_peers
from diagnosis.registry import BuildContext
from models import Base, Company, RawImportRecord, SectorCostBenchmark, SignalRecord


@pytest.fixture()
def mem_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


# ------------------------------------------------------------------------------------------------ ATECO formatting
def test_clean_and_format_ateco_code():
    assert clean_ateco_code("28.93.00") == "289300"
    assert clean_ateco_code("289300") == "289300"
    assert clean_ateco_code(289300) == "289300"
    assert clean_ateco_code(None) == ""

    assert format_ateco_code("289300") == "28.93.00"
    assert format_ateco_code("28930") == "28.93.0"
    assert format_ateco_code("2893") == "28.93"
    assert format_ateco_code("289") == "28.9"
    assert format_ateco_code("28") == "28"
    assert format_ateco_code("all") == "all"


# ------------------------------------------------------------------------------------------------ Percentile Monotonicity
def test_mid_rank_percentile_monotonicity():
    peers = [10.0, 20.0, 20.0, 30.0, 40.0, 50.0]
    test_values = np.linspace(0.0, 60.0, 61)
    percentiles = [compute_mid_rank_percentile(v, peers) for v in test_values]

    # Verify every element is >= previous
    for i in range(len(percentiles) - 1):
        assert percentiles[i] <= percentiles[i + 1], f"Non-monotonic at {test_values[i]} vs {test_values[i+1]}"

    # Verify bounds
    assert compute_mid_rank_percentile(0.0, peers) == 0.0
    assert compute_mid_rank_percentile(100.0, peers) == 100.0

    # Verify tie handling: value equal to ties at 20.0 (1 less, 2 equal -> rank 1 + 0.5*2 = 2; 2/6 = 33.3%)
    assert compute_mid_rank_percentile(20.0, peers) == pytest.approx(33.3, abs=0.1)


def test_mid_rank_percentile_edge_cases():
    assert compute_mid_rank_percentile(None, [10.0, 20.0]) is None
    assert compute_mid_rank_percentile(float("nan"), [10.0, 20.0]) is None
    assert compute_mid_rank_percentile(15.0, []) is None
    assert compute_mid_rank_percentile(15.0, [None, float("nan")]) is None


# ------------------------------------------------------------------------------------------------ Widening Logic
def test_peer_group_widening_hierarchy():
    counts = {
        "289300": 15,       # 6-digit >= 12
        "282110": 4,        # 6-digit < 12
        "28211": 14,        # 5-digit >= 12
        "284909": 3,        # 6-digit < 12
        "28490": 5,         # 5-digit < 12
        "2849": 20,         # 4-digit >= 12
        "281110": 2,        # 6-digit < 12
        "28111": 2,         # 5-digit < 12
        "2811": 5,          # 4-digit < 12
        "281": 30,          # 3-digit >= 12
        "280000": 1,        # 6-digit < 12
        "28000": 1,
        "2800": 1,
        "280": 1,
        "28": 50,           # 2-digit >= 12
        "999999": 1,        # rare code
        "all": 100,
    }

    # Case 1: 6-digit has >= 12
    code, lvl, n, label = resolve_peer_group("289300", counts, min_peers=12)
    assert lvl == "6-digit"
    assert code == "289300"
    assert n == 15
    assert label == "ATECO 28.93.00, 50-99 staff, Italy"

    # Case 2: widens to 5-digit
    code, lvl, n, label = resolve_peer_group("282110", counts, min_peers=12)
    assert lvl == "5-digit"
    assert code == "28211"
    assert n == 14
    assert label == "ATECO 28.21.1, 50-99 staff, Italy"

    # Case 3: widens to 4-digit
    code, lvl, n, label = resolve_peer_group("284909", counts, min_peers=12)
    assert lvl == "4-digit"
    assert code == "2849"
    assert n == 20
    assert label == "ATECO 28.49, 50-99 staff, Italy"

    # Case 4: widens to 3-digit
    code, lvl, n, label = resolve_peer_group("281110", counts, min_peers=12)
    assert lvl == "3-digit"
    assert code == "281"
    assert n == 30
    assert label == "ATECO 28.1, 50-99 staff, Italy"

    # Case 5: widens to 2-digit
    code, lvl, n, label = resolve_peer_group("280000", counts, min_peers=12)
    assert lvl == "2-digit"
    assert code == "28"
    assert n == 50
    assert label == "ATECO 28, 50-99 staff, Italy"

    # Case 6: falls back to all
    code, lvl, n, label = resolve_peer_group("999999", counts, min_peers=12)
    assert lvl == "all"
    assert code == "all"
    assert n == 100


# ------------------------------------------------------------------------------------------------ Self-Exclusion
def test_mid_rank_excludes_self_in_peer_index():
    data = [
        {"company_id": "c1", "nace_code": "289300", "headcount": 60, "ebitda_margin_pct": 25.0},
        {"company_id": "c2", "nace_code": "289300", "headcount": 65, "ebitda_margin_pct": 10.0},
        {"company_id": "c3", "nace_code": "289300", "headcount": 70, "ebitda_margin_pct": 10.0},
        {"company_id": "c4", "nace_code": "289300", "headcount": 75, "ebitda_margin_pct": 5.0},
    ]
    df = pd.DataFrame(data).set_index("company_id")
    idx = PeerIndex.build(df, min_peers=2)

    # For c1 (25.0): peers are [10.0, 10.0, 5.0]. All peers strictly less -> 100%
    pct1 = idx.get_company_percentile("c1", "ebitda_margin_pct")
    assert pct1 == 100.0

    # For c2 (10.0): peers are [25.0, 10.0, 5.0]. Strictly less: 1 (5.0), Equal: 1 (10.0) -> rank 1 + 0.5*1 = 1.5. 1.5/3 = 50%
    pct2 = idx.get_company_percentile("c2", "ebitda_margin_pct")
    assert pct2 == 50.0

    # For c4 (5.0): peers are [25.0, 10.0, 10.0]. Strictly less: 0, Equal: 0 -> 0%
    pct4 = idx.get_company_percentile("c4", "ebitda_margin_pct")
    assert pct4 == 0.0


# ------------------------------------------------------------------------------------------------ Headcount Terciles
def test_headcount_terciles():
    s = pd.Series([50, 55, 60, 65, 70, 75, 80, 85, 90, 95])
    t = compute_headcount_terciles(s)
    assert "tercile_1_small" in t
    assert "tercile_2_medium" in t
    assert "tercile_3_large" in t
    assert t["tercile_1_small"][0] == 50
    assert t["tercile_3_large"][1] == 95


# ------------------------------------------------------------------------------------------------ Deriver & Fact Verification
def test_derive_peers_emits_valid_contract_facts():
    data = [
        {"company_id": "comp_a", "nace_code": "289300", "headcount": 60, "ebitda_margin_pct": 12.0, "materials_pct_revenue": 45.0},
        {"company_id": "comp_b", "nace_code": "289300", "headcount": 70, "ebitda_margin_pct": 8.0, "materials_pct_revenue": 50.0},
        {"company_id": "comp_c", "nace_code": "289300", "headcount": 80, "ebitda_margin_pct": 10.0, "materials_pct_revenue": 40.0},
    ]
    df = pd.DataFrame(data).set_index("company_id")
    idx = PeerIndex.build(df, min_peers=2)

    facts = CompanyFacts(company_id="comp_a")
    facts.add(Fact(key="id.ateco", value="289300", grade=GRADE_A))
    facts.add(Fact(key="fin.ebitda_margin_pct", value=12.0, grade=GRADE_A))

    ctx = BuildContext(peer_index=idx)
    derived = list(derive_peers(facts, ctx))

    keys = {f.key for f in derived}
    assert "peer.group_label" in keys
    assert "peer.group_n" in keys
    assert "peer.group_level" in keys
    assert "peer.ebitda_margin_pct.pctile" in keys
    assert "peer.ebitda_margin_pct.median" in keys

    # Verify all emitted keys belong to FACT_KEYS
    for f in derived:
        assert f.key in FACT_KEYS, f"Unknown fact key emitted: {f.key}"
        assert f.grade == GRADE_A
        assert f.status == FACT_PRESENT

    # comp_a had ebitda 12.0 vs peers [8.0, 10.0] -> 100%
    pct_fact = next(f for f in derived if f.key == "peer.ebitda_margin_pct.pctile")
    assert pct_fact.value == 100.0

    # Peer group median of [8, 10, 12] is 10.0
    med_fact = next(f for f in derived if f.key == "peer.ebitda_margin_pct.median")
    assert med_fact.value == 10.0

    # Unmeasured metric (e.g. dso_days) should not have facts
    assert "peer.dso_days.pctile" not in keys
    assert "peer.dso_days.median" not in keys


# ------------------------------------------------------------------------------------------------ SQLite Persistence
def test_recompute_and_persistence_in_db(mem_db):
    # Seed 3 companies in SQLite
    c1 = Company(id="c1", registration_number="IT001", legal_name="Alpha Srl", nace_code="289300", headcount=55)
    c2 = Company(id="c2", registration_number="IT002", legal_name="Beta SpA", nace_code="289300", headcount=65)
    c3 = Company(id="c3", registration_number="IT003", legal_name="Gamma Srl", nace_code="289300", headcount=75)
    mem_db.add_all([c1, c2, c3])

    # Add AIDA Raw Exports
    mem_db.add(RawImportRecord(company_id="c1", dataset_name="AIDA Raw Exports", raw_row={
        "revenue_latest": 10000.0, "revenue_y-1": 9000.0, "ebitda_latest": 1500.0,
        "equity_latest": 5000.0, "total_assets_latest": 10000.0
    }))
    mem_db.add(RawImportRecord(company_id="c2", dataset_name="AIDA Raw Exports", raw_row={
        "revenue_latest": 12000.0, "revenue_y-1": 11000.0, "ebitda_latest": 1200.0,
        "equity_latest": 4000.0, "total_assets_latest": 10000.0
    }))
    mem_db.add(RawImportRecord(company_id="c3", dataset_name="AIDA Raw Exports", raw_row={
        "revenue_latest": 8000.0, "revenue_y-1": 8500.0, "ebitda_latest": 400.0,
        "equity_latest": 2000.0, "total_assets_latest": 8000.0
    }))
    mem_db.commit()

    summary = recompute_peer_stats(mem_db, min_peers=2, apply=True)
    assert summary["total_companies"] == 3
    assert summary["records_written"] > 0
    assert summary["applied"] is True

    # Query dx_peer_stats
    stats_rows = mem_db.query(DxPeerStats).all()
    assert len(stats_rows) > 0

    ebitda_stat = mem_db.query(DxPeerStats).filter_by(metric="ebitda_margin_pct", group_code="289300").first()
    assert ebitda_stat is not None
    assert ebitda_stat.peer_count == 3
    # Margins: c1=15.0%, c2=10.0%, c3=5.0% -> median is 10.0%
    assert ebitda_stat.median == pytest.approx(10.0, abs=0.01)
    assert ebitda_stat.distribution_json["n"] == 3


# ------------------------------------------------------------------------------------------------ Live Cohort Check
def test_live_cohort_rebuild_metrics():
    """
    Runs a read-only dry-run rebuild on the live database (or isolated test DB).
    Tests:
      1. Runtime < 60s
      2. >= 95% of companies get n >= 12
      3. Overlap check with SectorCostBenchmark where data exists
    """
    from database import get_db_session
    try:
        db = get_db_session()
        c_count = db.query(Company).count()
    except Exception:
        pytest.skip("Database session unavailable")

    if c_count < 100:
        pytest.skip(f"Live cohort not loaded (found {c_count} companies, expected ~953)")

    summary = recompute_peer_stats(db, min_peers=12, apply=False)

    # Acceptance 1: Runtime under 60 s
    assert summary["total_seconds"] < 60.0, f"Rebuild took {summary['total_seconds']} s (limit 60 s)"

    # Acceptance 2: at least 95% get n >= 12
    assert summary["share_companies_ge_12_pct"] >= 95.0, (
        f"Only {summary['share_companies_ge_12_pct']}% got n >= 12 (expected >= 95%)"
    )

    # Acceptance 3: Overlap with SectorCostBenchmark medians where 6-digit n >= 12
    benchmarks = {
        (b.nace_code, b.signal_key): b.median
        for b in db.query(SectorCostBenchmark).filter(
            SectorCostBenchmark.signal_key.in_(["materials_cost", "labour_cost"]),
            SectorCostBenchmark.median.isnot(None)
        ).all()
    }

    if benchmarks:
        df = load_cohort_dataframe(db)
        idx = PeerIndex.build(df, min_peers=12)
        # Check a couple of overlapping codes with large peer count
        matched = 0
        for code in ["282500", "289300", "282930", "281400"]:
            key = ("6-digit", code, "materials_pct_revenue")
            st = idx.group_stats.get(key)
            bench_med = benchmarks.get((code, "materials_cost"))
            if st and st.get("median") is not None and bench_med is not None:
                assert st["median"] == pytest.approx(bench_med, abs=0.01)
                matched += 1
        assert matched > 0, "No overlapping benchmark codes tested"
