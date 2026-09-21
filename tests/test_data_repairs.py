"""
Tests for the 2026-09-22 data fixes: margin compression in percentage points, the k EUR catalog bounds,
the guarded catalog/pain-point migrations, and the one-off repairs in data_repairs.py. Everything runs
on an in-memory SQLite database — none of it touches DATABASE_URL.
"""

import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import data_repairs as R
import painpoints as P
from company_service import compute_group_value, detect_column_groups
from indicators import (
    CATALOG_MIGRATIONS, INDICATOR_SEED, TREND_INDICATOR_KEYS, apply_catalog_migrations, seed_indicator_definitions,
)
from models import Base, Company, IndicatorDefinition, PainPointDefinition, RawImportRecord, SignalRecord


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    seed_indicator_definitions(session)
    P.seed_pain_point_definitions(session)
    yield session
    session.close()


def groups_of(*columns):
    return detect_column_groups(list(columns))


# ---- margin compression: percentage points of revenue ---------------------------------------------

ROW_AMOUNTS = {
    "revenue_latest": 200.0, "revenue_y-1": 150.0, "revenue_y-2": 100.0,
    "gross_margin_latest": 80.0, "gross_margin_y-1": 60.0, "gross_margin_y-2": 50.0,
}
GROUPS = groups_of(*ROW_AMOUNTS)


def test_an_amount_margin_is_converted_to_percent_of_revenue_before_the_decline_is_taken():
    # 50% of revenue two years ago, 40% now -> 10 points of compression, NOT the 30 (k EUR) it fell by.
    value, status = compute_group_value(GROUPS["gross_margin"], ROW_AMOUNTS, "margin_compression", companions=GROUPS)
    assert status == "present" and value == pytest.approx(10.0)


def test_an_improving_margin_floors_at_zero_points():
    row = dict(ROW_AMOUNTS, **{"gross_margin_latest": 120.0})       # 60% now vs 50% before
    value, status = compute_group_value(GROUPS["gross_margin"], row, "margin_compression", companions=GROUPS)
    assert status == "present" and value == 0.0


def test_the_old_bug_cannot_come_back_a_big_amount_never_lands_as_points():
    # Real shape of the bug: amounts in the tens of thousands, no revenue column supplied.
    g = groups_of("gross_margin_latest", "gross_margin_y-2")
    row = {"gross_margin_latest": 98816.8, "gross_margin_y-2": 42428.4}
    assert compute_group_value(g["gross_margin"], row, "margin_compression", companions=g) == (None, "not_yet_checked")


def test_without_revenue_only_percent_looking_margins_are_trusted():
    g = groups_of("gross_margin_latest", "gross_margin_y-2")
    value, status = compute_group_value(g["gross_margin"], {"gross_margin_latest": 30.0, "gross_margin_y-2": 40.0},
                                         "margin_compression", companions=g)
    assert status == "present" and value == pytest.approx(10.0)


@pytest.mark.parametrize("patch", [
    {"revenue_latest": 0.0},               # no base to divide by
    {"revenue_y-2": None},                 # missing point
    {"gross_margin_latest": 500.0},        # margin above revenue: cannot be an amount of it
])
def test_unusable_inputs_are_left_unchecked_not_guessed(patch):
    row = dict(ROW_AMOUNTS, **patch)
    assert compute_group_value(GROUPS["gross_margin"], row, "margin_compression", companions=GROUPS) == (None, "not_yet_checked")


def test_ebitda_is_a_real_trend_now():
    assert "ebitda_trend" in TREND_INDICATOR_KEYS
    g = groups_of("ebitda_latest", "ebitda_y-2")
    value, status = compute_group_value(g["ebitda"], {"ebitda_latest": 150.0, "ebitda_y-2": 100.0}, "ebitda_trend", companions=g)
    assert status == "present" and value == pytest.approx(50.0)          # % change, not the 150 level


# ---- catalog bounds are in thousand EUR --------------------------------------------------------------

def test_seed_monetary_bounds_are_in_thousand_eur():
    seed = {r["key"]: r for r in INDICATOR_SEED}
    assert seed["cash_position"]["raw_max"] == 3000 and seed["debt_level"]["raw_max"] == 20000
    assert (seed["total_assets"]["raw_min"], seed["total_assets"]["raw_max"]) == (5000, 50000)


def test_a_median_real_company_no_longer_scores_zero_on_cash_and_debt():
    from scoring import normalize_indicator_value
    seed = {r["key"]: r for r in INDICATOR_SEED}
    assert normalize_indicator_value(2042.0, seed["cash_position"]) > 60     # was 0.07 with the € bounds
    assert 30 < normalize_indicator_value(8901.0, seed["debt_level"]) < 60   # was 0.04


# ---- guarded catalog migrations ----------------------------------------------------------------------

def _set_old_defaults(db):
    """Rewind the freshly seeded catalog to what a pre-fix database holds."""
    for key, field, value in [("cash_position", "raw_max", 3000000.0), ("debt_level", "raw_max", 20000000.0),
                              ("total_assets", "raw_min", 5000000.0), ("total_assets", "raw_max", 50000000.0)]:
        setattr(db.query(IndicatorDefinition).filter_by(key=key).one(), field, value)
    db.commit()


def test_migration_fixes_old_defaults_and_is_idempotent(db):
    _set_old_defaults(db)
    first = apply_catalog_migrations(db)
    assert {(k, f) for k, f, *_ in first["applied"]} >= {("cash_position", "raw_max"), ("debt_level", "raw_max"),
                                                          ("total_assets", "raw_min"), ("total_assets", "raw_max")}
    assert db.query(IndicatorDefinition).filter_by(key="cash_position").one().raw_max == 3000.0
    assert apply_catalog_migrations(db) == {"applied": [], "skipped": []}      # second run: nothing to do


def test_migration_never_overwrites_a_value_a_human_set(db):
    _set_old_defaults(db)
    db.query(IndicatorDefinition).filter_by(key="cash_position").one().raw_max = 4500000.0     # someone tuned it
    db.commit()
    out = apply_catalog_migrations(db)
    assert db.query(IndicatorDefinition).filter_by(key="cash_position").one().raw_max == 4500000.0
    assert ("cash_position", "raw_max", 4500000.0) in out["skipped"]


def test_dry_run_reports_but_writes_nothing(db):
    _set_old_defaults(db)
    out = apply_catalog_migrations(db, dry_run=True)
    assert out["applied"] and db.query(IndicatorDefinition).filter_by(key="cash_position").one().raw_max == 3000000.0


def test_the_mislabelled_importer_rows_are_relabelled_by_predicate(db):
    # As create_ad_hoc_indicator wrote them: bare label, the boilerplate comment, no proxy.
    for key, label in [("cogs", "Cogs"), ("ebitda_trend", "Ebitda Trend")]:
        row = db.query(IndicatorDefinition).filter_by(key=key).first()
        if row is None:
            row = IndicatorDefinition(key=key, label=label, category="Context & Segment Tags (not scored)", axis="context", weight=0.0)
            db.add(row)
        row.label, row.proxy = label, None
        row.comment = "Created from an uploaded dataset column via the flexible data feeder — informational, not scored."
    db.commit()
    apply_catalog_migrations(db)
    cogs = db.query(IndicatorDefinition).filter_by(key="cogs").one()
    assert "Production costs" in cogs.label and "NOT cost of goods sold" in cogs.proxy and "must not feed COGS" in cogs.comment
    assert "EBITDA % change" in db.query(IndicatorDefinition).filter_by(key="ebitda_trend").one().proxy
    assert all(m["id"] for m in CATALOG_MIGRATIONS)


# ---- pain-point rule migrations ------------------------------------------------------------------------

def _old_profit_erosion(db):
    row = db.query(PainPointDefinition).filter_by(key="profit_erosion").one()
    rules = [dict(r) for r in row.rules if r["indicator"] != "margin_compression"]
    rules.append(P._r("gross_margin_change_pp", "lower", -1, -8, 2, " pp"))
    row.rules = rules
    db.commit()
    return row


def test_profit_erosion_swaps_the_proposed_indicator_for_the_fixed_one(db):
    _old_profit_erosion(db)
    out = P.apply_pain_point_migrations(db)
    assert ("profit_erosion", "gross_margin_change_pp") in out["applied"]
    used = {r["indicator"] for r in db.query(PainPointDefinition).filter_by(key="profit_erosion").one().rules}
    assert "margin_compression" in used and "gross_margin_change_pp" not in used
    assert P.apply_pain_point_migrations(db) == {"applied": [], "skipped": []}


def test_an_edited_pain_point_rule_is_left_alone(db):
    row = _old_profit_erosion(db)
    rules = [dict(r) for r in row.rules]
    next(r for r in rules if r["indicator"] == "gross_margin_change_pp")["warn"] = -3      # tuned by hand
    row.rules = rules
    db.commit()
    out = P.apply_pain_point_migrations(db)
    assert ("profit_erosion", "gross_margin_change_pp") not in out["applied"]
    assert any(k == "profit_erosion" for k, *_ in out["skipped"])
    assert "gross_margin_change_pp" in {r["indicator"] for r in db.query(PainPointDefinition).filter_by(key="profit_erosion").one().rules}


def test_margin_compression_now_drives_a_pain_point_and_is_no_longer_exempt():
    assert "margin_compression" not in P.EXEMPT_NEED_INDICATORS
    assert "margin_compression" in {r["indicator"] for p in P.PAIN_POINT_SEED for r in p["rules"]}
    assert "gross_margin_change_pp" not in P.PROPOSED_INDICATORS


@pytest.mark.parametrize("key,indicator,old,new", [
    ("liquidity_squeeze", "cash_to_revenue", {"warn": 0.75, "severe": 0.15}, {"warn": 0.5, "severe": 0.05}),
    ("leadership_transition", "management_turnover", {"warn": 2, "severe": 5}, {"warn": 4, "severe": 8}),
    ("underinvestment", "capex_ratio", {"warn": 1.5, "severe": 0.3}, {"warn": 1.0, "severe": 0.2}),
    ("input_cost_pressure", "materials_cost", {"warn": 40, "severe": 55}, {"warn": 50, "severe": 65}),
])
def test_recalibrated_pain_point_thresholds_migrate_only_untouched_rules(db, key, indicator, old, new):
    row = db.query(PainPointDefinition).filter_by(key=key).one()
    rules = [dict(r) for r in row.rules]
    rule = next(r for r in rules if r["indicator"] == indicator)
    rule.update(old)
    rule["note"] = None                                                 # what the pre-fix seed shipped for most of these
    row.rules = rules
    db.commit()
    P.apply_pain_point_migrations(db)
    got = next(r for r in db.query(PainPointDefinition).filter_by(key=key).one().rules if r["indicator"] == indicator)
    assert {k: got[k] for k in new} == new

    tuned = dict(got, warn=old["warn"] + 0.123)                         # a human tuned it afterwards: never overwritten again
    row = db.query(PainPointDefinition).filter_by(key=key).one()
    row.rules = [tuned if r["indicator"] == indicator else dict(r) for r in row.rules]     # copies: JSON columns don't see in-place edits
    db.commit()
    P.apply_pain_point_migrations(db)
    assert next(r for r in db.query(PainPointDefinition).filter_by(key=key).one().rules if r["indicator"] == indicator)["warn"] == old["warn"] + 0.123


# ---- recomputing derived signals from the stored raw rows -------------------------------------------------

def _company_with_aida_row(db, reg="IT1", **raw):
    company = Company(legal_name=f"Co {reg}", registration_number=reg, country="Italy", segment="SME")
    db.add(company)
    db.flush()
    row = dict(ROW_AMOUNTS, company_id_by_aida="IT" + reg, ebitda_latest=150.0, **{"ebitda_y-1": 120.0, "ebitda_y-2": 100.0})
    row.update(raw)
    db.add(RawImportRecord(company_id=company.id, dataset_name=R.AIDA_DATASET, raw_row=row, mapping_snapshot={
        "gross_margin": "indicator:margin_compression", "ebitda": "indicator:ebitda_trend", "revenue": "indicator:revenue_trend"}))
    # what the buggy import stored: the absolute k EUR fall in margin, and the latest EBITDA level
    for key, value in [("margin_compression", 30.0), ("ebitda_trend", 150.0)]:
        db.add(SignalRecord(company_id=company.id, signal_key=key, source=R.AIDA_DATASET, numeric_value=value,
                            status="present", is_simulated=False, fetched_at=datetime(2026, 9, 1)))
    db.commit()
    return company


def _signal(db, company, key):
    return db.query(SignalRecord).filter_by(company_id=company.id, signal_key=key).one()


def test_derived_signals_are_recomputed_and_the_repair_is_idempotent(db):
    company = _company_with_aida_row(db)
    dry = R.recompute_derived_signals(db)
    assert {c["signal_key"] for c in dry["changes"]} == {"margin_compression", "ebitda_trend"}
    assert _signal(db, company, "margin_compression").numeric_value == 30.0            # dry run wrote nothing
    assert all(c["old_value"] in (30.0, 150.0) for c in dry["changes"])                # the report carries the backup

    R.recompute_derived_signals(db, apply=True)
    assert _signal(db, company, "margin_compression").numeric_value == pytest.approx(10.0)
    assert _signal(db, company, "ebitda_trend").numeric_value == pytest.approx(50.0)
    assert "percentage points of revenue" in json.loads(_signal(db, company, "margin_compression").raw_payload_ref)["basis"]
    assert _signal(db, company, "margin_compression").fetched_at == datetime(2026, 9, 1)  # data date untouched

    again = R.recompute_derived_signals(db)
    assert again["changes"] == [] and again["unchanged"] == 2


def test_a_row_that_can_no_longer_be_computed_moves_to_not_yet_checked(db):
    company = _company_with_aida_row(db, **{"revenue_y-2": 0.0})
    R.recompute_derived_signals(db, apply=True)
    sig = _signal(db, company, "margin_compression")
    assert sig.status == "not_yet_checked" and sig.numeric_value is None


# ---- realigning the shifted AIDA columns -----------------------------------------------------------------------

def test_shifted_cogs_and_swapped_leverage_are_realigned_from_the_export(db):
    company = _company_with_aida_row(db, **{"cogs_latest": 108.0, "cogs_y-1": 76.0, "cogs_y-2": 8.7,
                                              "leverage_ratio_latest": 4.2, "leverage_ratio_y-1": 9.05, "leverage_ratio_y-2": 8.63})
    db.add(SignalRecord(company_id=company.id, signal_key="cogs", source=R.AIDA_DATASET, numeric_value=108.0,
                        status="present", is_simulated=False))
    db.commit()
    truth = {"IT" + "IT1": {"cogs_latest": 161.0, "cogs_y-1": 108.0, "cogs_y-2": 76.0, "leverage_ratio_y-1": 8.63, "leverage_ratio_y-2": 9.05}}

    dry = R.apply_blob_corrections(db, truth)
    assert len(dry["changes"]) == 5 and dry["signal_changes"] == [
        {"company_id": company.id, "signal_key": "cogs", "old_value": 108.0, "new_value": 161.0}]
    assert db.query(RawImportRecord).one().raw_row["cogs_latest"] == 108.0             # dry run wrote nothing

    R.apply_blob_corrections(db, truth, apply=True)
    row = db.query(RawImportRecord).one().raw_row
    assert (row["cogs_latest"], row["cogs_y-1"], row["cogs_y-2"]) == (161.0, 108.0, 76.0)
    assert (row["leverage_ratio_y-1"], row["leverage_ratio_y-2"]) == (8.63, 9.05)
    assert row["revenue_latest"] == 200.0                                                # untouched columns survive
    assert _signal(db, company, "cogs").numeric_value == 161.0

    assert R.apply_blob_corrections(db, truth)["changes"] == []                          # idempotent


# ---- product_age was a founding year ------------------------------------------------------------------------------

def test_founding_year_product_age_is_reset_and_a_real_one_is_not(db):
    a = Company(legal_name="A", registration_number="A1", country="Italy")
    b = Company(legal_name="B", registration_number="B1", country="Italy")
    db.add_all([a, b])
    db.flush()
    db.add_all([
        SignalRecord(company_id=a.id, signal_key="product_age", source="Company Website Crawler", numeric_value=93.0, status="present",
                     is_simulated=False, text_value="founding / first product launch year stated as 1933"),
        SignalRecord(company_id=b.id, signal_key="product_age", source="Manual Entry", numeric_value=12.0, status="present",
                     is_simulated=False, text_value="core line launched 2014 (interview)"),
    ])
    db.commit()
    assert [c["company_id"] for c in R.reset_founding_year_product_age(db)["changes"]] == [a.id]
    R.reset_founding_year_product_age(db, apply=True)
    assert _signal(db, a, "product_age").status == "not_yet_checked" and _signal(db, a, "product_age").numeric_value is None
    assert _signal(db, b, "product_age").numeric_value == 12.0                            # a human's real entry survives
    assert R.reset_founding_year_product_age(db)["changes"] == []
