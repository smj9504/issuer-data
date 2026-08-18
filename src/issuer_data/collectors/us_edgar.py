"""SEC EDGAR collector: US master, financials (companyfacts), filings (submissions).

Free, no API key — but requires a descriptive User-Agent (set via config) or SEC
returns 403. Endpoints return columnar JSON; CIK is zero-padded to 10 digits.
"""

from __future__ import annotations

import csv
import datetime as _dt
import re
import zipfile
from pathlib import Path

from ..config import Settings
from ..http.client import HttpClient
from ..logging import get_logger
from ..models import DemandSignal, Filing, FinancialFact, SecurityRecord
from ..utils.dates import default_range, to_iso, today_iso
from .base import BaseCollector, NotSupportedError

log = get_logger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
TICKERS_EXCHANGE_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json"
FORM13F_LISTING_URL = "https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets"
FORM13F_ZIP_LINK_RE = re.compile(r'href="([^"]*form-13f-data-sets/[^"]*_form13f\.zip)"')

# Nasdaq's IPO calendar is an undocumented API backing nasdaq.com/market-activity/ipos;
# it 403s/hangs without a browser-like UA (SEC's descriptive UA is rejected outright).
NASDAQ_IPO_CALENDAR_URL = "https://api.nasdaq.com/api/ipo/calendar"
_NASDAQ_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)
# SEC's own full-text search over every filing's body + exhibits since 2001; also
# undocumented but stable in practice. `ciks` needs the zero-padded 10-digit form,
# and root forms (e.g. 'S-1') already cover their amendments ('S-1/A') — a form
# value containing '/' breaks the whole query, so amendment suffixes are omitted.
EDGAR_FULLTEXT_URL = "https://efts.sec.gov/LATEST/search-index"
_DEMAND_FORMS = "8-K,424B1,424B2,424B3,424B4,424B5,S-1"
_DEMAND_QUERY = '"oversubscribed"'
_TTW_QUERY = '"testing-the-waters"'

# EDGAR's electronic-filing era start — wide enough to catch an IPO-era S-1/
# DRS regardless of how long ago the company went public, unlike the 2-year
# lookback `fetch_filings` defaults to for typical "recent activity" calls.
_HISTORY_START = "1994-01-01"

# Price-band escalation is the only reliable free proxy for IPO demand: Nasdaq's
# calendar has no price/share fields on its 'filed' entries and many deals hold a
# fixed target raise (trimming shares as price rises), so filed vs priced amount
# stays flat regardless of demand — see _nasdaq_demand_signal's docstring. This
# instead reads the actual price language SEC filings use (verified against a
# real S-1/A -> 424B4 sequence): the initial S-1 usually has a blank placeholder
# price, a later S-1/A states "...offering price ... will be between $X.XX and
# $Y.YY", and the final 424B states "The initial public offering price is $Z.ZZ
# per share."
_PRICE_RANGE_RE = re.compile(
    r"offering\s+price[^$]{0,100}between\s*\$\s*(\d{1,4}\.\d{2})\s*and\s*\$\s*(\d{1,4}\.\d{2})",
    re.IGNORECASE,
)
_FINAL_PRICE_RE = re.compile(
    r"initial\s+public\s+offering\s+price\s+(?:is|of)\s*\$\s*(\d{1,4}\.\d{2})\s*per\s+share",
    re.IGNORECASE,
)

# Anchor-investor "indication of interest" disclosures — the closest US
# analog to a Korean cornerstone-investor allocation. Verified phrasing
# pattern: "<investor description> have/has indicated an interest in
# purchasing ... up to $<N> million/billion ...". Group 1 (investor text) is
# best-effort free text, not a cleaned name — the full matched sentence is
# always kept in the resulting DemandSignal's `detail` for human review.
_ANCHOR_INVESTOR_RE = re.compile(
    r"([A-Z][^.]{0,150}?)\s+(?:have|has)\s+indicated\s+an\s+interest\s+in\s+purchasing"
    r"[^$]{0,80}\$\s*([\d,]+(?:\.\d+)?)\s*(million|billion)?",
    re.IGNORECASE,
)

# EDGAR full-text hits for "testing-the-waters" are, empirically (checked
# against 5 real 2023-2024 IPOs), overwhelmingly the underwriting agreement's
# standard Rule 163B representation clause (filed as Exhibit 1.1 to the S-1/A
# or, for later unrelated deals, to an 8-K) rather than substantive TTW
# disclosure — a boilerplate clause nearly every modern IPO's underwriting
# agreement carries, regardless of how much real TTW outreach happened.
# Excluding it is what keeps _ttw_fulltext_signal selective.
_UNDERWRITING_EXHIBIT_RE = re.compile(r"^EX-1\.\d+$", re.IGNORECASE)

# Forms we treat as periodic financial reports.
_PERIODIC_FORMS = {"10-K", "10-Q", "20-F", "40-F"}
# Registration-statement forms that carry an IPO's price/terms language: S-1
# for domestic filers, F-1 for foreign private issuers (same purpose,
# different form family — both are preceded by the same DRS/DRS-A
# confidential-review process).
_REGISTRATION_FORMS = ("S-1", "S-1/A", "F-1", "F-1/A")
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
        # per-month cache for the Nasdaq IPO calendar (shared across symbols in a run)
        self._nasdaq_months: dict[str, dict] = {}
        # per-symbol cache for fetched filing document text, reset at the top
        # of fetch_demand_signals — avoids re-downloading the same S-1/424B
        # doc when multiple signal methods scan the same filing set. NOT a
        # process-lifetime cache like _nasdaq_months/_ticker_map: doc text is
        # large and never reused across symbols.
        self._doc_text_cache: dict[str, str | None] = {}

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

    # ---------------------------------------------------------- demand signals
    def fetch_demand_signals(self, symbol: str) -> list[DemandSignal]:
        """Best-effort demand/oversubscription signals for `symbol`'s IPO.

        US filers have no obligation to disclose order-book detail (unlike KR
        DART's 수요예측 결과), so this combines several free, official proxies:
        - Price-band escalation: the IPO price range disclosed in the earliest
          S-1/A that has real numbers, vs. the final confirmed price in the
          424B prospectus. This is the reliable one — a price hike (or an
          upsized deal) only happens because the book was oversubscribed.
        - Anchor-investor "indication of interest" disclosures — the closest
          US analog to a Korean cornerstone-investor allocation.
        - Confidential DRS review period — a time-window proxy for how long
          the company had to run TTW/NDR meetings before going public (not a
          content signal; TTW itself is never filed under Rule 163B).
        - Nasdaq's IPO calendar: final deal terms (price/shares/amount) as a
          factual record. NOT a demand signal on its own — see
          _nasdaq_demand_signal's docstring for why filed-vs-priced amount is
          usually flat regardless of actual demand.
        - SEC EDGAR full-text search: self-reported "oversubscribed" language
          in the company's own filings (mostly 8-K press-release exhibits),
          and separately "testing-the-waters" language (see
          _ttw_fulltext_signal's caveat about boilerplate false positives).

        Each sub-signal runs in its own try/except: one flaky signal type
        (e.g. a regex choking on unusual filing text) shouldn't blank out
        the others for the same symbol.
        """
        info = self._load_tickers().get(symbol.upper())
        if not info:
            raise ValueError(f"Unknown US ticker on EDGAR: {symbol}")
        cik = cik10(info["cik"])
        self._doc_text_cache = {}
        filings = self.fetch_filings(symbol, start=_HISTORY_START, end=today_iso())

        out: list[DemandSignal] = []
        price_band: list[DemandSignal] = []
        try:
            price_band = self._price_band_signal(symbol, filings)
            out.extend(price_band)
        except Exception as exc:  # noqa: BLE001
            log.warning("price-band signal failed for %s: %s", symbol, exc)

        try:
            out.extend(self._anchor_investor_signal(symbol, filings))
        except Exception as exc:  # noqa: BLE001
            log.warning("anchor-investor signal failed for %s: %s", symbol, exc)

        try:
            out.extend(self._confidential_review_signal(symbol, filings))
        except Exception as exc:  # noqa: BLE001
            log.warning("confidential-review signal failed for %s: %s", symbol, exc)

        try:
            out.extend(self._nasdaq_demand_signal(symbol))
        except Exception as exc:  # noqa: BLE001
            log.warning("Nasdaq calendar signal failed for %s: %s", symbol, exc)

        ipo_date = price_band[0].signal_date if price_band else None
        try:
            out.extend(self._edgar_fulltext_demand_signal(symbol, cik, ipo_date))
        except Exception as exc:  # noqa: BLE001
            log.warning("oversubscribed full-text signal failed for %s: %s", symbol, exc)

        try:
            out.extend(self._ttw_fulltext_signal(symbol, cik, ipo_date))
        except Exception as exc:  # noqa: BLE001
            log.warning("TTW full-text signal failed for %s: %s", symbol, exc)

        return out

    def _registration_and_424b_filings(
        self, filings: list[Filing]
    ) -> tuple[list[Filing], list[Filing]]:
        """Split `filings` into (S-1/S-1-A/F-1/F-1-A, 424B*) subsets,
        url-having and filed-date sorted — the document set both
        `_price_band_signal` and `_anchor_investor_signal` scan. Covers both
        domestic (S-1) and foreign-private-issuer (F-1) registrants; see
        `_REGISTRATION_FORMS`.
        """
        regs = sorted(
            (f for f in filings if f.filing_type in _REGISTRATION_FORMS and f.url),
            key=lambda f: f.filed_date or "",
        )
        prospectuses = sorted(
            (f for f in filings if f.filing_type and f.filing_type.startswith("424B") and f.url),
            key=lambda f: f.filed_date or "",
        )
        return regs, prospectuses

    def _price_band_signal(self, symbol: str, filings: list[Filing]) -> list[DemandSignal]:
        regs, prospectuses = self._registration_and_424b_filings(filings)

        range_lo = range_hi = range_date = None
        for f in regs:
            text = self._fetch_doc_text(f.url)
            m = _PRICE_RANGE_RE.search(text) if text else None
            if m:
                range_lo, range_hi, range_date = float(m.group(1)), float(m.group(2)), f.filed_date
                break

        final_price = final_date = final_url = None
        for f in prospectuses:
            text = self._fetch_doc_text(f.url)
            m = _FINAL_PRICE_RE.search(text) if text else None
            if m:
                final_price, final_date, final_url = float(m.group(1)), f.filed_date, f.url
                break

        if range_lo is None and final_price is None:
            return []
        if range_lo is not None and final_price is not None:
            pct = (final_price - range_hi) / range_hi * 100
            detail = (
                f"Initial range ${range_lo:.2f}-${range_hi:.2f} ({range_date}) "
                f"-> final ${final_price:.2f} ({final_date}), {pct:+.0f}% vs. range top"
            )
        elif final_price is not None:
            detail = f"Final price ${final_price:.2f} ({final_date}); no price range found in registration filings"
        else:
            detail = f"Initial range ${range_lo:.2f}-${range_hi:.2f} ({range_date}); no 424B priced yet"
        return [DemandSignal(
            symbol=symbol, market="US", source="edgar",
            signal_date=final_date or range_date,
            signal_type="price_band",
            price=final_price,
            detail=detail,
            url=final_url or regs[-1].url,
        )]

    def _anchor_investor_signal(self, symbol: str, filings: list[Filing]) -> list[DemandSignal]:
        """Anchor-investor "indication of interest" disclosures in
        registration-statement amendments and 424B filings — the closest US
        analog to a Korean cornerstone-investor allocation. Unlike
        `_price_band_signal`, this collects every match across every
        scanned document rather than stopping at the first: multiple
        distinct investors in one filing, and the same disclosure repeated
        across amendments, are both common. Investor-name extraction is
        best-effort free text (not cleaned/normalized), so `detail` keeps
        not just the matched sentence but ~`_EXCERPT_RADIUS` chars of
        surrounding text for a human to verify — real examples carry
        relevant detail the regex itself never captures (e.g. an existing
        indirect stake being converted to direct shares concurrently with
        the offering, or the investor's resulting post-IPO ownership %).
        """
        regs, prospectuses = self._registration_and_424b_filings(filings)
        docs = sorted((f for f in regs + prospectuses if f.filed_date), key=lambda f: f.filed_date)
        out: list[DemandSignal] = []
        seen: set[tuple[str, str]] = set()
        for f in docs:
            text = self._fetch_doc_text(f.url)
            if not text:
                continue
            for m in _ANCHOR_INVESTOR_RE.finditer(text):
                name = m.group(1).strip()
                amount = _parse_scaled_amount(m.group(2), m.group(3))
                if amount is None:
                    continue
                key = (name.lower(), f"{amount:.2f}")
                if key in seen:  # same disclosure repeated verbatim across amendments
                    continue
                seen.add(key)
                out.append(DemandSignal(
                    symbol=symbol, market="US", source="edgar",
                    signal_date=f.filed_date,
                    signal_type="anchor_investor",
                    investor_name=name,
                    indicated_amount=amount,
                    detail=f'{f.filing_type} ({f.filed_date}): "{_excerpt(text, m.start(), m.end())}"',
                    url=f.url,
                ))
        return out

    def _confidential_review_signal(self, symbol: str, filings: list[Filing]) -> list[DemandSignal]:
        """Length of the confidential DRS review period before the public
        registration statement — a time-*window* proxy for how long the
        company had to run TTW/NDR meetings before going public, not a
        content signal. Rule 163B exempts TTW communications themselves
        from any SEC filing, so EDGAR never shows what was said — only,
        indirectly, how long the company had to say it (see
        `_ttw_fulltext_signal` for the closest thing to a content signal).
        Covers both S-1 (domestic) and F-1 (foreign private issuer) filers;
        see `_REGISTRATION_FORMS`.
        """
        drs = sorted(
            (f for f in filings if f.filing_type in ("DRS", "DRS/A") and f.filed_date),
            key=lambda f: f.filed_date,
        )
        regs = sorted(
            (f for f in filings if f.filing_type in _REGISTRATION_FORMS and f.filed_date),
            key=lambda f: f.filed_date,
        )
        if not drs or not regs:
            return []
        first_drs, first_reg = drs[0], regs[0]
        days = _days_between(first_drs.filed_date, first_reg.filed_date)
        if days is None or days < 0:
            return []
        detail = (
            f"Confidential DRS review: first draft {first_drs.filed_date} -> "
            f"first public {first_reg.filing_type} {first_reg.filed_date} ({days} days, "
            f"{len(drs)} draft submission(s) on file)"
        )
        return [DemandSignal(
            symbol=symbol, market="US", source="edgar",
            signal_date=first_reg.filed_date,
            signal_type="confidential_review",
            detail=detail,
            url=first_reg.url,
        )]

    def _fetch_doc_text(self, url: str) -> str | None:
        if url in self._doc_text_cache:
            return self._doc_text_cache[url]
        from bs4 import BeautifulSoup

        try:
            content = self.client.get_bytes(url)
        except Exception as exc:  # noqa: BLE001
            log.warning("fetch failed for %s: %s", url, exc)
            self._doc_text_cache[url] = None
            return None
        text = BeautifulSoup(content, "lxml").get_text(separator=" ", strip=True)
        self._doc_text_cache[url] = text
        return text

    def _nasdaq_demand_signal(self, symbol: str) -> list[DemandSignal]:
        """Final deal terms from Nasdaq's IPO calendar — a factual record, NOT a
        demand indicator. Its 'filed' entries carry no price/share fields (only
        a target dollar amount), and many deals hold that target fixed while
        trimming shares as price rises — so filed vs. priced amount comes out
        flat (~0%) even when the deal was heavily oversubscribed. Use
        `_price_band_signal` for an actual demand proxy.
        """
        sym = symbol.upper()
        for yyyymm in _recent_months(24):
            rows = self._load_nasdaq_month(yyyymm).get("priced", {}).get("rows") or []
            for r in rows:
                if str(r.get("proposedTickerSymbol", "")).upper() != sym:
                    continue
                priced_amount = _parse_money(r.get("dollarValueOfSharesOffered"))
                filed_amount = self._nasdaq_filed_amount(r.get("dealID"), yyyymm)
                detail = f"Nasdaq IPO calendar final terms: {r.get('dealStatus') or 'Priced'}"
                if filed_amount and priced_amount:
                    detail += f"; target raise ~${filed_amount:,.0f}, final ${priced_amount:,.0f}"
                return [DemandSignal(
                    symbol=symbol, market="US", source="nasdaq",
                    signal_date=_parse_mdy(r.get("pricedDate")) or today_iso(),
                    signal_type="nasdaq_calendar",
                    filed_amount=filed_amount, priced_amount=priced_amount,
                    price=_parse_money(r.get("proposedSharePrice")),
                    shares=_parse_money(r.get("sharesOffered")),
                    detail=detail,
                    url="https://www.nasdaq.com/market-activity/ipos",
                )]
        return []

    def _nasdaq_filed_amount(self, deal_id: str | None, priced_month: str) -> float | None:
        """Look back up to 6 months from `priced_month` for `deal_id`'s original filed size."""
        if not deal_id:
            return None
        y, m = (int(x) for x in priced_month.split("-"))
        d = _dt.date(y, m, 1)
        for _ in range(6):
            rows = self._load_nasdaq_month(d.strftime("%Y-%m")).get("filed", {}).get("rows") or []
            for r in rows:
                if r.get("dealID") == deal_id:
                    return _parse_money(r.get("dollarValueOfSharesOffered"))
            d = (d.replace(day=1) - _dt.timedelta(days=1)).replace(day=1)
        return None

    def _load_nasdaq_month(self, yyyymm: str) -> dict:
        if yyyymm not in self._nasdaq_months:
            try:
                data = self.client.get_json(
                    NASDAQ_IPO_CALENDAR_URL, params={"date": yyyymm},
                    headers={"User-Agent": _NASDAQ_UA, "Accept": "application/json"},
                )
                self._nasdaq_months[yyyymm] = data.get("data") or {}
            except Exception as exc:  # noqa: BLE001
                log.warning("Nasdaq IPO calendar failed for %s: %s", yyyymm, exc)
                self._nasdaq_months[yyyymm] = {}
        return self._nasdaq_months[yyyymm]

    def _edgar_fulltext_signal(
        self, symbol: str, cik: str, query: str, signal_type: str,
        ipo_date: str | None = None, skip_underwriting_exhibits: bool = False,
    ) -> list[DemandSignal]:
        """SEC full-text hits for `query` across ALL of the company's 8-K/
        424B/S-1 filings — not just the IPO. A company that later does a
        follow-on offering will show up here too. Rather than guess which
        hits are the IPO (risky — SPAC mergers, uplistings etc. make that
        classification unreliable), each hit is annotated with its
        day-offset from `ipo_date` (the price-band signal's confirmed
        pricing date) so a reader can judge at a glance.
        """
        try:
            data = self.client.get_json(
                EDGAR_FULLTEXT_URL,
                params={"q": query, "ciks": cik, "forms": _DEMAND_FORMS},
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("EDGAR full-text search failed for %s (%s): %s", symbol, signal_type, exc)
            return []
        mention = query.strip('"')
        out: list[DemandSignal] = []
        for h in data.get("hits", {}).get("hits", []):
            src = h.get("_source", {})
            acc, fname = h.get("_id", "").split(":", 1) if ":" in h.get("_id", "") else (None, None)
            hit_cik = (src.get("ciks") or [None])[0]
            if not acc or not fname or not hit_cik:
                continue
            # `file_type` (e.g. "EX-1.1") is EDGAR's own structured exhibit
            # classification; `file_description` is filer-supplied free text
            # that happens to usually match it. Prefer file_type, since it's
            # the more reliable field — confirmed both populated identically
            # across real EFTS hits, but file_type is the authoritative one.
            exhibit_type = (src.get("file_type") or src.get("file_description") or "").strip()
            if skip_underwriting_exhibits and _UNDERWRITING_EXHIBIT_RE.match(exhibit_type):
                continue
            base = f"https://www.sec.gov/Archives/edgar/data/{int(hit_cik)}/{acc.replace('-', '')}"
            signal_date = to_iso(src.get("file_date")) or today_iso()
            detail = (
                f"{src.get('form')} {src.get('file_description') or ''}".strip()
                + f' mentions "{mention}"'
            )
            days = _days_between(ipo_date, signal_date) if ipo_date else None
            if days is not None:
                note = "IPO 확정일 대비" if abs(days) <= 30 else "IPO 확정일 대비 (별건일 가능성)"
                detail += f" [{note} {days:+d}일]"
            out.append(DemandSignal(
                symbol=symbol, market="US", source="edgar",
                signal_date=signal_date,
                signal_type=signal_type,
                detail=detail,
                url=f"{base}/{fname}",
            ))
        return out

    def _edgar_fulltext_demand_signal(
        self, symbol: str, cik: str, ipo_date: str | None = None
    ) -> list[DemandSignal]:
        return self._edgar_fulltext_signal(symbol, cik, _DEMAND_QUERY, "sec_fulltext", ipo_date)

    def _ttw_fulltext_signal(
        self, symbol: str, cik: str, ipo_date: str | None = None
    ) -> list[DemandSignal]:
        """Full-text hits for "testing-the-waters". Rule 163B exempts TTW
        communications from any SEC filing requirement, so a company
        describing its own TTW activity in risk-factor language is the only
        trace EDGAR has.

        Excludes Exhibit 1.x hits (`skip_underwriting_exhibits=True`):
        checked against 5 real 2023-2024 IPOs (CAVA, CART, KVYO, RDDT, BIRK),
        every single "testing-the-waters" hit came from the underwriting
        agreement's standard Rule 163B representation clause, filed as
        Exhibit 1.1 — boilerplate nearly every modern IPO carries regardless
        of actual TTW outreach, not substantive disclosure. Without this
        filter the signal is closer to "did this company IPO" than a
        selective TTW indicator. Even with it, treat remaining hits as a
        weaker/noisier signal than "oversubscribed" — this only removes the
        one confirmed boilerplate source, not every false positive.
        """
        return self._edgar_fulltext_signal(
            symbol, cik, _TTW_QUERY, "ttw_fulltext", ipo_date,
            skip_underwriting_exhibits=True,
        )

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


def _recent_months(n: int) -> list[str]:
    """The last `n` 'YYYY-MM' strings ending with the current month."""
    d = _dt.date.today().replace(day=1)
    months = []
    for _ in range(n):
        months.append(d.strftime("%Y-%m"))
        d = (d - _dt.timedelta(days=1)).replace(day=1)
    return months


def _parse_money(v) -> float | None:
    """Nasdaq calendar amounts are strings like '$382,500,000' or '18.00'."""
    if not v:
        return None
    try:
        return float(str(v).replace("$", "").replace(",", ""))
    except ValueError:
        return None


_SCALE = {"million": 1_000_000, "billion": 1_000_000_000}


def _parse_scaled_amount(number: str, scale: str | None) -> float | None:
    """'50', 'million' -> 50_000_000.0; '1.2', 'billion' -> 1_200_000_000.0;
    '50,000,000', None -> 50000000.0 (already in raw dollars).
    """
    try:
        n = float(number.replace(",", ""))
    except (ValueError, AttributeError):
        return None
    return n * _SCALE[scale.lower()] if scale else n


# How much surrounding context to keep around a regex match for a human to
# read (e.g. an anchor-investor sentence is often followed by unrelated-to-
# the-regex but highly relevant detail — existing stake being converted,
# resulting post-IPO ownership % — that the regex itself never captures).
_EXCERPT_RADIUS = 500


def _excerpt(text: str, start: int, end: int, radius: int = _EXCERPT_RADIUS) -> str:
    """`text[start:end]` expanded by `radius` chars on each side, nudged to
    the nearest sentence break — but only on a side that actually got
    truncated by `radius` (if the untruncated side already sits at the
    start/end of `text`, there's nothing to nudge: keep it, since trimming
    unconditionally to "the closest period" would collapse the excerpt back
    down to just the matched sentence whenever there's only one sentence of
    context available on that side, defeating `radius` entirely).
    """
    lo = max(0, start - radius)
    hi = min(len(text), end + radius)
    if lo > 0:
        next_period = text.find(". ", lo, start)
        if next_period != -1:
            lo = next_period + 2
    if hi < len(text):
        prev_period = text.rfind(". ", end, hi)
        if prev_period != -1:
            hi = prev_period + 1
    return text[lo:hi].strip()


def _days_between(iso_a: str, iso_b: str) -> int | None:
    """`iso_b` - `iso_a`, in days. Both must be 'YYYY-MM-DD'."""
    try:
        a = _dt.date.fromisoformat(iso_a)
        b = _dt.date.fromisoformat(iso_b)
        return (b - a).days
    except (ValueError, TypeError):
        return None


def _parse_mdy(v: str | None) -> str | None:
    """Nasdaq calendar dates are 'M/DD/YYYY'; convert to ISO."""
    if not v:
        return None
    try:
        m, d, y = v.split("/")
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
    except ValueError:
        return None


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None
