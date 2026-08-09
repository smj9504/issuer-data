"""SEC EDGAR collector: US master, financials (companyfacts), filings (submissions).

Free, no API key — but requires a descriptive User-Agent (set via config) or SEC
returns 403. Endpoints return columnar JSON; CIK is zero-padded to 10 digits.
"""

from __future__ import annotations

from ..config import Settings
from ..http.client import HttpClient
from ..logging import get_logger
from ..models import FinancialFact, Filing, SecurityRecord
from ..utils.dates import default_range, to_iso
from .base import BaseCollector, NotSupportedError

log = get_logger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
TICKERS_EXCHANGE_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json"

# Forms we treat as periodic financial reports.
_PERIODIC_FORMS = {"10-K", "10-Q", "20-F", "40-F"}
# Exchange -> canonical name
_EXCH = {"Nasdaq": "NASDAQ", "NYSE": "NYSE", "NYSE Arca": "NYSEARCA", "OTC": "OTC", "CBOE": "CBOE"}


def cik10(cik: str | int) -> str:
    return str(int(cik)).zfill(10)


class EdgarCollector(BaseCollector):
    market = "US"
    source = "edgar"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = HttpClient(rate_limit=settings.edgar_rate_limit)
        self._ticker_map: dict[str, dict] | None = None

    # ------------------------------------------------------------- ticker map
    def _load_tickers(self) -> dict[str, dict]:
        """Return {TICKER: {cik, ticker, title, exchange}}."""
        if self._ticker_map is not None:
            return self._ticker_map
        mapping: dict[str, dict] = {}
        # Prefer the exchange-annotated file; fall back to the plain one.
        try:
            data = self.client.get_json(TICKERS_EXCHANGE_URL)
            fields = data["fields"]
            idx = {name: i for i, name in enumerate(fields)}
            for row in data["data"]:
                ticker = str(row[idx["ticker"]]).upper()
                mapping[ticker] = {
                    "cik": row[idx["cik"]],
                    "ticker": ticker,
                    "title": row[idx["name"]],
                    "exchange": row[idx.get("exchange", -1)] if "exchange" in idx else None,
                }
        except Exception as exc:  # noqa: BLE001
            log.warning("EDGAR exchange list failed (%s); using plain ticker list", exc)
            data = self.client.get_json(TICKERS_URL)
            for row in data.values():
                ticker = str(row["ticker"]).upper()
                mapping[ticker] = {
                    "cik": row["cik_str"],
                    "ticker": ticker,
                    "title": row["title"],
                    "exchange": None,
                }
        self._ticker_map = mapping
        return mapping

    def _cik_for(self, symbol: str) -> str:
        m = self._load_tickers()
        info = m.get(symbol.upper())
        if not info:
            raise ValueError(f"Unknown US ticker on EDGAR: {symbol}")
        return cik10(info["cik"])

    # --------------------------------------------------------------- master
    def fetch_master(self, symbols: list[str] | None = None) -> list[SecurityRecord]:
        m = self._load_tickers()
        wanted = {s.upper() for s in symbols} if symbols else None
        out: list[SecurityRecord] = []
        for ticker, info in m.items():
            if wanted is not None and ticker not in wanted:
                continue
            exch = _EXCH.get(str(info.get("exchange")), info.get("exchange"))
            out.append(
                SecurityRecord(
                    market="US",
                    symbol=ticker,
                    name=info["title"],
                    country="US",
                    cik=cik10(info["cik"]),
                    currency="USD",
                    exchange=exch,
                    security_type="COMMON",
                    is_primary=True,
                    source="edgar",
                )
            )
        return out

    # ------------------------------------------------------------ financials
    def fetch_financials(self, symbol: str, years: int | None = None) -> list[FinancialFact]:
        cik = self._cik_for(symbol)
        try:
            facts = self.client.get_json(COMPANYFACTS_URL.format(cik10=cik))
        except Exception as exc:  # noqa: BLE001
            log.warning("EDGAR companyfacts failed for %s: %s", symbol, exc)
            return []
        out: list[FinancialFact] = []
        taxonomies = facts.get("facts", {})
        min_year = None
        if years is not None:
            import datetime as _dt

            min_year = _dt.date.today().year - years
        for taxonomy in ("us-gaap", "ifrs-full"):
            concepts = taxonomies.get(taxonomy, {})
            for concept, node in concepts.items():
                label = node.get("label")
                for unit, points in node.get("units", {}).items():
                    currency = unit if unit in ("USD", "EUR", "KRW", "HKD") else None
                    for p in points:
                        if p.get("form") not in _PERIODIC_FORMS:
                            continue
                        fy = p.get("fy")
                        fp = p.get("fp") or "FY"
                        if fy is None:
                            continue
                        if min_year is not None and fy < min_year:
                            continue
                        out.append(
                            FinancialFact(
                                symbol=symbol,
                                market="US",
                                fiscal_year=int(fy),
                                fiscal_period="FY" if fp == "FY" else fp,
                                statement_type=None,
                                account=concept,
                                account_local=label,
                                value=p.get("val"),
                                currency=currency,
                                unit=unit,
                                period_end=p.get("end"),
                                source="edgar",
                            )
                        )
        return out

    # --------------------------------------------------------------- filings
    def fetch_filings(self, symbol: str, start: str, end: str) -> list[Filing]:
        start, end = default_range(start, end, default_years=2)
        cik = self._cik_for(symbol)
        try:
            data = self.client.get_json(SUBMISSIONS_URL.format(cik10=cik))
        except Exception as exc:  # noqa: BLE001
            log.warning("EDGAR submissions failed for %s: %s", symbol, exc)
            return []
        recent = data.get("filings", {}).get("recent", {})
        accs = recent.get("accessionNumber", [])
        dates = recent.get("filingDate", [])
        forms = recent.get("form", [])
        primary_docs = recent.get("primaryDocument", [])
        descs = recent.get("primaryDocDescription", [])
        cik_int = int(cik)
        out: list[Filing] = []
        for i, acc in enumerate(accs):
            filed = to_iso(dates[i]) if i < len(dates) else None
            if filed and not (start <= filed <= end):
                continue
            acc_nodash = acc.replace("-", "")
            primary = primary_docs[i] if i < len(primary_docs) else ""
            base = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}"
            doc_url = f"{base}/{primary}" if primary else f"{base}/"
            out.append(
                Filing(
                    symbol=symbol,
                    market="US",
                    filing_id=acc,
                    filed_date=filed,
                    filing_type=forms[i] if i < len(forms) else None,
                    title=(descs[i] if i < len(descs) else None) or (forms[i] if i < len(forms) else None),
                    url=doc_url,
                    source="edgar",
                    doc_urls=[doc_url] if primary else [],
                )
            )
        return out

    def fetch_prices(self, symbol: str, start: str, end: str):
        raise NotSupportedError("EDGAR has no price data; use yfinance/fmp")
