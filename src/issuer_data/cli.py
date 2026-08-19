"""Command-line interface: python -m issuer_data <command>."""

from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

from .config import get_settings
from .logging import get_logger, setup_logging
from .storage.db import connect, init_db

log = get_logger(__name__)

MARKET_CHOICES = ["kr", "hk", "us", "all"]
COVERAGE_TYPES = ["metrics", "ratios", "ownership", "institutional", "actions",
                  "analyst", "insiders", "earnings", "news", "index", "esg", "demand"]
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
    if getattr(args, "ocr", None) is not None:
        settings.ocr_enabled = args.ocr
    if getattr(args, "ml_tables", False):
        settings.pdf_ml_tables = True
    if getattr(args, "ml_engine", None):
        settings.pdf_ml_engine = args.ml_engine
        settings.pdf_ml_tables = True
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
                    filing_types=_split_csv(args.filing_type),
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
    if getattr(args, "ocr", None) is not None:
        settings.ocr_enabled = args.ocr
    if getattr(args, "ml_tables", False):
        settings.pdf_ml_tables = True
    if getattr(args, "ml_engine", None):
        settings.pdf_ml_engine = args.ml_engine
        settings.pdf_ml_tables = True
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


def cmd_law(args) -> int:
    from .collectors.base import NotSupportedError
    from .collectors.kr_law import LawCollector
    from .storage.repository import Repository

    settings = get_settings()
    try:
        law = LawCollector(settings)
    except NotSupportedError as exc:
        print(str(exc))
        return 2

    conn = None
    repo = None
    company_id = None
    if args.save:
        conn = connect(settings.db_path)
        repo = Repository(conn)
        if args.symbol:
            company_id = _company_id_for_law_link(repo, args.symbol)

    try:
        rows_written = 0
        if args.action == "raw":
            if not args.target:
                print("--target is required for --action raw (see RAW_TARGETS in kr_law.py, "
                     "e.g. admrul/ordin/prec/expc/detc/trty/licbyl/lsStmd/lsAbrv)")
                return 2
            if args.law_id or args.mst:
                items = [law.fetch_raw(args.target, item_id=args.law_id, mst=args.mst)]
            else:
                if not args.query:
                    print("--query (or --law-id/--mst) is required for --action raw")
                    return 2
                items = law.search_raw(args.target, args.query,
                                       display=args.display, page=args.page)
            _print_rows(items, ["target", "item_key", "title"])
            if repo:
                rows_written = repo.upsert_law_api_raw(items, company_id=company_id)
        elif args.action == "search":
            if not args.query:
                print("--query is required for --action search")
                return 2
            recs = law.search_statutes(args.query, display=args.display, page=args.page)
            _print_rows(recs, ["law_id", "name", "law_type", "department",
                               "enforcement_date", "law_serial_no"])
            if repo:
                rows_written = repo.upsert_statutes(recs, company_id=company_id)
        elif args.action == "fetch":
            if not args.law_id and not args.mst:
                print("--law-id or --mst is required for --action fetch")
                return 2
            detail = law.get_statute_detail(law_id=args.law_id, mst=args.mst)
            print(f"{detail.name} ({detail.law_id}, MST={detail.law_serial_no})")
            print(f"enforcement_date={detail.enforcement_date}  articles={len(detail.articles)}")
            for a in detail.articles[:10]:
                print(f"  {a.article_no}: {(a.content or '')[:80]}")
            if repo:
                repo.upsert_statute_detail(detail, company_id=company_id)
                rows_written = 1
        elif args.action == "history":
            if not args.query:
                print("--query is required for --action history (name/keyword; "
                     "add --law-id to narrow if it matches more than one law)")
                return 2
            entries = law.search_history(args.query, law_id=args.law_id,
                                         display=args.display, page=args.page)
            _print_rows(entries, ["law_id", "name", "status", "revision_type",
                                  "promulgation_date", "enforcement_date", "law_serial_no"])
            if repo:
                rows_written = repo.upsert_statute_history(entries, company_id=company_id)
        elif args.action == "english":
            if args.law_id or args.mst:
                tr = law.get_english_detail(law_id=args.law_id, mst=args.mst)
                print(f"{tr.name_en} ({tr.law_id})")
                print((tr.content_en or "")[:500])
                if repo:
                    rows_written = repo.upsert_statute_translations([tr], company_id=company_id)
            else:
                if not args.query:
                    print("--query (or --law-id/--mst) is required for --action english")
                    return 2
                recs = law.search_english(args.query, display=args.display, page=args.page)
                _print_rows(recs, ["law_id", "name", "law_type", "enforcement_date",
                                   "law_serial_no"])
                if repo:
                    rows_written = repo.upsert_statutes(recs, company_id=company_id)
        elif args.action in ("oldnew", "threeway"):
            if not args.mst:
                print("--mst is required for --action oldnew/threeway")
                return 2
            entries = (law.get_old_and_new(mst=args.mst) if args.action == "oldnew"
                      else law.get_three_way(mst=args.mst))
            _print_rows(entries, ["law_id", "article_no", "old_text", "new_text"])
            if repo:
                rows_written = repo.upsert_statute_comparisons(entries, company_id=company_id)
        if repo:
            repo.commit()
            print(f"Saved {rows_written} row(s)"
                 + (f", linked to company_id={company_id}" if company_id else ""))
        return 0
    except NotSupportedError as exc:
        print(str(exc))
        return 2
    finally:
        if conn:
            conn.close()


def _company_id_for_law_link(repo, symbol_arg: str) -> int | None:
    from .utils.symbols import normalize_symbol

    market, _, symbol = symbol_arg.partition(":")
    if not symbol:
        market, symbol = "KR", market
    market = market.upper()
    symbol = normalize_symbol(market, symbol)
    cid = repo.get_company_id_for_symbol(market, symbol)
    if cid is None:
        log.warning("No company found for %s:%s; saving statute without a link", market, symbol)
    return cid


def _print_rows(items, fields: list[str]) -> None:
    if not items:
        print("(no results)")
        return
    print(" | ".join(fields))
    for it in items:
        d = it.model_dump()
        print(" | ".join(_truncate(d.get(f)) for f in fields))


def _truncate(v, n: int = 60) -> str:
    if v is None:
        return ""
    s = str(v)
    return s if len(s) <= n else s[: n - 1] + "…"


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
    c.add_argument("--filing-type",
                   help="with --type filings: comma-separated filter, e.g. 8-K,10-K "
                        "(case-insensitive substring match against filing_type)")
    c.add_argument("--extract-tables", action="store_true",
                   help="with --download-docs: run the structured PDF engine "
                        "(cross-page-stitched tables + reflowed narrative)")
    c.add_argument("--ocr", action=argparse.BooleanOptionalAction, default=None,
                   help="OCR scanned/image-only PDFs that have no text layer "
                        "(on by default; --no-ocr disables it)")
    c.add_argument("--ml-tables", action="store_true",
                   help="use a local ML table detector on pages with no ruled table "
                        "(needs `pip install '.[ml]'`; slower, no API cost)")
    c.add_argument("--ml-engine", choices=("table-transformer", "docling"),
                   help="which ML table detector to use (implies --ml-tables)")
    c.set_defaults(func=cmd_collect)

    d = sub.add_parser("download-docs", help="backfill original documents for stored filings")
    d.add_argument("--market", choices=MARKET_CHOICES, default="all")
    d.add_argument("--symbols", help="comma-separated symbols")
    d.add_argument("--limit", type=int, help="cap number of filings")
    d.add_argument("--extract-tables", action="store_true",
                   help="run the structured PDF engine (stitched tables + reflow)")
    d.add_argument("--ocr", action=argparse.BooleanOptionalAction, default=None,
                   help="OCR scanned/image-only PDFs (on by default; --no-ocr disables it)")
    d.add_argument("--ml-tables", action="store_true",
                   help="use a local ML table detector on pages with no ruled table "
                        "(needs `pip install '.[ml]'`; slower, no API cost)")
    d.add_argument("--ml-engine", choices=("table-transformer", "docling"),
                   help="which ML table detector to use (implies --ml-tables)")
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

    lw = sub.add_parser("law", help="query Korean statutes (국가법령정보 OpenAPI)")
    lw.add_argument("--action", required=True,
                    choices=["search", "fetch", "history", "english", "oldnew",
                             "threeway", "raw"],
                    help="search/fetch=현행법령 목록/본문, history=연혁, "
                         "english=영문법령, oldnew=신구법, threeway=3단비교, "
                         "raw=any other category via --target (행정규칙/자치법규/판례/...)")
    lw.add_argument("--target", help="API target code for --action raw, "
                    "e.g. admrul/ordin/prec/expc/detc/trty/licbyl/lsStmd/lsAbrv")
    lw.add_argument("--query", help="statute name/keyword")
    lw.add_argument("--law-id", help="법령ID (or the item's own id for --action raw)")
    lw.add_argument("--mst", help="법령일련번호 (MST)")
    lw.add_argument("--display", type=int, default=20)
    lw.add_argument("--page", type=int, default=1)
    lw.add_argument("--save", action="store_true", help="persist results to the database")
    lw.add_argument("--symbol", help="MARKET:SYMBOL to link results to an issuer (with --save)")
    lw.set_defaults(func=cmd_law)

    ev = sub.add_parser("eval", help="score PDF extraction accuracy on the gold set")
    ev.add_argument("--gold-dir", default="data/eval",
                    help="dir of real labelled cases <name>/{doc.pdf,expected.json}")
    ev.add_argument("--json", action="store_true", help="emit the full report as JSON")
    ev.add_argument("--escalate", action="store_true",
                    help="also run configured escalation and report lift/cost")
    ev.set_defaults(func=cmd_eval)

    return p


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to a legacy codepage (e.g. cp1252) that can't
    # encode Korean/Chinese text coming from KRX/HKEXnews/DART; force UTF-8.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    # pydantic-settings reads .env into Settings but never touches os.environ;
    # pykrx's KRX login reads KRX_ID/KRX_PW straight from os.environ, so load
    # .env into the real process environment too.
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging("DEBUG" if getattr(args, "verbose", False) else None)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
