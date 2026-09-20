"""Repository: all PostgreSQL reads/writes. Resolves symbols to company/security ids."""

from __future__ import annotations

import json
from collections.abc import Iterable

import psycopg

from ..logging import get_logger
from ..models import (
    Company,
    Filing,
    FilingDocument,
    FinancialFact,
    FxRate,
    LawApiRawItem,
    Price,
    Security,
    StatuteComparisonEntry,
    StatuteDetail,
    StatuteHistoryEntry,
    StatuteRecord,
    StatuteTranslation,
)
from ..utils.dates import today_iso

log = get_logger(__name__)

# Priority order of identifiers for cross-source entity resolution.
_ID_PRIORITY = ("LEI", "CIK", "CORP_CODE", "ISIN")


class Repository:
    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn
        self._pk_cache: dict[str, tuple[str, ...]] = {}

    # ------------------------------------------------------------------ helpers
    def _exec(self, sql: str, params: tuple = ()) -> psycopg.Cursor:
        return self.conn.execute(sql, params)

    def commit(self) -> None:
        self.conn.commit()

    # -------------------------------------------------------------- identifiers
    def _company_by_identifier(self, id_type: str, id_value: str) -> int | None:
        if not id_value:
            return None
        row = self._exec(
            "SELECT company_id FROM identifier_xref WHERE id_type=%s AND id_value=%s",
            (id_type, str(id_value)),
        ).fetchone()
        return row["company_id"] if row else None

    def _register_identifier(
        self, id_type: str, id_value: str | None, company_id: int, source: str | None
    ) -> None:
        if not id_value:
            return
        self._exec(
            # DO NOTHING here, unlike link_identifier: the first source to claim
            # an identifier keeps it, so a later collector cannot quietly move it.
            "INSERT INTO identifier_xref(id_type, id_value, company_id, source) "
            "VALUES (%s,%s,%s,%s) ON CONFLICT (id_type, id_value) DO NOTHING",
            (id_type, str(id_value), company_id, source),
        )

    def _company_by_name(self, name: str | None, country: str | None) -> int | None:
        if not name:
            return None
        norm = name.strip().lower()
        row = self._exec(
            # IS NOT DISTINCT FROM, so a NULL country matches a NULL argument;
            # plain `=` would leave every unknown-domicile company unmatched.
            "SELECT company_id FROM companies WHERE lower(name)=%s "
            "AND country IS NOT DISTINCT FROM %s LIMIT 1",
            (norm, country),
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
            "corp_code, isin, website, source, updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING company_id",
            (c.name, c.local_name, c.country, c.sector, c.industry, c.lei, c.cik,
             c.corp_code, c.isin, c.website, c.source, today_iso()),
        )
        return int(cur.fetchone()["company_id"])

    def _enrich_company(self, company_id: int, c: Company) -> None:
        """Fill NULL columns on an existing company from a new record (COALESCE)."""
        self._exec(
            "UPDATE companies SET "
            "name=COALESCE(name,%s), local_name=COALESCE(local_name,%s), "
            "country=COALESCE(country,%s), sector=COALESCE(sector,%s), "
            "industry=COALESCE(industry,%s), lei=COALESCE(lei,%s), cik=COALESCE(cik,%s), "
            "corp_code=COALESCE(corp_code,%s), isin=COALESCE(isin,%s), "
            "website=COALESCE(website,%s), updated_at=%s WHERE company_id=%s",
            (c.name, c.local_name, c.country, c.sector, c.industry, c.lei, c.cik,
             c.corp_code, c.isin, c.website, today_iso(), company_id),
        )

    def link_identifier(self, id_type: str, id_value: str, company_id: int, source: str) -> None:
        """Public helper for the `link` command / overrides."""
        self._exec(
            # DO UPDATE, not DO NOTHING: the point of `link` is to move an
            # identifier onto a different company, which a no-op would ignore.
            "INSERT INTO identifier_xref(id_type, id_value, company_id, source) "
            "VALUES (%s,%s,%s,%s) ON CONFLICT (id_type, id_value) DO UPDATE SET "
            "company_id=EXCLUDED.company_id, source=EXCLUDED.source",
            (id_type, str(id_value), company_id, source),
        )

    # ----------------------------------------------------------------- security
    def upsert_security(self, security: Security, company_id: int) -> int:
        existing = self._exec(
            "SELECT security_id FROM securities WHERE market=%s AND symbol=%s",
            (security.market, security.symbol),
        ).fetchone()
        if existing:
            sid = existing["security_id"]
            self._exec(
                "UPDATE securities SET company_id=%s, exchange=COALESCE(%s,exchange), "
                "security_type=COALESCE(%s,security_type), currency=COALESCE(%s,currency), "
                "isin=COALESCE(%s,isin), listing_date=COALESCE(%s,listing_date), "
                "is_primary=%s, updated_at=%s WHERE security_id=%s",
                (company_id, security.exchange, security.security_type, security.currency,
                 security.isin, security.listing_date, int(security.is_primary),
                 today_iso(), sid),
            )
            return sid
        cur = self._exec(
            "INSERT INTO securities(company_id, market, symbol, exchange, security_type, "
            "currency, isin, listing_date, is_primary, source, updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING security_id",
            (company_id, security.market, security.symbol, security.exchange,
             security.security_type, security.currency, security.isin,
             security.listing_date, int(security.is_primary), security.source, today_iso()),
        )
        sid = int(cur.fetchone()["security_id"])
        # register the ticker as an identifier too
        self._register_identifier("TICKER", f"{security.market}:{security.symbol}",
                                  company_id, security.source)
        return sid

    def get_security_id(self, market: str, symbol: str) -> int | None:
        row = self._exec(
            "SELECT security_id FROM securities WHERE market=%s AND symbol=%s",
            (market, symbol),
        ).fetchone()
        return row["security_id"] if row else None

    def get_company_id_for_symbol(self, market: str, symbol: str) -> int | None:
        row = self._exec(
            "SELECT company_id FROM securities WHERE market=%s AND symbol=%s",
            (market, symbol),
        ).fetchone()
        return row["company_id"] if row else None

    # -------------------------------------------------------------------- prices
    def upsert_prices(self, security_id: int, prices: Iterable[Price]) -> int:
        n = 0
        for p in prices:
            self._exec(
                "INSERT INTO prices(security_id, trade_date, open, high, low, close, "
                "volume, adj_close, currency, source) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
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
                "INSERT INTO financials(company_id, fiscal_year, fiscal_period, fs_scope, "
                "statement_type, account, account_local, value, currency, unit, "
                "period_end, source) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(company_id, fiscal_year, fiscal_period, fs_scope, statement_type, "
                # financials.account_local, qualified: unadorned it is ambiguous
                # between the stored row and the proposed one. COALESCE keeps a
                # local account name a later source does not carry.
                "account, source) DO UPDATE SET value=EXCLUDED.value, "
                "account_local=COALESCE(EXCLUDED.account_local, financials.account_local), "
                "currency=EXCLUDED.currency, unit=EXCLUDED.unit, "
                "period_end=EXCLUDED.period_end",
                (company_id, f.fiscal_year, f.fiscal_period, f.fs_scope or "CFS",
                 f.statement_type or "", f.account, f.account_local, f.value, f.currency,
                 f.unit, f.period_end, f.source),
            )
            n += 1
        return n

    # ------------------------------------------------------------------ filings
    def upsert_filings(self, company_id: int, filings: Iterable[Filing]) -> int:
        n = 0
        for fl in filings:
            doc_urls = json.dumps(fl.doc_urls or [])
            self._exec(
                "INSERT INTO filings(company_id, filing_id, filed_date, filing_type, "
                "title, url, doc_urls, source) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(company_id, filing_id, source) DO UPDATE SET "
                "filed_date=excluded.filed_date, filing_type=excluded.filing_type, "
                "title=excluded.title, url=excluded.url, doc_urls=excluded.doc_urls",
                (company_id, fl.filing_id, fl.filed_date, fl.filing_type, fl.title,
                 fl.url, doc_urls, fl.source),
            )
            n += 1
        return n

    def upsert_filing_document(self, doc: FilingDocument) -> None:
        self._exec(
            "INSERT INTO filing_documents(company_id, filing_id, source, doc_seq, doc_url, "
            "local_path, doc_format, file_size, text_content, text_chars, downloaded_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(company_id, filing_id, source, doc_seq) DO UPDATE SET "
            "doc_url=excluded.doc_url, local_path=excluded.local_path, "
            "doc_format=excluded.doc_format, file_size=excluded.file_size, "
            "text_content=excluded.text_content, text_chars=excluded.text_chars, "
            "downloaded_at=excluded.downloaded_at",
            (doc.company_id, doc.filing_id, doc.source, doc.doc_seq, doc.doc_url,
             doc.local_path, doc.doc_format, doc.file_size, doc.text_content,
             doc.text_chars, doc.downloaded_at),
        )

    def upsert_filing_tables(self, company_id: int, filing_id: str, source: str,
                             doc_seq: int, tables) -> int:
        """Persist stitched tables (one row per cell). Replaces any prior extraction
        for this (company, filing, source, doc_seq) so re-runs stay idempotent."""
        self._exec(
            "DELETE FROM filing_tables WHERE company_id=%s AND filing_id=%s AND source=%s "
            "AND doc_seq=%s", (company_id, filing_id, source, doc_seq),
        )
        n = 0
        for table_seq, tbl in enumerate(tables):
            for row_idx, row in enumerate(tbl.rows):
                for col_idx, value in enumerate(row):
                    self._exec(
                        "INSERT INTO filing_tables(company_id, filing_id, source, doc_seq, "
                        "table_seq, row_idx, col_idx, value, page_start, page_end, "
                        "confidence, needs_review, source_engine) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (company_id, filing_id, source, doc_seq, table_seq, row_idx,
                         col_idx, value, tbl.page_start, tbl.page_end, tbl.confidence,
                         1 if getattr(tbl, "needs_review", False) else 0, tbl.source_engine),
                    )
                    n += 1
        return n

    def upsert_extraction_report(self, company_id: int, filing_id: str, source: str,
                                 doc_seq: int, report) -> None:
        """Record the document-level verdict so a bad parse is visible, not silent.

        Idempotent per document: a re-extraction overwrites its own verdict but
        clears ``reviewed_at``, because a human's sign-off applies to the parse
        they actually looked at, not to whatever replaced it.
        """
        import json as _json

        cov = getattr(report, "coverage", None)
        arith = getattr(report, "arithmetic", None)
        cross = getattr(report, "crosscheck", None) or {}
        self._exec(
            "INSERT INTO filing_extraction_reports(company_id, filing_id, source, doc_seq, "
            "verdict, reasons, token_recall, numeric_recall, arith_checks, arith_passed, "
            "agreement, crosscheck, tables_total, tables_flagged, detail) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(company_id, filing_id, source, doc_seq) DO UPDATE SET "
            "verdict=excluded.verdict, reasons=excluded.reasons, "
            "token_recall=excluded.token_recall, numeric_recall=excluded.numeric_recall, "
            "arith_checks=excluded.arith_checks, arith_passed=excluded.arith_passed, "
            "agreement=excluded.agreement, crosscheck=excluded.crosscheck, "
            "tables_total=excluded.tables_total, tables_flagged=excluded.tables_flagged, "
            "detail=excluded.detail, reviewed_at=NULL, checked_at=to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')",
            (company_id, filing_id, source, doc_seq, report.verdict,
             "\n".join(report.reasons), getattr(cov, "token_recall", None),
             getattr(cov, "numeric_recall", None), getattr(arith, "checks", 0),
             getattr(arith, "passed", 0), report.agreement, cross.get("score"),
             report.tables_total, report.tables_flagged,
             _json.dumps(report.to_dict(), ensure_ascii=False)),
        )

    def extraction_review_queue(self, limit: int = 50, verdict: str | None = None):
        """Documents awaiting a human, worst first. The queue the checks exist for."""
        sql = ["SELECT * FROM filing_extraction_reports WHERE reviewed_at IS NULL"]
        params: list = []
        if verdict:
            sql.append("AND verdict = %s")
            params.append(verdict)
        else:
            sql.append("AND verdict <> 'pass'")
        sql.append("ORDER BY CASE verdict WHEN 'fail' THEN 0 ELSE 1 END, "
                   "token_recall ASC, checked_at DESC LIMIT %s")
        params.append(limit)
        return self._exec(" ".join(sql), tuple(params)).fetchall()

    def filings_without_documents(self, company_id: int | None = None, limit: int | None = None):
        sql = (
            "SELECT f.company_id, f.filing_id, f.source, f.url, f.filing_type "
            "FROM filings f WHERE NOT EXISTS ("
            "  SELECT 1 FROM filing_documents d WHERE d.company_id=f.company_id "
            "  AND d.filing_id=f.filing_id AND d.source=f.source)"
        )
        params: list = []
        if company_id is not None:
            sql += " AND f.company_id=%s"
            params.append(company_id)
        if limit:
            sql += " LIMIT %s"
            params.append(limit)
        return self._exec(sql, tuple(params)).fetchall()

    def symbol_for_company(self, company_id: int) -> tuple[str, str] | None:
        row = self._exec(
            "SELECT market, symbol FROM securities WHERE company_id=%s "
            "ORDER BY is_primary DESC LIMIT 1",
            (company_id,),
        ).fetchone()
        return (row["market"], row["symbol"]) if row else None

    # ----------------------------------------------------------------------- fx
    def upsert_fx_rates(self, rates: Iterable[FxRate]) -> int:
        n = 0
        for r in rates:
            self._exec(
                "INSERT INTO fx_rates(rate_date, base_ccy, quote_ccy, rate_type, rate, source) "
                "VALUES (%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(rate_date, base_ccy, quote_ccy, rate_type, source) DO UPDATE SET "
                "rate=excluded.rate",
                (r.rate_date, r.base_ccy, r.quote_ccy, r.rate_type, r.rate, r.source),
            )
            n += 1
        return n

    # --------------------------------------------------------------------- peers
    def upsert_peer(self, company_id: int, peer_company_id: int, relation: str,
                    source: str, *, direction: int | None = None,
                    weight: float | None = None, evidence: str | None = None) -> None:
        if company_id == peer_company_id:
            return
        self._exec(
            # relation is part of the conflict target, so a pair that is both a
            # competitor and a customer keeps both edges; keyed on the pair
            # alone, the second one used to be dropped here without a word.
            #
            # DO UPDATE, not DO NOTHING: re-scoring an edge is the point, and a
            # no-op would pin it to whatever was written first. COALESCE keeps a
            # direction/weight/evidence the incoming row does not carry, so a
            # classification sweep that knows none of the three cannot blank a
            # hand-scored edge by running over it.
            "INSERT INTO company_peers(company_id, peer_company_id, relation, "
            "direction, weight, evidence, source) VALUES (%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (company_id, peer_company_id, relation, source) DO UPDATE SET "
            "direction=COALESCE(EXCLUDED.direction, company_peers.direction), "
            "weight=COALESCE(EXCLUDED.weight, company_peers.weight), "
            "evidence=COALESCE(EXCLUDED.evidence, company_peers.evidence)",
            (company_id, peer_company_id, relation, direction, weight, evidence, source),
        )

    # -------------------------------------------------------------- statutes
    # National reference data (국가법령정보 OpenAPI) — keyed by (law_id, source),
    # not tied to a symbol. `company_id` is an optional caller-supplied link.
    def upsert_statutes(
        self, records: Iterable[StatuteRecord], company_id: int | None = None
    ) -> int:
        n = 0
        for r in records:
            self._exec(
                "INSERT INTO statutes(law_id, law_serial_no, name, name_abbrev, law_type, "
                "department, promulgation_date, promulgation_no, enforcement_date, "
                "revision_type, detail_url, company_id, source, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(law_id, source) DO UPDATE SET "
                "law_serial_no=excluded.law_serial_no, name=excluded.name, "
                "name_abbrev=excluded.name_abbrev, law_type=excluded.law_type, "
                "department=excluded.department, promulgation_date=excluded.promulgation_date, "
                "promulgation_no=excluded.promulgation_no, "
                "enforcement_date=excluded.enforcement_date, "
                "revision_type=excluded.revision_type, detail_url=excluded.detail_url, "
                "company_id=COALESCE(excluded.company_id, statutes.company_id), "
                "updated_at=excluded.updated_at",
                (r.law_id, r.law_serial_no, r.name, r.name_abbrev, r.law_type, r.department,
                 r.promulgation_date, r.promulgation_no, r.enforcement_date, r.revision_type,
                 r.detail_url, company_id, r.source, today_iso()),
            )
            n += 1
        return n

    def upsert_statute_detail(self, detail: StatuteDetail, company_id: int | None = None) -> None:
        """Merge a body-fetch result into `statutes` (insert the row if it doesn't exist yet)."""
        articles_json = json.dumps(
            [a.model_dump() for a in detail.articles], ensure_ascii=False
        ) if detail.articles else None
        self._exec(
            "INSERT INTO statutes(law_id, law_serial_no, name, department, "
            "promulgation_date, enforcement_date, articles_json, raw_text, company_id, "
            "source, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(law_id, source) DO UPDATE SET "
            "law_serial_no=excluded.law_serial_no, name=excluded.name, "
            "department=COALESCE(excluded.department, statutes.department), "
            "promulgation_date=COALESCE(excluded.promulgation_date, statutes.promulgation_date), "
            "enforcement_date=COALESCE(excluded.enforcement_date, statutes.enforcement_date), "
            "articles_json=excluded.articles_json, raw_text=excluded.raw_text, "
            "company_id=COALESCE(excluded.company_id, statutes.company_id), "
            "updated_at=excluded.updated_at",
            (detail.law_id, detail.law_serial_no, detail.name, detail.department,
             detail.promulgation_date, detail.enforcement_date, articles_json, detail.raw_text,
             company_id, detail.source, today_iso()),
        )

    def upsert_statute_history(
        self, entries: Iterable[StatuteHistoryEntry], company_id: int | None = None
    ) -> int:
        n = 0
        for h in entries:
            self._exec(
                "INSERT INTO statute_history(law_id, law_serial_no, name, enforcement_date, "
                "promulgation_date, revision_type, status, company_id, source) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(law_id, law_serial_no, source) DO UPDATE SET "
                "name=excluded.name, enforcement_date=excluded.enforcement_date, "
                "promulgation_date=excluded.promulgation_date, "
                "revision_type=excluded.revision_type, status=excluded.status, "
                "company_id=COALESCE(excluded.company_id, statute_history.company_id)",
                (h.law_id, h.law_serial_no, h.name, h.enforcement_date, h.promulgation_date,
                 h.revision_type, h.status, company_id, h.source),
            )
            n += 1
        return n

    def upsert_statute_translations(
        self, entries: Iterable[StatuteTranslation], company_id: int | None = None
    ) -> int:
        n = 0
        for t in entries:
            self._exec(
                "INSERT INTO statute_translations(law_id, law_serial_no, name_en, content_en, "
                "company_id, source) VALUES (%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(law_id, source) DO UPDATE SET "
                "law_serial_no=excluded.law_serial_no, name_en=excluded.name_en, "
                "content_en=excluded.content_en, "
                "company_id=COALESCE(excluded.company_id, statute_translations.company_id)",
                (t.law_id, t.law_serial_no, t.name_en, t.content_en, company_id, t.source),
            )
            n += 1
        return n

    def upsert_statute_comparisons(
        self, entries: Iterable[StatuteComparisonEntry], company_id: int | None = None
    ) -> int:
        n = 0
        for c in entries:
            self._exec(
                "INSERT INTO statute_comparisons(law_id, article_no, old_text, new_text, "
                "company_id, source) VALUES (%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(law_id, article_no, source) DO UPDATE SET "
                "old_text=excluded.old_text, new_text=excluded.new_text, "
                "company_id=COALESCE(excluded.company_id, statute_comparisons.company_id)",
                (c.law_id, c.article_no, c.old_text, c.new_text, company_id, c.source),
            )
            n += 1
        return n

    def link_statute_to_company(self, law_id: str, source: str, company_id: int) -> None:
        """Attach (or reassign) the optional issuer link on an already-stored statute."""
        self._exec(
            "UPDATE statutes SET company_id=%s WHERE law_id=%s AND source=%s",
            (company_id, law_id, source),
        )

    def upsert_law_api_raw(
        self, items: Iterable[LawApiRawItem], company_id: int | None = None
    ) -> int:
        """Catch-all for statute-API categories without a dedicated table (see kr_law.py)."""
        n = 0
        for it in items:
            self._exec(
                "INSERT INTO law_api_raw(target, item_key, title, payload_json, company_id, "
                "source, fetched_at) VALUES (%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(target, item_key, source) DO UPDATE SET "
                "title=excluded.title, payload_json=excluded.payload_json, "
                "company_id=COALESCE(excluded.company_id, law_api_raw.company_id), "
                "fetched_at=excluded.fetched_at",
                (it.target, it.item_key, it.title, it.payload_json, company_id, it.source,
                 today_iso()),
            )
            n += 1
        return n

    # ------------------------------------------------------- coverage (generic)
    # grain -> (fk column, resolver method name)
    _COMPANY_GRAIN = ("company_id", "get_company_id_for_symbol")
    _SECURITY_GRAIN = ("security_id", "get_security_id")

    def _pk_columns(self, table: str) -> tuple[str, ...]:
        """The table's primary-key columns, in key order.

        Read from the database rather than kept in a list here. A hand-written
        map is a second copy of the schema that nothing forces anyone to update:
        widen a key (as kr_stake_changes needed) and the upsert would go on
        conflicting on the old one. Asking the database cannot drift.
        """
        cached = self._pk_cache.get(table)
        if cached is None:
            rows = self._exec(
                "SELECT a.attname FROM pg_index i "
                "JOIN pg_attribute a ON a.attrelid = i.indrelid "
                "                   AND a.attnum = ANY(i.indkey) "
                "WHERE i.indrelid = %s::regclass AND i.indisprimary "
                "ORDER BY array_position(i.indkey, a.attnum)",
                (table,),
            ).fetchall()
            cached = tuple(r["attname"] for r in rows)
            if not cached:
                # Without a key there is nothing to conflict on, so every
                # re-collection would append a duplicate set of rows.
                raise ValueError(f"{table} has no primary key; refusing to upsert")
            self._pk_cache[table] = cached
        return cached

    def upsert_coverage(self, table: str, grain: str, rows: Iterable) -> int:
        """Insert rows for an Extension-B coverage table, updating on conflict.

        Each row is a pydantic model carrying market+symbol (resolved to the FK id)
        plus columns matching the table. `grain` is 'company' or 'security'.

        The conflict clause updates every non-key column; it must not be a plain
        DO NOTHING. One statement serves every coverage table, so "keep the row
        already there" would apply to all of them at once, and it fails
        invisibly: no error, the row count stays right, and each re-collection
        silently keeps the first value it ever saw. A restated filing would
        never land.
        """
        fk_col, resolver = self._SECURITY_GRAIN if grain == "security" else self._COMPANY_GRAIN
        resolve = getattr(self, resolver)
        pk = self._pk_columns(table)
        batch: list[tuple] = []
        sql = None
        for row in rows:
            data = row.model_dump()
            market = data.pop("market")
            symbol = data.pop("symbol")
            fk_id = resolve(market, symbol)
            if fk_id is None:
                continue
            cols = [fk_col] + list(data.keys())
            if sql is None:
                placeholders = ",".join(["%s"] * len(cols))
                updatable = [c for c in cols if c not in pk]
                action = (
                    "DO UPDATE SET "
                    + ", ".join(f"{c}=EXCLUDED.{c}" for c in updatable)
                    if updatable else "DO NOTHING"   # every column is in the key
                )
                sql = (f"INSERT INTO {table}({','.join(cols)}) VALUES ({placeholders}) "
                       f"ON CONFLICT ({','.join(pk)}) {action}")
            batch.append(tuple([fk_id] + list(data.values())))
        if not batch:
            return 0
        # One round trip for the symbol's rows rather than one per row: over a
        # network connection that difference dominates a market-wide sweep.
        self.conn.cursor().executemany(sql, batch)
        return len(batch)

    # --------------------------------------------------------------- run logging
    def start_run(self, market: str, data_type: str, source: str) -> int:
        cur = self._exec(
            "INSERT INTO collection_runs(market, data_type, source, started_at, status) "
            "VALUES (%s,%s,%s,%s,%s) RETURNING run_id",
            (market, data_type, source, today_iso(), "running"),
        )
        run_id = int(cur.fetchone()["run_id"])
        self.commit()
        return run_id

    def finish_run(self, run_id: int, status: str, rows: int, error: str | None = None) -> None:
        self._exec(
            "UPDATE collection_runs SET finished_at=%s, status=%s, rows_written=%s, error=%s "
            "WHERE run_id=%s",
            (today_iso(), status, rows, error, run_id),
        )
        self.commit()

    def known_rcept_nos(self, table: str, company_id: int, source: str) -> set[str]:
        """접수번호 already stored for this company, so a re-run does not re-open
        documents it has already parsed. DART filings are immutable once accepted
        (corrections arrive under a new 접수번호), so a stored one is final."""
        if table not in ("kr_stake_changes", "kr_treasury_disposals"):
            raise ValueError(f"{table} is not keyed by rcept_no")
        rows = self._exec(
            f"SELECT DISTINCT rcept_no FROM {table} WHERE company_id=%s AND source=%s",
            (company_id, source),
        ).fetchall()
        return {r["rcept_no"] for r in rows}

    # ------------------------------------------------------- scan cursor
    # A market-wide sweep is resumable: each symbol's outcome is written as it
    # finishes so an interrupted run (quota, network, Ctrl-C) restarts from the
    # first unfinished symbol instead of re-fetching everything already stored.
    def scan_scope(self, market: str, data_type: str, source: str,
                   start: str | None, end: str | None) -> tuple[str, str, str, str, str]:
        """The key prefix identifying one sweep. The date range is part of it:
        a symbol finished for 2025 is not finished for a 2026 sweep."""
        return (market, data_type, source, start or "", end or "")

    def scan_done_symbols(self, market: str, data_type: str, source: str,
                          start: str | None, end: str | None) -> set[str]:
        """Symbols already completed for this scope. Errors are NOT included —
        a symbol that failed should be retried on the next run, not skipped."""
        rows = self._exec(
            "SELECT symbol FROM scan_progress WHERE market=%s AND data_type=%s AND source=%s "
            "AND range_start=%s AND range_end=%s AND status='done'",
            self.scan_scope(market, data_type, source, start, end),
        ).fetchall()
        return {r["symbol"] for r in rows}

    def mark_scan_symbol(self, market: str, data_type: str, source: str,
                         start: str | None, end: str | None, symbol: str,
                         status: str, rows: int = 0, api_calls: int = 0,
                         error: str | None = None) -> None:
        self._exec(
            "INSERT INTO scan_progress(market, data_type, source, range_start, range_end, "
            "symbol, status, rows_written, api_calls, error, updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(market, data_type, source, range_start, range_end, symbol) "
            "DO UPDATE SET status=excluded.status, rows_written=excluded.rows_written, "
            "api_calls=excluded.api_calls, error=excluded.error, updated_at=excluded.updated_at",
            (*self.scan_scope(market, data_type, source, start, end), symbol, status,
             rows, api_calls, error, today_iso()),
        )
        self.commit()

    def clear_scan_progress(self, market: str, data_type: str, source: str,
                            start: str | None, end: str | None) -> int:
        """Forget one scope's cursor so the next run starts from the top."""
        cur = self._exec(
            "DELETE FROM scan_progress WHERE market=%s AND data_type=%s AND source=%s "
            "AND range_start=%s AND range_end=%s",
            self.scan_scope(market, data_type, source, start, end),
        )
        self.commit()
        return cur.rowcount or 0

    def scan_summary(self, market: str, data_type: str, source: str,
                     start: str | None, end: str | None) -> dict[str, int]:
        rows = self._exec(
            "SELECT status, COUNT(*) AS n, SUM(rows_written) AS rows_written, "
            "SUM(api_calls) AS api_calls FROM scan_progress "
            "WHERE market=%s AND data_type=%s AND source=%s AND range_start=%s AND range_end=%s "
            "GROUP BY status",
            self.scan_scope(market, data_type, source, start, end),
        ).fetchall()
        out = {"done": 0, "error": 0, "rows_written": 0, "api_calls": 0}
        for r in rows:
            out[r["status"]] = r["n"]
            out["rows_written"] += r["rows_written"] or 0
            out["api_calls"] += r["api_calls"] or 0
        return out

    # ---------------------------------------------------- API call budget
    # OpenDART meters per calendar day per key and locks the account out on
    # overrun, so the count has to outlive the process: an in-memory counter
    # resets on every re-run and walks straight past the cap.
    def api_calls_today(self, source: str, day: str | None = None) -> int:
        row = self._exec(
            "SELECT calls FROM api_call_budget WHERE source=%s AND call_date=%s",
            (source, day or today_iso()),
        ).fetchone()
        return int(row["calls"]) if row else 0

    def record_api_calls(self, source: str, n: int, day: str | None = None) -> int:
        """Add `n` to today's tally and return the new total."""
        if n <= 0:
            return self.api_calls_today(source, day)
        day = day or today_iso()
        self._exec(
            # Qualified: an unadorned `calls` on the right is ambiguous between
            # the stored row and the proposed one.
            "INSERT INTO api_call_budget(source, call_date, calls) VALUES (%s,%s,%s) "
            "ON CONFLICT(source, call_date) DO UPDATE SET "
            "calls = api_call_budget.calls + EXCLUDED.calls",
            (source, day, n),
        )
        self.commit()
        return self.api_calls_today(source, day)
