"""
The pure part of the Target Matrix crawl selection: turning a data_editor's edits into a
set of company ids, and fingerprinting a table's data. (The widget choreography around
them — baseline, epoch keys, sorting, filters — needs a real browser and was verified in
one; see the docstring on views.target_matrix._selectable_table for why it exists.)
"""

import pandas as pd

from views.target_matrix import _fold_edits, _table_signature, TICK_COLUMN


IDS = ["a", "b", "c", "d"]


def test_ticking_maps_the_edited_position_to_that_row_id():
    # Position is into the frame the editor was GIVEN, so a sorted view can't skew it.
    assert _fold_edits(set(), IDS, {2: {TICK_COLUMN: True}}) == {"c"}
    assert _fold_edits(set(), IDS, {0: {TICK_COLUMN: True}, 3: {TICK_COLUMN: True}}) == {"a", "d"}


def test_unticking_removes_and_other_picks_are_left_alone():
    already = {"a", "c", "elsewhere"}  # "elsewhere" is in another table / filtered out
    assert _fold_edits(already, IDS, {0: {TICK_COLUMN: False}}) == {"c", "elsewhere"}


def test_edits_are_cumulative_so_applying_them_twice_changes_nothing():
    edits = {1: {TICK_COLUMN: True}, 2: {TICK_COLUMN: False}}
    once = _fold_edits({"c"}, IDS, edits)
    assert once == {"b"}
    assert _fold_edits(once, IDS, edits) == once


def test_out_of_range_positions_and_other_columns_are_ignored():
    edits = {9: {TICK_COLUMN: True}, -1: {TICK_COLUMN: True}, 1: {"Need Score": 99.0}}
    assert _fold_edits({"a"}, IDS, edits) == {"a"}


def test_fold_does_not_mutate_the_selection_it_was_given():
    selected = {"a"}
    _fold_edits(selected, IDS, {1: {TICK_COLUMN: True}})
    assert selected == {"a"}


def test_signature_changes_with_any_value_or_row_order_but_not_with_a_copy():
    frame = pd.DataFrame({"name": ["x", "y", "z"], "need": [1.0, 2.0, 3.0]})
    assert _table_signature(frame) == _table_signature(frame.copy())
    changed = frame.copy()
    changed.loc[1, "need"] = 2.5
    assert _table_signature(changed) != _table_signature(frame)
    assert _table_signature(frame.iloc[::-1].reset_index(drop=True)) != _table_signature(frame)


def test_signature_handles_missing_values_and_an_empty_frame():
    with_gaps = pd.DataFrame({"name": ["x", None], "need": [None, 2.0]})
    assert _table_signature(with_gaps) == _table_signature(with_gaps.copy())
    assert isinstance(_table_signature(pd.DataFrame({"name": []})), str)


# ---- bulk selection rules ------------------------------------------------------------------

from views.target_matrix import _top_n_ids, _ids_in_score_ranges, _match_pasted_list  # noqa: E402


def _frame(prefix, rows):
    return pd.DataFrame(
        [{"id": f"{prefix}{i}", "legal_name": f"{prefix.upper()} Firma {i} GmbH", "registration_number": f"HRB-{prefix}{i}",
          "need_score": need, "readiness_score": ready, "total_completeness_pct": comp, "website_url": site}
         for i, (need, ready, comp, site) in enumerate(rows)])


MIDCAP = _frame("m", [(90, 50, 10, "a.de"), (80, 45, 20, None), (70, 90, 30, "c.de"), (60, 10, 40, "  ")])
SME = _frame("s", [(55, 42, 5, "x.it"), (95, 80, 50, "y.it")])
FRAMES = {"Midcap": MIDCAP, "SME": SME}


def test_top_n_is_taken_from_each_segment_separately_never_pooled():
    # Pooled, the top 2 by need would be m0 (90) and s1 (95). Per segment it is 2 of each.
    assert _top_n_ids(FRAMES, "need_score", 2) == {"m0", "m1", "s1", "s0"}
    assert _top_n_ids(FRAMES, "need_score", 1) == {"m0", "s1"}


def test_top_n_lowest_first_and_a_short_segment():
    assert _top_n_ids(FRAMES, "readiness_score", 1, ascending=True) == {"m3", "s0"}
    assert _top_n_ids(FRAMES, "need_score", 50) == set(MIDCAP["id"]) | set(SME["id"])


def test_skip_no_website_excludes_blank_and_whitespace_urls_before_ranking():
    # m1 (no url) and m3 (blank) are dropped first, so the top 2 reaches down to m2.
    assert _top_n_ids(FRAMES, "need_score", 2, require_website=True) == {"m0", "m2", "s1", "s0"}
    assert _ids_in_score_ranges(FRAMES, (0, 100), (0, 100), require_website=True) == {"m0", "m2", "s0", "s1"}


def test_score_ranges_are_inclusive_and_both_axes_must_match():
    # m2 fails on readiness (90 > 85), m3 on both, s0 on need (55 < 60).
    assert _ids_in_score_ranges(FRAMES, (60, 100), (40, 85)) == {"m0", "m1", "s1"}
    assert _ids_in_score_ranges(FRAMES, (90, 90), (50, 50)) == {"m0"}  # edges count
    assert _ids_in_score_ranges(FRAMES, (0, 10), (0, 10)) == set()


ALL = pd.concat([MIDCAP, SME], ignore_index=True)


def test_pasted_list_matches_names_and_registration_numbers_ignoring_case_and_punctuation():
    ids, missing, ambiguous = _match_pasted_list("M firma 0 gmbh\nhrb m2\n  \nHRB-s1", ALL)
    assert ids == {"m0", "m2", "s1"} and missing == [] and ambiguous == []


def test_pasted_list_accepts_excel_style_tabs_and_semicolons():
    ids, _, _ = _match_pasted_list("HRB-m0\tHRB-m1;HRB-m3", ALL)
    assert ids == {"m0", "m1", "m3"}


def test_a_fragment_resolves_only_when_it_is_unique():
    # "Firma 2 GmbH" is in one name only; "Firma" is in every name and must not tick everything.
    ids, missing, ambiguous = _match_pasted_list("Firma 2 GmbH\nFirma\nnonexistent corp\nab", ALL)
    assert ids == {"m2"}
    assert ambiguous == ["Firma"]
    assert missing == ["nonexistent corp", "ab"]  # too short a fragment never substring-matches


def test_pasted_list_empty_or_none_is_harmless():
    assert _match_pasted_list("", ALL) == (set(), [], [])
    assert _match_pasted_list(None, ALL) == (set(), [], [])
