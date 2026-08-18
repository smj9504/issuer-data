"""Registry mapping (market, data_type) -> default source, and source -> collector.

Collectors are imported lazily so a missing optional dependency or API key only
affects the sources that need it.
"""

from __future__ import annotations

from ..config import Settings
from ..logging import get_logger
from .base import BaseCollector

log = get_logger(__name__)

MARKETS = ("KR", "HK", "US")
DATA_TYPES = ("master", "prices", "financials", "filings")

# Default source per (market, data_type).
# NOTE: KRX's data portal now requires a (free) member login (KRX_ID/KRX_PW) for
# its JSON endpoints, so pykrx returns nothing anonymously. To keep KR working
# out-of-the-box we default KR master/prices to yfinance (Yahoo, no account);
# pass `--source krx` to use pykrx once KRX credentials are set.
DEFAULTS: dict[tuple[str, str], str] = {
    ("KR", "master"): "yfinance",
    ("KR", "prices"): "yfinance",
    ("KR", "financials"): "dart",
    ("KR", "filings"): "dart",
    ("HK", "master"): "hkexnews",
    ("HK", "prices"): "yfinance",
    ("HK", "financials"): "fmp",
    ("HK", "filings"): "hkexnews",
    ("US", "master"): "edgar",
    ("US", "prices"): "yfinance",
    ("US", "financials"): "edgar",
    ("US", "filings"): "edgar",
}

# Extension-B coverage defaults. yfinance (actions) and edgar (US insiders) work
# with no key; the rest default to FMP/DART and skip cleanly without credentials.
for _m in MARKETS:
    DEFAULTS[(_m, "actions")] = "yfinance"
    DEFAULTS[(_m, "metrics")] = "fmp"
    DEFAULTS[(_m, "ratios")] = "fmp"
    DEFAULTS[(_m, "analyst")] = "fmp"
    DEFAULTS[(_m, "earnings")] = "fmp"
    DEFAULTS[(_m, "news")] = "fmp"
    DEFAULTS[(_m, "esg")] = "fmp"
DEFAULTS[("US", "insiders")] = "edgar"
DEFAULTS[("US", "institutional")] = "fmp"
DEFAULTS[("US", "index")] = "fmp"
DEFAULTS[("KR", "insiders")] = "dart"
DEFAULTS[("KR", "ownership")] = "dart"
# NOTE: US ownership (13D/G significant-holder) has no clean free structured source
# (EDGAR 13D/G is unstructured filing text; FMP has no free endpoint), so it is
# deliberately left with no default — `--type ownership --market us` skips cleanly.
# KR ownership is served by DART's majorstock (5%+) disclosures above.

ALL_SOURCES = ("krx", "dart", "hkexnews", "edgar", "yfinance", "fmp", "alphavantage")


def default_source(market: str, data_type: str) -> str | None:
    return DEFAULTS.get((market.upper(), data_type))


def build_collector(source: str, settings: Settings) -> BaseCollector:
    """Instantiate a collector by source name (lazy import)."""
    source = source.lower()
    if source == "edgar":
        from .us_edgar import EdgarCollector

        return EdgarCollector(settings)
    if source == "krx":
        from .kr_krx import KrxCollector

        return KrxCollector(settings)
    if source == "dart":
        from .kr_dart import DartCollector

        return DartCollector(settings)
    if source == "hkexnews":
        from .hk_hkexnews import HkexNewsCollector

        return HkexNewsCollector(settings)
    if source == "yfinance":
        from .yf import YFinanceCollector

        return YFinanceCollector(settings)
    if source == "fmp":
        from .fmp import FmpCollector

        return FmpCollector(settings)
    if source == "alphavantage":
        from .alpha_vantage import AlphaVantageCollector

        return AlphaVantageCollector(settings)
    raise ValueError(f"Unknown source: {source}")
