"""Cross-market prices via the Yahoo Finance chart API.

Implemented against the public chart endpoint using our proxy-aware ``requests``
HTTP client rather than the ``yfinance`` library, because the library's
``curl_cffi`` transport (TLS impersonation) fails through a MITM proxy. Covers
US, Korea (.KS/.KQ), and Hong Kong (.HK) OHLCV.
"""

from __future__ import annotations

import datetime as _dt

from ..config import Settings
from ..http.client import HttpClient
from ..logging import get_logger
from ..models import Price, SecurityRecord
from ..utils.dates import default_range
from .base import BaseCollector, NotSupportedError

log = get_logger(__name__)

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)
_CCY = {"KR": "KRW", "HK": "HKD", "US": "USD"}


def yahoo_symbols(market: str, symbol: str) -> list[str]:
    """Map a market symbol to candidate Yahoo tickers (try in order)."""
    market = market.upper()
    s = symbol.upper()
    if market == "US":
        return [s]
    if market == "KR":
        if s.endswith((".KS", ".KQ")):
            return [s]
        code = s.zfill(6)
        return [f"{code}.KS", f"{code}.KQ"]  # KOSPI first, then KOSDAQ
    if market == "HK":
        if s.endswith(".HK"):
            return [s]
        digits = "".join(ch for ch in s if ch.isdigit())
        code = digits.lstrip("0").zfill(4) if digits else s
        return [f"{code}.HK"]
    return [s]


def _to_epoch(iso_date: str) -> int:
    return int(_dt.datetime.strptime(iso_date, "%Y-%m-%d").replace(tzinfo=_dt.timezone.utc).timestamp())


class YFinanceCollector(BaseCollector):
    market = "multi"
    source = "yfinance"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = HttpClient(rate_limit=settings.default_rate_limit, headers={"User-Agent": _UA})

    def _fetch_chart(self, market: str, symbol: str, start: str, end: str) -> tuple[str, dict] | None:
        params = {
            "period1": _to_epoch(start),
            "period2": _to_epoch(end) + 86400,
            "interval": "1d",
            "events": "div,splits",
        }
        for ysym in yahoo_symbols(market, symbol):
            try:
                data = self.client.get_json(CHART_URL.format(symbol=ysym), params=params)
            except Exception as exc:  # noqa: BLE001
                log.debug("yahoo chart %s failed: %s", ysym, exc)
                continue
            result = (data.get("chart") or {}).get("result")
            if result and result[0].get("timestamp"):
                return ysym, result[0]
        return None

    # --------------------------------------------------------------- prices
    def fetch_prices(self, symbol: str, start: str, end: str) -> list[Price]:
        market = _market_for(symbol, self._current_market)
        start, end = default_range(start, end)
        fetched = self._fetch_chart(market, symbol, start, end)
        if not fetched:
            return []
        _ysym, res = fetched
        ts = res["timestamp"]
        meta = res.get("meta", {})
        currency = meta.get("currency") or _CCY.get(market)
        quote = (res.get("indicators", {}).get("quote") or [{}])[0]
        adj = (res.get("indicators", {}).get("adjclose") or [{}])
        adjclose = adj[0].get("adjclose") if adj and adj[0] else None
        opens, highs = quote.get("open", []), quote.get("high", [])
        lows, closes, vols = quote.get("low", []), quote.get("close", []), quote.get("volume", [])
        out: list[Price] = []
        for i, t in enumerate(ts):
            close = _at(closes, i)
            if close is None:
                continue
            trade_date = _dt.datetime.fromtimestamp(t, _dt.timezone.utc).strftime("%Y-%m-%d")
            out.append(
                Price(
                    symbol=symbol,
                    market=market,
                    trade_date=trade_date,
                    open=_at(opens, i),
                    high=_at(highs, i),
                    low=_at(lows, i),
                    close=close,
                    volume=int(_at(vols, i)) if _at(vols, i) is not None else None,
                    adj_close=_at(adjclose, i) if adjclose else None,
                    currency=currency,
                    source="yfinance",
                )
            )
        return out

    # --------------------------------------------------------------- master
    def fetch_master(self, symbols: list[str] | None = None) -> list[SecurityRecord]:
        if not symbols:
            raise NotSupportedError("yfinance master requires explicit --symbols")
        out: list[SecurityRecord] = []
        for symbol in symbols:
            market = _market_for(symbol, self._current_market)
            fetched = self._fetch_chart(market, symbol, *default_range(None, None, 1))
            if not fetched:
                continue
            ysym, res = fetched
            meta = res.get("meta", {})
            out.append(
                SecurityRecord(
                    market=market,
                    symbol=symbol,
                    name=meta.get("longName") or meta.get("shortName") or symbol,
                    country=market if market != "US" else "US",
                    exchange=meta.get("fullExchangeName") or meta.get("exchangeName"),
                    currency=meta.get("currency") or _CCY.get(market),
                    security_type="COMMON",
                    is_primary=True,
                    source="yfinance",
                )
            )
        return out

    # yfinance library fundamentals need its curl_cffi transport (blocked here).
    def fetch_financials(self, symbol: str, years: int | None = None):
        raise NotSupportedError(
            "yfinance financials unavailable in this environment; use fmp/edgar/dart"
        )

    # market context set by the orchestrator via attribute before each call
    _current_market: str = "US"


def _at(seq, i):
    try:
        v = seq[i]
    except (IndexError, TypeError):
        return None
    if v is None:
        return None
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _market_for(symbol: str, default: str) -> str:
    s = symbol.upper()
    if s.endswith((".KS", ".KQ")):
        return "KR"
    if s.endswith(".HK"):
        return "HK"
    return default
