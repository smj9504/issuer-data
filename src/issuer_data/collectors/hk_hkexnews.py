"""HKEXnews (Hong Kong) collector: HK master + filings via the official portal.

Uses the undocumented JSON endpoints behind HKEXnews' Listed Company Information
search:
  * active-stock list (code -> internal stockId + name)
  * titleSearchServlet (filing list; ``result`` is a JSON *string* to double-parse)

HKEXnews serves filings + master only — NOT OHLCV or structured statements (those
come from yfinance/FMP).
"""

from __future__ import annotations

import datetime as _dt
import json

from ..config import Settings
from ..http.client import HttpClient
from ..logging import get_logger
from ..models import Filing, SecurityRecord
from ..utils.dates import compact, default_range
from .base import BaseCollector, NotSupportedError

log = get_logger(__name__)

_BASE = "https://www1.hkexnews.hk"
_STOCKLIST_URL = _BASE + "/ncms/script/eds/activestock_sehk_e.json"
_SEARCH_URL = _BASE + "/search/titleSearchServlet.do"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)


def hk_code(symbol: str) -> str:
    """Normalize an HK symbol to the 5-digit HKEXnews code (Tencent -> 00700)."""
    digits = "".join(ch for ch in symbol.upper().replace(".HK", "") if ch.isdigit())
    return digits.zfill(5) if digits else symbol


class HkexNewsCollector(BaseCollector):
    market = "HK"
    source = "hkexnews"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = HttpClient(
            rate_limit=settings.default_rate_limit,
            headers={
                "User-Agent": _UA,
                "Referer": _BASE + "/search/titlesearch.xhtml?lang=en",
            },
        )
        self._stocks: dict[str, dict] | None = None  # code -> {i, n}

    def _load_stocks(self) -> dict[str, dict]:
        if self._stocks is not None:
            return self._stocks
        data = self.client.get_json(_STOCKLIST_URL)
        mapping: dict[str, dict] = {}
        for item in data:
            code = str(item.get("c", "")).zfill(5)
            mapping[code] = {"stock_id": item.get("i"), "name": item.get("n")}
        self._stocks = mapping
        return mapping

    # --------------------------------------------------------------- master
    def fetch_master(self, symbols: list[str] | None = None) -> list[SecurityRecord]:
        stocks = self._load_stocks()
        wanted = {hk_code(s) for s in symbols} if symbols else None
        out: list[SecurityRecord] = []
        for code, info in stocks.items():
            if wanted is not None and code not in wanted:
                continue
            out.append(
                SecurityRecord(
                    market="HK",
                    symbol=code,
                    name=info["name"],
                    local_name=info["name"],
                    country="HK",
                    exchange="HKEX",
                    currency="HKD",
                    security_type="COMMON",
                    is_primary=True,
                    source="hkexnews",
                )
            )
        return out

    # --------------------------------------------------------------- filings
    def fetch_filings(self, symbol: str, start: str, end: str) -> list[Filing]:
        start, end = default_range(start, end, default_years=2)
        code = hk_code(symbol)
        stocks = self._load_stocks()
        info = stocks.get(code)
        if not info or info.get("stock_id") is None:
            log.warning("HKEXnews: unknown stock code %s", code)
            return []
        params = {
            "sortDir": 0,
            "sortByOptions": "DateTime",
            "category": 0,
            "market": "SEHK",
            "stockId": info["stock_id"],
            "documentType": -1,
            "fromDate": _fmt(start),
            "toDate": _fmt(end),
            "title": "",
            "searchType": 1,
            "t": 1,
            "lang": "en",
        }
        data = self.client.get_json(_SEARCH_URL, params=params)
        raw = data.get("result")
        if not raw:
            return []
        try:
            records = json.loads(raw)
        except (ValueError, TypeError) as exc:
            log.warning("HKEXnews: cannot parse result for %s: %s", code, exc)
            return []
        out: list[Filing] = []
        for r in records:
            link = r.get("FILE_LINK")
            url = (_BASE + link) if link and link.startswith("/") else link
            out.append(
                Filing(
                    symbol=code,
                    market="HK",
                    filing_id=str(r.get("NEWS_ID")),
                    filed_date=_parse_dt(r.get("DATE_TIME")),
                    filing_type=_strip_html(r.get("LONG_TEXT")),
                    title=r.get("TITLE"),
                    url=url,
                    source="hkexnews",
                    doc_urls=[url] if url else [],
                )
            )
        return out

    def fetch_prices(self, symbol: str, start: str, end: str):
        raise NotSupportedError("HKEXnews has no price data; use yfinance/fmp for HK prices")

    def fetch_financials(self, symbol: str, years: int | None = None):
        raise NotSupportedError("HKEXnews has no structured financials; use fmp for HK")


def _fmt(iso_date: str) -> str:
    return compact(iso_date)  # YYYYMMDD


def _parse_dt(value) -> str | None:
    """'28/06/2024 22:59' -> '2024-06-28'."""
    if not value:
        return None
    s = str(value).split(" ")[0]
    try:
        d = _dt.datetime.strptime(s, "%d/%m/%Y")
        return d.strftime("%Y-%m-%d")
    except ValueError:
        return None


def _strip_html(value) -> str | None:
    if not value:
        return None
    import re

    text = re.sub(r"<[^>]+>", " ", str(value))
    return " ".join(text.split()) or None
