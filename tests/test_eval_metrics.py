"""Unit tests for the eval metrics on tiny hand-verifiable cases."""

from issuer_data.eval.metrics import (
    grits_con,
    numeric_exact_match,
    paragraph_continuity,
    teds,
)

A = [["Account", "2023"], ["Revenue", "100"], ["Cost", "60"]]


def test_teds_identical_is_one():
    assert teds(A, A) == 1.0
    assert teds([], []) == 1.0


def test_teds_one_cell_diff():
    B = [["Account", "2023"], ["Revenue", "999"], ["Cost", "60"]]
    # one leaf relabel: TED=1; tree size = 1 + 3*(1+2) = 10 → TEDS = 1 - 1/10
    assert teds(B, A) == 0.9


def test_teds_missing_row_penalized():
    short = [["Account", "2023"], ["Revenue", "100"]]
    s = teds(short, A)
    assert 0.0 < s < 1.0


def test_grits_identical_and_partial():
    assert grits_con(A, A) == 1.0
    # drop one row → 4 matched cells of 6+4 total = 0.8
    partial = [["Account", "2023"], ["Revenue", "100"]]
    assert grits_con(partial, A) == 2 * 4 / (4 + 6)
    assert grits_con([], []) == 1.0
    assert grits_con([], A) == 0.0


def test_numeric_exact_match():
    pred = [["Revenue", "1,234"], ["Cost", "567"]]
    gold = [["Revenue", "1234"], ["Cost", "567"]]
    assert numeric_exact_match(pred, gold) == 1.0        # commas ignored
    assert numeric_exact_match([["x", "1"]], [["y", "2"]]) == 0.0
    assert numeric_exact_match([["a", "b"]], [["c", "d"]]) == 1.0  # no numbers


def test_paragraph_continuity():
    gold = ["The company grew revenue this year.", "Margins expanded."]
    joined = "Header 1  The company grew revenue this year. Margins expanded.  - 2 -"
    assert paragraph_continuity(joined, gold) == 1.0
    # a paragraph left split across a spurious break is not found contiguously
    split = "The company grew revenue this\n\nyear. Margins expanded."
    assert paragraph_continuity(split, gold) == 0.5
    assert paragraph_continuity("anything", []) == 1.0
