"""Tests for engine consensus — two detectors refereeing each other."""

from issuer_data.pdf_agreement import compare_tables
from issuer_data.pdf_extract import StitchedTable


def _t(rows, start=1, end=1):
    return StitchedTable(rows=rows, page_start=start, page_end=end)


def test_identical_extractions_agree_completely():
    rows = [["Account", "2023"], ["Revenue", "1,234"]]
    assert compare_tables([_t(rows)], [_t(rows)]).score == 1.0


def test_a_differing_cell_lowers_the_score():
    a = [_t([["Account", "2023"], ["Revenue", "1,234"]])]
    b = [_t([["Account", "2023"], ["Revenue", "1,284"]])]
    score = compare_tables(a, b).score
    assert 0.0 < score < 1.0


def test_a_table_the_other_detector_missed_scores_zero():
    a = [_t([["Account", "2023"], ["Revenue", "1,234"]])]
    report = compare_tables(a, [])
    assert report.score == 0.0
    assert report.per_table[0].matched is False


def test_a_similar_table_on_another_page_does_not_confirm():
    """Restricting candidates to overlapping pages stops a lookalike table
    elsewhere in the filing from vouching for this one."""
    rows = [["Account", "2023"], ["Revenue", "1,234"]]
    report = compare_tables([_t(rows, 1, 1)], [_t(rows, 40, 40)])
    assert report.score == 0.0


def test_a_stitched_span_matches_any_overlapping_table():
    rows = [["Account", "2023"], ["Revenue", "1,234"]]
    assert compare_tables([_t(rows, 1, 3)], [_t(rows, 2, 2)]).score == 1.0


def test_no_tables_on_either_side_is_not_a_disagreement():
    assert compare_tables([], []).score is None


def test_finding_nothing_when_the_other_found_something_is_a_disagreement():
    assert compare_tables([], [_t([["a", "1"]])]).score == 0.0


def test_blank_spacer_rows_are_not_a_disagreement():
    """The text-geometry detector emits a blank row between printed rows. Left
    in, it scores an identical table at ~0.59 and every clean document gets
    flagged — an alarm that always fires is one people stop reading."""
    lines_grid = [["Account", "2023"], ["Revenue", "600"], ["Cost", "400"]]
    text_grid = [["Account", "2023"], ["", ""], ["Revenue", "600"],
                 ["", ""], ["Cost", "400"]]
    assert compare_tables([_t(lines_grid)], [_t(text_grid)]).score == 1.0


def test_normalizing_blanks_does_not_hide_a_real_difference():
    lines_grid = [["Account", "2023"], ["Revenue", "600"]]
    text_grid = [["Account", "2023"], ["", ""], ["Revenue", "6000"]]
    assert compare_tables([_t(lines_grid)], [_t(text_grid)]).score < 1.0


def test_an_empty_column_is_dropped_from_both_sides():
    lines_grid = [["Account", "2023"], ["Revenue", "600"]]
    text_grid = [["Account", "", "2023"], ["Revenue", "", "600"]]
    assert compare_tables([_t(lines_grid)], [_t(text_grid)]).score == 1.0
