"""Cross-cutting services: FX collection, peer collection, and comparison output."""

from __future__ import annotations

import datetime as _dt
import sqlite3

from .collectors.base import NotSupportedError
from .collectors.registry import build_collector
from .collectors.resolver import resolve_and_store
from .config import Settings
from .http.client import HttpClient
from .logging import get_logger
from .models import FxRate
from .storage.repository import Repository
from .utils.dates import default_range

log = get_logger(__name__)

_YCHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)


# ------------------------------------------------------------------------- FX
def collect_fx(repo: Repository, settings: Settings, start: str | None, end: str | None) -> int:
    """Collect daily FX (ccy -> USD) for every non-USD currency present in the DB.

    Uses Yahoo '<CCY>=X' (which quotes USD/CCY) and inverts to CCY->USD so that
    close_usd = close_local * rate in the comparison views.
    """
    start, end = default_range(start, end)
    currencies = _needed_currencies(repo.conn)
    if not currencies:
        log.info("No non-USD currencies present; nothing to collect for FX")
        return 0
    client = HttpClient(rate_limit=settings.default_rate_limit, headers={"User-Agent": _UA})
    total = 0
    for ccy in currencies:
        rates = _fetch_ccy_to_usd(client, ccy, start, end)
        total += repo.upsert_fx_rates(rates)
        repo.commit()
        log.info("fx %s->USD: %d rows", ccy, len(rates))
    return total


def _needed_currencies(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT currency FROM securities WHERE currency IS NOT NULL AND currency <> 'USD' "
        "UNION SELECT DISTINCT currency FROM prices WHERE currency IS NOT NULL AND currency <> 'USD' "
        "UNION SELECT DISTINCT currency FROM financials WHERE currency IS NOT NULL AND currency <> 'USD'"
    ).fetchall()
    return sorted({r[0] for r in rows if r[0]})


_PERIOD_DAYS = {"FY": 365, "H1": 183, "Q1": 92, "Q2": 92, "Q3": 92, "Q4": 92}


def compute_period_average_fx(repo: Repository, settings: Settings) -> int:
    """Derive period-average FX ('avg') rows from daily spot, per (currency, fiscal
    period) present in `financials`. Used for accounting-correct IS/CF conversion.

    Ensures spot rates cover each period first (auto-extends collection).
    """
    conn = repo.conn
    periods = conn.execute(
        "SELECT DISTINCT currency, fiscal_year, fiscal_period, period_end FROM financials "
        "WHERE currency IS NOT NULL AND currency <> 'USD'"
    ).fetchall()
    if not periods:
        return 0
    # Ensure spot FX covers the whole span of stored financial periods.
    ends = [r["period_end"] for r in periods if r["period_end"]]
    if ends:
        span_start = min(_window_start(e, "FY") for e in ends)
        span_end = max(ends)
        collect_fx(repo, settings, span_start, span_end)

    total = 0
    for r in periods:
        ccy, fy, fp = r["currency"], r["fiscal_year"], r["fiscal_period"]
        pend = r["period_end"] or f"{fy}-12-31"
        wstart = _window_start(pend, fp)
        row = conn.execute(
            "SELECT AVG(rate) a FROM fx_rates WHERE base_ccy=? AND quote_ccy='USD' "
            "AND rate_type='spot' AND rate_date BETWEEN ? AND ?",
            (ccy, wstart, pend),
        ).fetchone()
        if row and row["a"] is not None:
            repo.upsert_fx_rates([FxRate(rate_date=pend, base_ccy=ccy, quote_ccy="USD",
                                        rate_type="avg", rate=float(row["a"]), source="derived")])
            total += 1
    repo.commit()
    log.info("fx period-average: %d rows", total)
    return total


def _window_start(period_end_iso: str, fiscal_period: str) -> str:
    days = _PERIOD_DAYS.get(fiscal_period, 365)
    d = _dt.datetime.strptime(period_end_iso[:10], "%Y-%m-%d").date() - _dt.timedelta(days=days)
    return d.strftime("%Y-%m-%d")


def _fetch_ccy_to_usd(client: HttpClient, ccy: str, start: str, end: str) -> list[FxRate]:
    params = {
        "period1": _epoch(start),
        "period2": _epoch(end) + 86400,
        "interval": "1d",
    }
    try:
        data = client.get_json(_YCHART.format(sym=f"{ccy}=X"), params=params)
    except Exception as exc:  # noqa: BLE001
        log.warning("FX fetch %s failed: %s", ccy, exc)
        return []
    result = (data.get("chart") or {}).get("result")
    if not result or not result[0].get("timestamp"):
        return []
    res = result[0]
    ts = res["timestamp"]
    closes = (res.get("indicators", {}).get("quote") or [{}])[0].get("close", [])
    out: list[FxRate] = []
    for i, t in enumerate(ts):
        usd_per = _at(closes, i)  # 1 USD = usd_per CCY
        if not usd_per:
            continue
        date = _dt.datetime.fromtimestamp(t, _dt.timezone.utc).strftime("%Y-%m-%d")
        out.append(FxRate(rate_date=date, base_ccy=ccy, quote_ccy="USD",
                          rate=1.0 / usd_per, source="yfinance"))
    return out


# ------------------------------------------------------------------------ peers
def collect_peers(repo: Repository, settings: Settings, market: str, symbols: list[str] | None) -> int:
    """Collect peer relationships via FMP for the given symbols (or all in market)."""
    try:
        fmp = build_collector("fmp", settings)
    except NotSupportedError as exc:
        log.warning("Peers need FMP: %s", exc)
        return 0
    fmp._current_market = market
    if not symbols:
        rows = repo.conn.execute(
            "SELECT symbol FROM securities WHERE market=?", (market,)
        ).fetchall()
        symbols = [r["symbol"] for r in rows]
    total = 0
    for sym in symbols:
        cid = repo.get_company_id_for_symbol(market, sym)
        if cid is None:
            continue
        try:
            peers = fmp.fetch_peers(sym)
        except Exception as exc:  # noqa: BLE001
            log.warning("peers %s failed: %s", sym, exc)
            continue
        for peer_sym in peers:
            peer_cid = repo.get_company_id_for_symbol(market, peer_sym)
            if peer_cid is None:
                # create a lightweight company for the peer so the link resolves
                from .models import SecurityRecord

                resolve_and_store(repo, [SecurityRecord(
                    market=market, symbol=peer_sym, name=peer_sym,
                    country=market if market != "US" else "US", source="fmp")])
                peer_cid = repo.get_company_id_for_symbol(market, peer_sym)
            if peer_cid is not None:
                repo.upsert_peer(cid, peer_cid, "fmp_peer", "fmp")
                total += 1
    repo.commit()
    log.info("peers: %d relationships", total)
    return total


# --------------------------------------------------------------------- compare
def compare_symbols(conn: sqlite3.Connection, symbols: list[str]) -> None:
    """Print a multi-market side-by-side table (local + USD) for the given symbols."""
    if not symbols:
        print("No symbols given.")
        return
    from .utils.symbols import normalize_symbol

    rows = []
    for token in symbols:
        if ":" in token:
            market, symbol = token.split(":", 1)
            market = market.upper()
        else:
            market, symbol = _infer_market(token), token
        symbol = normalize_symbol(market or "", symbol)
        sec = _find_security(conn, symbol, market)
        if not sec:
            rows.append({"symbol": symbol, "note": "not found"})
            continue
        lp = conn.execute(
            "SELECT trade_date, close_local, close_usd FROM v_latest_price WHERE security_id=?",
            (sec["security_id"],),
        ).fetchone()
        rev = _latest_metric(conn, sec["company_id"], ("Revenues", "revenue", "RevenueFromContractWithCustomerExcludingAssessedTax", "totalRevenue"))
        ni = _latest_metric(conn, sec["company_id"], ("NetIncomeLoss", "netIncome", "ProfitLoss"))
        rows.append({
            "market": sec["market"], "symbol": sec["symbol"], "name": sec["name"] or "",
            "ccy": sec["currency"] or "", "type": sec["security_type"] or "",
            "date": lp["trade_date"] if lp else "",
            "close_local": lp["close_local"] if lp else None,
            "close_usd": lp["close_usd"] if lp else None,
            "revenue": rev, "net_income": ni,
        })

    header = f"{'market':6} {'symbol':10} {'name':22} {'ccy':4} {'type':7} {'date':10} " \
             f"{'close_local':>14} {'close_usd':>13} {'revenue':>16} {'net_income':>16}"
    print(header)
    print("-" * len(header))
    for r in rows:
        if r.get("note"):
            print(f"{r['symbol']:10} -> {r['note']}")
            continue
        print(f"{r['market']:6} {r['symbol']:10} {r['name'][:22]:22} {r['ccy']:4} "
              f"{r['type']:7} {r['date']:10} {_fmt(r['close_local']):>14} "
              f"{_fmt(r['close_usd']):>13} {_fmt(r['revenue']):>16} {_fmt(r['net_income']):>16}")


def _infer_market(symbol: str) -> str | None:
    s = symbol.upper()
    if s.endswith((".KS", ".KQ")):
        return "KR"
    if s.endswith(".HK"):
        return "HK"
    return None


def _find_security(conn, symbol, market):
    if market:
        return conn.execute(
            "SELECT s.*, c.name FROM securities s JOIN companies c USING(company_id) "
            "WHERE s.market=? AND s.symbol=?", (market.upper(), symbol),
        ).fetchone()
    return conn.execute(
        "SELECT s.*, c.name FROM securities s JOIN companies c USING(company_id) "
        "WHERE s.symbol=? ORDER BY s.is_primary DESC LIMIT 1", (symbol,),
    ).fetchone()


def _latest_metric(conn, company_id, accounts):
    placeholders = ",".join("?" * len(accounts))
    row = conn.execute(
        f"SELECT value FROM financials WHERE company_id=? AND account IN ({placeholders}) "
        "AND value IS NOT NULL ORDER BY fiscal_year DESC, fiscal_period DESC LIMIT 1",
        (company_id, *accounts),
    ).fetchone()
    return row["value"] if row else None


# -------------------------------------------------------------------- helpers
def _epoch(iso_date: str) -> int:
    return int(_dt.datetime.strptime(iso_date, "%Y-%m-%d")
               .replace(tzinfo=_dt.timezone.utc).timestamp())


def _at(seq, i):
    try:
        v = seq[i]
        return float(v) if v is not None and float(v) == float(v) else None
    except (IndexError, TypeError, ValueError):
        return None


def _fmt(v):
    if v is None:
        return "-"
    if isinstance(v, float):
        if abs(v) >= 1e9:
            return f"{v/1e9:,.2f}B"
        if abs(v) >= 1e6:
            return f"{v/1e6:,.2f}M"
        return f"{v:,.2f}"
    return str(v)
