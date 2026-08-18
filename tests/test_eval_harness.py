"""End-to-end eval-harness tests (reportlab-gated)."""

import pytest

from issuer_data.eval.gold import synthetic_cases
from issuer_data.eval.harness import run_eval


def test_harness_runs_and_scores_synthetic_matrix():
    pytest.importorskip("reportlab")
    cases = synthetic_cases()
    assert cases, "expected synthetic cases with reportlab installed"
    report = run_eval(cases)
    # every metric in range; the US split-table case exercises cross-page stitching
    for r in report["cases"]:
        for k in ("teds", "grits", "numeric_em", "continuity"):
            assert r[k] is None or 0.0 <= r[k] <= 1.0
    assert "US/en/split-table" in report["categories"]
    o = report["overall"]
    assert o["teds"] is not None and 0.0 <= o["teds"] <= 1.0
    assert report["escalated_total"] == 0  # no escalator → nothing escalated


class _NoopEscalator:
    name = "noop"

    def escalate(self, rows, context_text, col_count):
        return None  # declines → tables keep local rows but still count as attempts


def test_harness_reports_cost_when_escalating():
    pytest.importorskip("reportlab")
    cases = synthetic_cases()
    # threshold above 1.0 forces every (perfectly-grounded) table to escalate,
    # so the harness demonstrably accounts escalated-count and cost.
    report = run_eval(cases, escalator=_NoopEscalator(), threshold=1.1,
                      cost_per_page=0.02)
    assert report["escalated_total"] > 0
    assert report["est_cost_total"] > 0
