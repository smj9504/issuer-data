"""DART 공시유형(kind) must be selectable, and must not leak to other sources.

fetch_filings was pinned to kind="A" (정기공시), so 지분공시(D, 주식등의대량보유상황
보고서) and 주요사항보고(B, 자기주식처분결정) could not be collected at all — they are
absent from an A listing, so no downstream filter could recover them.
"""

import pytest

from issuer_data.collectors.base import NotSupportedError
from issuer_data.collectors.kr_dart import _FILING_KINDS
from issuer_data.orchestrator import _fetch_filings


class _DartLike:
    """A collector whose fetch_filings accepts `kind`, as kr_dart's does."""

    source = "dart"

    def __init__(self):
        self.seen = []

    def fetch_filings(self, symbol, start, end, kind=None):
        self.seen.append(kind)
        return []


class _EdgarLike:
    """A collector with no filing-kind concept."""

    source = "edgar"

    def __init__(self):
        self.calls = 0

    def fetch_filings(self, symbol, start, end):
        self.calls += 1
        return []


def test_kind_is_passed_through_to_a_dart_like_collector():
    c = _DartLike()
    _fetch_filings(c, "005930", "2026-01-01", "2026-02-01", "D")
    assert c.seen == ["D"]


def test_no_kind_leaves_the_collector_default_untouched():
    """Omitting the flag must not start passing kind=None into every collector."""
    c = _EdgarLike()
    _fetch_filings(c, "AAPL", "2026-01-01", "2026-02-01", None)
    assert c.calls == 1  # called via the plain signature, no TypeError


def test_kind_on_a_source_that_has_none_is_a_clean_skip():
    """--dart-kind against EDGAR is a skipped run, not a TypeError mid-collection."""
    c = _EdgarLike()
    with pytest.raises(NotSupportedError, match="no filing-kind concept"):
        _fetch_filings(c, "AAPL", "2026-01-01", "2026-02-01", "D")


def test_every_dart_kind_is_accepted():
    c = _DartLike()
    for kind in _FILING_KINDS:
        _fetch_filings(c, "005930", "2026-01-01", "2026-02-01", kind)
    assert c.seen == list(_FILING_KINDS)


def test_kind_dictionary_covers_the_documented_dart_codes():
    """A..J are DART's 공시유형; a gap here silently makes a category unreachable."""
    assert set(_FILING_KINDS) == set("ABCDEFGHIJ")
    assert _FILING_KINDS["D"] == "지분공시"
    assert _FILING_KINDS["B"] == "주요사항보고"


def test_unknown_kind_is_rejected_with_the_valid_options(monkeypatch):
    """A typo must fail loudly rather than silently returning 정기공시."""
    from issuer_data.collectors import kr_dart

    collector = kr_dart.DartCollector.__new__(kr_dart.DartCollector)
    with pytest.raises(ValueError, match="Unknown DART filing kind"):
        collector.fetch_filings("005930", "2026-01-01", "2026-02-01", kind="Z")
