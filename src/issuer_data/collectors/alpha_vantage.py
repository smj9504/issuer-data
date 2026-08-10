"""Alpha Vantage collector: prices (TIME_SERIES_DAILY), overview, fundamentals.

Standalone REST client (www.alphavantage.co) using an API key from config. The
free tier is very tight (~5 requests/min, ~25/day) and returns a "Note"/
"Information" message instead of data when throttled — we detect that and warn.
"""

from __future__ import annotations

from ..config import Settings
from ..http.client import HttpClient
from ..logging import get_logger
from ..models import FinancialFact, Price, SecurityRecord
from ..utils.dates import default_range
from .base import BaseCollector, NotSupportedError

log = get_logger(__name__)

_BASE = "https://www.alphavantage.co/query"
_STATEMENTS = [
    ("INCOME_STATEMENT", "IS"),
    ("BALANCE_SHEET", "BS"),
    ("CASH_FLOW", "CF"),
]
_META_KEYS = {"fiscalDateEnding", "reportedCurrency"}


class AlphaVantageCollector(BaseCollector):
    market = "multi"
    source = "alphavantage"
    _current_market: str = "US"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.api_key = settings.alphavantage_api_key
        if not self.api_key:
            raise NotSupportedError(
                "Alpha Vantage requires ISSUER_ALPHAVANTAGE_API_KEY "
                "(free at alphavantage.co/support/#api-key)"
            )
        self.client = HttpClient(rate_limit=settings.alphavantage_rate_limit)

    def _get(self, function: str, **params) -> dict:
        params.update(function=function, apikey=self.api_key)
        data = self.client.get_json(_BASE, params=params)
        if isinstance(data, dict) and ("Note" in data or "Information" in data):
            msg = data.get("Note") or data.get("Information")
            log.warning("Alpha Vantage throttled/limited: %s", str(msg)[:120])
            return {}
        return data if isinstance(data, dict) else {}

    # --------------------------------------------------------------- master
    def fetch_master(self, symbols: list[str] | None = None) -> list[SecurityRecord]:
        if not symbols:
            raise NotSupportedError("Alpha Vantage master requires explicit --symbols")
        out: list[SecurityRecord] = []
        for symbol in symbols:
            d = self._get("OVERVIEW", symbol=symbol)
            if not d or not d.get("Symbol"):
                continue
            out.append(
                SecurityRecord(
                    market=self._current_market,
                    symbol=symbol,
                    name=d.get("Name"),
                    country=d.get("Country"),
                    sector=d.get("Sector"),
                    industry=d.get("Industry"),
                    cik=_pad_cik(d.get("CIK")),
                    currency=d.get("Currency"),
                    exchange=d.get("Exchange"),
                    security_type="COMMON",
                    is_primary=True,
                    source="alphavantage",
                )
            )
        return out

    # --------------------------------------------------------------- prices
    def fetch_prices(self, symbol: str, start: str, end: str) -> list[Price]:
        start, end = default_range(start, end)
        d = self._get("TIME_SERIES_DAILY", symbol=symbol, outputsize="full")
        series = d.get("Time Series (Daily)")
        if not series:
            return []
        out: list[Price] = []
        for date, row in series.items():
            if not (start <= date <= end):
                continue
            out.append(
                Price(
                    symbol=symbol,
                    market=self._current_market,
                    trade_date=date,
                    open=_f(row.get("1. open")),
                    high=_f(row.get("2. high")),
                    low=_f(row.get("3. low")),
                    close=_f(row.get("4. close")),
                    volume=_i(row.get("5. volume")),
                    currency=_ccy(self._current_market),
                    source="alphavantage",
                )
            )
        return out

    # ------------------------------------------------------------ financials
    def fetch_financials(self, symbol: str, years: int | None = None) -> list[FinancialFact]:
        out: list[FinancialFact] = []
        for function, sttype in _STATEMENTS:
            d = self._get(function, symbol=symbol)
            if not d:
                continue
            for key, period in (("annualReports", "FY"), ("quarterlyReports", "Q")):
                for rec in d.get(key, []) or []:
                    end_date = rec.get("fiscalDateEnding", "")
                    year = _i(end_date[:4]) if end_date else None
                    if year is None:
                        continue
                    currency = rec.get("reportedCurrency")
                    for account, value in rec.items():
                        if account in _META_KEYS:
                            continue
                        val = _f(value)
                        if val is None:
                            continue
                        out.append(
                            FinancialFact(
                                symbol=symbol,
                                market=self._current_market,
                                fiscal_year=year,
                                fiscal_period=period,
                                statement_type=sttype,
                                account=account,
                                value=val,
                                currency=currency,
                                period_end=end_date or None,
                                source="alphavantage",
                            )
                        )
        return out

    # -------------------------------------------------------------- news
    def fetch_news(self, symbol: str):
        from ..models import NewsItem

        d = self._get("NEWS_SENTIMENT", tickers=symbol, limit=50)
        out: list[NewsItem] = []
        for item in d.get("feed", []) or []:
            score = item.get("overall_sentiment_score")
            for ts in item.get("ticker_sentiment", []) or []:
                if ts.get("ticker") == symbol:
                    score = ts.get("ticker_sentiment_score")
                    break
            out.append(
                NewsItem(
                    symbol=symbol,
                    market=self._current_market,
                    published_at=_ts(item.get("time_published")) or "",
                    title=item.get("title") or "",
                    url=item.get("url"),
                    sentiment=_f(score),
                    source="alphavantage",
                )
            )
        return out


def _ts(v: str | None) -> str | None:
    """Alpha Vantage timestamps look like '20260809T105936' -> ISO 8601."""
    if not v or len(v) < 15:
        return None
    return f"{v[0:4]}-{v[4:6]}-{v[6:8]}T{v[9:11]}:{v[11:13]}:{v[13:15]}"


def _ccy(market: str) -> str:
    return {"KR": "KRW", "HK": "HKD", "US": "USD"}.get(market.upper(), "USD")


def _f(v):
    try:
        if v in (None, "None", "-", ""):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v):
    f = _f(v)
    return int(f) if f is not None else None


def _pad_cik(v):
    if not v or str(v).lower() == "none":
        return None
    try:
        return str(int(v)).zfill(10)
    except (TypeError, ValueError):
        return str(v)
