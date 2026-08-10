import pytest

from issuer_data.models import Filing
from issuer_data.orchestrator import _filter_filing_types


def _filing(filing_type):
    return Filing(symbol="X", market="US", filing_id="1", filing_type=filing_type, source="test")


@pytest.mark.parametrize("filing_type,wanted,expected", [
    ("8-K", ["8-K"], True),
    ("10-K", ["8-K"], False),
    ("8-K/A", ["8-K"], True),                          # substring catches amendments
    ("분기보고서 (2024.09)", ["분기보고서"], True),        # DART free-text report name
    ("Annual Report - results announcement", ["annual report"], True),  # HKEXnews long text, case-insensitive
    (None, ["8-K"], False),                            # missing filing_type never matches
])
def test_filter_filing_types_matching(filing_type, wanted, expected):
    filings = [_filing(filing_type)]
    result = _filter_filing_types(filings, wanted)
    assert (result == filings) is expected


def test_filter_filing_types_none_is_noop():
    filings = [_filing("8-K"), _filing("10-K"), _filing(None)]
    assert _filter_filing_types(filings, None) == filings


def test_filter_filing_types_multiple_wanted():
    filings = [_filing("8-K"), _filing("10-K"), _filing("S-1")]
    result = _filter_filing_types(filings, ["8-K", "10-K"])
    assert [f.filing_type for f in result] == ["8-K", "10-K"]
