import pytest

from issuer_data.utils.symbols import normalize_symbol


@pytest.mark.parametrize("market,raw,expected", [
    ("US", "aapl", "AAPL"),
    ("US", "BRK.B", "BRK.B"),
    ("KR", "005930", "005930"),
    ("KR", "5930", "005930"),
    ("KR", "005930.KS", "005930"),
    ("KR", "035720.KQ", "035720"),
    ("HK", "0700.HK", "00700"),
    ("HK", "00700", "00700"),
    ("HK", "700", "00700"),
    ("HK", "9988", "09988"),
])
def test_normalize_symbol(market, raw, expected):
    assert normalize_symbol(market, raw) == expected
