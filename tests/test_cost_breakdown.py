"""
The nine-line cost breakdown: importer (year-matched revenue, conflict safety, withdrawal of stale rows),
analytics (shares, peers, outliers, movers) and the assistant's get_cost_breakdown tool.
"""

from datetime import datetime

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import assistant_llm
import cost_analytics as ca
import cost_breakdown as cb
from aida_cost_breakdown_import import DATASET_NAME, apply_records, build_records, pair_revenue
from indicators import INDICATOR_SEED, seed_indicator_definitions
from models import Base, Company, RawImportRecord, SignalRecord

PERIOD_HEADER = {"latest": "Ultimo anno disp.", "y-1": "Anno - 1", "y-2": "Anno - 2", "y-3": "Anno - 3"}


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    seed_indicator_definitions(session)
    yield session
    session.close()


def _company(db, name, reg, nace="282999"):
    c = Company(legal_name=name, registration_number=reg, nace_code=nace, country="Italy")
    db.add(c)
    db.commit()
    return c


def _frame(rows):
    """rows: {match_key: {category: [latest, y-1, y-2, y-3]}} -> the sheet as read_cost_breakdown_sheet returns it."""
    data = {}
    for key, cats in rows.items():
        rec = {}
        total = [0.0] * 4
        for cat, vals in cats.items():
            for p, v in zip(cb.PERIODS, vals):
                rec[f"{cb.AIDA_HEADERS[cat]} | migl EUR | {PERIOD_HEADER[p]}"] = v
                total[cb.PERIODS.index(p)] += v
        for cat in cb.CATEGORY_KEYS:  # lines a row doesn't mention are zero, so the file still reconciles
            for p in cb.PERIODS:
                rec.setdefault(f"{cb.AIDA_HEADERS[cat]} | migl EUR | {PERIOD_HEADER[p]}", 0.0)
        for p, t in zip(cb.PERIODS, total):
            rec[f"{cb.TOTAL_HEADER} | migl EUR | {PERIOD_HEADER[p]}"] = t
        data[key] = rec
    return pd.DataFrame.from_dict(data, orient="index").rename_axis("match_key")


MIX = {"materials": [400, 380, 360, 300], "services": [80, 70, 60, 50], "personnel": [200, 190, 180, 170],
       "depreciation": [20, 20, 20, 20]}  # total latest 700


def test_every_category_has_k_share_and_revenue_signals_in_the_catalog():
    keys = {d["key"] for d in INDICATOR_SEED}
    assert cb.TOTAL_KEY in keys
    for c in cb.CATEGORY_KEYS:
        assert {cb.k_key(c), cb.share_key(c), cb.rev_key(c)} <= keys
    new = [d for d in INDICATOR_SEED if d["key"].startswith("cost_")]
    assert len(new) == 1 + 3 * len(cb.CATEGORY_KEYS)
    assert all(d["axis"] == "context" and d["weight"] == 0.0 for d in new)  # evidence only, never scored


def test_units_distinguish_money_from_percentages():
    assert assistant_llm.signal_unit("cost_services_k").startswith("thousand EUR")
    assert assistant_llm.signal_unit("cost_services_share").startswith("% of total production costs")
    assert assistant_llm.signal_unit("cost_services_rev").startswith("% of revenue")


def test_revenue_is_paired_only_when_the_year_is_proven_by_the_cost_series():
    totals = {"latest": 700.0, "y-1": 650.0, "y-2": 600.0, "y-3": 540.0}
    same_vintage = {"production_costs_latest": 700.2, "revenue_latest": 1000, "production_costs_y-1": 650, "revenue_y-1": 900}
    assert pair_revenue(totals, same_vintage)["latest"] == 1000 and pair_revenue(totals, same_vintage)["y-1"] == 900
    # the PL export is one year OLDER: its latest year equals our y-1, so our latest year has NO revenue
    older = {"production_costs_latest": 650.0, "revenue_latest": 880, "production_costs_y-1": 600, "revenue_y-1": 800}
    paired = pair_revenue(totals, older)
    assert paired["latest"] is None and paired["y-1"] == 880 and paired["y-2"] == 800
    assert all(v is None for v in pair_revenue(totals, None).values())
    assert all(v is None for v in pair_revenue(totals, {"production_costs_latest": 5, "revenue_latest": 9}).values())


def test_import_writes_amounts_shares_and_only_year_matched_revenue_ratios(db):
    a = _company(db, "ALPHA SRL", "IT00000000001")
    b = _company(db, "BETA SRL", "IT00000000002")
    db.add_all([
        RawImportRecord(company_id=a.id, dataset_name="AIDA Raw Exports", raw_row={
            "production_costs_latest": 700.0, "revenue_latest": 1000.0}),
        RawImportRecord(company_id=b.id, dataset_name="AIDA Raw Exports", raw_row={
            "production_costs_latest": 560.0, "revenue_latest": 900.0}),  # equals our y-1 total: an older vintage
    ])
    db.commit()
    mix_b = {"materials": [350, 330, 310, 280], "services": [90, 80, 70, 60], "personnel": [160, 150, 140, 130]}
    df = _frame({"IT00000000001": MIX, "IT00000000002": mix_b})
    records, unmatched = build_records(db, df)
    assert unmatched == []
    ra, rb = records["IT00000000001"]["signals"], records["IT00000000002"]["signals"]

    assert ra[cb.k_key("services")]["value"] == 80
    assert ra[cb.share_key("services")]["value"] == pytest.approx(80 / 700 * 100, abs=1e-3)
    assert ra[cb.rev_key("services")]["value"] == pytest.approx(8.0)
    assert ra["service_costs"]["value"] == pytest.approx(8.0)
    assert sum(ra[cb.share_key(c)]["value"] for c in cb.CATEGORY_KEYS if cb.share_key(c) in ra) == pytest.approx(100.0, abs=1e-2)

    # BETA's revenue belongs to the previous fiscal year: absolute and share values exist, revenue ratios do not
    assert rb[cb.k_key("services")]["value"] == 90 and cb.share_key("services") in rb
    assert cb.rev_key("services") not in rb and "service_costs" not in rb
    assert records["IT00000000002"]["blob"]["revenue_ratio_available"] is False


def test_apply_is_idempotent_conflict_safe_and_withdraws_stale_service_costs(db):
    a = _company(db, "ALPHA SRL", "IT00000000001")
    b = _company(db, "BETA SRL", "IT00000000002")
    db.add(RawImportRecord(company_id=a.id, dataset_name="AIDA Raw Exports",
                           raw_row={"production_costs_latest": 700.0, "revenue_latest": 1000.0}))
    # the old, wrong import left BETA a service_costs row from this dataset (a ratio of mismatched years)
    db.add(SignalRecord(company_id=b.id, signal_key="service_costs", numeric_value=140.0, status="present",
                        source=DATASET_NAME, is_simulated=False, fetched_at=datetime.utcnow()))
    # a human-entered ALPHA value must never be overwritten
    db.add(SignalRecord(company_id=a.id, signal_key=cb.k_key("materials"), numeric_value=1.0, status="present",
                        source="analyst", is_simulated=False, fetched_at=datetime.utcnow()))
    db.commit()
    df = _frame({"IT00000000001": MIX, "IT00000000002": {"services": [90, 80, 70, 60], "materials": [350, 330, 310, 280]}})
    records, _ = build_records(db, df)

    dry = apply_records(db, records, apply=False)
    assert db.query(SignalRecord).filter_by(signal_key=cb.k_key("services")).count() == 0  # a dry run writes nothing
    assert dry["withdrawn"] == 1 and len(dry["conflicts"]) == 1

    apply_records(db, records, apply=True)
    held = db.query(SignalRecord).filter_by(company_id=a.id, signal_key=cb.k_key("materials")).one()
    assert held.numeric_value == 1.0 and held.source == "analyst"
    stale = db.query(SignalRecord).filter_by(company_id=b.id, signal_key="service_costs").one()
    assert stale.status == "not_yet_checked" and stale.numeric_value is None
    assert db.query(SignalRecord).filter_by(company_id=a.id, signal_key="service_costs").one().numeric_value == pytest.approx(8.0)

    again = apply_records(db, build_records(db, df)[0], apply=True)
    assert again["inserted"] == 0 and again["updated"] == 0 and again["withdrawn"] == 0
    assert db.query(RawImportRecord).filter_by(dataset_name=DATASET_NAME).count() == 2  # blobs replaced, not duplicated


def _populate(db, n=8):
    """n peers in one sector with similar mixes plus one outlier whose services line is huge."""
    df_rows = {}
    for i in range(n):
        _company(db, f"PEER {i}", f"IT0000000010{i}")
        df_rows[f"IT0000000010{i}"] = {"materials": [400, 400, 400, 400], "services": [100 + i, 100, 100, 100],
                                        "personnel": [250, 250, 250, 250]}
    _company(db, "OUTLIER SRL", "IT00000000200")
    df_rows["IT00000000200"] = {"materials": [200, 300, 350, 400], "services": [400, 250, 150, 100],
                                 "personnel": [150, 200, 250, 250]}
    records, _ = build_records(db, _frame(df_rows))
    apply_records(db, records, apply=True)


def test_company_breakdown_reports_gap_percentile_and_trend(db):
    _populate(db)
    frame = ca.load_cost_frame(db)
    out = db.query(Company).filter_by(legal_name="OUTLIER SRL").one()
    b = ca.company_breakdown(db, out, frame)
    svc = next(c for c in b["categories"] if c["category"] == "services")
    assert svc["k_eur"] == 400 and svc["share_of_costs_pct"] == pytest.approx(400 / 750 * 100, abs=1e-2)
    assert svc["gap_vs_peers_pp"] > 20 and svc["peer_percentile"] == 100.0
    assert svc["share_change_pp_3y"] > 20  # 100/750 -> 400/750 over the filed years
    assert svc["k_eur_by_period"]["y-3"] == 100 and b["revenue_ratio_available"] is False
    assert sum(c["share_of_costs_pct"] for c in b["categories"] if c["share_of_costs_pct"] is not None) == pytest.approx(100.0, abs=1e-2)


def test_outliers_and_movers_surface_the_unusual_company(db):
    _populate(db)
    frame = ca.load_cost_frame(db)
    t = ca.anomaly_table(frame, basis="share", min_gap_pp=8.0)
    assert not t.empty and t.iloc[0]["Company"] == "OUTLIER SRL"
    assert (t["Gap (pp)"].abs() >= 8.0).all()
    names = dict(zip(frame["company_id"], frame["legal_name"]))
    mv = ca.share_movers(ca.load_blobs(db), names, "services", top=3, rising=True)
    assert mv.iloc[0]["Company"] == "OUTLIER SRL" and mv.iloc[0]["Change (pp)"] > 20
    hm = ca.sector_heatmap(frame)
    assert list(hm["Companies"]) == [9] and hm[cb.LABELS["services"]].iloc[0] > 0


def test_assistant_tool_returns_the_breakdown_with_units(db):
    _populate(db)
    result = assistant_llm._execute_tool(db, "get_cost_breakdown", {"company_name": "OUTLIER"})
    assert result["legal_name"] == "OUTLIER SRL" and "thousand EUR" in result["units"]
    assert {c["category"] for c in result["categories"]} == set(cb.CATEGORY_KEYS)
    missing = _company(db, "NO DATA SRL", "IT00000000300")
    assert "error" in assistant_llm._execute_tool(db, "get_cost_breakdown", {"company_name": missing.legal_name})
    listed = assistant_llm._execute_tool(db, "list_available_signals", {})["signals"]
    assert next(s for s in listed if s["key"] == "cost_services_share")["unit"].startswith("% of total production costs")
    ranked = assistant_llm._execute_tool(db, "top_companies_by_signal", {"signal_key": "cost_services_k", "n": 1})
    assert ranked["results"][0]["legal_name"] == "OUTLIER SRL" and ranked["unit"].startswith("thousand EUR")
