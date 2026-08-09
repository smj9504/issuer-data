"""Repository: all SQLite reads/writes. Resolves symbols to company/security ids."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from ..logging import get_logger
from ..models import (
    Company,
    Filing,
    FilingDocument,
    FinancialFact,
    FxRate,
    Price,
    Security,
)
from ..utils.dates import today_iso

log = get_logger(__name__)

# Priority order of identifiers for cross-source entity resolution.
_ID_PRIORITY = ("LEI", "CIK", "CORP_CODE", "ISIN")


class Repository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ------------------------------------------------------------------ helpers
    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def commit(self) -> None:
        self.conn.commit()

    # -------------------------------------------------------------- identifiers
    def _company_by_identifier(self, id_type: str, id_value: str) -> int | None:
        if not id_value:
            return None
        row = self._exec(
            "SELECT company_id FROM identifier_xref WHERE id_type=? AND id_value=?",
            (id_type, str(id_value)),
        ).fetchone()
        return row["company_id"] if row else None

    def _register_identifier(
        self, id_type: str, id_value: str | None, company_id: int, source: str | None
    ) -> None:
        if not id_value:
            return
        self._exec(
            "INSERT OR IGNORE INTO identifier_xref(id_type, id_value, company_id, source) "
            "VALUES (?,?,?,?)",
            (id_type, str(id_value), company_id, source),
        )

    def _company_by_name(self, name: str | None, country: str | None) -> int | None:
        if not name:
            return None
        norm = name.strip().lower()
        row = self._exec(
            "SELECT company_id FROM companies WHERE lower(name)=? "
            "AND (country IS ? OR country=?) LIMIT 1",
            (norm, country, country),
        ).fetchone()
        return row["company_id"] if row else None

    # ------------------------------------------------------------------ company
    def resolve_company(self, company: Company, *, allow_name_match: bool = True) -> int:
        """Find or create a company_id, registering its identifiers."""
        identifiers = {
            "LEI": company.lei,
            "CIK": company.cik,
            "CORP_CODE": company.corp_code,
            "ISIN": company.isin,
        }
        # 1. Strong-identifier match
        company_id: int | None = None
        for id_type in _ID_PRIORITY:
            company_id = self._company_by_identifier(id_type, identifiers[id_type])
            if company_id:
                break
        # 2. Fallback name match
        if company_id is None and allow_name_match:
            company_id = self._company_by_name(company.name, company.country)
        # 3. Create
        if company_id is None:
            company_id = self._insert_company(company)
        else:
            self._enrich_company(company_id, company)
        # register all known identifiers
        for id_type, val in identifiers.items():
            self._register_identifier(id_type, val, company_id, company.source)
        return company_id

    def _insert_company(self, c: Company) -> int:
        cur = self._exec(
            "INSERT INTO companies(name, local_name, country, sector, industry, lei, cik, "
            "corp_code, isin, website, source, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (c.name, c.local_name, c.country, c.sector, c.industry, c.lei, c.cik,
             c.corp_code, c.isin, c.website, c.source, today_iso()),
        )
        return int(cur.lastrowid)

    def _enrich_company(self, company_id: int, c: Company) -> None:
        """Fill NULL columns on an existing company from a new record (COALESCE)."""
        self._exec(
            "UPDATE companies SET "
            "name=COALESCE(name,?), local_name=COALESCE(local_name,?), "
            "country=COALESCE(country,?), sector=COALESCE(sector,?), "
            "industry=COALESCE(industry,?), lei=COALESCE(lei,?), cik=COALESCE(cik,?), "
            "corp_code=COALESCE(corp_code,?), isin=COALESCE(isin,?), "
            "website=COALESCE(website,?), updated_at=? WHERE company_id=?",
            (c.name, c.local_name, c.country, c.sector, c.industry, c.lei, c.cik,
             c.corp_code, c.isin, c.website, today_iso(), company_id),
        )

    def link_identifier(self, id_type: str, id_value: str, company_id: int, source: str) -> None:
        """Public helper for the `link` command / overrides."""
        self._exec(
            "INSERT OR REPLACE INTO identifier_xref(id_type, id_value, company_id, source) "
            "VALUES (?,?,?,?)",
            (id_type, str(id_value), company_id, source),
        )

    # ----------------------------------------------------------------- security
    def upsert_security(self, security: Security, company_id: int) -> int:
        existing = self._exec(
            "SELECT security_id FROM securities WHERE market=? AND symbol=?",
            (security.market, security.symbol),
        ).fetchone()
        if existing:
            sid = existing["security_id"]
            self._exec(
                "UPDATE securities SET company_id=?, exchange=COALESCE(?,exchange), "
                "security_type=COALESCE(?,security_type), currency=COALESCE(?,currency), "
                "isin=COALESCE(?,isin), listing_date=COALESCE(?,listing_date), "
                "is_primary=?, updated_at=? WHERE security_id=?",
                (company_id, security.exchange, security.security_type, security.currency,
                 security.isin, security.listing_date, int(security.is_primary),
                 today_iso(), sid),
            )
            return sid
        cur = self._exec(
            "INSERT INTO securities(company_id, market, symbol, exchange, security_type, "
            "currency, isin, listing_date, is_primary, source, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (company_id, security.market, security.symbol, security.exchange,
             security.security_type, security.currency, security.isin,
             security.listing_date, int(security.is_primary), security.source, today_iso()),
        )
        sid = int(cur.lastrowid)
        # register the ticker as an identifier too
        self._register_identifier("TICKER", f"{security.market}:{security.symbol}",
                                  company_id, security.source)
        return sid

    def get_security_id(self, market: str, symbol: str) -> int | None:
        row = self._exec(
            "SELECT security_id FROM securities WHERE market=? AND symbol=?",
            (market, symbol),
        ).fetchone()
        return row["security_id"] if row else None

    def get_company_id_for_symbol(self, market: str, symbol: str) -> int | None:
        row = self._exec(
            "SELECT company_id FROM securities WHERE market=? AND symbol=?",
            (market, symbol),
        ).fetchone()
        return row["company_id"] if row else None

    # -------------------------------------------------------------------- prices
    def upsert_prices(self, security_id: int, prices: Iterable[Price]) -> int:
        n = 0
        for p in prices:
            self._exec(
                "INSERT INTO prices(security_id, trade_date, open, high, low, close, "
                "volume, adj_close, currency, source) VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(security_id, trade_date, source) DO UPDATE SET "
                "open=excluded.open, high=excluded.high, low=excluded.low, "
                "close=excluded.close, volume=excluded.volume, adj_close=excluded.adj_close, "
                "currency=excluded.currency",
                (security_id, p.trade_date, p.open, p.high, p.low, p.close, p.volume,
                 p.adj_close, p.currency, p.source),
            )
            n += 1
        return n

    # --------------------------------------------------------------- financials
    def upsert_financials(self, company_id: int, facts: Iterable[FinancialFact]) -> int:
        n = 0
        for f in facts:
            self._exec(
                "INSERT INTO financials(company_id, fiscal_year, fiscal_period, "
                "statement_type, account, account_local, value, currency, unit, "
                "period_end, source) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(company_id, fiscal_year, fiscal_period, statement_type, "
                "account, source) DO UPDATE SET value=excluded.value, "
                "account_local=COALESCE(excluded.account_local, account_local), "
                "currency=excluded.currency, unit=excluded.unit, period_end=excluded.period_end",
                (company_id, f.fiscal_year, f.fiscal_period, f.statement_type or "",
                 f.account, f.account_local, f.value, f.currency, f.unit, f.period_end,
                 f.source),
            )
            n += 1
        return n

    # ------------------------------------------------------------------ filings
    def upsert_filings(self, company_id: int, filings: Iterable[Filing]) -> int:
        n = 0
        for fl in filings:
            self._exec(
                "INSERT INTO filings(company_id, filing_id, filed_date, filing_type, "
                "title, url, source) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(company_id, filing_id, source) DO UPDATE SET "
                "filed_date=excluded.filed_date, filing_type=excluded.filing_type, "
                "title=excluded.title, url=excluded.url",
                (company_id, fl.filing_id, fl.filed_date, fl.filing_type, fl.title,
                 fl.url, fl.source),
            )
            n += 1
        return n

    def upsert_filing_document(self, doc: FilingDocument) -> None:
        self._exec(
            "INSERT INTO filing_documents(company_id, filing_id, source, doc_seq, doc_url, "
            "local_path, doc_format, file_size, text_content, text_chars, downloaded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(company_id, filing_id, source, doc_seq) DO UPDATE SET "
            "doc_url=excluded.doc_url, local_path=excluded.local_path, "
            "doc_format=excluded.doc_format, file_size=excluded.file_size, "
            "text_content=excluded.text_content, text_chars=excluded.text_chars, "
            "downloaded_at=excluded.downloaded_at",
            (doc.company_id, doc.filing_id, doc.source, doc.doc_seq, doc.doc_url,
             doc.local_path, doc.doc_format, doc.file_size, doc.text_content,
             doc.text_chars, doc.downloaded_at),
        )

    def filings_without_documents(self, company_id: int | None = None, limit: int | None = None):
        sql = (
            "SELECT f.company_id, f.filing_id, f.source, f.url, f.filing_type "
            "FROM filings f WHERE NOT EXISTS ("
            "  SELECT 1 FROM filing_documents d WHERE d.company_id=f.company_id "
            "  AND d.filing_id=f.filing_id AND d.source=f.source)"
        )
        params: list = []
        if company_id is not None:
            sql += " AND f.company_id=?"
            params.append(company_id)
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return self._exec(sql, tuple(params)).fetchall()

    def symbol_for_company(self, company_id: int) -> tuple[str, str] | None:
        row = self._exec(
            "SELECT market, symbol FROM securities WHERE company_id=? "
            "ORDER BY is_primary DESC LIMIT 1",
            (company_id,),
        ).fetchone()
        return (row["market"], row["symbol"]) if row else None

    # ----------------------------------------------------------------------- fx
    def upsert_fx_rates(self, rates: Iterable[FxRate]) -> int:
        n = 0
        for r in rates:
            self._exec(
                "INSERT INTO fx_rates(rate_date, base_ccy, quote_ccy, rate, source) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(rate_date, base_ccy, quote_ccy, source) DO UPDATE SET "
                "rate=excluded.rate",
                (r.rate_date, r.base_ccy, r.quote_ccy, r.rate, r.source),
            )
            n += 1
        return n

    # --------------------------------------------------------------------- peers
    def upsert_peer(self, company_id: int, peer_company_id: int, relation: str, source: str) -> None:
        if company_id == peer_company_id:
            return
        self._exec(
            "INSERT OR IGNORE INTO company_peers(company_id, peer_company_id, relation, source) "
            "VALUES (?,?,?,?)",
            (company_id, peer_company_id, relation, source),
        )

    # --------------------------------------------------------------- run logging
    def start_run(self, market: str, data_type: str, source: str) -> int:
        cur = self._exec(
            "INSERT INTO collection_runs(market, data_type, source, started_at, status) "
            "VALUES (?,?,?,?,?)",
            (market, data_type, source, today_iso(), "running"),
        )
        self.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, rows: int, error: str | None = None) -> None:
        self._exec(
            "UPDATE collection_runs SET finished_at=?, status=?, rows_written=?, error=? "
            "WHERE run_id=?",
            (today_iso(), status, rows, error, run_id),
        )
        self.commit()
