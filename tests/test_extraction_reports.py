"""Tests for the persisted verdict: the review queue and the audit trail.

The reason this table exists is that a bad parse should leave a record instead of
a quietly empty result, so these tests are mostly about a failure staying visible.
"""

import json

import pytest

from issuer_data.eval.harness import gate_calibration
from issuer_data.pdf_extract import StitchedTable
from issuer_data.pdf_validate import validate
from issuer_data.storage.repository import Repository


@pytest.fixture()
def repo(conn):
    conn.execute("INSERT INTO companies (company_id, name, source) VALUES (1,'T','x')")
    conn.execute("INSERT INTO filings (company_id, filing_id, source) VALUES (1,'F1','dart')")
    return Repository(conn)


def _page(lines):
    return {"page_no": 1, "width": 600.0, "height": 800.0, "tables": [],
            "lines": [{"text": t, "x0": 50.0, "x1": 400.0,
                       "top": 300.0 + i * 20, "bottom": 315.0 + i * 20}
                      for i, t in enumerate(lines)]}


def _clean_report():
    table = StitchedTable(rows=[["Revenue", "600"]], page_start=1, page_end=1)
    return validate([_page(["Revenue 600"])], "Revenue 600", [table])


def _failed_report():
    return validate([_page([])], "", [])


def test_a_verdict_is_stored_with_its_evidence(repo):
    repo.upsert_extraction_report(1, "F1", "dart", 0, _clean_report())
    row = repo.conn.execute("SELECT * FROM filing_extraction_reports").fetchone()
    assert row["verdict"] == "pass"
    assert row["token_recall"] == 1.0
    assert json.loads(row["detail"])["coverage"]["token_recall"] == 1.0


def test_the_review_queue_holds_what_did_not_pass(repo):
    repo.upsert_extraction_report(1, "F1", "dart", 0, _clean_report())
    repo.upsert_extraction_report(1, "F1", "dart", 1, _failed_report())
    queue = repo.extraction_review_queue()
    assert [row["doc_seq"] for row in queue] == [1]
    assert queue[0]["verdict"] == "fail"
    assert "no text layer" in queue[0]["reasons"]


def test_failures_sort_ahead_of_reviews(repo):
    table = StitchedTable(rows=[["Revenue", "600"]], page_start=1, page_end=1)
    table.needs_review = True
    review = validate([_page(["Revenue 600"])], "Revenue 600", [table])
    repo.upsert_extraction_report(1, "F1", "dart", 0, review)
    repo.upsert_extraction_report(1, "F1", "dart", 1, _failed_report())
    assert [row["verdict"] for row in repo.extraction_review_queue()] == ["fail", "review"]


def test_re_extracting_replaces_the_verdict_and_clears_a_sign_off(repo):
    """A human's sign-off applies to the parse they actually looked at, not to
    whatever replaced it."""
    repo.upsert_extraction_report(1, "F1", "dart", 0, _clean_report())
    repo.conn.execute("UPDATE filing_extraction_reports SET reviewed_at = '2026-01-01'")
    repo.upsert_extraction_report(1, "F1", "dart", 0, _failed_report())
    rows = repo.conn.execute("SELECT * FROM filing_extraction_reports").fetchall()
    assert len(rows) == 1
    assert rows[0]["verdict"] == "fail"
    assert rows[0]["reviewed_at"] is None


# ------------------------------------------------------------- gate calibration
def test_calibration_counts_what_the_gate_missed():
    """The number that justifies auditing a sample of PASS documents by hand."""
    cases = [
        {"verdict": "pass", "teds": 1.00},    # passed and good
        {"verdict": "pass", "teds": 0.40},    # passed but wrong — the blind spot
        {"verdict": "fail", "teds": 0.05},    # caught
        {"verdict": "review", "teds": 0.99},  # a false alarm: flagged but fine
    ]
    gate = gate_calibration(cases)
    assert gate["passed_correct"] == 1
    assert gate["missed"] == 1
    assert gate["caught"] == 1
    assert gate["false_alarms"] == 1
    assert gate["recall"] == 0.5           # caught 1 of the 2 bad documents
    assert gate["review_rate"] == 0.5


def test_calibration_says_nothing_without_labels():
    assert gate_calibration([{"verdict": "pass", "teds": None}])["n"] == 0
