"""Financial Modeling Prep (FMP) collector: global master, prices, financials, filings, peers.

Standalone REST client (financialmodelingprep.com) using an API key from config —
NOT the MCP server, so the project stays reusable. Free tier is limited
(~250 req/day, some endpoints US-only).

Uses the "stable" API (financialmodelingprep.com/stable/...). The legacy
/api/v3 and /api/v4 endpoints this used to target were retired 2025-08-31 and
now return 403 for everyone; the stable API takes the symbol as a query param
instead of a path segment and renamed several fields (calendarYear ->
fiscalYear, no more historical/peersList response wrappers, etc).
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from ..config import Settings
from ..http.client import HttpClient
from ..logging import get_logger
from ..models import Filing, FinancialFact, FxRate, Price, SecurityRecord
from ..utils.dates import default_range, to_iso
from .base import BaseCollector, NotSupportedError

log = get_logger(__name__)

_BASE = "https://financialmodelingprep.com/stable"
_ACCESSION_RE = re.compile(r"\d{10}-\d{2}-\d{6}")
# Free-tier statement/ratio endpoints 402 above this, whatever the requested limit.
_FREE_TIER_LIMIT = 5
# Free-tier sec-filings-search 402s once `from` goes back further than this.
_FREE_TIER_LOOKBACK_DAYS = 300

# statement -> (endpoint, our statement_type, key accounts we keep as-is [all kept])
_STATEMENTS = [
    ("income-statement", "IS"),
    ("balance-sheet-statement", "BS"),
    ("cash-flow-statement", "CF"),
]
# metadata keys in a statement record that are not financial line items
_META_KEYS = {
    "date", "symbol", "reportedCurrency", "cik", "fillingDate", "filingDate",
    "acceptedDate", "calendarYear", "fiscalYear", "period", "link", "finalLink",
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
            data = self._get("profile", symbol=self._sym(symbol))
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
                    exchange=p.get("exchange"),
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
        data = self._get(
            "historical-price-eod/full", symbol=self._sym(symbol), **{"from": start, "to": end}
        )
        if not isinstance(data, list):
            return []
        out: list[Price] = []
        for row in data:
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
                data = self._get(
                    endpoint,
                    symbol=self._sym(symbol),
                    period=period_kind,
                    limit=min(limit if period_kind == "annual" else limit * 4, _FREE_TIER_LIMIT),
                )
                if not isinstance(data, list):
                    continue
                for rec in data:
                    year = _int(rec.get("fiscalYear") or rec.get("calendarYear"))
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
        # Free-tier sec-filings-search rejects a `from` older than ~11 months back.
        earliest = (date.today() - timedelta(days=_FREE_TIER_LOOKBACK_DAYS)).isoformat()
        start = max(start, earliest)
        data = self._get(
            "sec-filings-search/symbol", symbol=self._sym(symbol), **{"from": start, "to": end}, limit=10
        )
        if not isinstance(data, list):
            return []
        out: list[Filing] = []
        for rec in data:
            filed = to_iso(rec.get("filingDate") or rec.get("acceptedDate"))
            if filed and not (start <= filed <= end):
                continue
            link = rec.get("finalLink") or rec.get("link")
            accession = _ACCESSION_RE.search(link or "")
            form_type = rec.get("formType")
            out.append(
                Filing(
                    symbol=symbol,
                    market=self._current_market,
                    filing_id=accession.group(0) if accession else str(filed or link),
                    filed_date=filed,
                    filing_type=form_type,
                    title=form_type,
                    url=link,
                    source="fmp",
                    doc_urls=[link] if link else [],
                )
            )
        return out

    # ----------------------------------------------------------------- peers
    def fetch_peers(self, symbol: str) -> list[str]:
        data = self._get("stock-peers", symbol=self._sym(symbol))
        if not isinstance(data, list):
            return []
        return [r["symbol"] for r in data if r.get("symbol")]

    # --------------------------------------------------- Extension B coverage
    def fetch_daily_metrics(self, symbol: str, start: str, end: str):
        from ..models import DailyMetric

        start, end = default_range(start, end)
        # Free-tier historical-market-capitalization rejects any from/to filter;
        # pull the latest window and filter client-side instead.
        data = self._get("historical-market-capitalization", symbol=self._sym(symbol), limit=500)
        out: list = []
        if isinstance(data, list):
            for r in data:
                metric_date = to_iso(r.get("date"))
                if metric_date and not (start <= metric_date <= end):
                    continue
                out.append(DailyMetric(
                    symbol=symbol, market=self._current_market, metric_date=metric_date,
                    market_cap=r.get("marketCap"), currency=_ccy(self._current_market), source="fmp"))
        return out

    def fetch_ratios(self, symbol: str, years: int | None = None):
        from ..models import Ratio

        out: list = []
        for endpoint in ("ratios", "key-metrics"):
            data = self._get(
                endpoint, symbol=self._sym(symbol), period="annual",
                limit=min(years or 5, _FREE_TIER_LIMIT),
            )
            if not isinstance(data, list):
                continue
            for rec in data:
                year = _int(rec.get("fiscalYear") or rec.get("calendarYear")) or _year_of(rec.get("date"))
                if year is None:
                    continue
                for metric, value in rec.items():
                    if metric in ("symbol", "date", "calendarYear", "fiscalYear", "period") \
                            or not isinstance(value, (int, float)):
                        continue
                    out.append(Ratio(symbol=symbol, market=self._current_market,
                                     fiscal_year=year, fiscal_period="FY",
                                     metric=metric, value=float(value), source="fmp"))
        return out

    def fetch_corporate_actions(self, symbol: str, start: str, end: str):
        from ..models import CorporateAction

        out: list = []
        div = self._get("dividends", symbol=self._sym(symbol), limit=_FREE_TIER_LIMIT)
        for r in (div if isinstance(div, list) else []):
            out.append(CorporateAction(symbol=symbol, market=self._current_market,
                                       ex_date=to_iso(r.get("date")), action_type="dividend",
                                       amount=r.get("dividend") or r.get("adjDividend"),
                                       currency=_ccy(self._current_market), source="fmp"))
        spl = self._get("splits", symbol=self._sym(symbol), limit=_FREE_TIER_LIMIT)
        for r in (spl if isinstance(spl, list) else []):
            num, den = r.get("numerator"), r.get("denominator")
            out.append(CorporateAction(symbol=symbol, market=self._current_market,
                                       ex_date=to_iso(r.get("date")), action_type="split",
                                       ratio=(num / den) if num and den else None, source="fmp"))
        return out

    def fetch_analyst(self, symbol: str):
        from ..models import AnalystEstimate

        data = self._get("analyst-estimates", symbol=self._sym(symbol), period="annual", limit=10)
        out: list = []
        for r in (data if isinstance(data, list) else []):
            year = _year_of(r.get("date"))
            if year is None:
                continue
            for metric, key, num_key in (
                ("revenue", "revenue", "numAnalystsRevenue"),
                ("eps", "eps", "numAnalystsEps"),
                ("ebitda", "ebitda", None),
            ):
                out.append(AnalystEstimate(
                    symbol=symbol, market=self._current_market, fiscal_year=year, metric=metric,
                    avg_est=r.get(f"{key}Avg"),
                    high_est=r.get(f"{key}High"), low_est=r.get(f"{key}Low"),
                    num_analysts=_int(r.get(num_key)) if num_key else None, source="fmp"))
        return out

    def fetch_news(self, symbol: str):
        from ..models import NewsItem

        # /stable/news/stock requires a paid plan; skip cleanly if restricted.
        data = self._get("news/stock", symbols=self._sym(symbol), limit=50)
        out: list = []
        for r in (data if isinstance(data, list) else []):
            out.append(NewsItem(symbol=symbol, market=self._current_market,
                                published_at=to_iso(r.get("publishedDate")) or "",
                                title=r.get("title") or "", url=r.get("url"), source="fmp"))
        return out

    def fetch_esg(self, symbol: str):
        from ..models import EsgScore

        # /stable/esg-disclosures requires a paid plan; skip cleanly if restricted.
        data = self._get("esg-disclosures", symbol=self._sym(symbol))
        out: list = []
        for r in (data if isinstance(data, list) else []):
            out.append(EsgScore(symbol=symbol, market=self._current_market,
                                period=to_iso(r.get("date")) or "",
                                env=r.get("environmentalScore"), soc=r.get("socialScore"),
                                gov=r.get("governanceScore"), total=r.get("ESGScore"), source="fmp"))
        return out

    def fetch_institutional(self, symbol: str):
        from ..models import InstitutionalHolding

        # /stable/institutional-ownership/symbol-ownership 404s on the free plan
        # for every symbol/quarter (paid-plan gate); skip cleanly if restricted.
        data = self._get("institutional-ownership/symbol-ownership", symbol=self._sym(symbol))
        out: list = []
        for r in (data if isinstance(data, list) else []):
            out.append(InstitutionalHolding(
                symbol=symbol, market=self._current_market,
                quarter=to_iso(r.get("dateReported")) or "", manager=r.get("holder") or "",
                shares=r.get("shares"), value=r.get("marketValue"), source="fmp"))
        return out

    # -------------------------------------------------------------------- fx
    def fetch_fx(self, pairs: list[tuple[str, str]], start: str, end: str) -> list[FxRate]:
        start, end = default_range(start, end)
        out: list[FxRate] = []
        for base, quote in pairs:
            pair = f"{base}{quote}"
            data = self._get("historical-price-eod/full", symbol=pair, **{"from": start, "to": end})
            if not isinstance(data, list):
                continue
            for row in data:
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


def _year_of(date_str) -> int | None:
    if not date_str:
        return None
    try:
        return int(str(date_str)[:4])
    except ValueError:
        return None


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
