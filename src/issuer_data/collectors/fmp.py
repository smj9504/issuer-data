"""Financial Modeling Prep (FMP) collector: global master, prices, financials, filings, peers.

Standalone REST client (financialmodelingprep.com) using an API key from config —
NOT the MCP server, so the project stays reusable.

Uses FMP's current "stable" API (financialmodelingprep.com/stable). The old
`/api/v3` and `/api/v4` endpoints were retired on 2025-08-31 and now return a
"Legacy Endpoint" error for keys created after that date. The free tier is
limited: it covers US symbols only for prices/statements, ~250 req/day, and
several endpoints (news, ESG, institutional ownership, index constituents,
SEC filing search) are premium-only — those return HTTP 4xx and are skipped
cleanly rather than aborting the run.
"""

from __future__ import annotations

import requests

from ..config import Settings
from ..http.client import HttpClient
from ..logging import get_logger
from ..models import Filing, FinancialFact, FxRate, Price, SecurityRecord
from ..utils.dates import default_range, to_iso
from .base import BaseCollector, NotSupportedError

log = get_logger(__name__)

_BASE = "https://financialmodelingprep.com/stable"

# HTTP statuses FMP uses for "not on your plan" / bad request — skip, don't abort.
_PLAN_LIMITED = {400, 401, 402, 403}

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
        """GET a stable endpoint. Returns parsed JSON, or None when the endpoint
        is unavailable on the current plan (HTTP 4xx) so callers skip it cleanly."""
        params["apikey"] = self.api_key
        try:
            return self.client.get_json(f"{_BASE}/{path}", params=params)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status in _PLAN_LIMITED:
                log.warning("FMP %s unavailable on this plan (HTTP %s) — skipping", path, status)
                return None
            raise

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
                    exchange=p.get("exchange") or p.get("exchangeShortName"),
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
        data = self._get("historical-price-eod/full", symbol=self._sym(symbol),
                         **{"from": start, "to": end})
        hist = _rows(data)
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
                data = self._get(endpoint, symbol=self._sym(symbol), period=period_kind,
                                 limit=limit if period_kind == "annual" else limit * 4)
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
        data = self._get("sec-filings-search/symbol", symbol=self._sym(symbol),
                         limit=100, **{"from": start, "to": end})
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
        data = self._get("stock-peers", symbol=self._sym(symbol))
        if isinstance(data, list):
            # stable returns [{symbol, companyName, ...}]; legacy was [{peersList: [...]}]
            if data and "peersList" in data[0]:
                return list(data[0].get("peersList", []))
            return [str(r["symbol"]) for r in data if r.get("symbol")]
        return []

    # --------------------------------------------------- Extension B coverage
    def fetch_daily_metrics(self, symbol: str, start: str, end: str):
        from ..models import DailyMetric

        start, end = default_range(start, end)
        data = self._get("historical-market-capitalization", symbol=self._sym(symbol),
                         limit=500, **{"from": start, "to": end})
        out: list = []
        if isinstance(data, list):
            for r in data:
                out.append(DailyMetric(
                    symbol=symbol, market=self._current_market, metric_date=to_iso(r.get("date")),
                    market_cap=r.get("marketCap"), currency=_ccy(self._current_market), source="fmp"))
        return out

    def fetch_ratios(self, symbol: str, years: int | None = None):
        from ..models import Ratio

        out: list = []
        for endpoint in ("ratios", "key-metrics"):
            data = self._get(endpoint, symbol=self._sym(symbol), period="annual", limit=years or 5)
            if not isinstance(data, list):
                continue
            for rec in data:
                year = _int(rec.get("fiscalYear") or rec.get("calendarYear")) or _year_of(rec.get("date"))
                if year is None:
                    continue
                for metric, value in rec.items():
                    if metric in ("symbol", "date", "calendarYear", "fiscalYear",
                                  "period", "reportedCurrency") \
                            or not isinstance(value, (int, float)):
                        continue
                    out.append(Ratio(symbol=symbol, market=self._current_market,
                                     fiscal_year=year, fiscal_period="FY",
                                     metric=metric, value=float(value), source="fmp"))
        return out

    def fetch_corporate_actions(self, symbol: str, start: str, end: str):
        from ..models import CorporateAction

        out: list = []
        div = self._get("dividends", symbol=self._sym(symbol))
        for r in _rows(div):
            out.append(CorporateAction(symbol=symbol, market=self._current_market,
                                       ex_date=to_iso(r.get("date")), action_type="dividend",
                                       amount=r.get("dividend") or r.get("adjDividend"),
                                       currency=_ccy(self._current_market), source="fmp"))
        spl = self._get("splits", symbol=self._sym(symbol))
        for r in _rows(spl):
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
            for metric, key in (("revenue", "revenue"), ("eps", "eps"), ("ebitda", "ebitda")):
                out.append(AnalystEstimate(
                    symbol=symbol, market=self._current_market, fiscal_year=year, metric=metric,
                    avg_est=r.get(f"{key}Avg"),
                    high_est=r.get(f"{key}High"), low_est=r.get(f"{key}Low"),
                    num_analysts=_int(r.get("numAnalystsRevenue") or r.get("numAnalystsEps")),
                    source="fmp"))
        return out

    def fetch_news(self, symbol: str):
        from ..models import NewsItem

        data = self._get("news/stock", symbols=self._sym(symbol), limit=50)
        out: list = []
        for r in (data if isinstance(data, list) else []):
            out.append(NewsItem(symbol=symbol, market=self._current_market,
                                published_at=to_iso(r.get("publishedDate")) or "",
                                title=r.get("title") or "", url=r.get("url"), source="fmp"))
        return out

    def fetch_esg(self, symbol: str):
        from ..models import EsgScore

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

        data = self._get("institutional-ownership/symbol-positions-summary", symbol=self._sym(symbol))
        out: list = []
        for r in (data if isinstance(data, list) else []):
            out.append(InstitutionalHolding(
                symbol=symbol, market=self._current_market,
                quarter=to_iso(r.get("dateReported")) or "", manager=r.get("holder") or "",
                shares=r.get("shares"), value=r.get("marketValue"), source="fmp"))
        return out

    def fetch_earnings(self, symbol: str):
        from ..models import EarningsEvent

        # Historical + upcoming earnings calendar for the ticker.
        data = self._get("earnings", symbol=self._sym(symbol), limit=80)
        out: list = []
        for r in (data if isinstance(data, list) else []):
            date = to_iso(r.get("date"))
            if not date:
                continue
            out.append(EarningsEvent(
                symbol=symbol, market=self._current_market, event_date=date,
                event_type="earnings", fiscal_period=None,
                eps_estimate=r.get("epsEstimated"),
                eps_actual=r.get("epsActual") if "epsActual" in r else r.get("eps"),
                source="fmp"))
        return out

    def fetch_index_membership(self, symbol: str):
        from ..models import IndexMembership

        # FMP's constituent lists are US-only (S&P 500 / Nasdaq 100 / Dow Jones).
        if self._current_market != "US":
            raise NotSupportedError("FMP index membership is US-only")
        target = self._sym(symbol).upper()
        out: list = []
        for index_name, endpoint in (("S&P500", "sp500-constituent"),
                                     ("NASDAQ100", "nasdaq-constituent"),
                                     ("DOWJONES", "dowjones-constituent")):
            data = self._get(endpoint)
            for r in (data if isinstance(data, list) else []):
                if str(r.get("symbol", "")).upper() == target:
                    out.append(IndexMembership(
                        symbol=symbol, market=self._current_market,
                        index_name=index_name, added=to_iso(r.get("dateFirstAdded")),
                        source="fmp"))
                    break
        return out

    # -------------------------------------------------------------------- fx
    def fetch_fx(self, pairs: list[tuple[str, str]], start: str, end: str) -> list[FxRate]:
        start, end = default_range(start, end)
        out: list[FxRate] = []
        for base, quote in pairs:
            pair = f"{base}{quote}"
            data = self._get("historical-price-eod/full", symbol=pair,
                             **{"from": start, "to": end})
            hist = _rows(data)
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


def _rows(data) -> list:
    """Normalize an FMP price/actions payload to a list of row dicts.

    The stable API returns a flat list; the retired v3 API wrapped rows in
    {"historical": [...]}. Accept both, and treat None (plan-limited) as empty.
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("historical") or []
    return []


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
