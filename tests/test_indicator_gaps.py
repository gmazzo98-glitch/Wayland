"""
scripts/indicator_gaps.py: the curated gap plan must stay consistent with the real indicator catalog,
and its arithmetic (which class an indicator lands in, and the weighted summary) must be right.
No database is touched — the matrix functions take plain data.
"""

import importlib.util
from pathlib import Path

import pytest

from indicators import INDICATOR_SEED

_spec = importlib.util.spec_from_file_location("indicator_gaps", Path(__file__).resolve().parent.parent / "scripts" / "indicator_gaps.py")
gaps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gaps)

SEEDED = {d["key"] for d in INDICATOR_SEED}


def test_every_plan_entry_names_a_real_indicator():
    """A typo, or an indicator that was renamed or removed, must not survive silently in the plan."""
    orphans = sorted(set(gaps.GAP_PLAN) - SEEDED)
    assert not orphans, f"GAP_PLAN names indicators that are not in the catalog: {orphans}"


def test_every_plan_entry_uses_a_known_class_and_says_where_the_data_comes_from():
    for key, (cls, where, _effort) in gaps.GAP_PLAN.items():
        assert cls in gaps.CLASS_ORDER, f"{key}: unknown class {cls!r}"
        assert where.strip(), f"{key}: no source given"


def test_a_new_indicator_shows_up_as_unclassified_instead_of_being_dropped():
    rows = gaps.build_matrix([{"key": "brand_new_signal", "label": "Brand new", "axis": "need", "weight": 2.0}], 10, {})
    assert rows[0]["class"] == "UNCLASSIFIED" and rows[0]["pct"] == 0


def test_an_indicator_with_data_for_most_companies_is_reported_live_whatever_the_plan_says():
    defs = [{"key": "patent_count", "label": "Patents", "axis": "readiness", "weight": 3.0},
            {"key": "approval_chain_depth", "label": "Approval chain", "axis": "readiness", "weight": 3.0}]
    rows = {r["key"]: r for r in gaps.build_matrix(defs, 100, {"patent_count": 60, "approval_chain_depth": 90})}
    assert rows["patent_count"]["class"] == "LIVE" and rows["patent_count"]["pct"] == 60
    # A first-contact interview item stays MANUAL even if someone happened to fill it in bulk.
    assert rows["approval_chain_depth"]["class"] == "MANUAL"


def test_below_half_coverage_keeps_the_planned_class():
    rows = gaps.build_matrix([{"key": "patent_count", "label": "Patents", "axis": "readiness", "weight": 3.0}], 100, {"patent_count": 49})
    assert rows[0]["class"] == "RUN"


def test_weighted_summary_counts_a_both_axis_indicator_on_each_axis_and_ignores_context():
    rows = [
        {"axis": "both", "weight": 5.0, "class": "COMPUTE"},
        {"axis": "need", "weight": 3.0, "class": "RUN"},
        {"axis": "readiness", "weight": 2.0, "class": "RUN"},
        {"axis": "context", "weight": 0.0, "class": "LIVE"},
    ]
    summary = gaps.summarise(rows)
    assert summary["need"] == {"COMPUTE": 5.0, "RUN": 3.0}
    assert summary["readiness"] == {"COMPUTE": 5.0, "RUN": 2.0}


def test_real_counts_ignore_simulated_and_unchecked_signals(tmp_path):
    import sqlalchemy as sa
    engine = sa.create_engine(f"sqlite:///{(tmp_path / 'g.db').as_posix()}")
    with engine.begin() as conn:
        conn.execute(sa.text("create table companies (id text)"))
        conn.execute(sa.text("create table signal_records (signal_key text, status text, is_simulated boolean)"))
        conn.execute(sa.text("insert into companies values ('a'), ('b'), ('c')"))
        conn.execute(sa.text(
            "insert into signal_records values "
            "('k','present',0), ('k','absent',0), ('k','present',1), ('k','not_yet_checked',0), ('other','present',0)"))
    total, counts = gaps.real_counts(engine)
    assert total == 3
    assert counts == {"k": 2, "other": 1}, "only real (non-simulated) present/absent values count"
