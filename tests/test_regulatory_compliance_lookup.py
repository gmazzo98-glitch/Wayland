"""The curated lookup must never turn a sector match into a legal score."""

import pytest

from scrapers.regulatory_compliance_lookup import lookup_regulatory_exposure


@pytest.mark.parametrize("nace", ["28.15", "C28.15", "281500"])
def test_c28_formats_find_five_regimes_without_a_score(nace):
    result = lookup_regulatory_exposure(nace, 75)
    assert result["field_status"] == "needs_company_facts"
    assert result["exposure_score"] is None
    assert {match["regime"] for match in result["screening_matches"]} == {
        "NIS2", "Machinery Regulation", "Cyber Resilience Act", "AI Act", "CBAM"
    }
    assert all(match["celex"] and match["provisions"] and match["source_urls"]
               for match in result["screening_matches"])


@pytest.mark.parametrize("headcount,status", [(49, "possible_exception"), (50, "candidate_only"), (249, "candidate_only"), (250, "candidate_only")])
def test_nis2_size_bands_do_not_turn_small_entity_into_confirmed_absence(headcount, status):
    matches = lookup_regulatory_exposure("C28", headcount)["screening_matches"]
    assert next(match for match in matches if match["regime"] == "NIS2")["screening_status"] == status


@pytest.mark.parametrize("nace,headcount", [(None, 75), ("C28", None), ("C28", -1), ("unknown", 75)])
def test_missing_inputs_are_not_zero_exposure(nace, headcount):
    result = lookup_regulatory_exposure(nace, headcount)
    assert result["field_status"] == "missing_input"
    assert result["exposure_score"] is None
    assert result["screening_matches"] == []


def test_uncovered_sector_is_not_confirmed_zero_exposure():
    result = lookup_regulatory_exposure("25.11", 75)
    assert result["field_status"] == "not_covered"
    assert result["exposure_score"] is None
    assert result["screening_matches"] == []
