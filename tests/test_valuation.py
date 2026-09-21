"""
Tests for the valuation feature: the Damodaran parsers, the pure engine (valuation.py) and
its database loading (valuation_service.py).

No network and no shared database: parsers run on literal row lists shaped like the real
workbooks, and database tests use a private in-memory SQLite — never the live DB the other
test modules run against.
"""

import copy

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import valuation_data as vd
from valuation_data import Assumptions, Reference
from valuation import (
    DcfInputs, Financials, PeerStats, ValuationContext, best_financials, build_peer_stats,
    extract_financials, map_industry, rebased_multiple, resolve_country, revenue_cagr,
    run_dcf, value_company, value_universe, weighted_margin,
)


# ------------------------------------------------------------------ fixtures

def _assumptions(**overrides) -> Assumptions:
    rows = {(a["key"], a["scope"]): dict(a) for a in vd.DEFAULT_ASSUMPTIONS}
    for k, v in overrides.items():
        rows[(k, "*")]["value_num"] = v
    return Assumptions(rows)


def _reference() -> Reference:
    return Reference(
        data={
            "multiples": {"industries": {"Machinery": {"n_firms": 210, "ev_ebitda_pos": 15.0, "ev_ebit_pos": 19.0,
                                                       "ev_ebitda_all": 17.0, "ev_ebit_all": 19.2}}},
            "wacc": {"industries": {"Machinery": {"n_firms": 210, "beta": 1.1, "cost_equity": 0.098,
                                                  "equity_weight": 0.9, "debt_weight": 0.1, "tax_rate": 0.15,
                                                  "wacc_eur": 0.08}},
                     "inputs": {"risk_free_usd": 0.0395, "erp_region": 0.0527}},
            "working_capital": {"industries": {"Machinery": {"nwc_sales": 0.17}}},
            "country_risk": {"mature_erp": 0.0423, "countries": {
                "Italy": {"erp_total": 0.0669, "crp": 0.0246, "tax_rate": 0.2781},
                "Germany": {"erp_total": 0.0423, "crp": 0.0, "tax_rate": 0.2993}}},
        },
        meta={n: {"as_of": "2026-01-05"} for n in ("multiples", "wacc", "working_capital", "country_risk")},
    )


def _ctx(peers=None, **overrides) -> ValuationContext:
    return ValuationContext(assumptions=_assumptions(**overrides), reference=_reference(),
                            sector_map={"28": ("Machinery", ""), "25": ("Machinery", "approx: fabricated metal")},
                            peers=peers or {})


def _fin(revenue=(10_000, 9_500, 9_000), ebitda=(1_500, 1_400, 1_300), ebit=(1_100, 1_000, 900),
         cash=(500, 0, 0), debt=(2_000, 0, 0), capex_m=(-300, -280, -250), employees=(50, 48, 46),
         unit=1000.0) -> Financials:
    s = {"revenue": list(revenue), "ebitda": list(ebitda), "ebit": list(ebit), "cash": list(cash),
         "debt": list(debt), "capex_material": list(capex_m), "employees": list(employees)}
    return Financials(dataset="test", unit_eur=unit, series=s)


COMPANY = {"id": "c1", "legal_name": "Test SRL", "country": "Italy", "nace_code": "282999", "headcount": 50}


def _peers(median_margin=0.10, median_growth=0.02, n=500):
    return {("Italy", "28"): PeerStats(n=n, median_margin=median_margin, median_growth=median_growth)}


# ------------------------------------------------------------------ Damodaran parsers

def test_parse_multiples_reads_both_blocks_by_header_text():
    rows = [
        ["Date updated:", 46027.0] + [""] * 8,
        [""] * 10,
        ["", "", "Only positive EBITDA firms", "", "", "", "All firms", "", "", ""],
        ["Industry Name", "Number of firms", "EV/EBITDAR&D", "EV/EBITDA", "EV/EBIT", "EV/EBIT (1-t)",
         "EV/EBITDAR&D", "EV/EBITDA", "EV/EBIT", "EV/EBIT (1-t)"],
        ["Machinery", 210.0, 12.5, 14.98, 19.05, 25.1, 14.2, 17.4, 19.2, 25.4],
        ["", None, None, None, None, None, None, None, None, None],
    ]
    out = vd.parse_multiples(rows)["industries"]["Machinery"]
    assert out["ev_ebitda_pos"] == 14.98 and out["ev_ebit_pos"] == 19.05
    assert out["ev_ebitda_all"] == 17.4 and out["n_firms"] == 210.0
    assert vd._as_of(rows) == "2026-01-05"


def test_parse_multiples_fails_loudly_if_the_layout_changes():
    with pytest.raises(ValueError):
        vd.parse_multiples([["Something", "else"], ["Machinery", 1.0]])


def test_parse_wacc_finds_euro_cost_of_capital_and_regional_erp():
    rows = [
        ["Long Term Treasury bond rate", "", "", 0.0395],
        ["Risk Premium to Use for Equity", "", "", 0.0527],
        ["Industry Name", "Number of Firms", "Beta", "Cost of Equity", "E/(D+E)", "Std Dev in Stock", "Cost of Debt",
         "Tax Rate", "After-tax Cost of Debt", "D/(D+E)", "Cost of Capital", "Cost of Capital (Euros)"],
        ["Machinery", 210.0, 1.1166, 0.0983, 0.898, 0.37, 0.055, 0.155, 0.041, 0.102, 0.0925, 0.0818],
    ]
    out = vd.parse_wacc(rows)
    assert out["inputs"]["erp_region"] == 0.0527
    m = out["industries"]["Machinery"]
    assert m["wacc_eur"] == 0.0818 and m["beta"] == 1.1166 and m["equity_weight"] == 0.898


def test_parse_wacc_requires_the_euro_column():
    rows = [["Risk Premium to Use for Equity", "", "", 0.05],
            ["Industry Name", "Beta", "Cost of Capital"], ["Machinery", 1.0, 0.09]]
    with pytest.raises(ValueError):
        vd.parse_wacc(rows)


def test_parse_working_capital():
    rows = [["Industry Name", "Number of firms", "Acc Rec/ Sales", "Inventory/Sales", "Acc Pay/ Sales",
             "Non-cash WC/ Sales"],
            ["Machinery", 210.0, 0.18, 0.17, 0.10, 0.1737]]
    assert vd.parse_working_capital(rows)["industries"]["Machinery"]["nwc_sales"] == 0.1737


def test_parse_country_risk_joins_tax_rates():
    erp = [
        ["Enter the current risk premium for a mature market", "", "", "", 0.0423],
        ["Country", "Africa", "Moody's rating", "Rating-based Default Spread", "Total Equity Risk Premium",
         "Country Risk Premium", "Sovereign CDS", "Total Equity Risk Premium2", "Country Risk Premium3"],
        ["Italy", "Western Europe", "Baa2", 0.0162, 0.0669, 0.0246, 0.0047, 0.0495, 0.0072],
        ["Germany", "Western Europe", "Aaa", 0.0, 0.0423, 0.0, 0.0007, 0.0434, 0.0011],
    ]
    tax = [["Country", "Tax Rate", "Looked up for 2025"], ["Germany", 0.2993, 0.2993], ["Italy", 0.2781, 0.2781]]
    out = vd.parse_country_risk(erp, tax)
    assert out["mature_erp"] == 0.0423
    assert out["countries"]["Italy"]["erp_total"] == 0.0669 and out["countries"]["Italy"]["tax_rate"] == 0.2781
    assert out["countries"]["Germany"]["crp"] == 0.0


def test_refresh_stores_each_dataset_with_the_publishers_date(monkeypatch):
    fetched = {n: {"payload": {"industries": {"X": {}}}, "as_of": "2026-01-05", "source_url": f"u/{n}"}
               for n in vd.REFERENCE_FILES}
    monkeypatch.setattr(vd, "fetch_reference_payloads", lambda download=None: fetched)
    db = _sqlite()
    summary = vd.refresh_reference_data(db)
    assert {s["dataset"] for s in summary} == set(vd.REFERENCE_FILES)
    ref = vd.load_reference(db)
    assert ref.loaded and ref.as_of("multiples") == "2026-01-05"


# ------------------------------------------------------------------ financials, country, sector

def test_extract_financials_reads_suffix_columns_and_ignores_text_placeholders():
    raw = {"revenue_latest": 100.0, "revenue_y-1": 90.0, "revenue_y-2": "n.s.", "ebitda_latest": 10,
           "Total Debt_latest": 50}
    f = extract_financials(raw, "ds", 1000.0)
    assert f.series["revenue"] == [100.0, 90.0, None]
    assert f.series["debt"] == [50.0, None, None]
    assert f.eur("revenue") == 100_000.0


def test_extract_financials_returns_none_without_revenue():
    assert extract_financials({"ebitda_latest": 5}, "ds", 1.0) is None


def test_best_financials_prefers_the_more_complete_dataset():
    thin = Financials("a", 1.0, {"revenue": [1, None, None], "ebitda": [1, None, None]})
    full = Financials("b", 1.0, {"revenue": [1, 1, 1], "ebitda": [1, 1, 1]})
    assert best_financials([thin, full]).dataset == "b"


@pytest.mark.parametrize("recorded,default,expected", [
    ("Italy", "Italy", ("Italy", False)), ("italia", "Italy", ("Italy", False)),
    ("Deutschland", "Italy", ("Germany", False)), ("DE", "Italy", ("Germany", False)),
    (None, "Italy", ("Italy", True)), ("", "Germany", ("Germany", True)),
    ("France", "Italy", ("France", False)),
])
def test_resolve_country(recorded, default, expected):
    assert resolve_country(recorded, default) == expected


def test_map_industry_uses_longest_prefix_and_strips_formatting():
    smap = {"28": ("Machinery", ""), "244": ("Metals & Mining", ""), "24": ("Steel", "")}
    assert map_industry("282999", smap)[0] == "Machinery"        # AIDA/ATECO digits
    assert map_industry("C28.29", smap)[0] == "Machinery"        # NACE with section letter
    assert map_industry("2441", smap)[0] == "Metals & Mining"    # longer prefix beats the division
    assert map_industry("2410", smap)[0] == "Steel"
    assert map_industry("6419", smap) is None                    # financials are deliberately unmapped
    assert map_industry(None, smap) is None


def test_default_sector_map_leaves_financial_firms_unmapped():
    prefixes = {p for p, _, _ in vd.DEFAULT_SECTOR_MAP}
    assert not any(p.startswith(("64", "65", "66")) for p in prefixes)


# ------------------------------------------------------------------ normalisation, peers

def test_weighted_margin_renormalises_when_years_are_missing():
    f = Financials("d", 1.0, {"revenue": [100, 100, None], "ebitda": [10, 20, None]})
    margin, years = weighted_margin(f, (0.5, 0.3, 0.2))
    assert years == 2 and margin == pytest.approx((0.5 * 0.10 + 0.3 * 0.20) / 0.8)


def test_revenue_cagr_uses_the_oldest_available_year():
    assert revenue_cagr(Financials("d", 1.0, {"revenue": [121, 110, 100]})) == pytest.approx(0.10)
    assert revenue_cagr(Financials("d", 1.0, {"revenue": [110, 100, None]})) == pytest.approx(0.10)
    assert revenue_cagr(Financials("d", 1.0, {"revenue": [100, None, None]})) is None


def test_build_peer_stats_takes_group_medians():
    obs = [(("Italy", "28"), 0.05, 0.0), (("Italy", "28"), 0.10, 0.02), (("Italy", "28"), 0.30, 0.10),
           (("Italy", "25"), None, None)]
    s = build_peer_stats(obs)
    assert s[("Italy", "28")].n == 3 and s[("Italy", "28")].median_margin == 0.10
    assert s[("Italy", "25")].median_margin is None


# ------------------------------------------------------------------ DCF and re-based multiples

def test_dcf_matches_the_closed_form_for_a_flat_business():
    # No growth, capex = D&A, no working capital: FCF is a constant f, and with mid-year
    # discounting a constant perpetuity is worth f * (1 + W)^0.5 / W exactly.
    inp = DcfInputs(revenue0=1_000.0, growth1=0.0, margin0=0.20, margin_target=0.20,
                    da_pct=0.05, capex_pct=0.05, tax_rate=0.25, nwc_pct=0.0)
    wacc = 0.10
    f = (0.20 - 0.05) * 1_000.0 * (1 - 0.25)
    assert run_dcf(inp, wacc, 0.0)["ev"] == pytest.approx(f * (1 + wacc) ** 0.5 / wacc)


def test_dcf_refuses_a_discount_rate_too_close_to_growth():
    inp = DcfInputs(1_000.0, 0.02, 0.2, 0.2, 0.05, 0.05, 0.25, 0.1)
    with pytest.raises(ValueError):
        run_dcf(inp, 0.04, 0.02)


def test_dcf_value_rises_with_growth_and_falls_with_the_discount_rate():
    inp = DcfInputs(1_000.0, 0.05, 0.2, 0.2, 0.05, 0.05, 0.25, 0.15)
    assert run_dcf(inp, 0.10, 0.03)["ev"] > run_dcf(inp, 0.10, 0.02)["ev"]
    assert run_dcf(inp, 0.09, 0.02)["ev"] > run_dcf(inp, 0.11, 0.02)["ev"]


def test_rebased_multiple():
    assert rebased_multiple(15.0, 0.08, 0.08, 0.02) == pytest.approx(15.0)      # same risk -> unchanged
    assert rebased_multiple(15.0, 0.08, 0.13, 0.02) == pytest.approx(15.0 * 0.06 / 0.11)
    with pytest.raises(ValueError):
        rebased_multiple(15.0, 0.08, 0.01, 0.02)


# ------------------------------------------------------------------ the valuation itself

def test_a_valued_company_gets_ordered_ranges_from_both_methods():
    r = value_company(COMPANY, _fin(), _ctx(peers=_peers()))
    assert r.status == "valued"
    assert {m.key for m in r.methods} == {"ev_ebitda", "ev_ebit", "dcf"}
    for m in r.methods:
        assert m.ev_low < m.ev_base < m.ev_high
    assert r.ev_low <= r.ev_base <= r.ev_high
    # the headline range must cover both methods, however far apart they are
    assert r.ev_low <= min(r.method("dcf").ev_base, r.method("ev_ebitda").ev_base)
    assert r.ev_high >= max(r.method("dcf").ev_base, r.method("ev_ebitda").ev_base)
    assert r.implied_ev_ebitda == pytest.approx(r.ev_base / r.ebitda_norm)


def test_units_are_converted_to_euros():
    r = value_company(COMPANY, _fin(), _ctx())
    assert r.revenue == 10_000_000.0                       # 10,000 thousand
    assert r.ebitda_norm == pytest.approx(r.margin_norm * 10_000_000.0)


def test_equity_is_withheld_until_the_debt_definition_is_confirmed():
    r = value_company(COMPANY, _fin(), _ctx())
    assert r.equity_base is None and "confirmed" in r.equity_withheld
    assert r.ev_base is not None                           # EV never depends on it
    r2 = value_company(COMPANY, _fin(), _ctx(net_debt_definition_verified=1.0))
    assert r2.equity_withheld == ""
    assert r2.equity_base == pytest.approx(r2.ev_base - 1_500_000.0)   # (2,000 - 500) thousand
    assert r2.equity_low == pytest.approx(r2.ev_low - 1_500_000.0)


def test_negative_equity_is_reported_not_hidden():
    r = value_company(COMPANY, _fin(debt=(90_000, 0, 0)), _ctx(net_debt_definition_verified=1.0))
    assert r.equity_base < 0 and any("negative" in w for w in r.warnings)


def test_loss_making_company_is_refused_not_valued():
    r = value_company(COMPANY, _fin(ebitda=(-500, -400, -300)), _ctx())
    assert r.status == "not_valued" and r.reason_code == "negative_earnings"


def test_missing_financials_reference_sector_and_country_each_give_their_own_reason():
    assert value_company(COMPANY, None, _ctx()).reason_code == "no_financials"
    assert value_company(COMPANY, _fin(ebitda=(None, None, None)), _ctx()).reason_code == "no_financials"
    empty_ref = ValuationContext(_assumptions(), Reference(), {"28": ("Machinery", "")})
    assert value_company(COMPANY, _fin(), empty_ref).reason_code == "no_reference"
    assert value_company({**COMPANY, "nace_code": "6419"}, _fin(), _ctx()).reason_code == "no_sector"
    assert value_company({**COMPANY, "country": "Atlantis"}, _fin(), _ctx()).reason_code == "no_country"


def test_country_is_recognised_from_the_company_and_can_be_overridden():
    it = value_company(COMPANY, _fin(), _ctx())
    de = value_company({**COMPANY, "country": "Germany"}, _fin(), _ctx())
    assert it.param_country == "Italy" and de.param_country == "Germany"
    assert it.discount.wacc > de.discount.wacc            # country risk raises Italy's rate
    assert de.discount.country_adjustment < 0             # Germany's ERP sits below the European average
    forced = value_company(COMPANY, _fin(), _ctx(), country_override="Germany")
    assert forced.param_country == "Germany" and forced.country == "Italy"
    assert any("overridden" in w for w in forced.warnings)


def test_missing_country_falls_back_to_the_default_and_says_so():
    r = value_company({**COMPANY, "country": None}, _fin(), _ctx())
    assert r.status == "valued" and r.country == "Italy" and r.country_assumed
    assert any("assumed" in w for w in r.warnings)


def test_a_higher_size_premium_lowers_every_method():
    lo = value_company(COMPANY, _fin(), _ctx(size_premium=0.02))
    hi = value_company(COMPANY, _fin(), _ctx(size_premium=0.06))
    for key in ("ev_ebitda", "ev_ebit", "dcf"):
        assert hi.method(key).ev_base < lo.method(key).ev_base


def test_manual_sector_multiple_replaces_the_derived_ebitda_multiple():
    ctx = _ctx()
    ctx.assumptions.rows[(vd.OVERRIDE_KEY, "Machinery")] = {"value_num": 6.0}
    r = value_company(COMPANY, _fin(), ctx)
    m = r.method("ev_ebitda")
    assert "manual" in m.label and m.ev_base == pytest.approx(r.ebitda_norm * 6.0)
    assert m.ev_low == pytest.approx(m.ev_base * 0.8) and m.ev_high == pytest.approx(m.ev_base * 1.2)


def test_peer_benchmark_needs_enough_peers():
    few = value_company(COMPANY, _fin(), _ctx(peers=_peers(n=3)))
    assert few.peer is None and any("peer benchmark" in t for _, t in few.confidence_reasons)
    many = value_company(COMPANY, _fin(), _ctx(peers=_peers(n=100)))
    assert many.peer is not None


def test_value_at_stake_only_appears_below_the_peer_median_and_is_not_in_the_valuation():
    below = value_company(COMPANY, _fin(), _ctx(peers=_peers(median_margin=0.20)))   # own margin ~14.5%
    above = value_company(COMPANY, _fin(), _ctx(peers=_peers(median_margin=0.05)))
    assert below.value_at_stake and above.value_at_stake is None
    v = below.value_at_stake
    assert v["gap_pp"] == pytest.approx(0.20 - below.margin_norm)
    assert v["ebitda_uplift"] == pytest.approx(v["gap_pp"] * below.revenue)
    assert v["ev_uplift"] == pytest.approx(v["ebitda_uplift"] * below.implied_ev_ebitda)
    # no turnaround credit in the base valuation: the same company vs a lower-margin peer group differs
    # only through the (downward) mean-reversion, never upward
    assert below.ev_base == pytest.approx(value_company(COMPANY, _fin(), _ctx()).ev_base)


def test_above_peer_margins_mean_revert_downward_in_the_dcf():
    rich = value_company(COMPANY, _fin(), _ctx(peers=_peers(median_margin=0.05)))
    flat = value_company(COMPANY, _fin(), _ctx())
    assert rich.method("dcf").ev_base < flat.method("dcf").ev_base


def test_implausible_revenue_per_employee_flags_a_unit_mismatch():
    r = value_company(COMPANY, _fin(unit=1.0), _ctx())          # 10,000 euros of revenue for 50 people
    assert any("units" in w for w in r.warnings) and r.confidence_grade in ("C", "D")


def test_sparse_history_lowers_the_input_quality_grade():
    full = value_company(COMPANY, _fin(), _ctx(peers=_peers()))
    thin = value_company(COMPANY, _fin(revenue=(10_000, None, None), ebitda=(1_500, None, None),
                                       ebit=(1_100, None, None)), _ctx(peers=_peers()))
    assert thin.confidence_score < full.confidence_score
    assert any("year(s)" in t for _, t in thin.confidence_reasons)


def test_method_gap_is_reported_separately_from_the_grade():
    r = value_company(COMPANY, _fin(), _ctx(peers=_peers()))
    assert r.disagreement is not None and r.disagreement > 0
    assert not any("disagree" in t for _, t in r.confidence_reasons)


def test_without_ebit_only_the_ebitda_multiple_is_available():
    r = value_company(COMPANY, _fin(ebit=(None, None, None)), _ctx())
    assert [m.key for m in r.methods] == ["ev_ebitda"]
    assert any("EBIT is not in the source" in w for w in r.warnings)


def test_the_result_lists_every_reference_value_and_assumption_it_used():
    r = value_company(COMPANY, _fin(), _ctx(peers=_peers()))
    names = " ".join(row["Input"] for row in r.inputs_used)
    for needle in ("EV/EBITDA", "cost of capital", "Equity risk premium", "Size & illiquidity", "tax", "working capital"):
        assert needle.lower() in names.lower()
    assert any("Judgment" in row["Source"] for row in r.inputs_used)


def test_valuation_is_deterministic():
    a = value_company(COMPANY, _fin(), _ctx(peers=_peers()))
    b = value_company(copy.deepcopy(COMPANY), _fin(), _ctx(peers=_peers()))
    assert (a.ev_low, a.ev_base, a.ev_high) == (b.ev_low, b.ev_base, b.ev_high)


# ------------------------------------------------------------------ database side (private SQLite)

def _sqlite():
    from models import Base
    import valuation_models  # noqa: F401
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_seeding_is_insert_only_so_edits_survive():
    db = _sqlite()
    vd.seed_valuation_defaults(db)
    row = db.get(vd.ValuationAssumption, ("size_premium", "*"))
    row.value_num = 0.05
    db.commit()
    vd.seed_valuation_defaults(db)
    assert vd.load_assumptions(db).num("size_premium") == 0.05
    assert vd.load_assumptions(db).text("default_country") == "Italy"


def test_assumption_scope_overrides_the_global_default():
    a = Assumptions({("size_premium", "*"): {"value_num": 0.035}, ("size_premium", "Germany"): {"value_num": 0.03}})
    assert a.num("size_premium", "Germany") == 0.03 and a.num("size_premium", "Italy") == 0.035
    assert a.num("size_premium") == 0.035


def test_loading_financials_and_a_universe_valuation_from_imported_rows():
    from models import Company, RawImportRecord
    import valuation_service as vs
    db = _sqlite()
    vd.seed_valuation_defaults(db)
    db.add_all([Company(id=f"c{i}", legal_name=f"Co {i}", registration_number=f"R{i}", nace_code="282999",
                        country="Italy") for i in range(3)])
    row = lambda rev, e: {"revenue_latest": rev, "revenue_y-1": rev * 0.9, "revenue_y-2": rev * 0.8,   # noqa: E731
                          "ebitda_latest": e, "ebitda_y-1": e, "ebitda_y-2": e, "ebit_latest": e * 0.7,
                          "ebit_y-1": e * 0.7, "ebit_y-2": e * 0.7, "employees_latest": 50}
    db.add_all([
        RawImportRecord(company_id="c0", dataset_name="AIDA", raw_row=row(10_000, 1_000)),
        RawImportRecord(company_id="c1", dataset_name="AIDA", raw_row=row(20_000, 3_000)),
        RawImportRecord(company_id="c1", dataset_name="crawler_jobs", raw_row={"open_roles": 4}),  # no financials
        RawImportRecord(company_id="c2", dataset_name="AIDA", raw_row=row(5_000, -200)),
    ])
    db.commit()
    companies, fins, ctx = vs.load_universe(db)
    assert set(fins) == {"c0", "c1", "c2"} and fins["c1"].dataset == "AIDA"
    assert fins["c1"].eur("revenue") == 20_000_000.0
    assert ctx.peers[("Italy", "28")].n == 3
    ctx.reference = _reference()                      # market data isn't loaded in this database
    results = {r.company_id: r for r in value_universe(companies, fins, ctx)}
    assert results["c0"].status == "valued" and results["c1"].status == "valued"
    assert results["c2"].reason_code == "negative_earnings"
    assert results["c1"].ev_base > results["c0"].ev_base          # bigger, more profitable company is worth more


# ------------------------------------------------------------------ editing helpers + cache key

def test_saving_assumptions_changes_only_what_changed_and_ignores_unknown_keys():
    db = _sqlite()
    vd.seed_valuation_defaults(db)
    n = vd.apply_assumption_changes(db, {"size_premium": 0.05, "terminal_growth": 0.02, "no_such_key": 1.0},
                                    default_country="Germany")
    assert n == 2                                            # size_premium + default country; growth unchanged
    a = vd.load_assumptions(db)
    assert a.num("size_premium") == 0.05 and a.text("default_country") == "Germany"


def test_net_debt_flag_round_trips():
    db = _sqlite()
    vd.seed_valuation_defaults(db)
    assert vd.load_assumptions(db).num("net_debt_definition_verified") == 0.0
    vd.set_net_debt_verified(db, True)
    assert vd.load_assumptions(db).num("net_debt_definition_verified") == 1.0


def test_sector_overrides_are_added_updated_and_removed():
    db = _sqlite()
    vd.seed_valuation_defaults(db)
    vd.set_sector_overrides(db, {"Machinery": 6.0, "Steel": 4.5})
    assert vd.load_assumptions(db).scoped(vd.OVERRIDE_KEY) == {"Machinery": 6.0, "Steel": 4.5}
    vd.set_sector_overrides(db, {"Machinery": 7.0, "Bad": -1.0})
    assert vd.load_assumptions(db).scoped(vd.OVERRIDE_KEY) == {"Machinery": 7.0}   # Steel removed, non-positive ignored


def test_sector_map_replacement_normalises_prefixes_and_drops_blanks():
    db = _sqlite()
    vd.seed_valuation_defaults(db)
    n = vd.replace_sector_map(db, {"28.2": ("Machinery", "custom"), "C24": ("Steel", ""), "": ("Steel", ""),
                                   "99": (None, "")})
    assert n == 2
    m = vd.load_sector_map(db)
    assert m == {"282": ("Machinery", "custom"), "24": ("Steel", "")}


def test_the_ui_cache_key_is_a_string_that_changes_with_the_inputs():
    import valuation_service as vs
    db = _sqlite()
    vd.seed_valuation_defaults(db)
    before = vs.universe_fingerprint(db)
    assert isinstance(before, str)
    assert vs.universe_fingerprint(db) == before                     # stable when nothing changed
    vd.apply_assumption_changes(db, {"size_premium": 0.06})
    assert vs.universe_fingerprint(db) != before
