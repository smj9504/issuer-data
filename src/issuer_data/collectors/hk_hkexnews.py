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
import io
import json
import re

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

# Annual-report statement headings, as they appear verbatim as the first line
# of every page belonging to that statement (repeats on multi-page statements).
_STATEMENT_HEADINGS = {
    "IS": {
        "consolidated income statement",
        "consolidated statement of profit or loss",
        "consolidated statement of profit or loss and other comprehensive income",
        "income statement",
        "statement of profit or loss",
    },
    "BS": {
        "consolidated statement of financial position",
        "consolidated balance sheet",
        "statement of financial position",
        "balance sheet",
    },
    "CF": {
        "consolidated statement of cash flows",
        "consolidated cash flow statement",
        "statement of cash flows",
        "cash flow statement",
    },
}
# A data row: some label text, then two number columns (current year, prior year).
_ROW_RE = re.compile(r"^(.*\S)\s+(\(?[\d][\d,]*\)?)\s+(\(?[\d][\d,]*\)?)\s*$")
# Strip a trailing footnote reference like "7" or "12(a)" off a row label.
_NOTE_TAIL_RE = re.compile(r"\s+\d{1,3}(\([a-zA-Z0-9]+\))?$")
_YEAR_PAIR_RE = re.compile(r"(20\d{2})\D{1,20}(20\d{2})")
_UNIT_RE = re.compile(
    r"(RMB|HKD|USD|CNY|EUR|HK\$|US\$)[’'`]?\s*(MILLION|’000|'000|000)", re.IGNORECASE
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

    # ------------------------------------------------------------ financials
    def fetch_financials(self, symbol: str, years: int | None = None):
        """Best-effort financial facts parsed from the latest Annual Report PDF.

        HKEXnews has no structured (XBRL-like) financials API, only filing
        PDFs. This locates the newest "Annual Report" filing, downloads it,
        and regex-parses the Income Statement / Balance Sheet / Cash Flow
        Statement pages by their repeated page heading. It's inherently
        fragile across the wide variety of annual-report templates HK issuers
        use — treat it as a fallback, not a replacement for a real XBRL feed.
        """
        code = hk_code(symbol)
        annual = self._latest_annual_report(code)
        if not annual:
            log.warning("HKEXnews: no Annual Report filing found for %s", code)
            return []
        try:
            content = self.client.get_bytes(annual.url)
        except Exception as exc:  # noqa: BLE001
            log.warning("HKEXnews: failed to download annual report for %s: %s", code, exc)
            return []
        return _parse_annual_report(content, code)

    def _latest_annual_report(self, code: str) -> Filing | None:
        filings = self.fetch_filings(code, None, None)
        reports = [f for f in filings if f.title and "ANNUAL REPORT" in f.title.upper() and f.url]
        if not reports:
            return None
        return max(reports, key=lambda f: f.filed_date or "")


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
    text = re.sub(r"<[^>]+>", " ", str(value))
    return " ".join(text.split()) or None


# ------------------------------------------------------------- annual report
def _first_line_norm(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return re.sub(r"\s+", " ", line).lower()
    return ""


def _find_statement_pages(pages_text: list[str], headings: set[str]) -> list[str]:
    start = next((i for i, t in enumerate(pages_text) if _first_line_norm(t) in headings), None)
    if start is None:
        return []
    block = [pages_text[start]]
    for t in pages_text[start + 1:]:
        if _first_line_norm(t) not in headings:
            break
        block.append(t)
    return block


_JUNK_LABELS = {"annual report", "interim report"}  # page-footer artifacts, e.g. "Annual Report 2025 129"


def _clean_label(label: str) -> str | None:
    label = _NOTE_TAIL_RE.sub("", label).strip()
    if not label or not any(c.isalpha() for c in label):
        return None
    if label.lower() in _JUNK_LABELS:
        return None
    return label


def _detect_unit(text: str) -> tuple[str | None, float]:
    m = _UNIT_RE.search(text)
    if not m:
        return None, 1.0
    ccy = {"HK$": "HKD", "US$": "USD"}.get(m.group(1).upper(), m.group(1).upper())
    scale = 1_000_000.0 if m.group(2).upper() == "MILLION" else 1_000.0
    return ccy, scale


def _num(v: str) -> float | None:
    neg = v.startswith("(") and v.endswith(")")
    digits = v.strip("()").replace(",", "")
    try:
        n = float(digits)
    except ValueError:
        return None
    return -n if neg else n


def _parse_annual_report(content: bytes, symbol: str) -> list:
    import pdfplumber

    from ..models import FinancialFact

    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            pages_text = [(p.extract_text() or "") for p in pdf.pages]
    except Exception as exc:  # noqa: BLE001
        log.warning("HKEXnews: failed to open annual report PDF for %s: %s", symbol, exc)
        return []

    out: list[FinancialFact] = []
    for sttype, headings in _STATEMENT_HEADINGS.items():
        block = _find_statement_pages(pages_text, headings)
        if not block:
            continue
        full_text = "\n".join(block)
        years = _YEAR_PAIR_RE.search(full_text)
        if not years:
            continue
        y_cur, y_prev = int(years.group(1)), int(years.group(2))
        currency, scale = _detect_unit(full_text)
        for line in full_text.splitlines():
            m = _ROW_RE.match(line.strip())
            if not m:
                continue
            label = _clean_label(m.group(1))
            if not label:
                continue
            for year, raw in ((y_cur, m.group(2)), (y_prev, m.group(3))):
                val = _num(raw)
                if val is None:
                    continue
                out.append(FinancialFact(
                    symbol=symbol, market="HK", fiscal_year=year, fiscal_period="FY",
                    statement_type=sttype, account=label, value=val * scale,
                    currency=currency, period_end=f"{year}-12-31", source="hkexnews"))
    return out
