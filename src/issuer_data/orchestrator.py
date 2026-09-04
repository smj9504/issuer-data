"""Collection orchestration: ties collectors, resolver, and repository together."""

from __future__ import annotations

import inspect

import psycopg

from .collectors.base import NotSupportedError
from .collectors.registry import build_collector, default_source
from .collectors.resolver import resolve_and_store
from .config import Settings
from .logging import get_logger
from .models import Filing, SecurityRecord
from .storage.repository import Repository
from .utils.dates import default_range
from .utils.symbols import normalize_symbol

log = get_logger(__name__)


class Orchestrator:
    def __init__(self, conn: psycopg.Connection, settings: Settings) -> None:
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
        filing_types: list[str] | None = None,
        extract_tables: bool = False,
        filing_kind: str | None = None,
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
                rows = self._collect_filings(collector, market, symbols, start, end,
                                             download_docs, filing_types, extract_tables,
                                             filing_kind)
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

    def _collect_filings(self, collector, market, symbols, start, end, download_docs,
                          filing_types: list[str] | None = None, extract_tables=False,
                          filing_kind: str | None = None) -> int:
        start, end = default_range(start, end, default_years=2)
        symbols = self._resolve_symbols(market, symbols)
        self.ensure_master(market, symbols)
        total = 0
        pending: list[tuple[int, list]] = []
        for sym in symbols:
            try:
                filings = _fetch_filings(collector, sym, start, end, filing_kind)
            except NotSupportedError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("filings %s %s failed: %s", market, sym, exc)
                continue
            filings = _filter_filing_types(filings, filing_types)
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
                download_filing_documents(self.repo, self.settings, cid, filings,
                                          extract_tables=extract_tables)
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
        "demand": ("fetch_demand_signals", "demand_signals", "company", False),
        # DART 원문 기반 (KR only): 지분 변동 명세와 자기주식 처분/취득
        "stake": ("fetch_stake_changes", "kr_stake_changes", "company", True),
        "treasury": ("fetch_treasury_disposals", "kr_treasury_disposals", "company", True),
    }

    # Coverage tables keyed by DART 접수번호. For these a filing already stored
    # never needs its 원문 re-fetched, which is what makes a repeat sweep cheap.
    _RCEPT_KEYED = {"stake": "kr_stake_changes", "treasury": "kr_treasury_disposals"}

    def collect_coverage(self, market: str, data_type: str, source: str | None,
                         symbols: list[str] | None, start: str | None, end: str | None,
                         resume: bool = False, max_api_calls: int | None = None,
                         reparse: bool = False) -> int:
        """Collect one coverage type across `symbols`, or the whole market.

        With no `symbols` this sweeps every stored security in the market. That
        is thousands of symbols against a metered API, so the sweep is made
        interruptible: each symbol's outcome is committed as it finishes, and
        `resume=True` skips the ones already done for this exact scope
        (market/type/source plus the date range). `max_api_calls` caps the day's
        spend; hitting it ends the run cleanly with the cursor intact, rather
        than letting the provider lock the key out partway through.
        """
        from .collectors.base import BaseCollector, CallBudget, QuotaExceededError

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
            # If the collector doesn't override the coverage method, mark 'skipped'
            # up-front so an unimplemented type never shows 'ok' with 0 rows (which
            # happens when the symbol list is empty and the method is never called).
            if getattr(type(collector), method_name) is getattr(BaseCollector, method_name):
                raise NotSupportedError(f"{src} does not implement {data_type}")
            budget = None
            if max_api_calls is not None and hasattr(collector, "budget"):
                budget = CallBudget(self.repo, src, max_api_calls)
                collector.budget = budget
                log.info("%s budget: %d of %d calls left today",
                         src, budget.remaining, budget.limit)
            syms = self._resolve_symbols(market, symbols)
            start2, end2 = default_range(start, end, default_years=2) if dated else (None, None)
            if resume:
                done = self.repo.scan_done_symbols(market, data_type, src, start2, end2)
                remaining = [s for s in syms if s not in done]
                log.info("Resuming %s/%s: %d of %d symbols already done",
                         market, data_type, len(syms) - len(remaining), len(syms))
                syms = remaining
            self.ensure_master(market, syms)
            rcept_table = self._RCEPT_KEYED.get(data_type)
            for sym in syms:
                before_calls = getattr(collector, "api_calls", 0)
                if rcept_table is not None and hasattr(collector, "seen_rcept_nos"):
                    # `reparse` re-opens documents already stored. Needed after a
                    # parser or key fix: the stored rows are the *output* of the old
                    # code, and skipping their 접수번호 would keep the bad output
                    # forever. Costs a full re-fetch, so it is opt-in.
                    cid = self.repo.get_company_id_for_symbol(market, sym)
                    collector.seen_rcept_nos = (
                        set() if reparse
                        else (self.repo.known_rcept_nos(rcept_table, cid, src)
                              if cid else set())
                    )
                try:
                    items = method(sym, start2, end2) if dated else method(sym)
                except (NotSupportedError, QuotaExceededError):
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s %s:%s failed: %s", data_type, market, sym, exc)
                    # Roll back before recording the failure. If what failed was
                    # a statement rather than the network, the transaction is
                    # poisoned and every later one raises until it is cleared --
                    # including this symbol's cursor write and the next symbol's.
                    # A sweep would keep running while silently recording
                    # nothing, and the resume that follows would re-fetch
                    # everything and spend the day's quota again.
                    self.conn.rollback()
                    self._mark_symbol(market, data_type, src, start2, end2, sym, "error",
                                      0, getattr(collector, "api_calls", 0) - before_calls,
                                      str(exc), budget)
                    continue
                n = self.repo.upsert_coverage(table, grain, items)
                self.repo.commit()
                rows += n
                self._mark_symbol(market, data_type, src, start2, end2, sym, "done", n,
                                  getattr(collector, "api_calls", 0) - before_calls,
                                  None, budget)
                log.info("%s %s:%s -> %d rows", data_type, market, sym, n)
            self.repo.finish_run(run_id, "ok", rows)
        except QuotaExceededError as exc:
            # Not a failure: the sweep stopped where it was told to. The cursor is
            # committed per symbol, so --resume picks up from exactly here.
            log.warning("Stopping %s/%s: %s", market, data_type, exc)
            self.repo.finish_run(run_id, "quota", rows, str(exc))
        except NotSupportedError as exc:
            log.warning("Skip %s/%s via %s: %s", market, data_type, src, exc)
            self.repo.finish_run(run_id, "skipped", rows, str(exc))
        except Exception as exc:  # noqa: BLE001
            log.error("Error %s/%s via %s: %s", market, data_type, src, exc)
            self.repo.finish_run(run_id, "error", rows, str(exc))
        return rows

    def _mark_symbol(self, market, data_type, src, start, end, symbol, status, rows,
                     api_calls, error, budget) -> None:
        """Commit one symbol's outcome, then flush the call ledger.

        Both happen at the symbol boundary so a hard kill loses at most one
        symbol: the cursor never claims a symbol is done before its rows are
        committed, and the day's call count never drifts far behind reality.
        """
        self.repo.mark_scan_symbol(market, data_type, src, start, end, symbol, status,
                                   rows, api_calls, error)
        if budget is not None:
            budget.flush()

    # ------------------------------------------------------------------ helpers
    def _resolve_symbols(self, market: str, symbols: list[str] | None) -> list[str]:
        if symbols:
            return [normalize_symbol(market, s) for s in symbols]
        rows = self.conn.execute(
            "SELECT symbol FROM securities WHERE market=%s ORDER BY symbol", (market,)
        ).fetchall()
        return [r["symbol"] for r in rows]


def _default_ccy(market: str) -> str:
    return {"KR": "KRW", "HK": "HKD", "US": "USD"}.get(market.upper(), "USD")


def _fetch_filings(collector, symbol: str, start: str, end: str, filing_kind: str | None):
    """Call fetch_filings, passing `kind` only to collectors that accept it.

    `kind` is DART's 공시유형 and has no counterpart at EDGAR/HKEXnews/FMP, so it
    stays off the shared BaseCollector signature; passing it blindly would break
    every other source.
    """
    if not filing_kind:
        return collector.fetch_filings(symbol, start, end)
    params = inspect.signature(collector.fetch_filings).parameters
    if "kind" not in params:
        raise NotSupportedError(
            f"{collector.source} has no filing-kind concept; --dart-kind applies to DART only"
        )
    return collector.fetch_filings(symbol, start, end, kind=filing_kind)


def _filter_filing_types(filings: list[Filing], filing_types: list[str] | None) -> list[Filing]:
    """Keep only filings whose filing_type contains one of `filing_types`.

    Case-insensitive substring match, not exact — EDGAR/FMP filing_type is a
    short form code ('8-K') where substring == exact match, but DART's is a
    free-text report name ('분기보고서 (2024.09)') and HKEXnews's is a long
    description, so exact match would silently under-match those two sources.
    """
    if not filing_types:
        return filings
    wanted = [t.lower() for t in filing_types]
    return [f for f in filings if f.filing_type and any(w in f.filing_type.lower() for w in wanted)]
