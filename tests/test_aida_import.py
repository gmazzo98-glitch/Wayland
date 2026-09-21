"""
Tests for the raw-AIDA importer (aida_import.py) and the roster fix that keeps auditors out of "management".
Everything runs on synthetic frames / an in-memory database; the last test reads the real export files and
skips itself when they are not on this machine.
"""

from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import aida_import as A
from company_service import _is_not_management, sync_management_composition_signals
from indicators import INDICATOR_SEED, SRC_AIDA, seed_indicator_definitions
from models import Base, Company, CompanyPerson, RawImportRecord, SignalRecord, SourceHealth


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    seed_indicator_definitions(session)
    yield session
    session.close()


# ---- reading by header name, never by position -----------------------------------------------------------

def test_years_are_matched_by_header_name_even_when_the_export_orders_them_oddly():
    # The real export lists this series Ultimo, Anno-2, Anno-1 — the order the curated file mis-read.
    df = pd.DataFrame([[1.0, 3.0, 2.0]], columns=[
        "Rapporto di indebitamento | Ultimo anno disp.", "Rapporto di indebitamento | Anno - 2", "Rapporto di indebitamento | Anno - 1"])
    cols = A.series_columns(df, "Rapporto di indebitamento")
    assert df[cols["latest"]].iloc[0] == 1.0 and df[cols["y-1"]].iloc[0] == 2.0 and df[cols["y-2"]].iloc[0] == 3.0


def test_a_header_that_merely_starts_with_the_name_is_not_the_series():
    df = pd.DataFrame([[1, 2]], columns=["EBITDA | migl EUR | Ultimo anno disp.", "EBITDA margin | % | Ultimo anno disp."])
    assert A.series_columns(df, "EBITDA") == {"latest": "EBITDA | migl EUR | Ultimo anno disp."}


def test_headers_with_embedded_newlines_and_accents_are_found():
    assert A.normalize_header("TOTALE ATTIVO\nmigl EUR\nAnno - 1") == "TOTALE ATTIVO | migl EUR | Anno - 1"
    df = pd.DataFrame([[1]], columns=["N. di società nel gruppo societario"])
    assert A.find_column(df, "gruppo societario") == "N. di società nel gruppo societario"
    assert A.find_column(df, "no such thing") is None


# ---- parsing the stacked cells -----------------------------------------------------------------------------

@pytest.mark.parametrize("token,expected", [("100,00", 100.0), ("25,85", 25.85), (">99,99", 99.99), ("WO", 100.0), ("MO", 50.01),
                                            ("n.d.", None), ("-", None), ("NG", None), ("", None), (None, None)])
def test_ownership_percentage_tokens(token, expected):
    assert A.pct_token(token) == expected


def test_a_parent_and_its_own_owners_do_not_double_count():
    # Parent GmbH holds 100% directly; two people own the parent (indirect: direct '-', total 60 and 40).
    s = A.parse_shareholders("PARENT GMBH\nANNA ROSSI\nLUCA ROSSI", "Società\nPersone fisiche o famiglie\nPersone fisiche o famiglie",
                             "DE\nIT\nIT", "100,00\n-\n-", "100,00\n60,00\n40,00")
    assert s["disclosed"] == pytest.approx(100.0)                    # NOT 200: indirect holders are inside the parent's stake
    assert s["family"] == pytest.approx(100.0)                       # the two people, by their TOTAL stakes
    assert s["foreign"] == pytest.approx(100.0) and s["foreign_countries"] == ["DE"]


def test_unknown_direct_stake_falls_back_to_the_total_and_wo_mo_are_understood():
    s = A.parse_shareholders("A SPA\nB SRL", "Società\nSocietà", "IT\nFR", "n.d.\nMO", "70,00\nn.d.")
    stakes = {l["name"]: l["stake"] for l in s["lines"]}
    assert stakes["A SPA"] == 70.0                                    # direct is 'n.d.' -> its TOTAL stands in
    assert stakes["B SRL"] == 50.01                                   # 'MO' (majority owned) in the direct column = a >50% lower bound
    assert s["disclosed"] == pytest.approx(120.01)


def test_the_placeholder_name_line_still_carries_the_real_immediate_shareholder():
    # Verbatim shape of the real Thyssenkrupp Nucera row: line 0 is the immediate shareholder (BvD id, DE, 100% direct)
    # with the placeholder in its NAME cell; its name is in the ISH column.
    s = A.parse_shareholders(
        "There is no shareholders information for this company\nTHYSSENKRUPP AG\nINDUSTRIE DE NORA S.P.A.\nFEDERICO DE NORA S.P.A.",
        "Società\nSocietà\nSocietà\nSocietà", "DE\nDE\nIT\nIT", "100,00\n-\n-\n-", "100,00\n100,00\n25,85\n11,45",
        bvd_ids="DE4070571522\nDE5110216866\nIT03998870962\nIT06623310965", immediate_names="THYSSENKRUPP NUCERA AG & CO. KGAA")
    assert s["lines"][0]["name"] == "THYSSENKRUPP NUCERA AG & CO. KGAA" and s["lines"][0]["stake"] == 100.0
    assert s["disclosed"] == pytest.approx(100.0)                     # the three indirect owners are inside the parent's 100%
    assert s["foreign"] == pytest.approx(100.0) and s["foreign_countries"] == ["DE"]


def test_no_shareholder_information_is_not_a_shareholder():
    s = A.parse_shareholders("There is no shareholders information for this company", "Società", "", "n.d.", "n.d.")
    assert s["lines"] == [] and s["disclosed"] == 0.0


@pytest.mark.parametrize("text,distress", [
    ("Concordato preventivo", True), ("Concordato preventivo (CCI)", True), ("Misure cautelari e protettive", True),
    ("Scioglimento e liquidazione", True), ("Procedimento unitario", True), ("Cancellazione d'ufficio a seguito istituzione cciaa di fermo", True),
    ("Trasferimento in altra provincia", False), ("Fusione mediante incorporazione in altra società", False), ("", False)])
def test_which_registry_entries_are_distress(text, distress):
    assert A.is_distress_procedure(text) is distress


def test_a_distress_procedure_is_open_only_without_a_recorded_closing_date():
    open_ = A.parse_procedures("Trasferimento in altra provincia\nConcordato preventivo\nConcordato preventivo", "01/01/2024", None)
    assert open_["flag"] == 1 and open_["distress"] == ["Concordato preventivo"]               # repeated lines are one procedure
    closed = A.parse_procedures("Concordato preventivo", "01/01/2015", "16/05/2017")
    assert closed["flag"] == 0 and closed["closings"] == ["16/05/2017"]
    assert A.parse_procedures("Trasferimento in altra provincia", None, None)["flag"] == 0
    assert A.parse_procedures(None, None, None) == {"names": [], "distress": [], "closings": [], "starts": [], "open": 0, "flag": 0}


@pytest.mark.parametrize("letter,ordinal", [("A+", 1), ("A", 1), ("B-", 2), ("C", 3), ("D", 4), ("U", None), ("-", None), (None, None)])
def test_independence_class_as_an_ordinal(letter, ordinal):
    assert A.independence_ordinal(letter) == ordinal


# ---- the derivations, hand-checked against the Thyssenkrupp Nucera Italy row -----------------------------------

NUCERA = {
    "revenue_latest": 206531.106, "revenue_y-1": 112469.413, "revenue_y-2": 81748.79,
    "ebit_latest": 32529.382, "ebit_y-2": 11546.054, "ebitda_latest": 32657.483, "ebitda_y-2": 11696.549,
    "net_income_latest": 24234.904, "gross_margin_latest": 98816.833, "gross_margin_y-2": 42428.387,
    "production_costs_latest": 160969.607, "personnel_costs_latest": 8716.906, "materials_latest": 91489.944,
    "employees_latest": 95, "employees_y-2": 74, "cash_latest": 15298.539, "total_debt_latest": 77087.42,
    "total_assets_latest": 109549.88, "intangibles_latest": 1024.316, "equity_latest": 26099.33,
    "leverage_ratio_latest": 4.2, "interest_coverage_latest": None,
    "material_capex_latest": -696.32, "immaterial_capex_latest": -939.512,
}


def test_financial_derivations_match_the_hand_calculation():
    s = A.derive_financial_signals(NUCERA)
    v = {k: d["value"] for k, d in s.items()}
    assert v["revenue_trend"] == pytest.approx((206531.106 - 81748.79) / 81748.79 * 100, rel=1e-6)
    assert v["ebit_margin"] == pytest.approx(15.75, abs=0.01)
    assert v["net_margin"] == pytest.approx(11.73, abs=0.01)
    assert v["cash_to_revenue"] == pytest.approx(0.889, abs=0.001)                 # months of revenue
    assert v["labour_cost"] == pytest.approx(4.22, abs=0.01) and v["materials_cost"] == pytest.approx(44.30, abs=0.01)
    assert v["average_salary"] == pytest.approx(91.75, abs=0.01)                    # k EUR per employee
    assert v["capex_ratio"] == pytest.approx(0.792, abs=0.001)                      # outflows are negative in AIDA; ratio is positive
    assert v["employee_growth"] == pytest.approx((95 - 74) / 74 * 100)
    assert v["intangibles_share"] == pytest.approx(0.935, abs=0.001)
    # margin compression is percentage points of revenue: 51.90% -> 47.85% of revenue = 4.05 points, NOT the k EUR figure
    assert v["margin_compression"] == pytest.approx(4.05, abs=0.01)
    assert v["cogs"] == 160969.607 and v["total_assets"] == 109549.88 and v["leverage_ratio"] == 4.2
    assert s["interest_coverage_ratio"]["status"] == "not_yet_checked"              # 'n.s.' is never turned into a number
    assert v["cogs_ratio"] == pytest.approx(100 - 47.85, abs=0.01)                  # materials share = 100 - gross margin % of revenue
    assert v["number_of_employees"] == 95


def test_a_zero_or_missing_denominator_is_left_unchecked_not_divided():
    s = A.derive_financial_signals(dict(NUCERA, **{"revenue_latest": 0.0, "employees_latest": None}))
    for key in ("ebit_margin", "cash_to_revenue", "labour_cost", "materials_cost", "capex_ratio", "revenue_per_employee", "average_salary"):
        assert s[key]["status"] == "not_yet_checked", key
    assert s["margin_compression"]["status"] == "not_yet_checked"


def test_a_margin_larger_than_revenue_cannot_be_an_amount_of_it():
    s = A.derive_financial_signals(dict(NUCERA, **{"gross_margin_latest": 999999.0}))
    assert s["margin_compression"]["status"] == "not_yet_checked"


def test_every_derived_key_exists_in_the_catalog():
    catalog = {r["key"] for r in INDICATOR_SEED} | {"cogs", "material_capex", "immaterial_capex"}   # the last three: importer-created rows
    keys = set(A.derive_financial_signals(NUCERA)) | set(A.derive_structure_signals(
        participations=1, procedures=A.parse_procedures(None, None, None), independence="D", group_size=4, accounts_close=datetime(2025, 9, 30),
        shareholders=A.parse_shareholders("X", "Società", "IT", "100,00", "100,00")))
    assert keys - catalog == set(), keys - catalog


def test_ownership_shares_are_only_written_when_the_list_covers_enough_of_the_equity():
    thin = A.parse_shareholders("ONLY\nOTHER", "Società\nSocietà", "DE\nIT", "10,00\nn.d.", "10,00\nn.d.")
    s = A.derive_structure_signals(participations=0, procedures=A.parse_procedures(None, None, None), independence="D", group_size=2,
                                   accounts_close=None, shareholders=thin)
    assert s["foreign_ownership_share"]["status"] == "not_yet_checked" and s["family_ownership_share"]["status"] == "not_yet_checked"
    assert s["subsidiary_participations"]["value"] == 0.0 and s["subsidiary_participations"]["status"] == "present"   # 0 is a real answer
    assert s["last_accounts_year"]["status"] == "not_yet_checked"


# ---- writing -------------------------------------------------------------------------------------------------------

def _record(value_seed=1.0):
    signals = A.derive_financial_signals(NUCERA)
    signals.update(A.derive_structure_signals(participations=2, procedures=A.parse_procedures(None, None, None), independence="D", group_size=4,
                                              accounts_close=datetime(2025, 9, 30), shareholders=A.parse_shareholders("P GMBH", "Società", "DE", "100,00", "100,00")))
    return {"signals": signals, "blob": {"company_id_by_aida": "IT1", "revenue_latest": 1.0}, "stale_accounts": False}


def _company(db, bvd="IT1"):
    c = Company(legal_name="Acme", registration_number="R-" + bvd, country="Italy", segment="SME", external_ref_id=bvd)
    db.add(c)
    db.commit()
    return c


def test_import_writes_signals_a_blob_and_source_health_and_a_rerun_changes_nothing(db):
    company = _company(db)
    db.add(SignalRecord(company_id=company.id, signal_key="total_assets", source="Bundesanzeiger", status="not_yet_checked"))   # empty scaffold row
    db.commit()

    dry = A.apply_records(db, {"IT1": _record()})
    assert dry["companies"] == 1 and dry["inserted"] + dry["updated"] > 20 and db.query(RawImportRecord).count() == 0      # dry run wrote nothing

    A.apply_records(db, {"IT1": _record()}, apply=True)
    ta = db.query(SignalRecord).filter_by(company_id=company.id, signal_key="total_assets").one()
    assert ta.numeric_value == 109549.88 and ta.status == "present" and ta.source == SRC_AIDA and ta.is_simulated is False
    assert "TOTALE ATTIVO" in ta.raw_payload_ref
    assert db.query(SignalRecord).filter_by(company_id=company.id, signal_key="labour_cost").one().numeric_value == pytest.approx(4.22, abs=0.01)
    assert db.query(RawImportRecord).filter_by(dataset_name=SRC_AIDA).count() == 1
    health = db.query(SourceHealth).filter_by(source_name=SRC_AIDA).one()
    assert health.mode == "live" and health.last_status == "success" and health.total_calls == 1

    again = A.apply_records(db, {"IT1": _record()}, apply=True)
    assert again["inserted"] == 0 and again["updated"] == 0 and again["unchanged"] > 20
    assert db.query(RawImportRecord).filter_by(dataset_name=SRC_AIDA).count() == 1                                         # replaced, not duplicated


def test_a_value_someone_entered_by_hand_is_never_overwritten(db):
    company = _company(db)
    db.add(SignalRecord(company_id=company.id, signal_key="average_salary", source="Manual Entry", status="present",
                        numeric_value=70.0, is_simulated=False))
    db.commit()
    report = A.apply_records(db, {"IT1": _record()}, apply=True)
    assert any(c["signal_key"] == "average_salary" and c["held_by"] == "Manual Entry" for c in report["conflicts"])
    assert db.query(SignalRecord).filter_by(company_id=company.id, signal_key="average_salary").one().numeric_value == 70.0


def test_a_simulated_placeholder_and_an_old_aida_value_are_replaced_and_backed_up(db):
    company = _company(db)
    db.add_all([
        SignalRecord(company_id=company.id, signal_key="cash_position", source="Aida Main 50-99", status="present", numeric_value=1.0, is_simulated=False),
        SignalRecord(company_id=company.id, signal_key="debt_level", source="Bundesanzeiger", status="present", numeric_value=9.0, is_simulated=True),
    ])
    db.commit()
    report = A.apply_records(db, {"IT1": _record()}, apply=True)
    assert {b["signal_key"] for b in report["backup"]} == {"cash_position", "debt_level"}          # the old values are kept for the backup file
    assert db.query(SignalRecord).filter_by(company_id=company.id, signal_key="cash_position").one().numeric_value == 15298.539
    assert db.query(SignalRecord).filter_by(company_id=company.id, signal_key="debt_level").one().is_simulated is False


def test_an_export_row_without_a_matching_company_is_reported_not_created(db):
    _company(db, "IT1")
    report = A.apply_records(db, {"IT1": _record(), "IT999": _record()}, apply=True)
    assert report["unmatched"] == ["IT999"] and db.query(Company).count() == 1


# ---- catalog: bounds, units and the boot-time seeding -----------------------------------------------------------------------

def test_a_source_listed_at_two_phases_seeds_one_health_row_and_does_not_crash_boot(db):
    from database import seed_source_health
    from models import IndicatorDefinition
    for key, phase in (("x_early", 2), ("x_late", 3)):
        db.add(IndicatorDefinition(key=key, label=key, category="c", axis="context", weight=0.0, source_system="Two Phase Source", phase=phase))
    db.commit()
    seed_source_health(db)                                             # used to raise IntegrityError on the second insert
    rows = db.query(SourceHealth).filter_by(source_name="Two Phase Source").all()
    assert len(rows) == 1 and rows[0].phase == 2
    assert seed_source_health(db) == 0                                 # and it stays idempotent


def test_every_aida_catalog_row_uses_a_single_phase():
    phases = {r["phase"] for r in INDICATOR_SEED if r.get("source_system") == SRC_AIDA}
    assert len(phases) == 1


def test_recalibrated_bounds_and_units_reach_an_existing_database_only_where_untouched(db):
    from indicators import apply_catalog_migrations
    from models import IndicatorDefinition

    def row(key):
        return db.query(IndicatorDefinition).filter_by(key=key).one()
    for key, lo, hi in (("materials_cost", 10.0, 60.0), ("labour_cost", 15.0, 55.0), ("capex_ratio", 1.0, 15.0), ("average_salary", 35000.0, 90000.0)):
        row(key).raw_min, row(key).raw_max = lo, hi                    # the pre-fix defaults
    row("average_salary").proxy = "Average personnel cost per employee (Personalaufwand ÷ headcount)"
    row("labour_cost").raw_max = 50.0                                   # ...except one a human tuned
    db.commit()
    out = apply_catalog_migrations(db)
    assert (row("materials_cost").raw_min, row("materials_cost").raw_max) == (25.0, 65.0)
    assert (row("capex_ratio").raw_min, row("capex_ratio").raw_max) == (0.0, 4.0)
    assert (row("average_salary").raw_min, row("average_salary").raw_max) == (35.0, 90.0) and "k EUR" in row("average_salary").proxy
    assert row("labour_cost").raw_max == 50.0                           # the hand-tuned value survives
    assert ("labour_cost", "raw_max", 50.0) in out["skipped"]


def test_calibrated_bounds_spread_the_real_portfolio_instead_of_saturating_it():
    from scoring import normalize_indicator_value
    seed = {r["key"]: r for r in INDICATOR_SEED}
    # real medians: materials 44.4%, labour 23.2%, capex 2.0% of revenue, average salary 59 k EUR
    assert 30 < normalize_indicator_value(44.4, seed["materials_cost"]) < 65          # was 68 for the median and 100 for the top decile
    assert 20 < normalize_indicator_value(23.2, seed["labour_cost"]) < 50
    assert 35 < normalize_indicator_value(2.0, seed["capex_ratio"]) < 65               # was ~93 for the median company
    assert 25 < normalize_indicator_value(59.0, seed["average_salary"]) < 60           # was 0.0 with bounds in whole euro


def test_new_indicators_render_with_the_right_unit_not_as_k_eur_amounts():
    from views.company_detail import _format_indicator_value as fmt
    assert fmt("ebit_margin", 15.75) == "15.8%"                        # "ebit" in the name used to make it a k EUR amount
    assert fmt("cash_to_revenue", 0.889) == "0.89 months"
    assert fmt("last_accounts_year", 2025.0) == "2025"
    assert fmt("average_salary", 91.75) == "91.8k" and fmt("cogs", 160969.6) == "160,970k"
    assert fmt("group_size", 264.0) == "264" and fmt("margin_compression", 4.05) == "4.05 pp"


# ---- management: auditors and advisors are not management ----------------------------------------------------------------

def _person(db, company, name, age, role="CONSIGLIERE", group="DM", position=None, appointed=None):
    raw = {"DM | Tipologia di posizione": position} if position else {}
    db.add(CompanyPerson(company_id=company.id, dataset_name="t", role_group=group, position_in_row=db.query(CompanyPerson).count(),
                         full_name=name, age=age, role=role, raw_fields=raw, appointment_date=appointed, current_or_former="Current"))
    db.commit()


def test_who_counts_as_not_management():
    def p(**kw):
        return CompanyPerson(role_group=kw.get("group", "DM"), role=kw.get("role", "CONSIGLIERE"), raw_fields=kw.get("raw", {}))
    assert _is_not_management(p(role="SINDACO EFFETTIVO")) and _is_not_management(p(role="PRESIDENTE DEL COLLEGIO SINDACALE"))
    assert _is_not_management(p(raw={"DM | Tipologia di posizione": "AudC"})) and _is_not_management(p(group="ADV"))
    assert not _is_not_management(p()) and not _is_not_management(p(role="AMMINISTRATORE DELEGATO", raw={"DM | Tipologia di posizione": "SenMan"}))


def test_management_indicators_ignore_the_audit_committee(db):
    company = _company(db)
    now = datetime.utcnow()
    _person(db, company, "Anna Bianchi", 50, appointed=now - timedelta(days=4000))
    _person(db, company, "Carlo Verdi", 60, appointed=now - timedelta(days=100))
    _person(db, company, "Mario Sindaco", 80, role="SINDACO EFFETTIVO", position="AudC", appointed=now - timedelta(days=200))
    result = sync_management_composition_signals(db, company)
    assert result["management_age"]["value"] == pytest.approx(55.0)                  # 63.3 if the 80-year-old auditor were counted
    assert result["independent_board_members"]["value"] == 2.0                       # the auditor is not a board member
    assert result["management_turnover"]["value"] == 1.0                             # only Carlo's recent appointment, not the auditor's


# ---- against the real export files (skipped where they are not present) ---------------------------------------------------------

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "Data"


@pytest.mark.skipif(not (DATA_DIR / "WAYLAND_FINANCIAL_PL_C28_SME_50_99_V0.xls").exists(), reason="raw AIDA exports not on this machine")
def test_the_real_exports_load_and_match_the_hand_verified_row():
    records = A.build_records(A.load_exports(DATA_DIR))
    assert len(records) == 954
    n = records["IT13338030151"]["signals"]                                              # Thyssenkrupp Nucera Italy
    assert n["cogs"]["value"] == pytest.approx(160969.607)                               # the LATEST production costs (the curated file held last year's)
    assert n["total_assets"]["value"] == pytest.approx(109549.88) and n["leverage_ratio"]["value"] == pytest.approx(4.2)
    blob = records["IT13338030151"]["blob"]
    assert blob["production_costs_y-1"] == pytest.approx(108046.374) and blob["personnel_costs_latest"] == pytest.approx(8716.906)
    assert blob["leverage_ratio_y-1"] == pytest.approx(8.63) and blob["leverage_ratio_y-2"] == pytest.approx(9.05)     # the pair the curated file swapped
    assert n["bvd_independence"]["status"] == "present" and n["group_size"]["value"] > 100
    present = lambda k: sum(1 for r in records.values() if r["signals"][k]["status"] == "present")
    assert present("total_assets") == 954 and present("labour_cost") == 954 and present("subsidiary_participations") == 954
    assert 5 <= sum(1 for r in records.values() if r["signals"]["distress_procedure"]["value"] == 1.0) <= 25
    assert all(0 <= r["signals"]["margin_compression"]["value"] <= 100 for r in records.values() if r["signals"]["margin_compression"]["value"] is not None)
