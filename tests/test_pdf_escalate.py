"""Tests for the config-gated PDF escalation framework."""

from issuer_data.config import Settings
from issuer_data.pdf_escalate import (
    NullEscalator,
    TextReconstructionEscalator,
    _parse_rows,
    build_escalator,
    choose_escalations,
)
from issuer_data.pdf_extract import StitchedTable, apply_escalation


def _tbl(conf):
    return StitchedTable(rows=[["A", "1"]], page_start=1, page_end=1, confidence=conf)


def test_choose_escalations():
    tables = [_tbl(0.9), _tbl(0.4), _tbl(0.65)]
    assert choose_escalations(tables, 0.66) == [1, 2]


# --------------------------------------------------------------- build_escalator
def test_build_escalator_defaults_to_null():
    assert isinstance(build_escalator(Settings()), NullEscalator)


def test_build_escalator_local_only_blocks_egress():
    s = Settings(pdf_escalation_enabled=True, pdf_local_only=True,
                 escalation_api_key="k")
    assert isinstance(build_escalator(s), NullEscalator)  # local_only wins


def test_build_escalator_needs_key():
    s = Settings(pdf_escalation_enabled=True, pdf_local_only=False)
    assert isinstance(build_escalator(s), NullEscalator)  # no key → stay local


def test_build_escalator_real_when_fully_opted_in():
    s = Settings(pdf_escalation_enabled=True, pdf_local_only=False,
                 escalation_api_key="k")
    assert isinstance(build_escalator(s), TextReconstructionEscalator)


# ----------------------------------------------------------------- _parse_rows
def test_parse_rows_tolerates_fences_and_prose():
    text = 'Here is the table:\n```json\n{"rows": [["Revenue", "100"], ["Cost", "60"]]}\n```'
    assert _parse_rows(text) == [["Revenue", "100"], ["Cost", "60"]]


def test_parse_rows_rejects_malformed():
    assert _parse_rows("no json here") is None
    assert _parse_rows('{"notrows": 1}') is None
    assert _parse_rows('{"rows": "nope"}') is None


# --------------------------------------------------- apply_escalation wiring
class _FakeEscalator:
    """Returns a clean table whose numbers ARE in the page text (so it re-grounds)."""

    name = "fake"

    def __init__(self):
        self.calls = 0

    def escalate(self, rows, context_text, col_count):
        self.calls += 1
        return [["Account", "Value"], ["Revenue", "100"]]


def test_low_confidence_flagged_without_escalator():
    raw = "Revenue 100 Cost 60"
    tables = [StitchedTable(rows=[["Revenue", "100"]], page_start=1, page_end=1,
                            confidence=0.4)]
    escalated, cost = apply_escalation(tables, {1: raw}, raw, escalator=NullEscalator(),
                                       threshold=0.66, cost_per_page=0.02)
    assert escalated == 0 and cost == 0.0
    assert tables[0].needs_review is True     # flagged, but nothing left the box


def test_escalator_upgrades_low_confidence_table():
    raw = "Account Value Revenue 100 Cost 60"
    tables = [StitchedTable(rows=[["Rev", "??"]], page_start=1, page_end=1, confidence=0.3)]
    esc = _FakeEscalator()
    escalated, cost = apply_escalation(tables, {1: raw}, raw, escalator=esc,
                                       threshold=0.66, cost_per_page=0.02)
    assert esc.calls == 1
    assert escalated == 1 and cost == 0.02
    t = tables[0]
    assert t.source_engine == "fake"
    assert t.rows == [["Account", "Value"], ["Revenue", "100"]]
    assert t.confidence == 1.0 and t.needs_review is False  # re-grounded, flag cleared


def test_escalator_output_still_ungrounded_stays_flagged():
    raw = "Revenue 100"                        # 777 is absent → escalation didn't help
    tables = [StitchedTable(rows=[["Rev", "??"]], page_start=1, page_end=1, confidence=0.3)]

    class _Bad:
        name = "bad"

        def escalate(self, rows, context_text, col_count):
            return [["Revenue", "777"]]

    apply_escalation(tables, {1: raw}, raw, escalator=_Bad(), threshold=0.66,
                     cost_per_page=0.0)
    assert tables[0].needs_review is True      # ungrounded output keeps the review flag
