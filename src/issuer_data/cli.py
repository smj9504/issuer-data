"""Command-line interface: python -m issuer_data <command>."""

from __future__ import annotations

import argparse
import sys

from .config import get_settings
from .logging import get_logger, setup_logging
from .storage.db import connect, init_db

log = get_logger(__name__)

MARKET_CHOICES = ["kr", "hk", "us", "all"]
COVERAGE_TYPES = ["metrics", "ratios", "ownership", "institutional", "actions",
                  "analyst", "insiders", "earnings", "news", "index", "esg"]
TYPE_CHOICES = ["master", "prices", "financials", "filings", "fx", "peers", "lei",
                *COVERAGE_TYPES, "all"]
SOURCE_CHOICES = ["krx", "dart", "hkexnews", "edgar", "yfinance", "fmp", "alphavantage"]


def _split_csv(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


def _markets(arg: str) -> list[str]:
    return ["KR", "HK", "US"] if arg == "all" else [arg.upper()]


def _types(arg: str) -> list[str]:
    if arg == "all":
        return ["master", "prices", "financials", "filings"]
    return [arg]


# --------------------------------------------------------------------- commands
def cmd_init_db(args) -> int:
    path = init_db(get_settings().db_path)
    print(f"Initialized database at {path}")
    return 0


def cmd_collect(args) -> int:
    from .orchestrator import Orchestrator

    settings = get_settings()
    conn = connect(settings.db_path)
    orch = Orchestrator(conn, settings)
    symbols = _split_csv(args.symbols)
    total = 0
    try:
        for market in _markets(args.market):
            for data_type in _types(args.type):
                if data_type in ("fx", "peers", "lei"):
                    total += _collect_crosscutting(orch, data_type, market, symbols, args)
                    continue
                if data_type in COVERAGE_TYPES:
                    total += orch.collect_coverage(market, data_type, args.source,
                                                   symbols, args.start, args.end)
                    continue
                total += orch.collect(
                    market=market,
                    data_type=data_type,
                    source=args.source,
                    symbols=symbols,
                    start=args.start,
                    end=args.end,
                    limit=args.limit,
                    download_docs=args.download_docs,
                    extract_tables=args.extract_tables,
                )
    finally:
        conn.close()
    print(f"Done. Rows written: {total}")
    return 0


def _collect_crosscutting(orch, data_type, market, symbols, args) -> int:
    from .services import collect_fx, collect_peers, compute_period_average_fx

    if data_type == "fx":
        n = collect_fx(orch.repo, orch.settings, args.start, args.end)
        n += compute_period_average_fx(orch.repo, orch.settings)  # accounting-correct avg rows
        return n
    if data_type == "peers":
        return collect_peers(orch.repo, orch.settings, market, symbols)
    if data_type == "lei":
        from .collectors.gleif import enrich_leis

        return enrich_leis(orch.repo, orch.settings, symbols, market)
    return 0


def cmd_download_docs(args) -> int:
    from .documents import backfill_documents

    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        n = backfill_documents(conn, settings, _split_csv(args.symbols),
                               _markets(args.market) if args.market != "all" else None,
                               args.limit, extract_tables=args.extract_tables)
    finally:
        conn.close()
    print(f"Downloaded/updated {n} documents")
    return 0


def cmd_link(args) -> int:
    from .collectors.resolver import apply_overrides
    from .storage.repository import Repository

    settings = get_settings()
    conn = connect(settings.db_path)
    repo = Repository(conn)
    try:
        if args.overrides:
            n = apply_overrides(repo, settings.overrides_path)
            print(f"Applied overrides: {n} links")
        pairs = _split_csv(args.symbols)
        if pairs and len(pairs) >= 2:
            n = _link_symbols(repo, pairs)
            print(f"Linked {n} listings to one company")
    finally:
        conn.close()
    return 0


def _link_symbols(repo, market_symbols: list[str]) -> int:
    """Link a set of MARKET:SYMBOL (or bare US symbols) onto one company."""
    from .utils.symbols import normalize_symbol

    parsed = []
    for token in market_symbols:
        if ":" in token:
            m, s = token.split(":", 1)
        else:
            m, s = "US", token
        m = m.upper()
        parsed.append((m, normalize_symbol(m, s)))
    target = None
    for m, s in parsed:
        cid = repo.get_company_id_for_symbol(m, s)
        if cid is not None:
            target = cid
            break
    if target is None:
        log.warning("None of %s exist yet; run master collection first", market_symbols)
        return 0
    linked = 0
    for m, s in parsed:
        sid = repo.get_security_id(m, s)
        if sid is None:
            continue
        repo._exec("UPDATE securities SET company_id=? WHERE security_id=?", (target, sid))
        repo.link_identifier("TICKER", f"{m}:{s}", target, "manual")
        linked += 1
    repo.commit()
    return linked


def cmd_compare(args) -> int:
    from .services import compare_symbols

    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        compare_symbols(conn, _split_csv(args.symbols) or [])
    finally:
        conn.close()
    return 0


def cmd_status(args) -> int:
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        rows = conn.execute(
            "SELECT run_id, market, data_type, source, status, rows_written, "
            "started_at, finished_at, error FROM collection_runs "
            "ORDER BY run_id DESC LIMIT ?", (args.limit or 20,)
        ).fetchall()
        if not rows:
            print("No collection runs yet.")
            return 0
        print(f"{'id':>4} {'market':6} {'type':11} {'source':11} {'status':8} {'rows':>6}  started")
        for r in rows:
            print(f"{r['run_id']:>4} {r['market'] or '':6} {r['data_type'] or '':11} "
                  f"{r['source'] or '':11} {r['status'] or '':8} "
                  f"{(r['rows_written'] or 0):>6}  {r['started_at'] or ''}"
                  + (f"  ERR: {r['error']}" if r['error'] else ""))
    finally:
        conn.close()
    return 0


def cmd_query(args) -> int:
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        sql = args.sql.strip()
        if not sql.lower().startswith(("select", "with", "pragma", "explain")):
            print("Only read-only queries (SELECT/WITH/PRAGMA/EXPLAIN) are allowed.")
            return 2
        cur = conn.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        if cols:
            print(" | ".join(cols))
        for row in cur.fetchall():
            print(" | ".join("" if row[c] is None else str(row[c]) for c in cols))
    finally:
        conn.close()
    return 0


def cmd_eval(args) -> int:
    import json as _json

    from .eval.harness import default_cases, run_eval

    settings = get_settings()
    cases = default_cases(args.gold_dir)
    if not cases:
        print("No gold cases (install reportlab for synthetic cases, or add "
              f"labelled cases under {args.gold_dir}/).")
        return 0
    escalator = None
    if args.escalate:
        from .pdf_escalate import build_escalator

        escalator = build_escalator(settings)
    report = run_eval(cases, escalator=escalator,
                      threshold=settings.pdf_confidence_threshold,
                      cost_per_page=settings.escalation_cost_per_page)
    if args.json:
        print(_json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print(f"{'category':28} {'TEDS':>6} {'GriTS':>6} {'num-EM':>7} {'contin':>7}")
    for cat, m in sorted(report["categories"].items()):
        print(f"{cat:28} {_fmt(m['teds'])} {_fmt(m['grits'])} "
              f"{_fmt(m['numeric_em']):>7} {_fmt(m['continuity']):>7}")
    o = report["overall"]
    print(f"{'OVERALL':28} {_fmt(o['teds'])} {_fmt(o['grits'])} "
          f"{_fmt(o['numeric_em']):>7} {_fmt(o['continuity']):>7}")
    print(f"\ncases: {report['n_cases']}  escalated: {report['escalated_total']}  "
          f"est. cost: ${report['est_cost_total']:.3f}")
    return 0


def _fmt(v) -> str:
    return f"{v:6.3f}" if isinstance(v, (int, float)) else f"{'—':>6}"


# ------------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="issuer-data", description=__doc__)
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create the database and apply the schema").set_defaults(
        func=cmd_init_db
    )

    c = sub.add_parser("collect", help="collect data from a source")
    c.add_argument("--market", choices=MARKET_CHOICES, required=True)
    c.add_argument("--type", choices=TYPE_CHOICES, required=True)
    c.add_argument("--source", choices=SOURCE_CHOICES, default=None,
                   help="override the default source for this (market,type)")
    c.add_argument("--symbols", help="comma-separated symbols (e.g. 005930,AAPL)")
    c.add_argument("--start", help="start date YYYY-MM-DD")
    c.add_argument("--end", help="end date YYYY-MM-DD")
    c.add_argument("--limit", type=int, help="cap number of securities (smoke tests)")
    c.add_argument("--download-docs", action="store_true",
                   help="with --type filings: also download originals + extract text")
    c.add_argument("--extract-tables", action="store_true",
                   help="with --download-docs: run the structured PDF engine "
                        "(cross-page-stitched tables + reflowed narrative)")
    c.set_defaults(func=cmd_collect)

    d = sub.add_parser("download-docs", help="backfill original documents for stored filings")
    d.add_argument("--market", choices=MARKET_CHOICES, default="all")
    d.add_argument("--symbols", help="comma-separated symbols")
    d.add_argument("--limit", type=int, help="cap number of filings")
    d.add_argument("--extract-tables", action="store_true",
                   help="run the structured PDF engine (stitched tables + reflow)")
    d.set_defaults(func=cmd_download_docs)

    lk = sub.add_parser("link", help="link cross-listed symbols onto one company")
    lk.add_argument("--symbols", help="comma-separated MARKET:SYMBOL (e.g. US:BABA,HK:9988)")
    lk.add_argument("--overrides", action="store_true", help="apply the overrides CSV")
    lk.set_defaults(func=cmd_link)

    cmp = sub.add_parser("compare", help="multi-market side-by-side (local + USD)")
    cmp.add_argument("--symbols", required=True, help="comma-separated symbols")
    cmp.set_defaults(func=cmd_compare)

    st = sub.add_parser("status", help="recent collection runs")
    st.add_argument("--limit", type=int, default=20)
    st.set_defaults(func=cmd_status)

    q = sub.add_parser("query", help="run a read-only SQL query")
    q.add_argument("--sql", required=True)
    q.set_defaults(func=cmd_query)

    ev = sub.add_parser("eval", help="score PDF extraction accuracy on the gold set")
    ev.add_argument("--gold-dir", default="data/eval",
                    help="dir of real labelled cases <name>/{doc.pdf,expected.json}")
    ev.add_argument("--json", action="store_true", help="emit the full report as JSON")
    ev.add_argument("--escalate", action="store_true",
                    help="also run configured escalation and report lift/cost")
    ev.set_defaults(func=cmd_eval)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging("DEBUG" if getattr(args, "verbose", False) else None)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
