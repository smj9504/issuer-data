"""Collection orchestration: ties collectors, resolver, and repository together."""

from __future__ import annotations

import sqlite3

from .collectors.base import NotSupportedError
from .collectors.registry import build_collector, default_source
from .collectors.resolver import resolve_and_store
from .config import Settings
from .logging import get_logger
from .models import SecurityRecord
from .storage.repository import Repository
from .utils.dates import default_range
from .utils.symbols import normalize_symbol

log = get_logger(__name__)


class Orchestrator:
    def __init__(self, conn: sqlite3.Connection, settings: Settings) -> None:
        self.conn = conn
        self.settings = settings
        self.repo = Repository(conn)

    # ------------------------------------------------------------------ master
    def ensure_master(self, market: str, symbols: list[str]) -> None:
        """Make sure each symbol has a security+company row; fetch master if missing."""
        missing = [s for s in symbols if self.repo.get_security_id(market, s) is None]
        if not missing:
            return
        src = default_source(market, "master")
        if not src:
            return
        try:
            collector = build_collector(src, self.settings)
            collector._current_market = market
            records = collector.fetch_master(missing)
        except NotSupportedError as exc:
            log.warning("Cannot fetch master for %s via %s: %s", market, src, exc)
            records = []
        except Exception as exc:  # noqa: BLE001
            log.warning("Master fetch failed for %s %s: %s", market, missing, exc)
            records = []
        if records:
            resolve_and_store(self.repo, records)
        # For any still-missing symbol, create a minimal placeholder so FKs resolve.
        for s in missing:
            if self.repo.get_security_id(market, s) is None:
                rec = SecurityRecord(market=market, symbol=s, name=s, country=market,
                                     currency=_default_ccy(market), source="placeholder")
                resolve_and_store(self.repo, [rec])

    # ------------------------------------------------------------------ collect
    def collect(
        self,
        market: str,
        data_type: str,
        source: str | None = None,
        symbols: list[str] | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int | None = None,
        download_docs: bool = False,
    ) -> int:
        market = market.upper()
        src = source or default_source(market, data_type)
        if not src:
            log.warning("No source for %s/%s; skipping", market, data_type)
            return 0
        run_id = self.repo.start_run(market, data_type, src)
        rows = 0
        try:
            collector = build_collector(src, self.settings)
            # market context for market-agnostic collectors (yfinance/fmp/av)
            collector._current_market = market
            if data_type == "master":
                rows = self._collect_master(collector, symbols, limit)
            elif data_type == "prices":
                rows = self._collect_prices(collector, market, symbols, start, end)
            elif data_type == "financials":
                rows = self._collect_financials(collector, market, symbols)
            elif data_type == "filings":
                rows = self._collect_filings(collector, market, symbols, start, end, download_docs)
            else:
                raise ValueError(f"Unknown data_type {data_type}")
            self.repo.finish_run(run_id, "ok", rows)
        except NotSupportedError as exc:
            log.warning("Skip %s/%s via %s: %s", market, data_type, src, exc)
            self.repo.finish_run(run_id, "skipped", rows, str(exc))
        except Exception as exc:  # noqa: BLE001
            log.error("Error %s/%s via %s: %s", market, data_type, src, exc)
            self.repo.finish_run(run_id, "error", rows, str(exc))
        return rows

    def _collect_master(self, collector, symbols, limit) -> int:
        records = collector.fetch_master(symbols)
        if limit:
            records = records[:limit]
        mapping = resolve_and_store(self.repo, records)
        log.info("Stored %d securities (%s)", len(mapping), collector.source)
        return len(mapping)

    def _collect_prices(self, collector, market, symbols, start, end) -> int:
        start, end = default_range(start, end)
        symbols = self._resolve_symbols(market, symbols)
        self.ensure_master(market, symbols)
        total = 0
        for sym in symbols:
            try:
                prices = collector.fetch_prices(sym, start, end)
            except NotSupportedError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("prices %s %s failed: %s", market, sym, exc)
                continue
            sid = self.repo.get_security_id(market, sym)
            if sid is None:
                log.warning("No security_id for %s:%s; skipping prices", market, sym)
                continue
            n = self.repo.upsert_prices(sid, prices)
            self.repo.commit()
            total += n
            log.info("prices %s:%s -> %d rows", market, sym, n)
        return total

    def _collect_financials(self, collector, market, symbols) -> int:
        symbols = self._resolve_symbols(market, symbols)
        self.ensure_master(market, symbols)
        total = 0
        for sym in symbols:
            try:
                facts = collector.fetch_financials(sym)
            except NotSupportedError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("financials %s %s failed: %s", market, sym, exc)
                continue
            cid = self.repo.get_company_id_for_symbol(market, sym)
            if cid is None:
                log.warning("No company_id for %s:%s; skipping financials", market, sym)
                continue
            n = self.repo.upsert_financials(cid, facts)
            self.repo.commit()
            total += n
            log.info("financials %s:%s -> %d facts", market, sym, n)
        return total

    def _collect_filings(self, collector, market, symbols, start, end, download_docs) -> int:
        start, end = default_range(start, end, default_years=2)
        symbols = self._resolve_symbols(market, symbols)
        self.ensure_master(market, symbols)
        total = 0
        pending: list[tuple[int, list]] = []
        for sym in symbols:
            try:
                filings = collector.fetch_filings(sym, start, end)
            except NotSupportedError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("filings %s %s failed: %s", market, sym, exc)
                continue
            cid = self.repo.get_company_id_for_symbol(market, sym)
            if cid is None:
                log.warning("No company_id for %s:%s; skipping filings", market, sym)
                continue
            n = self.repo.upsert_filings(cid, filings)
            self.repo.commit()
            total += n
            log.info("filings %s:%s -> %d rows", market, sym, n)
            if download_docs:
                pending.append((cid, filings))
        if download_docs and pending:
            from .documents import download_filing_documents

            for cid, filings in pending:
                download_filing_documents(self.repo, self.settings, cid, filings)
        return total

    # --------------------------------------------------------------- coverage
    # data_type -> (collector method, table, grain, takes_date_range)
    COVERAGE = {
        "metrics": ("fetch_daily_metrics", "daily_metrics", "security", True),
        "ratios": ("fetch_ratios", "ratios", "company", False),
        "ownership": ("fetch_ownership", "ownership", "company", False),
        "institutional": ("fetch_institutional", "institutional_holdings", "company", False),
        "actions": ("fetch_corporate_actions", "corporate_actions", "security", True),
        "analyst": ("fetch_analyst", "analyst_estimates", "company", False),
        "insiders": ("fetch_insiders", "insider_trades", "company", True),
        "earnings": ("fetch_earnings", "earnings_events", "company", False),
        "news": ("fetch_news", "news", "company", False),
        "index": ("fetch_index_membership", "index_membership", "company", False),
        "esg": ("fetch_esg", "esg_scores", "company", False),
    }

    def collect_coverage(self, market: str, data_type: str, source: str | None,
                         symbols: list[str] | None, start: str | None, end: str | None) -> int:
        market = market.upper()
        method_name, table, grain, dated = self.COVERAGE[data_type]
        src = source or default_source(market, data_type)
        if not src:
            log.warning("No source for %s/%s; skipping", market, data_type)
            return 0
        run_id = self.repo.start_run(market, data_type, src)
        rows = 0
        try:
            collector = build_collector(src, self.settings)
            collector._current_market = market
            method = getattr(collector, method_name)
            syms = self._resolve_symbols(market, symbols)
            self.ensure_master(market, syms)
            if dated:
                start2, end2 = default_range(start, end, default_years=2)
            for sym in syms:
                try:
                    items = method(sym, start2, end2) if dated else method(sym)
                except NotSupportedError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s %s:%s failed: %s", data_type, market, sym, exc)
                    continue
                n = self.repo.upsert_coverage(table, grain, items)
                self.repo.commit()
                rows += n
                log.info("%s %s:%s -> %d rows", data_type, market, sym, n)
            self.repo.finish_run(run_id, "ok", rows)
        except NotSupportedError as exc:
            log.warning("Skip %s/%s via %s: %s", market, data_type, src, exc)
            self.repo.finish_run(run_id, "skipped", rows, str(exc))
        except Exception as exc:  # noqa: BLE001
            log.error("Error %s/%s via %s: %s", market, data_type, src, exc)
            self.repo.finish_run(run_id, "error", rows, str(exc))
        return rows

    # ------------------------------------------------------------------ helpers
    def _resolve_symbols(self, market: str, symbols: list[str] | None) -> list[str]:
        if symbols:
            return [normalize_symbol(market, s) for s in symbols]
        rows = self.conn.execute(
            "SELECT symbol FROM securities WHERE market=? ORDER BY symbol", (market,)
        ).fetchall()
        return [r["symbol"] for r in rows]


def _default_ccy(market: str) -> str:
    return {"KR": "KRW", "HK": "HKD", "US": "USD"}.get(market.upper(), "USD")
