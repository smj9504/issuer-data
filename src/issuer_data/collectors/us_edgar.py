"""SEC EDGAR collector: US master, financials (companyfacts), filings (submissions).

Free, no API key — but requires a descriptive User-Agent (set via config) or SEC
returns 403. Endpoints return columnar JSON; CIK is zero-padded to 10 digits.
"""

from __future__ import annotations

import re

from ..config import Settings
from ..http.client import HttpClient
from ..logging import get_logger
from ..models import Filing, FinancialFact, SecurityRecord
from ..utils.dates import default_range, to_iso
from .base import BaseCollector, NotSupportedError

log = get_logger(__name__)

# 13D/13G significant-ownership form types (with amendments).
_13DG_FORMS = {"SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A"}

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


_CF_HINTS = ("cashflow", "netcashprovided", "netcashused", "proceedsfrom", "paymentsto",
             "paymentsfor", "paymentsofdividends", "cashandcashequivalentsperiod")


def _stmt_type(concept: str, has_start: bool) -> str:
    """Classify a companyfacts concept into IS/BS/CF.

    Instant facts (no period `start`) are balance-sheet stocks; duration facts are
    income-statement flows unless the concept name signals cash flow.
    """
    if not has_start:
        return "BS"
    c = concept.lower()
    if any(h in c for h in _CF_HINTS):
        return "CF"
    return "IS"


def _xml_text(node, tag: str) -> str | None:
    el = node.find(tag)
    return el.get_text(strip=True) if el else None


def _xml_val(node, tag: str) -> float | None:
    """Read a Form-4 <tag><value>N</value></tag> numeric value."""
    el = node.find(tag)
    if not el:
        return None
    v = el.find("value")
    text = (v.get_text(strip=True) if v else el.get_text(strip=True))
    try:
        return float(text) if text else None
    except ValueError:
        return None


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
        # Dedupe to one point per (fy, fp, statement_type, account, unit) so that
        # restated comparatives / YTD-vs-quarter / amendments don't collapse onto the
        # financials PK last-write-wins. Winner = has an SEC `frame` (canonical) > latest filed.
        best: dict[tuple, tuple] = {}
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
                        if fy is None or (min_year is not None and fy < min_year):
                            continue
                        stmt = _stmt_type(concept, has_start="start" in p)
                        key = (int(fy), "FY" if fp == "FY" else fp, stmt, concept, unit)
                        rank = (1 if p.get("frame") else 0, p.get("filed") or "", p.get("end") or "")
                        prev = best.get(key)
                        if prev is None or rank > prev[0]:
                            best[key] = (rank, concept, label, unit, currency, p)
        out: list[FinancialFact] = []
        for (fy, fp, stmt, concept, _unit), (_rank, _c, label, unit, currency, p) in best.items():
            out.append(
                FinancialFact(
                    symbol=symbol, market="US", fiscal_year=fy, fiscal_period=fp,
                    statement_type=stmt, account=concept, account_local=label,
                    value=p.get("val"), currency=currency, unit=unit,
                    period_end=p.get("end"), source="edgar",
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

    # --------------------------------------------------------------- insiders
    def fetch_insiders(self, symbol: str, start: str, end: str, limit: int = 25):
        """Parse recent Form 4 (insider transaction) filings into InsiderTrade rows."""

        start, end = default_range(start, end, default_years=1)
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
        primary = recent.get("primaryDocument", [])
        cik_int = int(cik)
        out: list = []
        count = 0
        for i, acc in enumerate(accs):
            if count >= limit:
                break
            if (forms[i] if i < len(forms) else "") != "4":
                continue
            filed = to_iso(dates[i]) if i < len(dates) else None
            if filed and not (start <= filed <= end):
                continue
            acc_nodash = acc.replace("-", "")
            base = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}"
            xml_url = self._form4_xml_url(base, primary[i] if i < len(primary) else "")
            if not xml_url:
                continue
            rows = self._parse_form4(xml_url, symbol, filed, acc)
            out.extend(rows)
            count += 1
        return out

    # --------------------------------------------------------------- ownership
    def fetch_ownership(self, symbol: str, limit: int = 15):
        """Parse recent SC 13D/13G cover pages into OwnershipRow (best-effort).

        13D/G are filed as documents (not a structured feed), so the reporting
        person, beneficially-owned share count, and percent of class are pulled
        from the cover page heuristically; a filing that yields neither a holder
        nor a percent is skipped rather than guessed.
        """
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
        primary = recent.get("primaryDocument", [])
        cik_int = int(cik)
        out: list = []
        for i, acc in enumerate(accs):
            if len(out) >= limit:
                break
            form = forms[i] if i < len(forms) else ""
            if form not in _13DG_FORMS:
                continue
            filed = to_iso(dates[i]) if i < len(dates) else None
            acc_nodash = acc.replace("-", "")
            base = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}"
            doc = primary[i] if i < len(primary) else ""
            if not doc:
                continue
            row = self._parse_13dg(f"{base}/{doc}", symbol, filed, acc, form)
            if row is not None:
                out.append(row)
        return out

    def _parse_13dg(self, url: str, symbol: str, filed: str | None, acc: str, form: str):
        from ..models import OwnershipRow

        try:
            content = self.client.get_bytes(url)
        except Exception:  # noqa: BLE001
            return None
        text = _html_to_text(content)
        pct = _find_pct(text)
        shares = _find_shares(text)
        holder = _find_holder(text)
        if holder is None and pct is None:
            return None
        return OwnershipRow(symbol=symbol, market="US", as_of_date=filed or "",
                            holder_name=holder or "(unknown)", holder_type=form,
                            shares=shares, pct=pct, source="edgar")

    def _form4_xml_url(self, base: str, primary_doc: str) -> str | None:
        if primary_doc.lower().endswith(".xml"):
            # primaryDocument often points at the XSL-rendered HTML
            # (e.g. 'xslF345X06/form4.xml'); the raw XML is the basename at base.
            return f"{base}/{primary_doc.split('/')[-1]}"
        try:
            idx = self.client.get_json(f"{base}/index.json")
            for item in idx.get("directory", {}).get("item", []):
                name = item.get("name", "")
                if name.lower().endswith(".xml") and not name.lower().startswith("xsl"):
                    return f"{base}/{name}"
        except Exception:  # noqa: BLE001
            return None
        return None

    def _parse_form4(self, xml_url: str, symbol: str, filed: str | None, acc: str):
        from bs4 import BeautifulSoup

        from ..models import InsiderTrade

        try:
            content = self.client.get_bytes(xml_url)
        except Exception:  # noqa: BLE001
            return []
        soup = BeautifulSoup(content, "lxml-xml")
        owner = soup.find("rptOwnerName")
        insider = owner.get_text(strip=True) if owner else "unknown"
        is_dir = _xml_text(soup, "isDirector") in ("1", "true")
        is_off = _xml_text(soup, "isOfficer") in ("1", "true")
        title = _xml_text(soup, "officerTitle")
        relation = title or ("director" if is_dir else "officer" if is_off else None)
        rows: list = []
        for seq, txn in enumerate(soup.find_all("nonDerivativeTransaction")):
            shares = _xml_val(txn, "transactionShares")
            price = _xml_val(txn, "transactionPricePerShare")
            ad = _xml_text(txn, "transactionAcquiredDisposedCode")
            rows.append(InsiderTrade(
                symbol=symbol, market="US", filed_date=filed or "",
                insider=insider, relation=relation,
                txn_type="buy" if ad == "A" else "sell" if ad == "D" else (ad or "hold"),
                txn_seq=seq, shares=shares, price=price, filing_id=acc, source="edgar"))
        if not rows:  # holding-only Form 4 (no transaction) — still record the filing
            rows.append(InsiderTrade(symbol=symbol, market="US", filed_date=filed or "",
                                     insider=insider, relation=relation, txn_type="hold",
                                     filing_id=acc, source="edgar"))
        return rows


# --- 13D/G cover-page heuristics (module-level so they are unit-testable) ------
def _html_to_text(content: bytes) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(content, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text(separator="\n", strip=True)


def _find_pct(text: str) -> float | None:
    # "Percent of class represented by amount in row (11)  ... 5.2%"
    m = re.search(r"percent of class[\s\S]{0,140}?(\d{1,3}(?:\.\d+)?)\s*%", text, re.IGNORECASE)
    if not m:
        m = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%\s*(?:of|of the)\s+(?:the\s+)?class", text, re.IGNORECASE)
    if not m:
        return None
    try:
        val = float(m.group(1))
        return val if 0 <= val <= 100 else None
    except ValueError:
        return None


def _find_shares(text: str) -> float | None:
    m = re.search(r"aggregate amount beneficially owned[\s\S]{0,180}?([1-9]\d{0,2}(?:,\d{3})+|\d{4,})",
                  text, re.IGNORECASE)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


# Cover-page label boilerplate that sits between "NAME OF REPORTING PERSON" and
# the actual name (varies by filer/template), so it must be skipped over.
_HOLDER_SKIP = re.compile(
    r"i\.?r\.?s\.?|identification|entities only|check the appropriate|member of a group|"
    r"sec use only|^\(?[a-z0-9]\)?[.)]?$|^\(\d+\)|social security|of above person",
    re.IGNORECASE,
)


def _find_holder(text: str) -> str | None:
    """Reporting-person name: first real name line after the label, skipping the
    IRS-number / checkbox boilerplate that follows it on a 13D/G cover page."""
    m = re.search(r"name[s]?\s+of\s+reporting\s+person[s]?", text, re.IGNORECASE)
    if not m:
        return None
    tail = text[m.end():]
    for line in tail.split("\n")[:10]:
        cand = line.strip(" \t.:-()")
        if len(cand) < 3 or _HOLDER_SKIP.search(cand):
            continue
        if not re.search(r"[A-Za-z]{2,}", cand):   # need real letters (not a number/box)
            continue
        # a name line shouldn't be mostly digits
        # trim a trailing IRS EIN (e.g. "The Vanguard Group - 23-1945930")
        cand = re.sub(r"\s*[-–]?\s*\d{2}-\d{7}\s*$", "", cand).strip(" \t.:-")
        letters = sum(c.isalpha() for c in cand)
        if letters < max(3, len(cand) // 3):
            continue
        return cand[:200]
    return None
