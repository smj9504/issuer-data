"""Tests for cross-source validation against independently collected financials."""

import pytest

from issuer_data.crosscheck import crosscheck, load_facts
from issuer_data.pdf_extract import StitchedTable


@pytest.fixture()
def company(conn):
    conn.execute("INSERT INTO companies (company_id, name, source) VALUES (1,'T','x')")
    return 1


def _fact(conn, account, value, source="fmp", year=2023):
    conn.execute(
        "INSERT INTO financials (company_id, fiscal_year, fiscal_period, fs_scope, "
        "statement_type, account, account_local, value, source) "
        "VALUES (1,?,'FY','CFS','IS',?,?,?,?)", (year, account, account, value, source))


def _table(rows):
    return StitchedTable(rows=rows, page_start=1, page_end=1)


def test_a_figure_that_matches_at_a_unit_scale_is_confirmed(conn, company):
    """The filing prints 백만원; the API stores 원. Raw equality would fail on
    every correct figure."""
    _fact(conn, "매출액", 1_234_000_000.0)
    report = crosscheck(conn, 1, [_table([["계정", "2023"], ["매출액", "1,234"]])])
    assert len(report.confirmed) == 1
    assert report.confirmed[0].scale == 1e6
    assert report.score == 1.0


def test_a_labelled_row_whose_numbers_disagree_is_a_conflict(conn, company):
    """The strong signal: the document has this line and none of its cells agree."""
    _fact(conn, "당기순이익", 1_000_000_000.0)
    report = crosscheck(conn, 1, [_table([["계정", "2023"], ["당기순이익", "321"]])])
    assert len(report.conflicts) == 1
    assert report.score == 0.0


def test_a_figure_the_document_never_mentions_is_not_an_error(conn, company):
    """An interim report does not carry every annual line item. Counting absence
    as error would drown the signal, so unmatched facts stay out of the score."""
    _fact(conn, "이연법인세자산", 77_000_000.0)
    report = crosscheck(conn, 1, [_table([["계정", "2023"], ["매출액", "1,234"]])])
    assert len(report.unmatched) == 1
    assert report.score is None          # nothing was decided either way


def test_the_filings_own_source_can_be_excluded(conn, company):
    """A filing checked against figures parsed out of that same filing proves
    nothing, so the caller can exclude it."""
    _fact(conn, "매출액", 1_234_000_000.0, source="dart")
    facts = load_facts(conn, 1, exclude_sources=["dart"])
    assert facts == []
    report = crosscheck(conn, 1, [_table([["매출액", "1,234"]])], exclude_sources=["dart"])
    assert report.checks == []


def test_rounding_to_the_printed_unit_still_matches(conn, company):
    _fact(conn, "매출액", 1_234_567_890.0)
    report = crosscheck(conn, 1, [_table([["계정", "2023"], ["매출액", "1,235"]])])
    assert len(report.confirmed) == 1


def test_the_report_serializes_with_its_conflicts(conn, company):
    _fact(conn, "매출액", 1_234_000_000.0)
    _fact(conn, "영업이익", 500_000_000.0)
    report = crosscheck(conn, 1, [_table([["계정", "2023"], ["매출액", "1,234"],
                                          ["영업이익", "999"]])])
    payload = report.to_dict()
    assert payload["confirmed"] == 1
    assert len(payload["conflicts"]) == 1
    assert payload["conflicts"][0]["account"] == "영업이익"


def test_no_stored_facts_means_no_opinion(conn, company):
    report = crosscheck(conn, 1, [_table([["매출액", "1,234"]])])
    assert report.checks == [] and report.score is None
