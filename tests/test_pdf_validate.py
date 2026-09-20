"""Tests for the reference-free validation gate: coverage, arithmetic, verdict.

Each test corrupts an extraction in one specific way and asserts the matching
check fires. That is the whole claim being made — that these signals detect
failure without a label — so it is worth testing failure-first rather than
asserting that clean input stays clean.
"""

import pytest

from issuer_data.pdf_extract import StitchedTable
from issuer_data.pdf_validate import (
    Thresholds,
    check_arithmetic,
    check_tables_arithmetic,
    coverage,
    decide,
    parse_number,
    validate,
)


def _page(page_no, lines, height=800.0):
    return {"page_no": page_no, "width": 600.0, "height": height,
            "lines": [{"text": t, "x0": 50.0, "x1": 400.0,
                       "top": 10.0 + i * 250.0, "bottom": 25.0 + i * 250.0}
                      for i, t in enumerate(lines)],
            "tables": []}


def _table(rows, page=1):
    return StitchedTable(rows=rows, page_start=page, page_end=page)


# ----------------------------------------------------------------- parse_number
@pytest.mark.parametrize("cell,expected", [
    ("1,234", 1234.0),
    ("(1,234)", -1234.0),
    ("△1,234", -1234.0),          # the Korean negative convention
    ("-", 0.0),                   # a dash is nil, not "no value"
    ("1,234 백만원", 1234.0),      # a trailing unit is still a number
    ("1.234.567", 1234567.0),     # european grouping, not a decimal
    ("12.5", 12.5),
    ("매출액", None),
    ("FY2023", None),             # a label that happens to hold a year
    ("2023-12-31", None),         # a date column does not sum
    (None, None),
])
def test_parse_number(cell, expected):
    assert parse_number(cell) == expected


# --------------------------------------------------------------------- coverage
def test_coverage_is_one_when_everything_survives():
    pages = [_page(1, ["Revenue 1,234", "Operating income 456"])]
    text = "Revenue 1,234 Operating income 456"
    cov = coverage(pages, text, [])
    assert cov.token_recall == 1.0
    assert cov.numeric_recall == 1.0


def test_coverage_catches_a_dropped_table_row():
    """The failure grounding is blind to: numbers that were never emitted.

    A locally-read table always grounds at 1.0 because its digits come from the
    same text layer, so only coverage can see this.
    """
    pages = [_page(1, ["Account 2023", "Revenue 1,234", "Cost 555", "Net 679"])]
    kept = [_table([["Account", "2023"], ["Revenue", "1,234"]])]   # two rows lost
    cov = coverage(pages, "", kept)
    assert cov.numeric_recall < 0.6
    assert "555" in cov.missing_numbers
    assert any("Cost" in line for line in cov.lost_lines)


def test_coverage_ignores_running_headers_it_meant_to_drop():
    """A header removed on purpose is not lost content — otherwise every clean
    multi-page document would look like it was losing text."""
    bodies = ["revenue grew", "margins widened", "costs fell", "orders rose", "cash rose"]
    pages = [_page(i + 1, ["ACME Annual Report", f"{b} by {i}0 percent"])
             for i, b in enumerate(bodies)]
    text = " ".join(f"{b} by {i}0 percent" for i, b in enumerate(bodies))
    cov = coverage(pages, text, [])
    assert cov.token_recall == 1.0


def test_coverage_counts_table_cells_as_output():
    pages = [_page(1, ["Revenue 1,234"])]
    cov = coverage(pages, "", [_table([["Revenue", "1,234"]])])
    assert cov.token_recall == 1.0


# ------------------------------------------------------------------- arithmetic
def test_arithmetic_accepts_a_total_that_adds_up():
    rows = [["계정", "2023"], ["매출액", "600"], ["영업이익", "400"], ["합계", "1,000"]]
    report = check_arithmetic(rows)
    assert report.checks == 1
    assert report.passed == 1
    assert report.score == 1.0


def test_arithmetic_catches_a_shifted_cell():
    """The signal that needs no ground truth: the document contradicts itself."""
    rows = [["계정", "2023"], ["매출액", "600"], ["영업이익", "400"], ["합계", "1,400"]]
    report = check_arithmetic(rows)
    assert report.checks == 1
    assert report.passed == 0
    assert "1400" in report.failures[0].replace(",", "")


def test_arithmetic_reports_nothing_to_check_rather_than_a_failure():
    """A table with no total row is not a failed check — it is no check."""
    rows = [["Account", "2023"], ["Revenue", "600"], ["Cost", "400"]]
    report = check_arithmetic(rows)
    assert report.checks == 0
    assert report.score is None


def test_arithmetic_accepts_a_subtotal_block():
    """A grand total that sums only the block since the last subtotal still adds
    up; policing layout would flag correct tables."""
    rows = [["항목", "금액"],
            ["A", "100"], ["B", "200"], ["소계", "300"],
            ["C", "400"], ["D", "500"], ["합계", "900"]]
    report = check_arithmetic(rows)
    assert report.checks == 2
    assert report.passed == 2


def test_arithmetic_tolerates_rounding():
    rows = [["항목", "금액"], ["A", "333"], ["B", "333"], ["C", "334"], ["Total", "1,000"]]
    assert check_arithmetic(rows).passed == 1


def test_arithmetic_does_not_read_a_label_that_merely_contains_a_total_word():
    """'계정' contains '계'. Matching on substring reads the header as a total."""
    rows = [["계정", "2023"], ["매출액", "600"], ["영업이익", "400"]]
    assert check_arithmetic(rows).checks == 0


def test_check_tables_arithmetic_aggregates_and_names_the_table():
    good = _table([["항목", "금액"], ["A", "1"], ["B", "2"], ["합계", "3"]])
    bad = _table([["항목", "금액"], ["A", "1"], ["B", "2"], ["합계", "99"]])
    report = check_tables_arithmetic([good, bad])
    assert (report.checks, report.passed) == (2, 1)
    assert report.failures[0].startswith("table 1:")


# ----------------------------------------------------------------- the verdict
def test_verdict_passes_a_clean_document():
    pages = [_page(1, ["Revenue 600", "Cost 400", "Total 1,000"])]
    tables = [_table([["Account", "2023"], ["Revenue", "600"],
                      ["Cost", "400"], ["Total", "1,000"]])]
    report = validate(pages, "Revenue 600 Cost 400 Total 1,000", tables)
    assert report.verdict == "pass"
    assert report.reasons == []
    assert report.ok


def test_verdict_fails_a_document_with_no_text_layer():
    """The scanned-PDF case: an empty result has to be loud, not clean."""
    report = validate([_page(1, [])], "", [])
    assert report.verdict == "fail"
    assert "no text layer" in report.reasons[0]


def test_verdict_fails_when_content_was_lost_wholesale():
    pages = [_page(1, [f"line {i} with words {i}00" for i in range(20)])]
    report = validate(pages, "line 0 with words 000", [])
    assert report.verdict == "fail"
    assert "content was lost" in report.reasons[0]


def test_verdict_reviews_a_table_that_does_not_add_up():
    pages = [_page(1, ["Revenue 600", "Cost 400", "Total 9,999"])]
    tables = [_table([["Account", "2023"], ["Revenue", "600"],
                      ["Cost", "400"], ["Total", "9,999"]])]
    report = validate(pages, "Revenue 600 Cost 400 Total 9,999", tables)
    assert report.verdict == "review"
    assert "do not add up" in report.reasons[0]


def test_verdict_reviews_a_flagged_table():
    pages = [_page(1, ["Revenue 600"])]
    table = _table([["Revenue", "600"]])
    table.needs_review = True
    report = validate(pages, "Revenue 600", [table])
    assert report.verdict == "review"
    assert "below the confidence threshold" in report.reasons[0]


def test_decide_reruns_the_verdict_when_a_late_signal_arrives():
    """Cross-source and consensus results arrive after the parse; attaching one
    has to be able to change the verdict without re-reading the PDF."""
    pages = [_page(1, ["Revenue 600"])]
    report = validate(pages, "Revenue 600", [_table([["Revenue", "600"]])])
    assert report.verdict == "pass"

    report.crosscheck = {"conflicts": [{"account": "매출액"}]}
    decide(report)
    assert report.verdict == "review"
    assert "contradict data" in report.reasons[0]


def test_agreement_below_threshold_sends_a_document_to_review():
    pages = [_page(1, ["Revenue 600"])]
    report = validate(pages, "Revenue 600", [_table([["Revenue", "600"]])],
                      agreement=0.4)
    assert report.verdict == "review"
    assert "independent detectors agree" in report.reasons[0]


def test_thresholds_are_configurable():
    pages = [_page(1, [f"line {i} value {i}00" for i in range(10)])]
    text = " ".join(f"line {i} value {i}00" for i in range(9))   # ~90% kept
    strict = validate(pages, text, [], thresholds=Thresholds(coverage_fail=0.95))
    lenient = validate(pages, text, [], thresholds=Thresholds(coverage_min=0.5,
                                                             coverage_fail=0.3))
    assert strict.verdict == "fail"
    assert lenient.verdict == "pass"


def test_report_serializes_for_storage():
    pages = [_page(1, ["Revenue 600"])]
    payload = validate(pages, "Revenue 600", [_table([["Revenue", "600"]])]).to_dict()
    assert payload["verdict"] == "pass"
    assert payload["coverage"]["token_recall"] == 1.0
    assert payload["arithmetic"]["checks"] == 0


def test_coverage_is_unknown_rather_than_perfect_when_there_is_nothing_to_measure():
    """A document with no text layer has not recalled 100% — it has nothing to
    recall, and storing 1.0 next to a FAIL reads as though the parse went fine."""
    report = validate([_page(1, [])], "", [])
    assert report.coverage.token_recall is None
    assert report.coverage.numeric_recall is None
    assert report.to_dict()["coverage"]["token_recall"] is None
    assert report.verdict == "fail"


def test_prose_without_numbers_does_not_report_a_numeric_recall():
    pages = [_page(1, ["the company delivered record revenue"])]
    cov = validate(pages, "the company delivered record revenue", []).coverage
    assert cov.token_recall == 1.0
    assert cov.numeric_recall is None
