"""
Tests for Company Management, German & Italian country support,
Registration ID Normalization, CSV Bulk Import, and the flexible
column-mapping data feeder.
"""

import pytest
import pandas as pd
from datetime import datetime, timedelta
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
    detect_family_and_succession,
    sync_succession_signal,
    suggest_person_mapping,
    save_person_mapping_profile,
    load_person_mapping_profile,
    list_person_mapping_profile_names,
    sync_management_composition_signals,
    suggest_flat_person_mapping,
    apply_person_data_import,
    save_flat_person_mapping_profile,
    load_flat_person_mapping_profile,
    list_flat_person_mapping_profile_names,
    FLAT_IMPORT_ROLE_GROUP,
    compute_revenue_growth_vs_sector,
)

FLEX_TEST_REGS = ["IT11122233344", "IT99988877766"]
FLEX_TEST_DATASET_NAMES = ["Test AIDA Financials", "Test Shareholder Data"]
FLEX_TEST_INDICATOR_KEYS = ["a_brand_new_column"]
PEOPLE_TEST_REGS = ["IT44455566677", "IT55566677788", "IT66677788899", "IT77788899900"]
SUCCESSION_TEST_REGS = ["IT10101010101", "IT20202020202", "IT30303030303", "IT40404040404", "IT50505050505"]
PEOPLE_MAPPING_TEST_REGS = ["IT88899900011"]
PEOPLE_MAPPING_TEST_DATASET_NAMES = ["Test Mapping Roster"]
COMPOSITION_TEST_REGS = ["IT60606060606", "IT70707070707", "IT80808080808", "IT90909090909"]
FLAT_PEOPLE_TEST_REGS = ["IT11111000011", "IT22222000022", "IT33333000033"]
FLAT_PEOPLE_TEST_DATASET_NAMES = ["Test Flat Roster"]
REVENUE_SECTOR_TEST_REGS = ["HRB-771100"]


@pytest.fixture(scope="function")
def db():
    init_db()
    session = get_db_session()
    # Clean up any test records
    test_regs = (["HRB-889900", "IT09988776655", "HRB-554433", "IT55443322110"]
                 + FLEX_TEST_REGS + PEOPLE_TEST_REGS + SUCCESSION_TEST_REGS
                 + PEOPLE_MAPPING_TEST_REGS + COMPOSITION_TEST_REGS + FLAT_PEOPLE_TEST_REGS
                 + REVENUE_SECTOR_TEST_REGS)

    def _cleanup():
        for reg in test_regs:
            c = session.query(Company).filter_by(registration_number=reg).first()
            if c:
                session.query(RawImportRecord).filter_by(company_id=c.id).delete()
                session.query(CompanyPerson).filter_by(company_id=c.id).delete()
                session.query(SignalRecord).filter_by(company_id=c.id).delete()
                session.delete(c)
        session.query(Company).filter_by(legal_name="Some Unknown Company Not In DB").delete()
        for name in FLEX_TEST_DATASET_NAMES:
            p = session.query(ColumnMappingProfile).filter_by(dataset_name=name).first()
            if p:
                session.delete(p)
        for name in PEOPLE_MAPPING_TEST_DATASET_NAMES:
            p = session.query(ColumnMappingProfile).filter_by(dataset_name=f"people::{name}").first()
            if p:
                session.delete(p)
        for name in FLAT_PEOPLE_TEST_DATASET_NAMES:
            p = session.query(ColumnMappingProfile).filter_by(dataset_name=f"flatpeople::{name}").first()
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
    # Germany: Arbeitsagentur, EPO OPS, EUIPO, Wappalyzer, Eurostat Export Exposure all applicable
    assert is_source_applicable("Arbeitsagentur", "Germany") is True
    assert is_source_applicable("EPO OPS", "Germany") is True
    assert is_source_applicable("EUIPO", "Germany") is True
    assert is_source_applicable("Wappalyzer", "Germany") is True
    assert is_source_applicable("Eurostat Export Exposure", "Germany") is True

    # Italy: Arbeitsagentur NOT applicable; EU/Universal APIs (incl. Eurostat Export Exposure,
    # which replaced the Germany-only Destatis adapter) ARE applicable
    assert is_source_applicable("Arbeitsagentur", "Italy") is False
    assert is_source_applicable("Handelsregister Free Snapshot", "Italy") is False
    assert is_source_applicable("Bundesanzeiger", "Italy") is False
    assert is_source_applicable("EPO OPS", "Italy") is True
    assert is_source_applicable("EUIPO", "Italy") is True
    assert is_source_applicable("Eurostat Export Exposure", "Italy") is True


def test_revenue_growth_vs_sector_needs_both_sides(db):
    """Neither revenue_trend nor sector_growth_benchmark exists yet for a brand
    new company — must stay not_yet_checked, never guessed from one side."""
    company, err = create_company(db, {
        "legal_name": "Test Revenue Sector GmbH", "registration_number": "HRB 771100",
        "country": "Germany", "nace_code": "C10.51",
    }, auto_sync=False)
    assert err is None

    result = compute_revenue_growth_vs_sector(db, company)
    assert result["status"] == "not_yet_checked"

    sig = db.query(SignalRecord).filter_by(company_id=company.id, signal_key="revenue_growth_vs_sector").first()
    assert sig.status == "not_yet_checked"
    assert sig.numeric_value is None


def test_revenue_growth_vs_sector_computes_differential(db):
    """Company revenue up 8% over 3 years, sector benchmark up 12% -> the
    company is underperforming its own market by 4 percentage points, even
    though its own revenue trend alone reads as positive growth."""
    company, err = create_company(db, {
        "legal_name": "Test Revenue Sector GmbH", "registration_number": "HRB 771100",
        "country": "Germany", "nace_code": "C10.51",
    }, auto_sync=False)
    assert err is None

    revenue_sig = db.query(SignalRecord).filter_by(company_id=company.id, signal_key="revenue_trend").first()
    revenue_sig.numeric_value = 8.0
    revenue_sig.status = "present"
    revenue_sig.confidence = 1.0
    revenue_sig.is_simulated = False

    sector_sig = db.query(SignalRecord).filter_by(company_id=company.id, signal_key="sector_growth_benchmark").first()
    sector_sig.numeric_value = 12.0
    sector_sig.status = "present"
    sector_sig.confidence = 0.85
    sector_sig.is_simulated = False
    db.commit()

    result = compute_revenue_growth_vs_sector(db, company)
    assert result["status"] == "present"
    assert result["value"] == -4.0

    sig = db.query(SignalRecord).filter_by(company_id=company.id, signal_key="revenue_growth_vs_sector").first()
    assert sig.numeric_value == -4.0
    assert sig.status == "present"
    assert sig.is_simulated is False
    assert sig.confidence == 0.85  # min() of the two inputs' confidence
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


def test_suggest_mapping_recognizes_company_legal_name_header_and_indicator_key_columns(db):
    """"Company Legal Name" (the header both LinkedIn Gem CSV formats use) must
    auto-suggest company:legal_name, and a column literally named after an
    existing indicator key (e.g. "external_collaboration") must auto-map onto
    it with zero manual mapping -- this is what lets a flat, indicator-key-named
    CSV (like the LinkedIn feed-signals export) round-trip with no new code."""
    groups = detect_column_groups(["Company Legal Name", "external_collaboration"])
    mapping = suggest_mapping(db, groups)
    assert mapping["Company Legal Name"] == "company:legal_name"
    assert mapping["external_collaboration"] == "indicator:external_collaboration"


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


# --- People & Ownership column mapping (generalizes Flexible Data Import's
# map-to-a-field + saved-preset behavior to the roster importer) ---

def test_suggest_person_mapping_uses_builtin_alias_by_default():
    groups = {"DM": ["DM\nNome completo", "DM\nCarica", "DM\nColonna Sconosciuta"]}
    suggestions = suggest_person_mapping(groups)
    assert suggestions["DM::Nome completo"] == "full_name"
    assert suggestions["DM::Carica"] == "role"
    assert suggestions["DM::Colonna Sconosciuta"] == ""  # unrecognized -> stays unmapped


def test_suggest_person_mapping_saved_profile_overrides_builtin_alias():
    groups = {"DM": ["DM\nCarica"]}
    # A saved profile always wins over the built-in guess, even a deliberately
    # odd one -- proves it's reviewed/re-appliable, not silently re-derived.
    profile = {"DM::Carica": "gender"}
    suggestions = suggest_person_mapping(groups, existing_profile=profile)
    assert suggestions["DM::Carica"] == "gender"


def test_person_mapping_profile_save_and_reload_round_trip(db):
    mapping = {"DM::Nome completo": "full_name", "DM::Carica": "role"}
    save_person_mapping_profile(db, "Test Mapping Roster", mapping)
    reloaded = load_person_mapping_profile(db, "Test Mapping Roster")
    assert reloaded == mapping
    assert "Test Mapping Roster" in list_person_mapping_profile_names(db)

    # Namespaced under the hood so it can never collide with a flexible-import
    # profile that happens to reuse the same human-chosen dataset name.
    profile = db.query(ColumnMappingProfile).filter_by(dataset_name="people::Test Mapping Roster").first()
    assert profile is not None
    assert db.query(ColumnMappingProfile).filter_by(dataset_name="Test Mapping Roster").first() is None


def test_import_company_people_field_overrides_respected(db):
    company, err = create_company(db, {
        "legal_name": "Test Mapping Override S.p.A.", "registration_number": "IT88899900011",
        "country": "Italy",
    }, auto_sync=False)
    assert err is None

    df = pd.DataFrame([{
        "Ragione sociale": "Test Mapping Override S.p.A.",
        "DM\nNome completo": "Mario Rossi",
        "DM\nCarica": "PRESIDENTE",
        "DM\nEtà": "55",
    }])

    # Deliberately un-map 'Carica' (would normally become role) and redirect
    # 'Età' onto gender -- proves an explicit override wins over the alias,
    # including an explicit "" (Ignore).
    overrides = {"DM::Carica": "", "DM::Età": "gender"}
    result = import_company_people(db, df, "Override Dataset", field_overrides=overrides, dry_run=False)
    assert result["people_created"] == 1

    person = db.query(CompanyPerson).filter_by(company_id=company.id).first()
    assert person.full_name == "Mario Rossi"  # not overridden -> alias still applies
    assert person.role is None                # explicitly ignored, not auto-mapped
    assert person.age is None                 # 'Età' no longer maps to age...
    assert person.gender == "55"              # ...it was redirected to gender instead
    assert person.raw_fields["Carica"] == "PRESIDENTE"  # original value survives regardless


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


# --- Family ownership & generational succession detection ---

def _add_person(db, company_id, full_name, age=None, cognome=None, appointment_date=None,
                 role_group="DM", position=0, dataset="Test Succession Dataset",
                 gender=None, nationality=None, resignation_date=None, current_or_former=None, role=None):
    person = CompanyPerson(
        company_id=company_id, dataset_name=dataset, role_group=role_group, position_in_row=position,
        full_name=full_name, age=age, appointment_date=appointment_date,
        gender=gender, nationality=nationality, resignation_date=resignation_date,
        current_or_former=current_or_former, role=role,
        raw_fields={"Cognome": cognome} if cognome else {},
    )
    db.add(person)
    db.commit()
    return person


def test_detect_family_and_succession_flags_shared_surname_as_family(db):
    company, err = create_company(db, {
        "legal_name": "Rossi Officina S.r.l.", "registration_number": "IT10101010101", "country": "Italy",
    }, auto_sync=False)
    assert err is None
    company.incorporation_date = datetime(1990, 1, 1)
    db.commit()

    _add_person(db, company.id, "Mario Rossi", age=65, cognome="Rossi", position=0)
    _add_person(db, company.id, "Anna Rossi", age=60, cognome="Rossi", position=1)

    result = detect_family_and_succession(db, company)
    assert result["is_family_company"] is True
    assert result["family_surnames"] == ["Rossi"]
    # Founder generation (>=60) still present under that surname -> no completed handover.
    assert result["new_generation"]["detected"] is False


def test_detect_family_and_succession_no_shared_surname_not_family(db):
    company, err = create_company(db, {
        "legal_name": "Multi Team S.r.l.", "registration_number": "IT20202020202", "country": "Italy",
    }, auto_sync=False)
    assert err is None

    _add_person(db, company.id, "Mario Rossi", age=50, cognome="Rossi", position=0)
    _add_person(db, company.id, "Anna Bianchi", age=45, cognome="Bianchi", position=1)

    result = detect_family_and_succession(db, company)
    assert result["is_family_company"] is False
    assert result["new_generation"]["detected"] is False


def test_detect_family_and_succession_does_not_double_count_same_person_across_role_groups(db):
    """The same board member routinely gets exploded into two rows from one
    import -- e.g. a "DM" director row and an "ADV" advisor row -- and only
    one of them carries the source file's "Sig./Signora" honorific. Without
    identity dedup, that one real person looks like two people sharing a
    surname and gets wrongly flagged as a family pair."""
    company, err = create_company(db, {
        "legal_name": "Solo Operator S.p.A.", "registration_number": "IT30303030303", "country": "Italy",
    }, auto_sync=False)
    assert err is None

    _add_person(db, company.id, "Sig. Andrea Parolari", age=71, cognome="Parolari",
                role_group="DM", position=0)
    _add_person(db, company.id, "Andrea Parolari", age=71, cognome="Parolari",
                role_group="ADV", position=0)

    result = detect_family_and_succession(db, company)
    assert result["is_family_company"] is False
    assert result["family_surnames"] == []


def test_sync_management_composition_signals_counts_real_board_not_dateless_duplicate(db):
    """A person with a future resignation_date (a scheduled mandate
    end-of-term, common in Italian board filings -- "in carica fino al ...")
    must still count as current. Regression for a bug where the richer,
    dated "DM" row was wrongly excluded as "former" while a dateless "ADV"
    duplicate of the same roster was counted as current instead."""
    company, err = create_company(db, {
        "legal_name": "Term Limited S.p.A.", "registration_number": "IT40404040404", "country": "Italy",
    }, auto_sync=False)
    assert err is None
    now = datetime.utcnow()

    _add_person(db, company.id, "Sig. Carlo Neri", age=60, cognome="Neri", role_group="DM", position=0,
                appointment_date=now - timedelta(days=500), resignation_date=now + timedelta(days=400),
                current_or_former="Current")
    _add_person(db, company.id, "Carlo Neri", age=60, cognome="Neri", role_group="ADV", position=0)

    result = sync_management_composition_signals(db, company)
    assert result["management_age"]["value"] == pytest.approx(60.0)
    # A future resignation_date is a scheduled term end, not a real turnover
    # event yet -- only the past appointment should be counted.
    assert result["management_turnover"]["value"] == pytest.approx(1.0)


def test_detect_family_and_succession_flags_new_generation_with_quantified_handover(db):
    company, err = create_company(db, {
        "legal_name": "Bianchi Group S.p.A.", "registration_number": "IT30303030303", "country": "Italy",
    }, auto_sync=False)
    assert err is None
    now = datetime.utcnow()
    young_age = 30
    birth_year = now.year - young_age
    company.incorporation_date = datetime(birth_year - 10, 1, 1)  # incorporated a decade before he was even born
    db.commit()

    appointment_date = datetime(now.year - 5, 6, 1)
    _add_person(db, company.id, "Luca Bianchi", age=young_age, cognome="Bianchi", appointment_date=appointment_date)

    result = detect_family_and_succession(db, company)
    ng = result["new_generation"]
    assert ng["detected"] is True
    assert ng["surname"] == "Bianchi"
    assert ng["years_since_handover"] == pytest.approx(5.0, abs=0.3)

    signal_result = sync_succession_signal(db, company)
    assert signal_result["signal_written"] is True
    sig = db.query(SignalRecord).filter_by(company_id=company.id, signal_key="new_generation_management").first()
    assert sig is not None
    assert sig.status == "present"
    assert sig.is_simulated is False
    assert sig.numeric_value == pytest.approx(5.0, abs=0.3)


def test_detect_family_and_succession_no_flag_when_founder_could_have_founded_it(db):
    company, err = create_company(db, {
        "legal_name": "Verdi Innovations S.r.l.", "registration_number": "IT40404040404", "country": "Italy",
    }, auto_sync=False)
    assert err is None
    now = datetime.utcnow()
    young_age = 35
    birth_year = now.year - young_age
    company.incorporation_date = datetime(birth_year + 25, 1, 1)  # she'd have been 25 - old enough to found it
    db.commit()

    _add_person(db, company.id, "Sara Verdi", age=young_age, cognome="Verdi")

    result = detect_family_and_succession(db, company)
    assert result["new_generation"]["detected"] is False


def test_detect_family_and_succession_no_flag_when_old_generation_still_present(db):
    company, err = create_company(db, {
        "legal_name": "Ferrari Costruzioni S.r.l.", "registration_number": "IT50505050505", "country": "Italy",
    }, auto_sync=False)
    assert err is None
    now = datetime.utcnow()
    young_age = 28
    birth_year = now.year - young_age
    company.incorporation_date = datetime(birth_year - 10, 1, 1)
    db.commit()

    _add_person(db, company.id, "Giovanni Ferrari", age=70, cognome="Ferrari", position=0)
    _add_person(db, company.id, "Marco Ferrari", age=young_age, cognome="Ferrari", position=1)

    result = detect_family_and_succession(db, company)
    assert result["is_family_company"] is True
    assert result["new_generation"]["detected"] is False


def test_sync_succession_signal_does_not_write_without_appointment_date(db):
    company, err = create_company(db, {
        "legal_name": "Bianchi Group S.p.A.", "registration_number": "IT30303030303", "country": "Italy",
    }, auto_sync=False)
    assert err is None
    now = datetime.utcnow()
    young_age = 30
    birth_year = now.year - young_age
    company.incorporation_date = datetime(birth_year - 10, 1, 1)
    db.commit()

    _add_person(db, company.id, "Luca Bianchi", age=young_age, cognome="Bianchi")  # no appointment_date

    result = sync_succession_signal(db, company)
    assert result["new_generation"]["detected"] is True
    assert result["new_generation"]["years_since_handover"] is None
    assert result["signal_written"] is False
    # create_company() pre-seeds every indicator as not_yet_checked — confirm
    # sync_succession_signal left that alone rather than fabricating a value.
    sig = db.query(SignalRecord).filter_by(company_id=company.id, signal_key="new_generation_management").first()
    assert sig is not None
    assert sig.status == "not_yet_checked"
    assert sig.numeric_value is None


def test_import_company_people_auto_triggers_succession_signal(db):
    """The Board & Management import path (import_company_people) should
    call sync_succession_signal automatically per matched company — no
    separate manual step needed."""
    company, err = create_company(db, {
        "legal_name": "Bianchi Group S.p.A.", "registration_number": "IT30303030303", "country": "Italy",
    }, auto_sync=False)
    assert err is None
    now = datetime.utcnow()
    young_age = 30
    birth_year = now.year - young_age
    company.incorporation_date = datetime(birth_year - 10, 1, 1)
    db.commit()

    df = pd.DataFrame([{
        "Ragione sociale": "Bianchi Group S.p.A.",
        "DM\nNome completo": "Luca Bianchi",
        "DM\nCognome": "Bianchi",
        "DM\nEtà": str(young_age),
        "DM\nData nomina": f"{now.year - 5}-06-01",
    }])
    result = import_company_people(db, df, "Auto Succession Dataset", dry_run=False)
    assert result["people_created"] == 1

    sig = db.query(SignalRecord).filter_by(company_id=company.id, signal_key="new_generation_management").first()
    assert sig is not None
    assert sig.status == "present"
    assert sig.numeric_value == pytest.approx(5.0, abs=0.3)


# --- Management/board composition indicators aggregated from the roster ---
#
# indicators.py carries several Leadership & Succession rows (age, gender/
# national diversity, turnover, tenure, non-family board headcount) whose
# raw material is exactly the CompanyPerson roster import_company_people
# already writes. These tests confirm sync_management_composition_signals
# actually turns that roster into the SignalRecords, honestly (nothing
# faked when the underlying field was never populated).

def test_sync_management_composition_signals_computes_age_gender_and_nationality(db):
    company, err = create_company(db, {
        "legal_name": "Composition Test One S.r.l.", "registration_number": "IT60606060606", "country": "Italy",
    }, auto_sync=False)
    assert err is None

    _add_person(db, company.id, "Anna Verdi", age=40, gender="F", nationality="Italian",
                dataset="Composition Dataset", position=0)
    _add_person(db, company.id, "Marco Neri", age=60, gender="M", nationality="German",
                dataset="Composition Dataset", position=1)

    result = sync_management_composition_signals(db, company)

    assert result["management_age"]["written"] is True
    assert result["management_age"]["value"] == pytest.approx(50.0)

    assert result["mgmt_gender_diversity"]["written"] is True
    assert result["mgmt_gender_diversity"]["value"] == pytest.approx(50.0)  # 1 of 2 is female

    assert result["mgmt_national_diversity"]["written"] is True
    assert result["mgmt_national_diversity"]["value"] == pytest.approx(50.0)  # neither nationality dominates

    for key in ("management_age", "mgmt_gender_diversity", "mgmt_national_diversity"):
        sig = db.query(SignalRecord).filter_by(company_id=company.id, signal_key=key).first()
        assert sig is not None
        assert sig.status == "present"
        assert sig.is_simulated is False


def test_sync_management_composition_signals_computes_average_tenure(db):
    company, err = create_company(db, {
        "legal_name": "Composition Test Two S.r.l.", "registration_number": "IT70707070707", "country": "Italy",
    }, auto_sync=False)
    assert err is None
    now = datetime.utcnow()
    appt_a = now - timedelta(days=4 * 365)
    appt_b = now - timedelta(days=10 * 365)

    _add_person(db, company.id, "Anna Verdi", age=40, appointment_date=appt_a,
                dataset="Tenure Dataset", position=0)
    _add_person(db, company.id, "Marco Neri", age=60, appointment_date=appt_b,
                dataset="Tenure Dataset", position=1)

    expected = (((now - appt_a).days / 365.25) + ((now - appt_b).days / 365.25)) / 2
    result = sync_management_composition_signals(db, company)
    assert result["senior_mgmt_tenure"]["written"] is True
    assert result["senior_mgmt_tenure"]["value"] == pytest.approx(expected, abs=0.05)


def test_sync_management_composition_signals_counts_recent_turnover_events(db):
    company, err = create_company(db, {
        "legal_name": "Composition Test Three S.r.l.", "registration_number": "IT80808080808", "country": "Italy",
    }, auto_sync=False)
    assert err is None
    now = datetime.utcnow()

    # One recent arrival, one recent departure, one old appointment well
    # outside the 3-year lookback -- only the first two should count.
    _add_person(db, company.id, "Recent Arrival", age=45, appointment_date=now - timedelta(days=200),
                dataset="Turnover Dataset", position=0)
    _add_person(db, company.id, "Recent Departure", age=50, appointment_date=now - timedelta(days=1500),
                resignation_date=now - timedelta(days=100), current_or_former="Precedente",
                dataset="Turnover Dataset", position=1)
    _add_person(db, company.id, "Long Tenured", age=58, appointment_date=now - timedelta(days=3000),
                dataset="Turnover Dataset", position=2)

    result = sync_management_composition_signals(db, company)
    assert result["management_turnover"]["written"] is True
    assert result["management_turnover"]["value"] == pytest.approx(2.0)


def test_sync_management_composition_signals_counts_non_family_board_members(db):
    company, err = create_company(db, {
        "legal_name": "Bruni Holding S.r.l.", "registration_number": "IT90909090909", "country": "Italy",
    }, auto_sync=False)
    assert err is None

    # Two Brunis share a surname -> family; the third person doesn't -> independent.
    _add_person(db, company.id, "Paolo Bruni", age=62, cognome="Bruni", dataset="Board Dataset", position=0)
    _add_person(db, company.id, "Elisa Bruni", age=35, cognome="Bruni", dataset="Board Dataset", position=1)
    _add_person(db, company.id, "Giulia Conti", age=48, cognome="Conti", dataset="Board Dataset", position=2)

    result = sync_management_composition_signals(db, company)
    assert result["independent_board_members"]["written"] is True
    assert result["independent_board_members"]["value"] == pytest.approx(1.0)


def test_sync_management_composition_signals_writes_nothing_without_underlying_data(db):
    company, err = create_company(db, {
        "legal_name": "Composition Test Four S.r.l.", "registration_number": "IT60606060606", "country": "Italy",
    }, auto_sync=False)
    assert err is None
    _add_person(db, company.id, "Someone", age=50, dataset="Sparse Dataset", position=0)

    result = sync_management_composition_signals(db, company)
    # Age is always computable off just 'age'; everything needing a field
    # that was never populated stays untouched rather than faked as zero.
    assert result["management_age"]["written"] is True
    assert result["mgmt_gender_diversity"]["written"] is False
    assert result["mgmt_national_diversity"]["written"] is False
    assert result["management_turnover"]["written"] is False
    assert result["senior_mgmt_tenure"]["written"] is False
    # No family surname detected at all -> the lone current person still counts as independent.
    assert result["independent_board_members"]["written"] is True
    assert result["independent_board_members"]["value"] == pytest.approx(1.0)


def test_import_company_people_auto_triggers_management_composition_signals(db):
    """import_company_people should call sync_management_composition_signals
    automatically per matched company, same as it already does for
    sync_succession_signal -- no separate manual recompute step needed."""
    company, err = create_company(db, {
        "legal_name": "Auto Composition S.r.l.", "registration_number": "IT60606060606", "country": "Italy",
    }, auto_sync=False)
    assert err is None

    df = pd.DataFrame([{
        "Ragione sociale": "Auto Composition S.r.l.",
        "DM\nNome completo": "Elena Bianchi\nMarco Bianchi",
        "DM\nEtà": "45\n50",
        "DM\nGenere": "F\nM",
    }])
    result = import_company_people(db, df, "Auto Composition Dataset", dry_run=False)
    assert result["people_created"] == 2

    sig = db.query(SignalRecord).filter_by(company_id=company.id, signal_key="mgmt_gender_diversity").first()
    assert sig is not None
    assert sig.status == "present"
    assert sig.numeric_value == pytest.approx(50.0)
    assert sig.is_simulated is False


# =============================================================================
# Flexible people import (one row per person) -- mirrors the flexible
# column-mapping feeder's own tests above, applied to CompanyPerson instead
# of Company/SignalRecord.
# =============================================================================

def _flat_people_df(legal_name):
    return pd.DataFrame([
        {
            "Company Legal Name": legal_name, "Full Name": "Jane Doe", "Role": "Chief Financial Officer",
            "Estimated Age": 47, "Gender": "F", "Nationality": "Germany",
            "Education Level": "Master's", "Appointment Date": "2019-03-01", "Current or Former": "current",
            "Digital Lead Role Match": "No",
        },
        {
            # No age/gender on purpose -- the realistic LinkedIn case: role,
            # tenure, and nationality are known, birthdate never is.
            "Company Legal Name": legal_name, "Full Name": "Tom Weber", "Role": "Head of Digital",
            "Estimated Age": None, "Gender": None, "Nationality": "Germany",
            "Education Level": "Bachelor's", "Appointment Date": "2021-06-01", "Current or Former": "current",
            "Digital Lead Role Match": "Yes",
        },
    ])


def test_suggest_flat_person_mapping_recognizes_standard_headers():
    columns = ["Company Legal Name", "Full Name", "Role", "Estimated Age", "Gender",
               "Nationality", "Appointment Date", "Current or Former",
               "Education Level", "Digital Lead Role Match", "Notes"]
    mapping = suggest_flat_person_mapping(columns)
    assert mapping["Company Legal Name"] == "match:legal_name"
    assert mapping["Full Name"] == "person:full_name"
    assert mapping["Role"] == "person:role"
    assert mapping["Estimated Age"] == "person:age"
    assert mapping["Gender"] == "person:gender"
    assert mapping["Nationality"] == "person:nationality"
    assert mapping["Appointment Date"] == "person:appointment_date"
    assert mapping["Current or Former"] == "person:current_or_former"
    # No dedicated CompanyPerson column exists for these yet -- must stay
    # unmapped (not silently dropped -- see raw_fields assertions below).
    assert mapping["Education Level"] == ""
    assert mapping["Digital Lead Role Match"] == ""
    assert mapping["Notes"] == ""


def test_suggest_flat_person_mapping_reuses_saved_profile_over_alias_guess():
    saved = {"Full Name": "person:role"}  # deliberately unusual, must win over the alias
    mapping = suggest_flat_person_mapping(["Full Name", "Role"], existing_profile=saved)
    assert mapping["Full Name"] == "person:role"
    assert mapping["Role"] == "person:role"


def test_apply_person_data_import_requires_exactly_one_match_column(db):
    df = _flat_people_df("Doesn't Matter GmbH")
    result = apply_person_data_import(db, df, {}, "Test Flat Roster", dry_run=True)
    assert result["matched"] == 0
    assert any("Match: Company Legal Name" in e for e in result["errors"])


def test_apply_person_data_import_dry_run_makes_no_writes(db):
    company, err = create_company(db, {
        "legal_name": "Flat Dry Run GmbH", "registration_number": "IT11111000011", "country": "Italy",
    }, auto_sync=False)
    assert err is None
    df = _flat_people_df("Flat Dry Run GmbH")
    mapping = suggest_flat_person_mapping(list(df.columns))

    preview = apply_person_data_import(db, df, mapping, "Test Flat Roster", dry_run=True)
    assert preview["matched"] == 2
    assert preview["people_created"] == 0
    assert db.query(CompanyPerson).filter_by(company_id=company.id).count() == 0


def test_apply_person_data_import_creates_people_and_preserves_raw_fields(db):
    company, err = create_company(db, {
        "legal_name": "Flat Import GmbH", "registration_number": "IT22222000022", "country": "Italy",
    }, auto_sync=False)
    assert err is None
    df = _flat_people_df("Flat Import GmbH")
    mapping = suggest_flat_person_mapping(list(df.columns))

    result = apply_person_data_import(db, df, mapping, "Test Flat Roster", dry_run=False)
    assert result["matched"] == 2
    assert result["people_created"] == 2
    assert result["unmatched"] == []
    assert result["conflicts"] == []

    people = {p.full_name: p for p in db.query(CompanyPerson).filter_by(company_id=company.id).all()}
    assert people["Jane Doe"].role == "Chief Financial Officer"
    assert people["Jane Doe"].age == 47
    assert people["Jane Doe"].gender == "F"
    assert people["Jane Doe"].role_group == FLAT_IMPORT_ROLE_GROUP
    # Honesty rule: Tom's age/gender were never provided -> stay None, not faked.
    assert people["Tom Weber"].age is None
    assert people["Tom Weber"].gender is None
    assert people["Tom Weber"].nationality == "Germany"
    # Every column -- mapped or not -- survives in raw_fields (Education
    # Level and Digital Lead Role Match have no dedicated column yet).
    assert people["Tom Weber"].raw_fields["Education Level"] == "Bachelor's"
    assert people["Tom Weber"].raw_fields["Digital Lead Role Match"] == "Yes"


def test_apply_person_data_import_unmatched_name_not_created(db):
    df = _flat_people_df("Some Unknown Company Not In DB")
    mapping = suggest_flat_person_mapping(list(df.columns))
    result = apply_person_data_import(db, df, mapping, "Test Flat Roster", dry_run=False)
    assert result["matched"] == 0
    assert result["unmatched"] == ["Some Unknown Company Not In DB", "Some Unknown Company Not In DB"]
    assert db.query(Company).filter_by(legal_name="Some Unknown Company Not In DB").first() is None


def test_apply_person_data_import_reupload_conflicts_once_per_company_not_per_row(db):
    company, err = create_company(db, {
        "legal_name": "Flat Conflict GmbH", "registration_number": "IT33333000033", "country": "Italy",
    }, auto_sync=False)
    assert err is None
    df = _flat_people_df("Flat Conflict GmbH")
    mapping = suggest_flat_person_mapping(list(df.columns))
    apply_person_data_import(db, df, mapping, "Test Flat Roster", dry_run=False)
    assert db.query(CompanyPerson).filter_by(company_id=company.id).count() == 2

    result = apply_person_data_import(db, df, mapping, "Test Flat Roster", overwrite_conflicts=False, dry_run=False)
    assert result["matched"] == 0
    assert len(result["conflicts"]) == 1  # not 2, even though 2 rows belong to this company
    assert db.query(CompanyPerson).filter_by(company_id=company.id).count() == 2  # unchanged, no duplicates

    result = apply_person_data_import(db, df, mapping, "Test Flat Roster", overwrite_conflicts=True, dry_run=False)
    assert result["matched"] == 2
    assert db.query(CompanyPerson).filter_by(company_id=company.id).count() == 2  # updated in place, still 2


def test_apply_person_data_import_flat_import_triggers_composition_signals(db):
    """Same auto-trigger contract as import_company_people, and the whole
    point of the age-filter relaxation below: Tom Weber (no age) must still
    count towards nationality diversity, tenure, and independent-board-member
    signals, while management_age correctly averages only Jane (the one
    person with a known age) rather than crashing or being skipped entirely."""
    company, err = create_company(db, {
        "legal_name": "Flat Composition GmbH", "registration_number": "IT11111000011", "country": "Italy",
    }, auto_sync=False)
    assert err is None
    df = _flat_people_df("Flat Composition GmbH")
    mapping = suggest_flat_person_mapping(list(df.columns))
    apply_person_data_import(db, df, mapping, "Test Flat Roster", dry_run=False)

    def _sig(key):
        return db.query(SignalRecord).filter_by(company_id=company.id, signal_key=key).first()

    assert _sig("management_age").numeric_value == pytest.approx(47.0)  # Jane only
    assert _sig("mgmt_national_diversity").numeric_value == pytest.approx(0.0)  # both Germany
    # Both Jane and Tom count as independent (no shared surname) even though
    # Tom's age is unknown -- this is the fix: previously an age-less person
    # was invisible to every composition signal, not just age-based ones.
    assert _sig("independent_board_members").numeric_value == pytest.approx(2.0)
    tenure_sig = _sig("senior_mgmt_tenure")
    assert tenure_sig is not None and tenure_sig.numeric_value > 0


def test_flat_import_age_relaxation_does_not_affect_aida_stacked_import(db):
    """Regression guard: the age-filter relaxation is scoped to
    FLAT_IMPORT_ROLE_GROUP only -- an age-less person from the AIDA-style
    stacked-cell importer (role_group 'DM') must still be excluded entirely,
    since that's the only signal distinguishing a real director from a
    shareholder/subsidiary entity row in that data shape."""
    company, err = create_company(db, {
        "legal_name": "Aida Unaffected S.r.l.", "registration_number": "IT22222000022", "country": "Italy",
    }, auto_sync=False)
    assert err is None
    _add_person(db, company.id, "No Age Person", age=None, gender="M", nationality="Italian",
                role_group="DM", dataset="Aida Age Regression Dataset", position=0)
    _add_person(db, company.id, "Known Age Person", age=50, gender="F", nationality="German",
                role_group="DM", dataset="Aida Age Regression Dataset", position=1)

    result = sync_management_composition_signals(db, company)
    # Only the aged person is visible -- gender diversity should be 100% F
    # (1 of 1 counted), not 50% (which would mean the age-less person leaked in).
    assert result["mgmt_gender_diversity"]["value"] == pytest.approx(100.0)
    assert result["independent_board_members"]["value"] == pytest.approx(1.0)


def test_flat_person_mapping_profile_round_trip(db):
    mapping = {"Company Legal Name": "match:legal_name", "Full Name": "person:full_name"}
    save_flat_person_mapping_profile(db, "Test Flat Roster", mapping)
    assert load_flat_person_mapping_profile(db, "Test Flat Roster") == mapping
    assert "Test Flat Roster" in list_flat_person_mapping_profile_names(db)
