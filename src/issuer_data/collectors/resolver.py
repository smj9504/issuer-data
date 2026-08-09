"""Entity resolution: map SecurityRecords to company_ids (cross-listing support).

Strategy (delegated to Repository.resolve_company):
1. Match on strong identifiers (LEI -> CIK -> CORP_CODE -> ISIN) via identifier_xref.
2. Fall back to normalized name + country.
3. Otherwise create a new company.

ADR/GDR listings that carry an ``underlying_isin``/``underlying_symbol`` are linked to
the same company as the underlying line. A curated overrides CSV can pre-seed / force
tricky dual-listings (e.g. BABA <-> 9988, TCEHY <-> 00700).
"""

from __future__ import annotations

import csv
from pathlib import Path

from ..logging import get_logger
from ..models import SecurityRecord
from ..storage.repository import Repository

log = get_logger(__name__)


def resolve_and_store(repo: Repository, records: list[SecurityRecord]) -> dict[str, int]:
    """Persist companies + securities for the given records.

    Returns a mapping of "MARKET:SYMBOL" -> company_id.
    """
    from ..utils.symbols import normalize_symbol

    out: dict[str, int] = {}
    # normalize each record's symbol to the market canonical form
    for rec in records:
        rec.symbol = normalize_symbol(rec.market, rec.symbol)
    # ADR/GDR records are processed last so their underlying may already exist.
    ordered = sorted(records, key=lambda r: (r.security_type or "COMMON") in ("ADR", "GDR"))
    for rec in ordered:
        company_id = _resolve_one(repo, rec)
        security = rec.to_security()
        repo.upsert_security(security, company_id)
        # register ISIN of the listing too (helps ADR<->underlying matching)
        if rec.isin:
            repo._register_identifier("ISIN", rec.isin, company_id, rec.source)
        out[f"{rec.market}:{rec.symbol}"] = company_id
    repo.commit()
    return out


def _resolve_one(repo: Repository, rec: SecurityRecord) -> int:
    # ADR/GDR: try to attach to the underlying listing's company first.
    if (rec.security_type or "COMMON") in ("ADR", "GDR"):
        cid = _find_underlying(repo, rec)
        if cid is not None:
            repo._enrich_company(cid, rec.to_company())
            # register the ADR's own identifiers against the same company
            for id_type, val in (("ISIN", rec.isin), ("CIK", rec.cik), ("LEI", rec.lei)):
                repo._register_identifier(id_type, val, cid, rec.source)
            return cid
    # Otherwise standard resolution. Disallow weak name matches for ADRs
    # (their names/tickers differ from the home line).
    allow_name = (rec.security_type or "COMMON") not in ("ADR", "GDR")
    return repo.resolve_company(rec.to_company(), allow_name_match=allow_name)


def _find_underlying(repo: Repository, rec: SecurityRecord) -> int | None:
    if rec.underlying_isin:
        cid = repo._company_by_identifier("ISIN", rec.underlying_isin)
        if cid:
            return cid
    if rec.underlying_symbol:
        # underlying_symbol may be "MARKET:SYMBOL" or a bare ticker
        us = rec.underlying_symbol
        if ":" in us:
            m, s = us.split(":", 1)
            cid = repo.get_company_id_for_symbol(m, s)
            if cid:
                return cid
        cid = repo._company_by_identifier("TICKER", us)
        if cid:
            return cid
    return None


def apply_overrides(repo: Repository, overrides_path: str | Path) -> int:
    """Load a curated crosswalk CSV and force listings onto shared companies.

    CSV columns: group,market,symbol  — all rows sharing a ``group`` value are
    merged onto one company_id (the first existing one found, else newly created).
    Returns the number of listings linked.
    """
    path = Path(overrides_path)
    if not path.exists():
        return 0
    from ..utils.symbols import normalize_symbol

    groups: dict[str, list[tuple[str, str]]] = {}
    with path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            g = (row.get("group") or "").strip()
            market = (row.get("market") or "").strip().upper()
            symbol = (row.get("symbol") or "").strip()
            if g and market and symbol:
                groups.setdefault(g, []).append((market, normalize_symbol(market, symbol)))

    linked = 0
    for members in groups.values():
        target_cid: int | None = None
        for market, symbol in members:
            cid = repo.get_company_id_for_symbol(market, symbol)
            if cid is not None:
                target_cid = cid
                break
        if target_cid is None:
            continue  # none of the listings exist yet; skip (re-run after master)
        for market, symbol in members:
            sid = repo.get_security_id(market, symbol)
            if sid is None:
                continue
            repo._exec(
                "UPDATE securities SET company_id=? WHERE security_id=?",
                (target_cid, sid),
            )
            repo.link_identifier("TICKER", f"{market}:{symbol}", target_cid, "override")
            linked += 1
    repo.commit()
    log.info("Applied %d override links from %s", linked, path)
    return linked
