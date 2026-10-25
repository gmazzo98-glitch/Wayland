"""
gg_indicators: the Indicators.xlsx schema loader and the two-axis engine (integration brief 3.1-3.8).
Pure data / pure functions: no database. Runs against the workbook copy in gg_indicators/source.
"""

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import openpyxl
import pytest

from gg_indicators import (
    CompanyContext, ConfigError, IndicatorRegistry, Observation, ObservationError, PLACEHOLDER_LABEL,
    SchemaError, WeightSetError, compare_weight_sets, default_weight_set, format_report, load_weight_sets,
    parse_tag, score_company, validate_weight_set,
)
from gg_indicators.bridge import observations_from_signal_map
from gg_indicators.quality import classify_quality_process
from gg_indicators.questionnaire import build_first_contact_section
from gg_indicators.schema import DEFAULT_WORKBOOK, SETTINGS_DIR, derive_variable_key, extract_tag, resolve_key
from gg_indicators.weights import weight_set_from_dict

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def reg():
    return IndicatorRegistry.load()


@pytest.fixture()
def r(reg):
    """A private copy a test may mutate."""
    return copy.deepcopy(reg)


@pytest.fixture(scope="module")
def placeholder(reg):
    return default_weight_set(reg)


def row_key(reg, row):
    return next(s.key for s in reg.specs if s.sheet_row == row)


def axis_score(result, axis):
    return result["scores"][axis]["score"]


# ----------------------------------------------------------------------------- 1. tag parsing

TAG_CASES = [
    # raw tag text,                       axis,        polarity,   readiness_caveat
    ("READINESS",                         "READINESS", "Standard", False),
    ("NEED",                              "NEED",      "Standard", False),
    ("BOTH",                              "BOTH",      "Standard", False),
    ("READINESS - NEGATIVE",              "READINESS", "Negative", False),   # row 49: spaced hyphen
    ("READINESS, NEGATIVE",               "READINESS", "Negative", False),   # rows 68, 70: comma
    ("READINESS, CRITICAL GATE",          "READINESS", "Standard", False),   # row 69
    ("READINESS BASELINE",                "READINESS", "Standard", False),   # row 67
    ("READINESS, caveat",                 "READINESS", "Standard", False),   # row 57
    ("READINESS-GAP / GG-FIT",            "READINESS", "Standard", False),   # rows 43, 44
    ("CONTEXT",                           "CONTEXT",   "Standard", False),   # row 30
    ("CONTEXT / MODERATOR",               "CONTEXT",   "Standard", False),   # rows 58, 71
    ("NEED, readiness caveat",            "NEED",      "Standard", True),    # rows 50, 51, 72: lowercase readiness is NOT READINESS
]


@pytest.mark.parametrize("raw,axis,polarity,caveat", TAG_CASES)
def test_parse_tag_forms(raw, axis, polarity, caveat):
    t = parse_tag(raw)
    assert (t.axis, t.polarity, t.readiness_caveat) == (axis, polarity, caveat)


def test_separator_style_never_changes_the_parse():
    assert parse_tag("READINESS - NEGATIVE").polarity == parse_tag("READINESS, NEGATIVE").polarity == "Negative"
    assert parse_tag("READINESS - NEGATIVE").axis == parse_tag("READINESS, NEGATIVE").axis == "READINESS"


def test_axis_match_is_case_sensitive_on_purpose():
    assert parse_tag("NEED, readiness caveat").axis == "NEED"
    assert parse_tag("need, readiness").axis == "NEED"        # no uppercase READINESS -> NEED
    assert parse_tag("CONTEXT / BOTH").axis == "CONTEXT"      # CONTEXT is tested first
    assert parse_tag("BOTH, READINESS").axis == "BOTH"        # BOTH before READINESS


def test_extract_tag_requires_a_leading_bracket():
    assert extract_tag("[NEED, readiness caveat] Rising debt...") == "NEED, readiness caveat"
    with pytest.raises(SchemaError):
        extract_tag("No bracket here")


def test_every_tag_in_the_workbook_is_covered_by_the_cases(reg):
    known = {c[0] for c in TAG_CASES}
    seen = {s.tag.raw for s in reg.specs}
    assert seen <= known, f"tag forms in the sheet with no parse test: {seen - known}"


def test_every_row_matches_the_sheets_own_formula_results():
    # the loader raises if the parse disagrees with the cached Scoring Schema formulas
    wb = openpyxl.load_workbook(DEFAULT_WORKBOOK, data_only=True)
    ss = wb["Scoring Schema"]
    reg_ = IndicatorRegistry.load()
    for s in reg_.specs:
        assert ss.cell(s.sheet_row, 4).value == s.tag.axis
        assert ss.cell(s.sheet_row, 5).value == s.tag.polarity


# ----------------------------------------------------------------------------- 2. schema facts

def test_workbook_shape_and_dreamy_rows(reg):
    assert len(reg.specs) == 74
    assert [s.sheet_row for s in reg.specs][:2] == [2, 3] and reg.specs[-1].sheet_row == 75
    new = {row_key(reg, 72): "NEED", row_key(reg, 73): "NEED", row_key(reg, 74): "BOTH", row_key(reg, 75): "READINESS"}
    for key, axis in new.items():
        assert reg.by_key[key].tag.axis == axis
    assert set(new) == {"system_integration", "maintenance_digitization", "quality_process_maturity", "digital_maturity_self_assessment"}
    # row 63 now measures age only
    assert reg.by_key["erp_systems_age"].sheet_row == 63
    assert "age only" in reg.by_key["erp_systems_age"].comment


def test_roles_caveats_both_and_first_contact_rows(reg):
    rows = lambda keys: sorted(reg.by_key[k].sheet_row for k in keys)
    assert rows(reg.gate_keys) == [67, 69, 70]
    assert rows(reg.moderator_keys) == [30, 58, 71]
    assert rows(reg.caveat_keys()) == [50, 51, 72]
    assert sorted(s.sheet_row for s in reg.specs if s.tag.axis == "BOTH") == [8, 74]
    assert sorted(s.sheet_row for s in reg.specs if s.acquisition_mode == "First contact only") == [11, 12, 32, 65, 70, 72, 73, 75]


def test_sheet_weights_are_empty_and_never_defaulted(reg):
    assert all(v is None for s in reg.specs for v in s.sheet_weights.values())


def test_keys_are_stable_when_a_label_is_renamed(reg):
    table = reg.key_table
    assert resolve_key("ERP / core systems age", 63, table)["key"] == "erp_systems_age"
    renamed = resolve_key("Core systems age (renamed)", 63, table)
    assert renamed["key"] == "erp_systems_age" and renamed["key_source"] == "override_row_label_renamed"
    brand_new = resolve_key("Order backlog", 99, table)
    assert brand_new["key"] == "order_backlog" and brand_new["key_source"] == "derived_unpersisted"
    assert derive_variable_key("Employer review sentiment (Kununu/Glassdoor)") == "employer_review_sentiment"


def test_70_keys_reuse_the_live_catalog_and_4_are_new(reg):
    from indicators import INDICATOR_SEED
    seed = {d["key"] for d in INDICATOR_SEED}
    in_seed = [k for k in reg.keys if k in seed]
    assert len(in_seed) == 70
    assert set(reg.keys) - seed == {"system_integration", "maintenance_digitization",
                                    "quality_process_maturity", "digital_maturity_self_assessment"}


def test_loading_never_modifies_the_workbook():
    before = hashlib.sha256(DEFAULT_WORKBOOK.read_bytes()).hexdigest()
    IndicatorRegistry.load()
    assert hashlib.sha256(DEFAULT_WORKBOOK.read_bytes()).hexdigest() == before


def test_negative_polarity_rows_have_their_direction_written_out(reg):
    neg = [s.key for s in reg.specs if s.tag.polarity == "Negative"]
    assert sorted(reg.by_key[k].sheet_row for k in neg) == [49, 68, 70]
    for k in neg:
        if reg.role(k) == "Scored":
            assert reg.mapping(k)["direction"]["READINESS"] == "lowers"
            assert reg.mapping(k)["axes"]["READINESS"] == {"trigger": 0.0, "not_trigger": 1.0}


def test_a_negative_row_mapped_as_raising_is_rejected(r):
    r.status_mappings["cash_position"]["axes"]["READINESS"] = {"trigger": 1.0, "not_trigger": 0.0}
    with pytest.raises(ConfigError):
        r.validate()


def test_acquisition_modes_map_onto_existing_tiers(reg):
    assert {m: v["automation_tier"] for m, v in reg.acquisition_modes["modes"].items()} == {
        "Registry or filing": "T1", "Web or job board scrape": "T2", "Manual desk research": "T2", "First contact only": "T3"}
    assert reg.tier(row_key(reg, 11)) == "T3"


def test_source_it_is_separate_and_complete(reg):
    assert set(reg.source_it) == set(reg.keys)
    assert {v["status"] for v in reg.source_it.values()} <= {"MAPPED", "PARTIAL", "TO_MAP"}
    c = json.loads((ROOT / "docs" / "indicator_contract.json").read_text(encoding="utf-8"))
    by = {v["variable_key"]: v for v in c["variables"]}
    s1 = openpyxl.load_workbook(DEFAULT_WORKBOOK)["Sheet1"]
    assert by["ebit_trend"]["source_text_sheet"] == s1.cell(33, 6).value  # untouched German-oriented text


def test_contract_file_is_current_and_carries_the_new_rows():
    spec = importlib.util.spec_from_file_location("build_indicator_contract", ROOT / "scripts" / "build_indicator_contract.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for path, text in mod.render_outputs(IndicatorRegistry.load()).items():
        assert path.read_text(encoding="utf-8") == text, f"{path.name} is stale - run: python scripts/build_indicator_contract.py"
    c = json.loads((ROOT / "docs" / "indicator_contract.json").read_text(encoding="utf-8"))
    assert c["variable_count"] == 74
    assert {"system_integration", "maintenance_digitization", "quality_process_maturity",
            "digital_maturity_self_assessment"} <= {v["variable_key"] for v in c["variables"]}


# ----------------------------------------------------------------------------- 3. observations & states

def test_observation_validation():
    with pytest.raises(ObservationError):
        Observation("x", "observed")                              # neither label nor strength
    with pytest.raises(ObservationError):
        Observation("x", "observed", label="trigger", strength=0.5)
    with pytest.raises(ObservationError):
        Observation("x", "observed", strength=1.5)
    with pytest.raises(ObservationError):
        Observation("x", "absent")
    Observation("x", "unknown")                                   # fine without a value
    Observation("x", "not_applicable")


def test_unknown_variable_or_label_is_loud(reg):
    with pytest.raises(ObservationError):
        score_company(reg, [Observation.trigger("not_a_variable")])
    with pytest.raises(ObservationError):
        score_company(reg, [Observation.with_label("ebit_trend", "sort_of")])
    with pytest.raises(ObservationError):
        score_company(reg, [Observation.trigger("ebit_trend"), Observation.not_trigger("ebit_trend")])


def test_a_both_variable_feeds_both_axes_from_one_observation(reg, placeholder):
    key = "quality_process_maturity"                              # row 74
    res = score_company(reg, [Observation.with_label(key, "long_held_multi_standard")], weight_set=placeholder)
    need = next(d for d in res["scores"]["NEED"]["detail"] if d["variable_key"] == key)
    ready = next(d for d in res["scores"]["READINESS"]["detail"] if d["variable_key"] == key)
    assert need["axis_value"] == 0.0 and ready["axis_value"] == 1.0
    assert need["counts_toward_score"] and ready["counts_toward_score"]
    assert axis_score(res, "NEED") == 0.0 and axis_score(res, "READINESS") == 100.0

    gap = score_company(reg, [Observation.with_label(key, "gap_vs_sector_norm")], weight_set=placeholder)
    assert axis_score(gap, "NEED") == 100.0                       # the gap feeds NEED...
    assert axis_score(gap, "READINESS") is None                   # ...and does not score (or penalise) READINESS
    assert gap["scores"]["READINESS"]["observed_but_not_mapped_to_this_axis"] == 1


def test_row8_new_generation_feeds_need_and_readiness_from_one_observation(reg, placeholder):
    res = score_company(reg, [Observation.trigger("new_generation_management")], weight_set=placeholder)
    assert axis_score(res, "NEED") == 100.0 and axis_score(res, "READINESS") == 100.0


def test_unknown_is_excluded_not_scored_as_bad_and_coverage_is_reported(reg, placeholder):
    a, b = "ebit_trend", "revenue_trend"
    only_a = score_company(reg, [Observation.trigger(a)], weight_set=placeholder)
    with_unknown = score_company(reg, [Observation.trigger(a), Observation.unknown(b)], weight_set=placeholder)
    assert axis_score(only_a, "NEED") == axis_score(with_unknown, "NEED") == 100.0   # not dragged down
    need = with_unknown["scores"]["NEED"]
    assert need["coverage_weight_pct"] == only_a["scores"]["NEED"]["coverage_weight_pct"]
    assert 0 < need["coverage_weight_pct"] < 100 and need["unknown"] == need["coverage_count"]["applicable"] - 1
    assert need["coverage_count"]["scored"] == 1
    # no observation at all: no score, not a zero
    empty = score_company(reg, [], weight_set=placeholder)
    assert axis_score(empty, "NEED") is None and empty["scores"]["NEED"]["coverage_weight_pct"] == 0.0


def test_not_applicable_leaves_the_denominator(reg, placeholder):
    base = score_company(reg, [Observation.trigger("ebit_trend")], weight_set=placeholder)["scores"]["NEED"]
    na = score_company(reg, [Observation.trigger("ebit_trend"), Observation.not_applicable("revenue_trend")],
                       weight_set=placeholder)["scores"]["NEED"]
    assert na["coverage_count"]["applicable"] == base["coverage_count"]["applicable"] - 1
    assert na["not_applicable"] == base["not_applicable"] + 1
    assert na["coverage_weight_pct"] > base["coverage_weight_pct"]    # denominator shrank
    assert na["score"] == base["score"] == 100.0
    # and an explicit not_applicable carries no penalty even when everything else is bad
    bad = score_company(reg, [Observation.not_trigger("ebit_trend"), Observation.not_applicable("revenue_trend")],
                        weight_set=placeholder)
    assert axis_score(bad, "NEED") == 0.0


def test_row73_applicability_comes_from_the_ateco_config(reg, placeholder):
    key = "maintenance_digitization"

    def need_applicable(ateco):
        res = score_company(reg, [], CompanyContext(ateco_code=ateco), placeholder)
        d = next(x for x in res["scores"]["NEED"]["detail"] if x["variable_key"] == key) if any(
            x["variable_key"] == key for x in res["scores"]["NEED"]["detail"]) else None
        return d is not None, res

    for ateco in ("28.99.9", "29.32.09", "30.30", "33.12", "35.11"):
        present, res = need_applicable(ateco)
        assert present and key not in res["applicability_undetermined"], ateco
    for ateco in ("41.20", "42.11", "43.21", "71.12.1"):
        present, res = need_applicable(ateco)
        assert not present, ateco
    present, res = need_applicable("10.51")                        # unlisted division: stays in, flagged
    assert present and key in res["applicability_undetermined"]
    present, res = need_applicable(None)
    assert present and key in res["applicability_undetermined"]
    assert reg.applicability_of(key, CompanyContext(ateco_code="43.99"))["status"] == "not_applicable"


def test_b2b_applicability_by_company_attribute(reg, placeholder):
    ctx = CompanyContext(attributes={"business_model": "pure_b2b", "has_customer_reviews": False, "has_employer_reviews": False})
    res = score_company(reg, [], ctx, placeholder)
    gone = {"physical_stores_trend", "store_geo_distribution", "product_quality_trend", "kununu_rating"}
    present = {d["variable_key"] for a in ("NEED", "READINESS") for d in res["scores"][a]["detail"]}
    assert not (gone & present)


def test_row75_absent_never_lowers_a_score(reg, placeholder):
    key = "digital_maturity_self_assessment"
    others = [Observation.not_trigger("management_age"), Observation.trigger("external_collaboration")]
    without = score_company(reg, others, weight_set=placeholder)
    unknown = score_company(reg, others + [Observation.unknown(key)], weight_set=placeholder)
    assert axis_score(without, "READINESS") == axis_score(unknown, "READINESS")
    assert unknown["scores"]["READINESS"]["coverage_weight_pct"] == without["scores"]["READINESS"]["coverage_weight_pct"]
    low_medium = score_company(reg, others + [Observation.with_label(key, "completed_low_to_medium")], weight_set=placeholder)
    assert axis_score(low_medium, "READINESS") > axis_score(without, "READINESS")      # completed = positive signal
    assert axis_score(low_medium, "NEED") == axis_score(without, "NEED")
    # there is no way to express 'low because missing': a graded strength is refused for this variable
    with pytest.raises(ObservationError):
        score_company(reg, [Observation.with_strength(key, 0.0)], weight_set=placeholder)


def test_row75_high_maturity_is_uncalibrated_so_unknown_with_a_warning(reg, placeholder):
    res = score_company(reg, [Observation.with_label("digital_maturity_self_assessment", "completed_high_maturity")],
                        weight_set=placeholder)
    assert axis_score(res, "READINESS") is None and any("completed_high_maturity" in w for w in res["warnings"])


def test_low_confidence_and_simulated_observations_are_unknown(reg, placeholder):
    res = score_company(reg, [Observation.trigger("ebit_trend", confidence=0.5)], weight_set=placeholder)
    assert axis_score(res, "NEED") is None
    res = score_company(reg, [Observation.trigger("ebit_trend", is_simulated=True)], weight_set=placeholder)
    assert axis_score(res, "NEED") is None
    gate = score_company(reg, [Observation.trigger("digital_lead_role_present", confidence=0.7)], weight_set=placeholder)
    assert gate["qualification_flag"]["flagged"] is False and gate["qualification_flag"]["gates"]["digital_lead_role_present"] == "unknown"


def test_graded_strength_interpolates_and_respects_polarity(reg, placeholder):
    half = score_company(reg, [Observation.with_strength("ebit_trend", 0.25)], weight_set=placeholder)
    assert axis_score(half, "NEED") == 25.0
    cash = score_company(reg, [Observation.with_strength("cash_position", 0.25)], weight_set=placeholder)
    assert axis_score(cash, "READINESS") == 75.0                   # Negative polarity: more trigger = lower READINESS


def test_cash_low_lowers_readiness_and_adds_nothing_to_need(reg, placeholder):
    res = score_company(reg, [Observation.trigger("cash_position")], weight_set=placeholder)
    assert axis_score(res, "READINESS") == 0.0 and axis_score(res, "NEED") is None


# ----------------------------------------------------------------------------- 4. gates

def test_a_failed_gate_sets_the_flag_and_leaves_the_scores_unchanged(reg, placeholder):
    base_obs = [Observation.trigger("ebit_trend"), Observation.not_trigger("management_age"),
                Observation.trigger("external_collaboration")]
    clean = score_company(reg, base_obs, weight_set=placeholder)
    failed = score_company(reg, base_obs + [Observation.trigger("digital_lead_role_present"),
                                            Observation.trigger("prior_open_innovation_usage"),
                                            Observation.trigger("approval_chain_depth")], weight_set=placeholder)
    for axis in ("NEED", "READINESS"):
        assert failed["scores"][axis]["score"] == clean["scores"][axis]["score"]
        assert failed["scores"][axis]["coverage_weight_pct"] == clean["scores"][axis]["coverage_weight_pct"]
    q = failed["qualification_flag"]
    assert q["flagged"] is True
    assert {x["variable_key"] for x in q["reasons"]} == {"digital_lead_role_present", "prior_open_innovation_usage", "approval_chain_depth"}
    assert all(x["reason"] for x in q["reasons"]) and q["gates"] == {k: "fail" for k in q["gates"]}
    assert clean["qualification_flag"]["flagged"] is False


def test_a_passing_or_unobserved_gate_does_not_flag(reg, placeholder):
    ok = score_company(reg, [Observation.not_trigger("digital_lead_role_present")], weight_set=placeholder)
    assert ok["qualification_flag"]["flagged"] is False
    assert ok["qualification_flag"]["gates"]["digital_lead_role_present"] == "pass"
    assert ok["qualification_flag"]["gates"]["approval_chain_depth"] == "unknown"


def test_gates_and_moderators_are_never_in_the_weighted_sum(reg, placeholder):
    res = score_company(reg, [], weight_set=placeholder)
    in_sum = {d["variable_key"] for a in ("NEED", "READINESS") for d in res["scores"][a]["detail"]}
    assert not in_sum & set(reg.gate_keys) and not in_sum & set(reg.moderator_keys)


# ----------------------------------------------------------------------------- 5. caveats, distress, moderators, posture

def test_caveat_flag_set_by_rows_50_51_72_and_they_add_nothing_to_readiness(reg, placeholder):
    for key in ("debt_level", "leverage_ratio", "system_integration"):
        res = score_company(reg, [Observation.trigger(key)], weight_set=placeholder)
        assert res["readiness_caveat"]["flagged"] is True
        assert [v["variable_key"] for v in res["readiness_caveat"]["variables"]] == [key]
        assert axis_score(res, "READINESS") is None and axis_score(res, "NEED") == 100.0
    clean = score_company(reg, [Observation.not_trigger("debt_level")], weight_set=placeholder)
    assert clean["readiness_caveat"]["flagged"] is False


def test_distress_cap_is_reported_but_not_applied_until_calibrated(reg, placeholder):
    res = score_company(reg, [Observation.trigger("ebit_trend"), Observation.trigger("system_integration")], weight_set=placeholder)
    assert res["distress"]["status"] == "not_calibrated" and res["distress"]["applied"] is False
    assert set(res["distress"]["candidates"]) == {"ebit_trend", "system_integration"}     # row 72 is covered too
    assert res["qualification_flag"]["flagged"] is False


def test_distress_qualification_flag_and_caveat_variable_72_counts(r, placeholder):
    r.distress["trigger"] = {"variable_trigger_strength_at_least": 0.5, "min_variables_triggered": 2}
    obs = [Observation.trigger("ebit_trend"), Observation.trigger("system_integration"), Observation.not_trigger("management_age")]
    res = score_company(r, obs, weight_set=placeholder)
    assert res["distress"]["applied"] and res["distress"]["triggered"] == ["ebit_trend", "system_integration"]
    assert res["qualification_flag"]["flagged"] and res["qualification_flag"]["reasons"][-1]["source"] == "distress"
    # scores are untouched by the qualification-flag action
    assert axis_score(res, "READINESS") == 0.0
    # the same two financial rows without the caveat variable do not reach min_variables_triggered
    one = score_company(r, [Observation.trigger("ebit_trend")], weight_set=placeholder)
    assert not one["distress"]["applied"]


def test_distress_readiness_cap_action(r, placeholder):
    r.distress["trigger"] = {"variable_trigger_strength_at_least": 0.5, "min_variables_triggered": 1}
    r.distress["action"]["type"] = "readiness_cap"
    r.distress["action"]["readiness_cap_value"] = 40
    obs = [Observation.trigger("debt_level"), Observation.trigger("management_age")]   # readiness 100 before the cap
    res = score_company(r, obs, weight_set=placeholder)
    assert axis_score(res, "READINESS") == 40.0 and res["scores"]["READINESS"]["score_uncapped"] == 100.0
    assert axis_score(res, "NEED") == 100.0                       # NEED is never capped


def test_moderators_are_off_by_default_and_never_scored(reg, placeholder):
    base = score_company(reg, [Observation.trigger("management_turnover"), Observation.trigger("materials_cost")], weight_set=placeholder)
    with_mod = score_company(reg, [Observation.trigger("management_turnover"), Observation.trigger("materials_cost"),
                                   Observation.with_label("family_ownership_share", "fully_family_owned"),
                                   Observation.with_label("product_type_tag", "physical_manufactured_good")], weight_set=placeholder)
    assert axis_score(with_mod, "NEED") == axis_score(base, "NEED")
    assert with_mod["moderators_applied"] == [] and with_mod["moderator_context"]["family_ownership_share"]["label"] == "fully_family_owned"


def test_note_only_moderator_rule_changes_no_number(reg, placeholder):
    obs = [Observation.trigger("ebit_trend")]
    plain = score_company(reg, obs, weight_set=placeholder)
    ma = score_company(reg, obs + [Observation.with_label("recent_ma_activity", "recent")], weight_set=placeholder)
    assert axis_score(ma, "NEED") == axis_score(plain, "NEED") and ma["interpretation_notes"]


def test_enabled_moderator_with_uncalibrated_factor_fails_loudly(r):
    for rule in r.moderators:
        if rule["id"] == "family_ownership_turnover_reading":
            rule["enabled"] = True
    with pytest.raises(ConfigError):
        r.validate()


def test_enabled_moderator_with_a_factor_reweights_its_targets(r, placeholder):
    for rule in r.moderators:
        if rule["id"] == "family_ownership_turnover_reading":
            rule["enabled"] = True
            rule["effect"]["factor"] = 0.0                         # test value only
    r.validate()
    obs = [Observation.trigger("management_turnover"), Observation.not_trigger("ebit_trend"),
           Observation.with_label("family_ownership_share", "fully_family_owned")]
    res = score_company(r, obs, weight_set=placeholder)
    assert axis_score(res, "NEED") == 0.0                          # turnover weight zeroed, only ebit (0) remains
    assert res["moderators_applied"][0]["rule"] == "family_ownership_turnover_reading"


@pytest.mark.parametrize("age,integration,cell,tier", [
    ("legacy", "siloed", "old_siloed", "strongest"),
    ("legacy", "integrated", "old_integrated", "middle"),
    ("current", "siloed", "new_siloed", "middle"),
    ("current", "integrated", "new_integrated", "weakest"),
])
def test_paired_systems_posture(reg, placeholder, age, integration, cell, tier):
    res = score_company(reg, [Observation.with_label("erp_systems_age", age),
                              Observation.with_label("system_integration", integration)], weight_set=placeholder)
    p = res["systems_posture"]
    assert (p["cell"], p["need_tier"]) == (cell, tier)
    # both rows are stored separately in the detail, never averaged into one entry
    detail = {d["variable_key"]: d for d in res["scores"]["NEED"]["detail"]}
    assert detail["erp_systems_age"]["axis_value"] in (0.0, 1.0) and detail["system_integration"]["axis_value"] in (0.0, 1.0)


def test_posture_needs_both_rows_and_a_cutoff_for_graded_values(reg, placeholder):
    only_age = score_company(reg, [Observation.with_label("erp_systems_age", "legacy")], weight_set=placeholder)["systems_posture"]
    assert only_age["cell"] is None and only_age["age"] == "old" and only_age["integration"] is None
    graded = score_company(reg, [Observation.with_strength("erp_systems_age", 0.6), Observation.with_strength("system_integration", 1.0)],
                           weight_set=placeholder)["systems_posture"]
    assert graded["cell"] is None and "numeric_cutoff" in graded["reason"]
    c = copy.deepcopy(reg)
    c.interactions["systems_posture"]["numeric_cutoff"] = 0.5
    assert score_company(c, [Observation.with_strength("erp_systems_age", 0.6), Observation.with_strength("system_integration", 1.0)],
                         weight_set=placeholder)["systems_posture"]["cell"] == "old_siloed"


# ----------------------------------------------------------------------------- 6. redundancy groups

def test_quality_group_row_74_carries_alone_when_row_29_is_not_applicable(reg, placeholder):
    key74, key29 = "quality_process_maturity", "product_quality_trend"

    def w74(res):
        return next(d["weight"] for d in res["scores"]["NEED"]["detail"] if d["variable_key"] == key74)

    both = score_company(reg, [], weight_set=placeholder)
    alone = score_company(reg, [Observation.not_applicable(key29)], weight_set=placeholder)
    # group weight is a whole unit; with 29 present it is shared, without it row 74 gets all of it
    assert w74(both) * 2 == pytest.approx(w74(alone))
    # unknown (not N/A) keeps the group shared
    assert w74(score_company(reg, [Observation.unknown(key29)], weight_set=placeholder)) == pytest.approx(w74(both))


def test_placeholder_is_equal_weight_per_dampened_unit_and_sums_to_one(reg, placeholder):
    res = score_company(reg, [], weight_set=placeholder)
    for axis in ("NEED", "READINESS"):
        detail = res["scores"][axis]["detail"]
        assert sum(d["weight"] for d in detail) == pytest.approx(1.0, abs=1e-4)
        by_group = {}
        for d in detail:
            by_group.setdefault(reg.group_of(d["variable_key"]) or d["variable_key"], 0.0)
            by_group[reg.group_of(d["variable_key"]) or d["variable_key"]] += d["weight"]
        units = list(by_group.values())
        assert max(units) - min(units) < 1e-4                           # every unit weighs the same


def test_variables_without_a_group_are_their_own_group(reg):
    assert reg.group_of("total_assets") is None and reg.group_of("ebit_trend") == "PROFITABILITY"


def test_active_grouping_is_editable_and_logged_on_every_output(r, placeholder):
    before = score_company(r, [], weight_set=placeholder)["grouping"]
    r.groupings["groupings"]["trial"] = {"status": "trial", "groups": {"ebit_trend": "X", "revenue_trend": "X"}}
    r.groupings["active_grouping_id"] = "trial"
    after = score_company(r, [], weight_set=placeholder)["grouping"]
    assert before["id"] == "sheet_proposal_2026-10" and after["id"] == "trial" and before["hash"] != after["hash"]


# ----------------------------------------------------------------------------- 7. weight sets

def explicit_set(reg, **over):
    need = [k for k in reg.scored_keys if "NEED" in reg.axes_of(k)]
    ready = [k for k in reg.scored_keys if "READINESS" in reg.axes_of(k)]
    raw = {"weight_set_id": "test-explicit", "status": "draft", "mode": "explicit", "redundancy_dampening": "none",
           "weights": {"NEED": {k: 1 / len(need) for k in need}, "READINESS": {k: 1 / len(ready) for k in ready}}}
    raw.update(over)
    return weight_set_from_dict(raw)


def test_valid_explicit_set_passes_and_is_stamped_on_the_output(reg):
    ws = explicit_set(reg)
    validate_weight_set(ws, reg)
    res = score_company(reg, [Observation.trigger("ebit_trend")], weight_set=ws)
    assert res["weight_set"]["id"] == "test-explicit"


def test_weights_must_sum_to_one_per_axis(reg):
    ws = explicit_set(reg)
    ws.weights["NEED"]["ebit_trend"] += 0.05
    with pytest.raises(WeightSetError, match="NEED weights sum"):
        validate_weight_set(ws, reg)
    ws = explicit_set(reg)
    ws.weights["READINESS"]["management_age"] = 0.0
    with pytest.raises(WeightSetError, match="READINESS weights sum"):
        validate_weight_set(ws, reg)
    with pytest.raises(WeightSetError, match="missing weights for axis"):
        validate_weight_set(explicit_set(reg, weights={"NEED": {"ebit_trend": 1.0}}), reg)


def test_weights_on_gate_or_moderator_rows_are_rejected(reg):
    for key in ("digital_lead_role_present", "family_ownership_share"):
        ws = explicit_set(reg)
        ws.weights["READINESS"][key] = 0.0
        with pytest.raises(WeightSetError, match="only Scored variables take weights"):
            validate_weight_set(ws, reg)


def test_weights_on_the_wrong_axis_negative_or_unknown_rows_are_rejected(reg):
    ws = explicit_set(reg)
    ws.weights["NEED"]["management_age"] = 0.0                    # READINESS-only variable
    with pytest.raises(WeightSetError, match="tag axis"):
        validate_weight_set(ws, reg)
    ws = explicit_set(reg)
    ws.weights["NEED"]["ebit_trend"] = -0.1
    with pytest.raises(WeightSetError, match="non-negative"):
        validate_weight_set(ws, reg)
    ws = explicit_set(reg)
    ws.weights["NEED"]["nope"] = 0.0
    with pytest.raises(WeightSetError, match="unknown variable"):
        validate_weight_set(ws, reg)


def test_a_both_variable_takes_a_weight_on_each_axis(reg):
    ws = explicit_set(reg)
    assert "quality_process_maturity" in ws.weights["NEED"] and "quality_process_maturity" in ws.weights["READINESS"]
    validate_weight_set(ws, reg)


def test_approved_requires_an_approver_and_placeholder_keeps_its_label(reg):
    with pytest.raises(WeightSetError, match="approved_by"):
        validate_weight_set(explicit_set(reg, status="approved"), reg)
    validate_weight_set(explicit_set(reg, status="approved", approved_by="GG", approved_on="2026-11-01"), reg)
    ph = explicit_set(reg, status="placeholder", label="looks fine")
    with pytest.raises(WeightSetError, match="PLACEHOLDER"):
        validate_weight_set(ph, reg)


def test_additional_weight_sets_drop_in_as_files_without_code_changes(reg, tmp_path):
    placeholder_src = (SETTINGS_DIR / "weight_sets" / "placeholder_equal_unit.json").read_text(encoding="utf-8")
    (tmp_path / "placeholder.json").write_text(placeholder_src, encoding="utf-8")
    ws = explicit_set(reg, weight_set_id="expert-v1", status="approved", approved_by="GG", approved_on="2026-11-01")
    (tmp_path / "expert.json").write_text(json.dumps({
        "weight_set_id": ws.weight_set_id, "status": "approved", "mode": "explicit", "redundancy_dampening": "none",
        "approved_by": "GG", "approved_on": "2026-11-01", "weights": ws.weights}), encoding="utf-8")
    sets = load_weight_sets(reg, tmp_path)
    assert set(sets) == {"placeholder-equal-unit-v1", "expert-v1"}
    assert default_weight_set(reg, tmp_path).weight_set_id == "expert-v1"       # approved wins over placeholder
    (tmp_path / "broken.json").write_text(json.dumps({"weight_set_id": "bad", "status": "draft", "mode": "explicit",
                                                     "weights": {"NEED": {"ebit_trend": 0.5}, "READINESS": {}}}), encoding="utf-8")
    with pytest.raises(WeightSetError):
        load_weight_sets(reg, tmp_path)


def test_two_weight_sets_run_side_by_side(reg, placeholder):
    ws = explicit_set(reg, weight_set_id="ebit-heavy")
    ws.weights["NEED"] = {k: 0.0 for k in ws.weights["NEED"]}
    ws.weights["NEED"]["ebit_trend"] = 0.5
    ws.weights["NEED"]["revenue_trend"] = 0.5
    obs = [Observation.trigger("ebit_trend"), Observation.not_trigger("revenue_trend"), Observation.trigger("materials_cost")]
    cmp_ = compare_weight_sets(reg, obs, [placeholder, ws])
    assert set(cmp_["results"]) == {"placeholder-equal-unit-v1", "ebit-heavy"}
    assert cmp_["scores_by_axis"]["NEED"]["ebit-heavy"] == 50.0
    assert cmp_["scores_by_axis"]["NEED"]["ebit-heavy"] != cmp_["scores_by_axis"]["NEED"]["placeholder-equal-unit-v1"]


def test_explicit_set_reuses_the_existing_rank_halving_dampening(reg):
    ws = explicit_set(reg, redundancy_dampening="rank_halving")
    ws.weights["NEED"] = {k: 0.0 for k in ws.weights["NEED"]}
    ws.weights["NEED"].update({"ebit_trend": 0.5, "margin_compression": 0.3, "revenue_trend": 0.2})   # one PROFITABILITY group
    res = score_company(reg, [Observation.trigger("ebit_trend")], weight_set=ws)
    w = {d["variable_key"]: d["weight"] for d in res["scores"]["NEED"]["detail"]}
    assert w["ebit_trend"] == pytest.approx(0.5) and w["margin_compression"] == pytest.approx(0.15) and w["revenue_trend"] == pytest.approx(0.05)


# ----------------------------------------------------------------------------- 8. reports, no blending

def test_every_report_prints_the_placeholder_label(reg, placeholder):
    res = score_company(reg, [Observation.trigger("ebit_trend")], weight_set=placeholder, company_id="ACME")
    text = format_report(res)
    assert PLACEHOLDER_LABEL in text and "placeholder-equal-unit-v1" in text and "ACME" in text
    assert "coverage" in text and "never combined" in text and "sheet_proposal" in text
    assert res["weight_set"]["label"] == PLACEHOLDER_LABEL


def test_no_combined_need_readiness_number_and_no_vienna_classifier(reg, placeholder):
    res = score_company(reg, [Observation.trigger("ebit_trend"), Observation.not_trigger("management_age")], weight_set=placeholder)
    flat = json.dumps(res).lower()
    for banned in ("combined", "blend", "composite", "total_score", "overall", "noway", "orchestrator", "vienna_level"):
        assert banned not in flat.replace("never combined", "")
    assert set(res["scores"]) == {"NEED", "READINESS"}


# ----------------------------------------------------------------------------- 9. quality norm (row 74)

def test_expected_certifications_follow_the_sector_table(reg):
    assert reg.expected_certifications("29.32.09")["expected_any_of"] == ["IATF 16949"]
    assert reg.expected_certifications("30.30")["expected_any_of"] == ["AS9100"]
    assert reg.expected_certifications("25.11.00")["expected_any_of"] == ["ISO 3834", "EN 15085"]
    assert reg.expected_certifications("28.99.9")["id"] == "generic_baseline"
    assert reg.expected_certifications(None)["expected_any_of"] == ["ISO 9001"]


def test_quality_gap_vs_sector_norm(reg, placeholder):
    obs, detail = classify_quality_process(reg, "29.32.09", [{"standard": "ISO 9001", "years_held": 20}])
    assert obs.label == "gap_vs_sector_norm" and detail["missing"] == ["IATF 16949"]      # ISO 9001 is not the automotive norm
    obs, _ = classify_quality_process(reg, "29.32.09", [{"standard": "iatf16949"}])
    assert obs.label == "certified_meets_norm"
    obs, _ = classify_quality_process(reg, "30.30", [{"standard": "EN 9100"}])
    assert obs.label == "certified_meets_norm"                                              # alias of AS9100
    obs, _ = classify_quality_process(reg, "28.99.9", [])
    assert obs.label == "gap_vs_sector_norm"                                                # checked, nothing held
    obs, _ = classify_quality_process(reg, "28.99.9", None)
    assert obs.state == "unknown"                                                           # not checked
    res = score_company(reg, [classify_quality_process(reg, "28.99.9", [])[0]], weight_set=placeholder)
    assert axis_score(res, "NEED") == 100.0


def test_long_held_multi_standard_needs_calibrated_thresholds(r):
    certs = [{"standard": "ISO 9001", "years_held": 15}, {"standard": "ISO 14001", "years_held": 12}]
    obs, detail = classify_quality_process(r, "28.99.9", certs)
    assert obs.label == "certified_meets_norm" and "TO_CALIBRATE" in detail["note"]
    r.certifications["thresholds"]["min_years_held_for_long_held"] = 10
    r.certifications["thresholds"]["min_standards_for_multi_standard"] = 2
    assert classify_quality_process(r, "28.99.9", certs)[0].label == "long_held_multi_standard"


# ----------------------------------------------------------------------------- 10. questionnaire, bridge, config markers

def test_first_contact_questionnaire_is_built_from_the_proxy_text(reg):
    sec = build_first_contact_section(reg)
    assert [i["sheet_row"] for i in sec["items"]] == [11, 12, 32, 65, 70, 72, 73, 75]
    by = {i["variable_key"]: i for i in sec["items"]}
    assert by["system_integration"]["proxy_text"] == reg.by_key["system_integration"].proxy
    assert reg.by_key["system_integration"].proxy.split(".")[0][:30].lower() in by["system_integration"]["question"].lower()
    assert all(any(o["records_as"] == "unknown" for o in i["answer_options"]) for i in sec["items"])   # a way to say "don't know"
    assert any(o["records_as"] == "not_applicable" for o in by["maintenance_digitization"]["answer_options"])


def test_bridge_converts_live_signal_maps_without_changing_direction(reg, placeholder):
    from indicators import INDICATOR_SEED
    defs = {d["key"]: d for d in INDICATOR_SEED}
    sm = {
        "cash_position": {"status": "present", "value": None, "source": "AIDA"},                # no value -> unknown
        "ebit_trend": {"status": "present", "value": defs["ebit_trend"]["raw_min"], "source": "AIDA", "confidence": 1.0, "is_simulated": False},
        "digital_lead_role_present": {"status": "present", "value": 0.0, "source": "web", "confidence": 0.9},
        "senior_mgmt_tenure": {"status": "not_yet_checked", "value": None},
    }
    obs = {o.variable_key: o for o in observations_from_signal_map(reg, sm, defs)}
    assert obs["cash_position"].state == "unknown" and obs["senior_mgmt_tenure"].state == "unknown"
    assert obs["ebit_trend"].state == "observed"
    # a missing role (live normalised 0 = unhealthy) fails the gate: trigger strength 1
    assert obs["digital_lead_role_present"].strength == 1.0
    res = score_company(reg, list(obs.values()), weight_set=placeholder)
    assert res["qualification_flag"]["flagged"] is True


def test_bridge_treats_live_absent_as_unknown_for_asymmetric_rows(r, placeholder):
    from indicators import INDICATOR_SEED
    defs = {d["key"]: d for d in INDICATOR_SEED}
    defs["digital_maturity_self_assessment"] = dict(defs["ebit_trend"])
    defs["prior_open_innovation_usage"] = defs["prior_open_innovation_usage"]
    sm = {"digital_maturity_self_assessment": {"status": "absent", "value": None},
          "external_collaboration": {"status": "absent", "value": None}}
    obs = {o.variable_key: o for o in observations_from_signal_map(r, sm, defs)}
    assert obs["digital_maturity_self_assessment"].state == "unknown"
    assert obs["external_collaboration"].state == "observed"       # ordinary rows keep the live engine's absent -> 0 reading


def test_every_todo_marker_is_discoverable_for_the_report(reg):
    m = reg.todo_markers()
    assert any("expected_certifications_by_ateco" in x for x in m["TO_CALIBRATE"])
    assert any("distress" in x for x in m["TO_CALIBRATE"])
    assert any("moderators" in x for x in m["TO_CALIBRATE"])
    assert any("source_it" in x for x in m["TO_MAP"])


def test_live_scoring_module_is_untouched_by_this_package():
    import scoring
    assert hasattr(scoring, "calculate_company_scores") and hasattr(scoring, "_apply_redundancy_dampening")
