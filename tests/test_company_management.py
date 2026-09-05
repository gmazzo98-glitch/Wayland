"""
Tests for Company Management, German & Italian country support,
Registration ID Normalization, CSV Bulk Import, and the flexible
column-mapping data feeder.
"""

import pytest
import pandas as pd
from database import init_db, get_db_session
from models import Company, SignalRecord, ColumnMappingProfile, IndicatorDefinition, RawImportRecord, CompanyPerson
from utils import normalize_registration_nr
from company_service import (
    create_company,
    import_companies_from_csv,
    get_csv_template,
    is_source_applicable,
    sync_company_applicable_sources,
    detect_column_groups,
    compute_group_value,
    valid_targets_for_group,
    suggest_mapping,
    apply_data_import,
    save_mapping_profile,
    load_mapping_profile,
    create_ad_hoc_indicator,
    detect_multivalue_groups,
    detect_loose_stacked_groups,
    detect_person_groups,
    strip_person_column_prefix,
    explode_person_group,
    import_company_people,
    delete_companies,
)

FLEX_TEST_REGS = ["IT11122233344", "IT99988877766"]
FLEX_TEST_DATASET_NAMES = ["Test AIDA Financials", "Test Shareholder Data"]
FLEX_TEST_INDICATOR_KEYS = ["a_brand_new_column"]
PEOPLE_TEST_REGS = ["IT44455566677", "IT55566677788", "IT66677788899", "IT77788899900"]


@pytest.fixture(scope="function")
def db():
    init_db()
    session = get_db_session()
    # Clean up any test records
    test_regs = ["HRB-889900", "IT09988776655", "HRB-554433", "IT55443322110"] + FLEX_TEST_REGS + PEOPLE_TEST_REGS

    def _cleanup():
        for reg in test_regs:
            c = session.query(Company).filter_by(registration_number=reg).first()
            if c:
                session.query(RawImportRecord).filter_by(company_id=c.id).delete()
                session.query(CompanyPerson).filter_by(company_id=c.id).delete()
                session.delete(c)
        session.query(Company).filter_by(legal_name="Some Unknown Company Not In DB").delete()
        for name in FLEX_TEST_DATASET_NAMES:
            p = session.query(ColumnMappingProfile).filter_by(dataset_name=name).first()
            if p:
                session.delete(p)
        for key in FLEX_TEST_INDICATOR_KEYS:
            ind = session.query(IndicatorDefinition).filter_by(key=key).first()
            if ind:
                session.delete(ind)
        session.commit()

    _cleanup()
    yield session
    _cleanup()
    session.close()


def test_registration_number_normalization():
    # German formats
    assert normalize_registration_nr("HRB 123456", "Germany") == "HRB-123456"
    assert normalize_registration_nr("Amtsgericht München HRB-987654", "Germany") == "HRB-987654"
    assert normalize_registration_nr("HRA 11223", "Germany") == "HRA-11223"

    # Italian formats
    assert normalize_registration_nr("IT01234567890", "Italy") == "IT01234567890"
    assert normalize_registration_nr("01234567890", "Italy") == "IT01234567890"
    assert normalize_registration_nr("REA MI-1234567", "Italy") == "REA-MI-1234567"
    assert normalize_registration_nr("REA 987654", "Italy") == "REA-987654"


def test_create_german_company(db):
    data = {
        "legal_name": "Test Bayern Agrar GmbH",
        "registration_number": "HRB 889900",
        "country": "Germany",
        "nace_code": "A01.11",
        "sector_name": "Smart Farming",
        "website_url": "https://bayern-agrar-test.de",
        "segment": "Midcap",
        "headcount": 300,
    }
    company, err = create_company(db, data, auto_sync=False)
    assert err is None
    assert company is not None
    assert company.legal_name == "Test Bayern Agrar GmbH"
    assert company.registration_number == "HRB-889900"
    assert company.country == "Germany"

    # Verify signals were initialized
    signals = db.query(SignalRecord).filter_by(company_id=company.id).all()
    assert len(signals) > 0
    for s in signals:
        assert s.status == "not_yet_checked"


def test_create_italian_company(db):
    data = {
        "legal_name": "Test Milano BioTech S.r.l.",
        "registration_number": "09988776655",
        "country": "Italy",
        "nace_code": "A01.13",
        "sector_name": "Vertical Farming",
        "website_url": "https://milano-biotech-test.it",
        "segment": "SME",
        "headcount": 45,
    }
    company, err = create_company(db, data, auto_sync=False)
    assert err is None
    assert company is not None
    assert company.legal_name == "Test Milano BioTech S.r.l."
    assert company.registration_number == "IT09988776655"
    assert company.country == "Italy"


def test_duplicate_registration_rejection(db):
    data = {
        "legal_name": "First Company GmbH",
        "registration_number": "HRB 889900",
        "country": "Germany",
    }
    c1, err1 = create_company(db, data, auto_sync=False)
    assert c1 is not None

    # Attempt to create another company with the exact same normalized registration number
    dup_data = {
        "legal_name": "Duplicate Company GmbH",
        "registration_number": "HRB-889900",
        "country": "Germany",
    }
    company, err = create_company(db, dup_data, auto_sync=False)
    assert company is None
    assert "already exists" in err


def test_source_applicability():
    # Germany: Destatis, Arbeitsagentur, EPO OPS, EUIPO, Wappalyzer all applicable
    assert is_source_applicable("Destatis", "Germany") is True
    assert is_source_applicable("Arbeitsagentur", "Germany") is True
    assert is_source_applicable("EPO OPS", "Germany") is True
    assert is_source_applicable("EUIPO", "Germany") is True
    assert is_source_applicable("Wappalyzer", "Germany") is True

    # Italy: Destatis, Arbeitsagentur NOT applicable; EU/Universal APIs ARE applicable
    assert is_source_applicable("Destatis", "Italy") is False
    assert is_source_applicable("Arbeitsagentur", "Italy") is False
    assert is_source_applicable("Handelsregister Free Snapshot", "Italy") is False
    assert is_source_applicable("Bundesanzeiger", "Italy") is False
    assert is_source_applicable("EPO OPS", "Italy") is True
    assert is_source_applicable("EUIPO", "Italy") is True
    assert is_source_applicable("EU Funding Portal", "Italy") is True
    assert is_source_applicable("Wappalyzer", "Italy") is True
    assert is_source_applicable("Google News", "Italy") is True


def test_csv_template_and_import(db):
    template = get_csv_template()
    assert "legal_name" in template
    assert "registration_number" in template
    assert "country" in template

    sample_csv = """legal_name,registration_number,country,nace_code,sector_name,website_url,segment,headcount
CSV Import Germany GmbH,HRB 554433,Germany,A01.11,Smart Farming,https://csv-de.de,Midcap,280
CSV Import Italy S.p.A.,IT55443322110,Italy,A01.13,Horticulture,https://csv-it.it,SME,35
"""
    result = import_companies_from_csv(db, sample_csv, auto_sync=False)
    assert result["created"] == 2
    assert result["skipped"] == 0
    assert len(result["errors"]) == 0

    de_comp = db.query(Company).filter_by(registration_number="HRB-554433").first()
    assert de_comp is not None
    assert de_comp.country == "Germany"

    it_comp = db.query(Company).filter_by(registration_number="IT55443322110").first()
    assert it_comp is not None
    assert it_comp.country == "Italy"


# =============================================================================
# Flexible column-mapping data feeder
# =============================================================================

def test_detect_column_groups_timeseries_and_single_point():
    groups = detect_column_groups([
        "revenue_latest", "revenue_y-1", "revenue_y-2", "region", "leverage_ratio_latest"
    ])
    assert set(groups.keys()) == {"revenue", "region", "leverage_ratio"}

    assert groups["revenue"]["is_timeseries"] is True
    assert groups["revenue"]["points"] == {
        "latest": "revenue_latest", "y-1": "revenue_y-1", "y-2": "revenue_y-2"
    }

    assert groups["region"]["is_timeseries"] is False
    assert groups["region"]["points"] == {"value": "region"}

    assert groups["leverage_ratio"]["is_timeseries"] is False
    assert groups["leverage_ratio"]["points"] == {"latest": "leverage_ratio_latest"}


def test_compute_group_value_direct_single_point():
    group = {"points": {"latest": "leverage_ratio_latest"}}
    row = {"leverage_ratio_latest": 1.8}
    value, status = compute_group_value(group, row, "leverage_ratio")
    assert status == "present"
    assert value == 1.8


def test_compute_group_value_direct_missing_is_not_faked():
    group = {"points": {"latest": "leverage_ratio_latest"}}
    row = {"leverage_ratio_latest": None}
    value, status = compute_group_value(group, row, "leverage_ratio")
    assert status == "not_yet_checked"
    assert value is None


def test_compute_group_value_revenue_trend():
    group = {"points": {"latest": "revenue_latest", "y-1": "revenue_y-1", "y-2": "revenue_y-2"}}
    row = {"revenue_latest": 120.0, "revenue_y-1": 110.0, "revenue_y-2": 100.0}
    value, status = compute_group_value(group, row, "revenue_trend")
    assert status == "present"
    assert value == pytest.approx(20.0)  # (120-100)/100 * 100


def test_compute_group_value_ebit_trend_negative_base_uses_abs():
    # EBIT swings from a loss (-10) to a profit (5): plain division would
    # flip the sign; abs-base division correctly reports a large improvement.
    group = {"points": {"latest": "ebit_latest", "y-2": "ebit_y-2"}}
    row = {"ebit_latest": 5.0, "ebit_y-2": -10.0}
    value, status = compute_group_value(group, row, "ebit_trend")
    assert status == "present"
    assert value == pytest.approx(150.0)  # (5 - (-10)) / abs(-10) * 100


def test_compute_group_value_trend_missing_base_left_not_yet_checked():
    group = {"points": {"latest": "revenue_latest", "y-2": "revenue_y-2"}}
    row = {"revenue_latest": 120.0, "revenue_y-2": None}
    value, status = compute_group_value(group, row, "revenue_trend")
    assert status == "not_yet_checked"
    assert value is None


def test_compute_group_value_trend_zero_base_left_not_yet_checked():
    group = {"points": {"latest": "revenue_latest", "y-2": "revenue_y-2"}}
    row = {"revenue_latest": 120.0, "revenue_y-2": 0.0}
    value, status = compute_group_value(group, row, "revenue_trend")
    assert status == "not_yet_checked"
    assert value is None


def test_compute_group_value_margin_compression_is_a_decline_not_a_percent_change():
    group = {"points": {"latest": "gross_margin_latest", "y-2": "gross_margin_y-2"}}
    # Margin fell from 40% to 30%: 10 points of compression.
    value, status = compute_group_value(group, {"gross_margin_latest": 30.0, "gross_margin_y-2": 40.0}, "margin_compression")
    assert status == "present"
    assert value == pytest.approx(10.0)
    # Margin improved: compression floors at 0, never negative.
    value, status = compute_group_value(group, {"gross_margin_latest": 45.0, "gross_margin_y-2": 40.0}, "margin_compression")
    assert status == "present"
    assert value == pytest.approx(0.0)


def test_trend_indicator_only_offered_for_timeseries_group(db):
    single_point_group = {"points": {"latest": "revenue_latest"}, "is_timeseries": False}
    timeseries_group = {"points": {"latest": "revenue_latest", "y-2": "revenue_y-2"}, "is_timeseries": True}

    single_options = valid_targets_for_group(db, single_point_group)
    multi_options = valid_targets_for_group(db, timeseries_group)

    assert "indicator:revenue_trend" not in single_options
    assert "indicator:revenue_trend" in multi_options


def _aida_style_dataframe():
    return pd.DataFrame([
        {
            "partita_iva": "IT11122233344", "ragione_sociale": "Test Agrifood Italia S.r.l.",
            "revenue_latest": 5_000_000, "revenue_y-1": 4_800_000, "revenue_y-2": 4_000_000,
            "leverage_ratio_latest": 1.2,
        },
        {
            "partita_iva": "IT99988877766", "ragione_sociale": "Test Vinicola Toscana S.p.A.",
            "revenue_latest": 8_000_000, "revenue_y-1": 8_200_000, "revenue_y-2": 9_000_000,
            "leverage_ratio_latest": 2.5,
        },
    ])


def _aida_style_mapping():
    return {
        "partita_iva": "company:registration_number",
        "ragione_sociale": "company:legal_name",
        "revenue": "indicator:revenue_trend",
        "leverage_ratio": "indicator:leverage_ratio",
    }


def test_apply_data_import_requires_a_match_key(db):
    # Neither Registration Number nor Legal Name mapped -> must error, not guess.
    df = _aida_style_dataframe()
    mapping = {k: v for k, v in _aida_style_mapping().items()
               if v not in ("company:registration_number", "company:legal_name")}
    result = apply_data_import(db, df, mapping, "Test AIDA Financials", country="Italy", dry_run=False)
    assert result["created"] == 0
    assert any("Registration Number" in e and "Legal Name" in e for e in result["errors"])


def test_apply_data_import_legal_name_match_never_creates(db):
    # No Registration Number column mapped, only Legal Name — e.g. a file
    # whose only identity column (like AIDA's BvD ID) doesn't reliably match
    # the registration numbers already on file. Must merge into an existing
    # company by exact legal_name, and must NEVER create a new company from
    # a name alone — an unmatched name is reported, not guessed into being.
    existing, err = create_company(db, {
        "legal_name": "Test Vinicola Toscana S.p.A.", "registration_number": "IT99988877766",
        "country": "Italy",
    }, auto_sync=False)
    assert err is None

    df = _aida_style_dataframe()  # has "Test Agrifood Italia S.r.l." (no existing company) and "Test Vinicola Toscana S.p.A." (exists)
    mapping = {k: v for k, v in _aida_style_mapping().items() if v != "company:registration_number"}

    result = apply_data_import(db, df, mapping, "Test Legal Name Match", country="Italy", dry_run=False)
    assert result["created"] == 0  # never creates in legal-name mode
    assert result["merged"] == 1
    assert result["unmatched"] == ["Test Agrifood Italia S.r.l."]
    assert db.query(Company).filter_by(legal_name="Test Agrifood Italia S.r.l.").first() is None

    sig = db.query(SignalRecord).filter_by(company_id=existing.id, signal_key="leverage_ratio").first()
    assert sig.status == "present"
    assert sig.numeric_value == 2.5


def test_apply_data_import_dry_run_makes_no_writes(db):
    df = _aida_style_dataframe()
    mapping = _aida_style_mapping()
    before = db.query(Company).filter(Company.registration_number.in_(FLEX_TEST_REGS)).count()
    assert before == 0

    result = apply_data_import(db, df, mapping, "Test AIDA Financials", country="Italy", dry_run=True)
    assert result["created"] == 2
    assert result["errors"] == []

    after = db.query(Company).filter(Company.registration_number.in_(FLEX_TEST_REGS)).count()
    assert after == 0  # dry run must not have written anything


def test_apply_data_import_creates_companies_and_signals(db):
    df = _aida_style_dataframe()
    mapping = _aida_style_mapping()
    result = apply_data_import(db, df, mapping, "Test AIDA Financials", country="Italy", dry_run=False)

    assert result["created"] == 2
    assert result["errors"] == []

    company = db.query(Company).filter_by(registration_number="IT11122233344").first()
    assert company is not None
    assert company.legal_name == "Test Agrifood Italia S.r.l."
    assert company.country == "Italy"

    revenue_sig = db.query(SignalRecord).filter_by(company_id=company.id, signal_key="revenue_trend").first()
    assert revenue_sig.status == "present"
    assert revenue_sig.numeric_value == pytest.approx(25.0)  # (5.0M - 4.0M) / 4.0M * 100
    assert revenue_sig.is_simulated is False
    assert revenue_sig.source == "Test AIDA Financials"

    leverage_sig = db.query(SignalRecord).filter_by(company_id=company.id, signal_key="leverage_ratio").first()
    assert leverage_sig.numeric_value == pytest.approx(1.2)
    assert leverage_sig.status == "present"


def test_apply_data_import_writes_raw_blob_alongside_structured_signals(db):
    df = _aida_style_dataframe()
    result = apply_data_import(db, df, _aida_style_mapping(), "Test AIDA Financials",
                                country="Italy", dry_run=False, source_filename="aida_export.xlsx")
    assert result["created"] == 2

    company = db.query(Company).filter_by(registration_number="IT11122233344").first()
    raw = db.query(RawImportRecord).filter_by(company_id=company.id, dataset_name="Test AIDA Financials").first()
    assert raw is not None
    assert raw.source_filename == "aida_export.xlsx"
    # The blob keeps EVERY original column, including ones the mapping didn't use.
    assert raw.raw_row["partita_iva"] == "IT11122233344"
    assert raw.raw_row["revenue_y-1"] == 4_800_000
    assert raw.raw_row["revenue_y-2"] == 4_000_000
    assert raw.mapping_snapshot == _aida_style_mapping()


def test_apply_data_import_raw_blob_dry_run_writes_nothing(db):
    df = _aida_style_dataframe()
    apply_data_import(db, df, _aida_style_mapping(), "Test AIDA Financials", country="Italy", dry_run=True)
    assert db.query(RawImportRecord).filter(
        RawImportRecord.company_id.in_(
            db.query(Company.id).filter(Company.registration_number.in_(FLEX_TEST_REGS))
        )
    ).count() == 0


def test_apply_data_import_raw_blob_overwritten_on_reimport(db):
    df = _aida_style_dataframe()
    apply_data_import(db, df, _aida_style_mapping(), "Test AIDA Financials", country="Italy", dry_run=False)
    company = db.query(Company).filter_by(registration_number="IT11122233344").first()

    updated_df = df.copy()
    updated_df.loc[updated_df["partita_iva"] == "IT11122233344", "revenue_latest"] = 6_000_000
    apply_data_import(db, updated_df, _aida_style_mapping(), "Test AIDA Financials",
                       country="Italy", overwrite_conflicts=True, dry_run=False)

    raw_records = db.query(RawImportRecord).filter_by(company_id=company.id, dataset_name="Test AIDA Financials").all()
    assert len(raw_records) == 1  # overwritten in place, not duplicated
    assert raw_records[0].raw_row["revenue_latest"] == 6_000_000


def test_apply_data_import_different_dataset_merges_no_conflict(db):
    df = _aida_style_dataframe()
    apply_data_import(db, df, _aida_style_mapping(), "Test AIDA Financials", country="Italy", dry_run=False)

    # A different dataset type enriching the same companies (e.g. shareholder
    # structure) should just merge in — never flagged as a conflict.
    shareholder_df = pd.DataFrame([
        {"partita_iva": "IT11122233344", "family_ownership_pct": 80},
    ])
    new_key = create_ad_hoc_indicator(db, "Family Ownership Pct (Test)")
    FLEX_TEST_INDICATOR_KEYS.append(new_key)
    mapping = {
        "partita_iva": "company:registration_number",
        "family_ownership_pct": f"indicator:{new_key}",
    }
    result = apply_data_import(db, shareholder_df, mapping, "Test Shareholder Data", country="Italy", dry_run=False)
    assert result["created"] == 0
    assert result["merged"] == 1
    assert result["conflicts"] == []


def test_apply_data_import_same_dataset_reupload_is_flagged_and_skipped_without_overwrite(db):
    df = _aida_style_dataframe()
    apply_data_import(db, df, _aida_style_mapping(), "Test AIDA Financials", country="Italy", dry_run=False)

    company = db.query(Company).filter_by(registration_number="IT11122233344").first()
    original_leverage = db.query(SignalRecord).filter_by(company_id=company.id, signal_key="leverage_ratio").first().numeric_value

    # Re-upload the SAME dataset name with changed figures.
    updated_df = df.copy()
    updated_df.loc[updated_df["partita_iva"] == "IT11122233344", "leverage_ratio_latest"] = 3.3

    preview = apply_data_import(db, updated_df, _aida_style_mapping(), "Test AIDA Financials", country="Italy", dry_run=True)
    assert len(preview["conflicts"]) == 2
    assert preview["created"] == 0
    assert preview["merged"] == 0

    result = apply_data_import(db, updated_df, _aida_style_mapping(), "Test AIDA Financials",
                                country="Italy", overwrite_conflicts=False, dry_run=False)
    assert len(result["conflicts"]) == 2
    assert result["overwritten"] == 0

    unchanged_sig = db.query(SignalRecord).filter_by(company_id=company.id, signal_key="leverage_ratio").first()
    assert unchanged_sig.numeric_value == pytest.approx(original_leverage)  # untouched without overwrite


def test_apply_data_import_same_dataset_reupload_overwrites_when_confirmed(db):
    df = _aida_style_dataframe()
    apply_data_import(db, df, _aida_style_mapping(), "Test AIDA Financials", country="Italy", dry_run=False)

    company = db.query(Company).filter_by(registration_number="IT11122233344").first()

    updated_df = df.copy()
    updated_df.loc[updated_df["partita_iva"] == "IT11122233344", "leverage_ratio_latest"] = 3.3

    result = apply_data_import(db, updated_df, _aida_style_mapping(), "Test AIDA Financials",
                                country="Italy", overwrite_conflicts=True, dry_run=False)
    assert result["overwritten"] == 2

    updated_sig = db.query(SignalRecord).filter_by(company_id=company.id, signal_key="leverage_ratio").first()
    assert updated_sig.numeric_value == pytest.approx(3.3)


def test_mapping_profile_save_and_reload_round_trip(db):
    mapping = _aida_style_mapping()
    save_mapping_profile(db, "Test AIDA Financials", "Italy", mapping)
    reloaded = load_mapping_profile(db, "Test AIDA Financials")
    assert reloaded == mapping

    profile = db.query(ColumnMappingProfile).filter_by(dataset_name="Test AIDA Financials").first()
    assert profile is not None
    assert profile.country == "Italy"


def test_create_ad_hoc_indicator_is_unscored_context_and_idempotent(db):
    key1 = create_ad_hoc_indicator(db, "A Brand New Column")
    FLEX_TEST_INDICATOR_KEYS.append(key1)
    defn = db.query(IndicatorDefinition).filter_by(key=key1).first()
    assert defn is not None
    assert defn.axis == "context"
    assert defn.weight == 0.0

    key2 = create_ad_hoc_indicator(db, "A Brand New Column")
    assert key2 == key1
    assert db.query(IndicatorDefinition).filter_by(key=key1).count() == 1


def test_delete_companies_cascades_signals_and_pilot_outcomes(db):
    from models import PilotOutcome

    company, err = create_company(db, {
        "legal_name": "Test Delete Me S.r.l.", "registration_number": "IT11122233344",
        "country": "Italy",
    }, auto_sync=False)
    assert err is None
    signal_count_before = db.query(SignalRecord).filter_by(company_id=company.id).count()
    assert signal_count_before > 0  # create_company seeds the full not-yet-checked scaffold

    outcome = PilotOutcome(company_id=company.id, pilot_label="Test Pilot")
    db.add(outcome)
    db.add(RawImportRecord(company_id=company.id, dataset_name="Test AIDA Financials", raw_row={"a": 1}))
    db.commit()

    result = delete_companies(db, [company.id])
    assert result["deleted"] == 1
    assert result["signals_deleted"] == signal_count_before
    assert result["pilot_outcomes_deleted"] == 1
    assert result["raw_import_records_deleted"] == 1

    assert db.query(Company).filter_by(id=company.id).first() is None
    assert db.query(SignalRecord).filter_by(company_id=company.id).count() == 0
    assert db.query(PilotOutcome).filter_by(company_id=company.id).count() == 0
    assert db.query(RawImportRecord).filter_by(company_id=company.id).count() == 0


def test_delete_companies_ignores_unknown_ids(db):
    result = delete_companies(db, ["not-a-real-id"])
    assert result == {
        "deleted": 0, "signals_deleted": 0,
        "pilot_outcomes_deleted": 0, "raw_import_records_deleted": 0,
        "people_deleted": 0,
    }


# --- Management & board roster import (newline-stacked multi-value cells) ---

def test_detect_multivalue_groups_by_header_prefix():
    columns = [
        "Ragione sociale", "BvD ID number",
        "DM\nNome completo", "DM\nCarica", "DM\nEtà",
        "ADV\nNome", "ADV\nCognome",
        "Lone\nColumn",
    ]
    groups = detect_multivalue_groups(columns)
    assert set(groups.keys()) == {"DM", "ADV"}
    assert groups["DM"] == ["DM\nNome completo", "DM\nCarica", "DM\nEtà"]
    assert groups["ADV"] == ["ADV\nNome", "ADV\nCognome"]
    # "Lone\nColumn" has a \n but no sibling under that prefix -> not a group


def test_explode_person_group_zips_positionally():
    row = {
        "DM\nNome completo": "Alice Rossi\nBob Bianchi\nCarla Verdi",
        "DM\nCarica": "PRESIDENTE\nCONSIGLIERE\nCONSIGLIERE",
        "DM\nEtà": "50\n40\n60",
    }
    people = explode_person_group(row, list(row.keys()))
    assert len(people) == 3
    assert people[0] == {"Nome completo": "Alice Rossi", "Carica": "PRESIDENTE", "Età": "50"}
    assert people[2]["Nome completo"] == "Carla Verdi"


def test_explode_person_group_handles_ragged_columns():
    # "Età" has one fewer line than the other two columns for this row —
    # must degrade to None for the missing person, not crash or drop rows.
    row = {
        "DM\nNome completo": "Alice Rossi\nBob Bianchi",
        "DM\nCarica": "PRESIDENTE\nCONSIGLIERE",
        "DM\nEtà": "50",
    }
    people = explode_person_group(row, list(row.keys()))
    assert len(people) == 2
    assert people[0]["Età"] == "50"
    assert people[1]["Età"] is None


def test_explode_person_group_single_person_no_newline():
    row = {"DM\nNome completo": "Solo Direttore", "DM\nCarica": "AMMINISTRATORE UNICO"}
    people = explode_person_group(row, list(row.keys()))
    assert len(people) == 1
    assert people[0]["Nome completo"] == "Solo Direttore"


def test_import_company_people_creates_and_matches_by_legal_name(db):
    company, err = create_company(db, {
        "legal_name": "Test Board Import S.p.A.", "registration_number": "IT44455566677",
        "country": "Italy",
    }, auto_sync=False)
    assert err is None

    df = pd.DataFrame([{
        "Ragione sociale": "Test Board Import S.p.A.",
        "DM\nNome completo": "Mario Rossi\nGiulia Bianchi",
        "DM\nCarica": "PRESIDENTE\nCONSIGLIERE",
        "DM\nEtà": "55\n42",
    }, {
        "Ragione sociale": "Some Unknown Company Not In DB",
        "DM\nNome completo": "Nobody",
        "DM\nCarica": "CONSIGLIERE",
        "DM\nEtà": "30",
    }])

    result = import_company_people(db, df, "Test Roster Dataset", dry_run=False)
    assert result["matched"] == 1
    assert result["people_created"] == 2
    assert result["unmatched"] == ["Some Unknown Company Not In DB"]
    assert result["errors"] == []

    people = db.query(CompanyPerson).filter_by(company_id=company.id).order_by(CompanyPerson.position_in_row).all()
    assert len(people) == 2
    assert people[0].full_name == "Mario Rossi"
    assert people[0].role == "PRESIDENTE"
    assert people[0].age == 55
    assert people[0].raw_fields["Nome completo"] == "Mario Rossi"
    assert people[1].full_name == "Giulia Bianchi"
    assert people[1].age == 42

    # No company was created for the unmatched name.
    assert db.query(Company).filter_by(legal_name="Some Unknown Company Not In DB").first() is None


def test_import_company_people_dry_run_makes_no_writes(db):
    company, err = create_company(db, {
        "legal_name": "Test Board Dry Run S.p.A.", "registration_number": "IT55566677788",
        "country": "Italy",
    }, auto_sync=False)
    assert err is None

    df = pd.DataFrame([{
        "Ragione sociale": "Test Board Dry Run S.p.A.",
        "DM\nNome completo": "Someone",
        "DM\nCarica": "CONSIGLIERE",
    }])
    result = import_company_people(db, df, "Dry Run Dataset", dry_run=True)
    assert result["matched"] == 1
    assert db.query(CompanyPerson).filter_by(company_id=company.id).count() == 0


def test_import_company_people_conflict_then_overwrite(db):
    company, err = create_company(db, {
        "legal_name": "Test Board Conflict S.p.A.", "registration_number": "IT66677788899",
        "country": "Italy",
    }, auto_sync=False)
    assert err is None

    df_v1 = pd.DataFrame([{
        "Ragione sociale": "Test Board Conflict S.p.A.",
        "DM\nNome completo": "Original Director",
        "DM\nCarica": "PRESIDENTE",
    }])
    r1 = import_company_people(db, df_v1, "Conflict Dataset", dry_run=False)
    assert r1["people_created"] == 1

    df_v2 = pd.DataFrame([{
        "Ragione sociale": "Test Board Conflict S.p.A.",
        "DM\nNome completo": "Updated Director",
        "DM\nCarica": "AMMINISTRATORE DELEGATO",
    }])

    # Without overwrite: flagged as a conflict, no change applied.
    r2 = import_company_people(db, df_v2, "Conflict Dataset", dry_run=False, overwrite_conflicts=False)
    assert r2["matched"] == 0
    assert len(r2["conflicts"]) == 1
    person = db.query(CompanyPerson).filter_by(company_id=company.id).first()
    assert person.full_name == "Original Director"

    # With overwrite: updates the existing person-slot in place, doesn't duplicate.
    r3 = import_company_people(db, df_v2, "Conflict Dataset", dry_run=False, overwrite_conflicts=True)
    assert r3["people_updated"] == 1
    assert db.query(CompanyPerson).filter_by(company_id=company.id).count() == 1
    person = db.query(CompanyPerson).filter_by(company_id=company.id).first()
    assert person.full_name == "Updated Director"
    assert person.role == "AMMINISTRATORE DELEGATO"


# --- Loose (non-'\n'-prefixed) stacked-column detection ---
#
# AIDA's shareholder-control and legal-ownership exports use the same
# newline-stacked-cell convention as the director/advisor board roster
# above, but DON'T mark every group column with a shared 'Prefix\n...'
# header the way DM/ADV do — real headers mix 'Azionisti\nNome' (newline)
# with 'Azionisti Numero BvD' (space) and 'Azionista - Ticker Symbol' (dash,
# a singular/plural variant), and the ownership-chain's 'Livello' column
# shares no header text with 'CSH' at all. These tests exercise
# detect_loose_stacked_groups/detect_person_groups against that shape with
# small min_multiline_cells/min_overlap so a handful of synthetic rows is
# enough evidence, mirroring the thresholds verified against the real files.

def test_strip_person_column_prefix_handles_separator_variants():
    assert strip_person_column_prefix("DM\nCarica") == "Carica"
    assert strip_person_column_prefix("Azionisti Numero BvD") == "Numero BvD"
    assert strip_person_column_prefix("Azionista - Ticker Symbol") == "Ticker Symbol"
    assert strip_person_column_prefix("Livello") == "Livello"  # nothing to strip


def test_detect_loose_stacked_groups_by_broadened_prefix():
    df = pd.DataFrame([
        {"Owner Name": "Acme Holding\nAcme Parent\nAcme Group", "Owner Type": "Company\nCompany\nCompany"},
        {"Owner Name": "Solo Owner", "Owner Type": "Person"},
        {"Owner Name": "X\nY", "Owner Type": "Company\nPerson"},
    ])
    groups = detect_loose_stacked_groups(df, min_multiline_cells=2, min_overlap=2)
    assert set(groups.keys()) == {"Owner"}
    assert groups["Owner"] == ["Owner Name", "Owner Type"]


def test_detect_loose_stacked_groups_excludes_same_prefix_scalar_column():
    # "Data di apertura" is genuinely stacked; "Data di chiusura" shares the
    # 'Data' prefix but is a real, always-single-valued scalar field that
    # just happens to start with the same word — must not be swept in.
    df = pd.DataFrame([
        {"Data di apertura": "01/01/2020\n01/01/2021", "Data di chiusura": "31/12/2029"},
        {"Data di apertura": "01/01/2019", "Data di chiusura": "31/12/2028"},
        {"Data di apertura": "01/01/2018\n01/01/2017\n01/01/2016", "Data di chiusura": "31/12/2027"},
        {"Data di apertura": "01/01/2015\n01/01/2014", "Data di chiusura": "31/12/2026"},
    ])
    groups = detect_loose_stacked_groups(df, min_multiline_cells=2, min_overlap=3)
    assert groups == {}


def test_detect_loose_stacked_groups_adopts_orphan_with_no_shared_prefix():
    # "Level" shares no header text with "Owner Name"/"Owner Type" — same
    # real-world shape as AIDA's ownership-chain 'Livello' next to 'CSH Nome'.
    df = pd.DataFrame([
        {"Owner Name": "Acme Holding\nAcme Parent\nAcme Group", "Owner Type": "Company\nCompany\nCompany", "Level": "3\n2\n1"},
        {"Owner Name": "Solo Owner", "Owner Type": "Person", "Level": "1"},
        {"Owner Name": "X\nY", "Owner Type": "Company\nPerson", "Level": "2\n1"},
        {"Owner Name": "P\nQ\nR\nS", "Owner Type": "C\nC\nC\nC", "Level": "4\n3\n2\n1"},
    ])
    groups = detect_loose_stacked_groups(df, min_multiline_cells=2, min_overlap=3)
    assert set(groups.keys()) == {"Owner"}
    assert set(groups["Owner"]) == {"Owner Name", "Owner Type", "Level"}


def test_detect_loose_stacked_groups_merges_spelling_variants_via_correlation():
    # "Shareholders ..." (plural) and "Shareholder ..." (singular) don't
    # share one exact prefix token but their content lines up row-for-row —
    # same shape as AIDA's Azionisti/Azionista split.
    df = pd.DataFrame([
        {"Shareholders Name": "A\nB\nC", "Shareholders Country": "IT\nIT\nFR",
         "Shareholder Type": "1\n2\n3", "Shareholder Source": "X\nY\nZ"},
        {"Shareholders Name": "Solo", "Shareholders Country": "IT",
         "Shareholder Type": "9", "Shareholder Source": "X"},
        {"Shareholders Name": "X\nY", "Shareholders Country": "IT\nDE",
         "Shareholder Type": "7\n8", "Shareholder Source": "A\nB"},
        {"Shareholders Name": "P\nQ\nR\nS", "Shareholders Country": "IT\nIT\nIT\nIT",
         "Shareholder Type": "4\n5\n6\n11", "Shareholder Source": "M\nN\nO\nP"},
    ])
    groups = detect_loose_stacked_groups(df, min_multiline_cells=2, min_overlap=3)
    assert len(groups) == 1
    cols = next(iter(groups.values()))
    assert set(cols) == {"Shareholders Name", "Shareholders Country", "Shareholder Type", "Shareholder Source"}


def test_detect_loose_stacked_groups_does_not_merge_independent_groups():
    # Two genuinely independent stacked groups whose counts only
    # coincidentally match on a minority of rows must stay separate — real
    # Directors vs Advisors columns in production data matched on ~7% of
    # rows; this synthetic case is deliberately similar (mostly mismatched
    # counts, occasional coincidental agreement).
    df = pd.DataFrame([
        {"GroupA Name": "A\nB\nC", "GroupA Role": "1\n2\n3", "GroupB Name": "X", "GroupB Role": "9"},
        {"GroupA Name": "A\nB", "GroupA Role": "1\n2", "GroupB Name": "X\nY\nZ", "GroupB Role": "9\n8\n7"},
        {"GroupA Name": "A", "GroupA Role": "1", "GroupB Name": "X\nY", "GroupB Role": "9\n8"},
        {"GroupA Name": "A\nB\nC\nD", "GroupA Role": "1\n2\n3\n4", "GroupB Name": "X\nY\nZ\nW\nV", "GroupB Role": "9\n8\n7\n6\n5"},
        {"GroupA Name": "A\nB", "GroupA Role": "1\n2", "GroupB Name": "X\nY\nZ\nW", "GroupB Role": "9\n8\n7\n6"},
    ])
    groups = detect_loose_stacked_groups(df, min_multiline_cells=2, min_overlap=3)
    assert set(groups.keys()) == {"GroupA", "GroupB"}
    assert set(groups["GroupA"]) == {"GroupA Name", "GroupA Role"}
    assert set(groups["GroupB"]) == {"GroupB Name", "GroupB Role"}


def test_detect_person_groups_unions_header_and_loose_groups_with_same_label():
    # "Azionisti\nCommenti"/"Azionisti\nNome" (literal '\n', found by
    # detect_multivalue_groups) and "Azionisti Tipo"/"Azionisti Fonte"
    # (space, found by detect_loose_stacked_groups) must end up as ONE
    # unioned 'Azionisti' group — an earlier version of this merge blindly
    # overwrote one detector's result with the other's, silently dropping
    # 'Azionisti\nNome' (the shareholder's own name) from the final group.
    df = pd.DataFrame([
        {"Azionisti\nCommenti": None, "Azionisti\nNome": "Mario Rossi\nCDP Venture\nOther Co",
         "Azionisti Tipo": "Persone fisiche\nSocietà\nSocietà", "Azionisti Fonte": "HO\nHO\nZP"},
        {"Azionisti\nCommenti": None, "Azionisti\nNome": "Solo Holder",
         "Azionisti Tipo": "Persone fisiche", "Azionisti Fonte": "HO"},
        {"Azionisti\nCommenti": "note", "Azionisti\nNome": "A\nB",
         "Azionisti Tipo": "X\nY", "Azionisti Fonte": "M\nN"},
        {"Azionisti\nCommenti": None, "Azionisti\nNome": "P\nQ\nR\nS",
         "Azionisti Tipo": "1\n2\n3\n4", "Azionisti Fonte": "A\nB\nC\nD"},
    ])
    groups = detect_person_groups(df)
    assert set(groups.keys()) == {"Azionisti"}
    assert set(groups["Azionisti"]) == {
        "Azionisti\nCommenti", "Azionisti\nNome", "Azionisti Tipo", "Azionisti Fonte",
    }


def test_import_company_people_with_loosely_detected_shareholder_group(db):
    """End-to-end: a shareholder-shaped file (mixed-separator group +
    Livello-style orphan, no literal '\\n' anywhere) imports correctly
    through the same import_company_people/CompanyPerson pipeline as the
    board roster — 'Tipo' lands on .role via the alias table, 'Nome' lands
    on .full_name via the existing nome/cognome fallback (no 'full name'
    alias fires, so it joins whatever's under 'nome' alone), and 'Livello'
    survives in raw_fields even though it has no structured column."""
    company, err = create_company(db, {
        "legal_name": "Test Shareholder Import S.p.A.", "registration_number": "IT77788899900",
        "country": "Italy",
    }, auto_sync=False)
    assert err is None

    df = pd.DataFrame([
        {"Ragione sociale": "Test Shareholder Import S.p.A.",
         "Azionisti Nome": "Mario Rossi\nCDP Venture Capital\nOther Holder",
         "Azionisti Tipo": "Persone fisiche\nSocietà\nSocietà",
         "Livello": "1\n2\n3"},
        {"Ragione sociale": "Some Unknown Shareholder Target",
         "Azionisti Nome": "Nobody", "Azionisti Tipo": "Persone fisiche", "Livello": "1"},
        {"Ragione sociale": "Padding Row One",
         "Azionisti Nome": "X\nY", "Azionisti Tipo": "A\nB", "Livello": "2\n1"},
        {"Ragione sociale": "Padding Row Two",
         "Azionisti Nome": "P\nQ\nR\nS", "Azionisti Tipo": "1\n2\n3\n4", "Livello": "4\n3\n2\n1"},
        {"Ragione sociale": "Padding Row Three",
         "Azionisti Nome": "M\nN\nO", "Azionisti Tipo": "a\nb\nc", "Livello": "3\n2\n1"},
        {"Ragione sociale": "Padding Row Four",
         "Azionisti Nome": "Solo2", "Azionisti Tipo": "T", "Livello": "1"},
    ])

    result = import_company_people(db, df, "Test Shareholder Dataset", dry_run=False)
    assert result["errors"] == []
    assert result["matched"] == 1
    assert result["people_created"] == 3

    people = db.query(CompanyPerson).filter_by(company_id=company.id).order_by(CompanyPerson.position_in_row).all()
    assert len(people) == 3
    assert people[0].full_name == "Mario Rossi"
    assert people[0].role == "Persone fisiche"
    assert people[0].raw_fields["Livello"] == "1"
    assert people[1].full_name == "CDP Venture Capital"
    assert people[1].role == "Società"
    assert people[2].full_name == "Other Holder"
    assert people[2].raw_fields["Livello"] == "3"
