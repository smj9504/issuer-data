"""SEC EDGAR collector: US master, financials (companyfacts), filings (submissions).

Free, no API key — but requires a descriptive User-Agent (set via config) or SEC
returns 403. Endpoints return columnar JSON; CIK is zero-padded to 10 digits.
"""

from __future__ import annotations

import csv
import re
import zipfile
from pathlib import Path

from ..config import Settings
from ..http.client import HttpClient
from ..logging import get_logger
from ..models import Filing, FinancialFact, SecurityRecord
from ..utils.dates import default_range, to_iso
from .base import BaseCollector, NotSupportedError

log = get_logger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
TICKERS_EXCHANGE_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json"
FORM13F_LISTING_URL = "https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets"
FORM13F_ZIP_LINK_RE = re.compile(r'href="([^"]*form-13f-data-sets/[^"]*_form13f\.zip)"')

# Forms we treat as periodic financial reports.
_PERIODIC_FORMS = {"10-K", "10-Q", "20-F", "40-F"}
# Exchange -> canonical name
_EXCH = {"Nasdaq": "NASDAQ", "NYSE": "NYSE", "NYSE Arca": "NYSEARCA", "OTC": "OTC", "CBOE": "CBOE"}


def _normalize_issuer(name: str) -> str:
    """13F filers hand-type issuer names inconsistently; strip to bare A-Z0-9."""
    return re.sub(r"[^A-Z0-9]+", "", name.upper())


def cik10(cik: str | int) -> str:
    return str(int(cik)).zfill(10)


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
        # lazily-built, process-lifetime cache for the bulk Form 13F dataset
        self._f13f_filers: dict[str, tuple[str, str]] | None = None
        self._f13f_rows: list[tuple[str, str, str, str]] | None = None

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
        for txn in soup.find_all("nonDerivativeTransaction"):
            shares = _xml_val(txn, "transactionShares")
            price = _xml_val(txn, "transactionPricePerShare")
            ad = _xml_text(txn, "transactionAcquiredDisposedCode")
            rows.append(InsiderTrade(
                symbol=symbol, market="US", filed_date=filed or "",
                insider=insider, relation=relation,
                txn_type="buy" if ad == "A" else "sell" if ad == "D" else ad,
                shares=shares, price=price, filing_id=acc, source="edgar"))
        if not rows:  # holding-only Form 4 (no transaction) — still record the filing
            rows.append(InsiderTrade(symbol=symbol, market="US", filed_date=filed or "",
                                     insider=insider, relation=relation, filing_id=acc,
                                     source="edgar"))
        return rows

    # -------------------------------------------------------- institutional
    def fetch_institutional(self, symbol: str):
        """Institutional (13F) holders of `symbol`, from SEC's bulk quarterly dataset.

        13F has no per-symbol lookup API; SEC only publishes the whole market's
        holdings as one quarterly ZIP. The first call in a process pays for
        downloading (~100MB) and scanning it (~4M rows); every symbol after
        that in the same run is an in-memory list scan, no extra I/O.
        """
        from ..models import InstitutionalHolding

        info = self._load_tickers().get(symbol.upper())
        if not info:
            raise ValueError(f"Unknown US ticker on EDGAR: {symbol}")
        target = _normalize_issuer(info["title"])
        filers, rows = self._load_form13f()
        out: list[InstitutionalHolding] = []
        for issuer_norm, acc, value, shares in rows:
            if issuer_norm != target:
                continue
            filer = filers.get(acc)
            if not filer:
                continue
            name, period = filer
            out.append(InstitutionalHolding(
                # VALUE is whole dollars since 2023-01-03 (was thousands before).
                symbol=symbol, market="US", quarter=period, manager=name,
                shares=_num(shares), value=_num(value),
                source="edgar"))
        return out

    def _load_form13f(self) -> tuple[dict[str, tuple[str, str]], list[tuple[str, str, str, str]]]:
        if self._f13f_rows is not None:
            return self._f13f_filers, self._f13f_rows
        zip_path = self._ensure_form13f_zip()
        log.info("Parsing Form 13F bulk dataset %s (this can take ~30s)...", zip_path.name)
        filers: dict[str, tuple[str, str]] = {}
        rows: list[tuple[str, str, str, str]] = []
        with zipfile.ZipFile(zip_path) as zf:
            with zf.open("COVERPAGE.tsv") as fh:
                reader = csv.reader(_decode(fh), delimiter="\t")
                header = next(reader)
                acc_i = header.index("ACCESSION_NUMBER")
                name_i = header.index("FILINGMANAGER_NAME")
                period_i = header.index("REPORTCALENDARORQUARTER")
                for r in reader:
                    filers[r[acc_i]] = (r[name_i], r[period_i])
            with zf.open("INFOTABLE.tsv") as fh:
                reader = csv.reader(_decode(fh), delimiter="\t")
                header = next(reader)
                acc_i = header.index("ACCESSION_NUMBER")
                issuer_i = header.index("NAMEOFISSUER")
                value_i = header.index("VALUE")
                shares_i = header.index("SSHPRNAMT")
                for r in reader:
                    rows.append((_normalize_issuer(r[issuer_i]), r[acc_i], r[value_i], r[shares_i]))
        log.info("Form 13F dataset ready: %d filers, %d holdings rows", len(filers), len(rows))
        self._f13f_filers, self._f13f_rows = filers, rows
        return filers, rows

    def _ensure_form13f_zip(self) -> Path:
        cache_dir = Path(self.settings.docs_dir) / "form13f_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        url = self._latest_form13f_zip_url()
        name = url.rsplit("/", 1)[-1]
        path = cache_dir / name
        if not path.exists():
            log.info("Downloading Form 13F bulk dataset from %s ...", url)
            path.write_bytes(self.client.get_bytes(url))
        return path

    def _latest_form13f_zip_url(self) -> str:
        html = self.client.get(FORM13F_LISTING_URL).text
        links = FORM13F_ZIP_LINK_RE.findall(html)
        if not links:
            raise NotSupportedError("Could not find a Form 13F dataset link on sec.gov")
        return "https://www.sec.gov" + links[0]


def _decode(fh):
    """Yield decoded text lines from a binary zip member (SEC TSVs are latin-1)."""
    for line in fh:
        yield line.decode("latin-1")


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None
