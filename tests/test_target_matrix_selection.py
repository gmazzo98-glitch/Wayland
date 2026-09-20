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
