"""Financial Modeling Prep (FMP) collector: global master, prices, financials, filings, peers.

Standalone REST client (financialmodelingprep.com) using an API key from config —
NOT the MCP server, so the project stays reusable. Free tier is limited
(~250 req/day, some endpoints US-only).
"""

from __future__ import annotations

from ..config import Settings
from ..http.client import HttpClient
from ..logging import get_logger
from ..models import Filing, FinancialFact, FxRate, Price, SecurityRecord
from ..utils.dates import default_range, to_iso
from .base import BaseCollector, NotSupportedError

log = get_logger(__name__)

_BASE = "https://financialmodelingprep.com/api"

# statement -> (endpoint, our statement_type, key accounts we keep as-is [all kept])
_STATEMENTS = [
    ("income-statement", "IS"),
    ("balance-sheet-statement", "BS"),
    ("cash-flow-statement", "CF"),
]
# metadata keys in a statement record that are not financial line items
_META_KEYS = {
    "date", "symbol", "reportedCurrency", "cik", "fillingDate", "filingDate",
    "acceptedDate", "calendarYear", "period", "link", "finalLink",
}


def fmp_symbol(market: str, symbol: str) -> str:
    """Map a market symbol to FMP's ticker form (Yahoo-like suffixes)."""
    market = market.upper()
    s = symbol.upper()
    if market == "KR":
        return s if s.endswith((".KS", ".KQ")) else f"{s.zfill(6)}.KS"
    if market == "HK":
        if s.endswith(".HK"):
            return s
        digits = "".join(ch for ch in s if ch.isdigit())
        return f"{digits.lstrip('0').zfill(4)}.HK" if digits else s
    return s


class FmpCollector(BaseCollector):
    market = "multi"
    source = "fmp"
    _current_market: str = "US"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.api_key = settings.fmp_api_key
        if not self.api_key:
            raise NotSupportedError(
                "FMP requires ISSUER_FMP_API_KEY (free tier at financialmodelingprep.com)"
            )
        self.client = HttpClient(rate_limit=settings.fmp_rate_limit)

    def _get(self, path: str, **params):
        params["apikey"] = self.api_key
        return self.client.get_json(f"{_BASE}/{path}", params=params)

    def _sym(self, symbol: str) -> str:
        return fmp_symbol(self._current_market, symbol)

    # --------------------------------------------------------------- master
    def fetch_master(self, symbols: list[str] | None = None) -> list[SecurityRecord]:
        if not symbols:
            raise NotSupportedError("FMP master requires explicit --symbols")
        out: list[SecurityRecord] = []
        for symbol in symbols:
            data = self._get(f"v3/profile/{self._sym(symbol)}")
            if not data:
                continue
            p = data[0]
            out.append(
                SecurityRecord(
                    market=self._current_market,
                    symbol=symbol,
                    name=p.get("companyName"),
                    country=p.get("country"),
                    sector=p.get("sector"),
                    industry=p.get("industry"),
                    isin=p.get("isin"),
                    cik=_pad_cik(p.get("cik")),
                    currency=p.get("currency"),
                    exchange=p.get("exchangeShortName"),
                    website=p.get("website"),
                    security_type="COMMON",
                    is_primary=True,
                    source="fmp",
                )
            )
        return out

    # --------------------------------------------------------------- prices
    def fetch_prices(self, symbol: str, start: str, end: str) -> list[Price]:
        start, end = default_range(start, end)
        data = self._get(f"v3/historical-price-full/{self._sym(symbol)}", **{"from": start, "to": end})
        hist = data.get("historical") if isinstance(data, dict) else None
        if not hist:
            return []
        out: list[Price] = []
        for row in hist:
            out.append(
                Price(
                    symbol=symbol,
                    market=self._current_market,
                    trade_date=row.get("date"),
                    open=row.get("open"),
                    high=row.get("high"),
                    low=row.get("low"),
                    close=row.get("close"),
                    volume=_int(row.get("volume")),
                    adj_close=row.get("adjClose"),
                    currency=_ccy(self._current_market),
                    source="fmp",
                )
            )
        return out

    # ------------------------------------------------------------ financials
    def fetch_financials(self, symbol: str, years: int | None = None) -> list[FinancialFact]:
        limit = years or 5
        out: list[FinancialFact] = []
        for endpoint, sttype in _STATEMENTS:
            for period_kind in ("annual", "quarter"):
                data = self._get(f"v3/{endpoint}/{self._sym(symbol)}",
                                 period=period_kind, limit=limit if period_kind == "annual" else limit * 4)
                if not isinstance(data, list):
                    continue
                for rec in data:
                    year = _int(rec.get("calendarYear"))
                    if year is None:
                        continue
                    period = _period(rec.get("period"))
                    currency = rec.get("reportedCurrency")
                    period_end = to_iso(rec.get("date"))
                    for account, value in rec.items():
                        if account in _META_KEYS or not isinstance(value, (int, float)):
                            continue
                        out.append(
                            FinancialFact(
                                symbol=symbol,
                                market=self._current_market,
                                fiscal_year=year,
                                fiscal_period=period,
                                statement_type=sttype,
                                account=account,
                                value=float(value),
                                currency=currency,
                                period_end=period_end,
                                source="fmp",
                            )
                        )
        return out

    # --------------------------------------------------------------- filings
    def fetch_filings(self, symbol: str, start: str, end: str) -> list[Filing]:
        start, end = default_range(start, end, default_years=2)
        data = self._get(f"v3/sec_filings/{self._sym(symbol)}", limit=100)
        if not isinstance(data, list):
            return []
        out: list[Filing] = []
        for rec in data:
            filed = to_iso(rec.get("fillingDate") or rec.get("filingDate") or rec.get("acceptedDate"))
            if filed and not (start <= filed <= end):
                continue
            link = rec.get("finalLink") or rec.get("link")
            out.append(
                Filing(
                    symbol=symbol,
                    market=self._current_market,
                    filing_id=str(rec.get("accessionNumber") or rec.get("fillingDate") or link),
                    filed_date=filed,
                    filing_type=rec.get("type") or rec.get("form"),
                    title=rec.get("type") or rec.get("form"),
                    url=link,
                    source="fmp",
                    doc_urls=[link] if link else [],
                )
            )
        return out

    # ----------------------------------------------------------------- peers
    def fetch_peers(self, symbol: str) -> list[str]:
        data = self._get("v4/stock_peers", symbol=self._sym(symbol))
        if isinstance(data, list) and data:
            return list(data[0].get("peersList", []))
        return []

    # -------------------------------------------------------------------- fx
    def fetch_fx(self, pairs: list[tuple[str, str]], start: str, end: str) -> list[FxRate]:
        start, end = default_range(start, end)
        out: list[FxRate] = []
        for base, quote in pairs:
            pair = f"{base}{quote}"
            data = self._get(f"v3/historical-price-full/{pair}", **{"from": start, "to": end})
            hist = data.get("historical") if isinstance(data, dict) else None
            if not hist:
                continue
            for row in hist:
                if row.get("close") is None:
                    continue
                out.append(
                    FxRate(rate_date=row.get("date"), base_ccy=base, quote_ccy=quote,
                           rate=float(row["close"]), source="fmp")
                )
        return out


def _period(p) -> str:
    p = str(p or "FY").upper()
    return "FY" if p in ("FY", "ANNUAL", "") else p


def _ccy(market: str) -> str:
    return {"KR": "KRW", "HK": "HKD", "US": "USD"}.get(market.upper(), "USD")


def _pad_cik(v):
    if not v:
        return None
    try:
        return str(int(v)).zfill(10)
    except (TypeError, ValueError):
        return str(v)


def _int(v):
    try:
        return int(v) if v is not None and str(v) != "" else None
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None
