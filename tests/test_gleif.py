"""LEI enrichment's query, against a real PostgreSQL.

`enrich_leis` builds its SQL by concatenation, which is the shape that survived
the move off SQLite unnoticed: nothing here is a literal the dialect greps can
read, and until now the function had no test to run the statement at all. Each
of these fails outright on a `?` bind -- psycopg passes it through and reports
"0 placeholders but N parameters were passed".
"""

import pytest

from issuer_data.collectors import gleif
from issuer_data.config import Settings
from issuer_data.models import Company, Security
from issuer_data.storage.repository import Repository


@pytest.fixture()
def repo(conn, monkeypatch):
    """Two issuers, one per market, neither carrying an LEI yet."""
    r = Repository(conn)
    for name, country, market, symbol in (
        ("Samsung Electronics", "KR", "KR", "005930"),
        ("SK hynix", "KR", "KR", "000660"),
        ("Apple Inc.", "US", "US", "AAPL"),
    ):
        cid = r.resolve_company(Company(name=name, country=country, source="test"))
        r.upsert_security(Security(market=market, symbol=symbol, source="test"), cid)
    r.commit()
    # The network half is not what these are about; every company gets an LEI so
    # the row count is exactly what the WHERE clause selected.
    monkeypatch.setattr(gleif, "lookup_lei",
                        lambda client, name, country=None: f"LEI-{name[:4].upper()}")
    return r


def _leis(conn):
    return {r["name"]: r["lei"] for r in conn.execute(
        "SELECT name, lei FROM companies ORDER BY name")}


def test_every_company_without_an_lei_when_nothing_is_narrowed(repo, conn):
    """The no-parameter path: an empty params tuple against a bare statement."""
    assert gleif.enrich_leis(repo, Settings(), None) == 3
    assert all(v for v in _leis(conn).values())


def test_market_narrows_the_sweep(repo, conn):
    assert gleif.enrich_leis(repo, Settings(), None, market="KR") == 2
    got = _leis(conn)
    assert got["Apple Inc."] is None
    assert got["Samsung Electronics"] and got["SK hynix"]


def test_symbols_narrow_the_sweep(repo, conn):
    """The = ANY list -- one parameter holding many values, not one each."""
    assert gleif.enrich_leis(repo, Settings(), ["005930"], market="KR") == 1
    got = _leis(conn)
    assert got["Samsung Electronics"]
    assert got["SK hynix"] is None and got["Apple Inc."] is None


def test_symbols_are_normalized_before_they_are_matched(repo, conn):
    """'005930.KS' and '5930' are the stored 005930; the filter has to agree."""
    assert gleif.enrich_leis(repo, Settings(), ["005930.KS", "660"], market="KR") == 2
    got = _leis(conn)
    assert got["Samsung Electronics"] and got["SK hynix"]
    assert got["Apple Inc."] is None


def test_a_company_that_already_has_one_is_left_alone(repo, conn):
    conn.execute("UPDATE companies SET lei='EXISTING' WHERE name='SK hynix'")
    repo.commit()
    assert gleif.enrich_leis(repo, Settings(), None, market="KR") == 1
    assert _leis(conn)["SK hynix"] == "EXISTING"
