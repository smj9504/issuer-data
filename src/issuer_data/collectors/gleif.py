"""GLEIF LEI enrichment — look up Legal Entity Identifiers by company name.

Free public API (api.gleif.org), no key. Enriches `companies.lei` and registers
the LEI in `identifier_xref` so future cross-source records resolve on it.
"""

from __future__ import annotations

from urllib.parse import quote

from ..config import Settings
from ..http.client import HttpClient
from ..logging import get_logger
from ..storage.repository import Repository

log = get_logger(__name__)

_URL = "https://api.gleif.org/api/v1/lei-records?filter[entity.legalName]={name}&page[size]=1"
_COUNTRY = {"KR": "KR", "HK": "HK", "US": "US"}


def lookup_lei(client: HttpClient, name: str, country: str | None = None) -> str | None:
    """Return the best-match LEI for a company name (optionally constrained by country)."""
    if not name:
        return None
    try:
        data = client.get_json(_URL.format(name=quote(name)))
    except Exception as exc:  # noqa: BLE001
        log.debug("GLEIF lookup failed for %s: %s", name, exc)
        return None
    for rec in data.get("data", []):
        attrs = rec.get("attributes", {})
        if country:
            addr_country = attrs.get("entity", {}).get("legalAddress", {}).get("country")
            if addr_country and addr_country != country:
                continue
        return rec.get("id")
    return None


def enrich_leis(repo: Repository, settings: Settings, symbols: list[str] | None,
                market: str | None = None) -> int:
    """Fill missing LEIs on companies (optionally limited to given symbols/market)."""
    client = HttpClient(rate_limit=settings.default_rate_limit)
    sql = "SELECT DISTINCT c.company_id, c.name, c.country FROM companies c " \
          "JOIN securities s ON s.company_id=c.company_id " \
          "WHERE c.lei IS NULL AND c.name IS NOT NULL"
    params: list = []
    if market:
        sql += " AND s.market=?"
        params.append(market.upper())
    if symbols:
        from ..utils.symbols import normalize_symbol

        norm = [normalize_symbol(market or "US", s) for s in symbols]
        sql += " AND s.symbol IN (%s)" % ",".join("?" * len(norm))
        params += norm
    rows = repo.conn.execute(sql, tuple(params)).fetchall()
    n = 0
    for row in rows:
        lei = lookup_lei(client, row["name"], _COUNTRY.get(row["country"]))
        if not lei:
            continue
        repo._exec("UPDATE companies SET lei=? WHERE company_id=?", (lei, row["company_id"]))
        repo.link_identifier("LEI", lei, row["company_id"], "gleif")
        n += 1
        log.info("LEI %s -> %s", row["name"], lei)
    repo.commit()
    return n
