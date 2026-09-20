import pytest

from issuer_data.models import Company, Price, Security
from issuer_data.storage.repository import Repository


def test_resolve_company_by_cik_is_stable(conn):
    repo = Repository(conn)
    c1 = repo.resolve_company(Company(name="Apple Inc.", cik="0000320193", source="edgar"))
    # a second record with the same CIK must resolve to the same company
    c2 = repo.resolve_company(Company(name="APPLE INC", cik="0000320193", source="fmp"))
    assert c1 == c2
    n = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    assert n == 1


def test_upsert_prices_is_idempotent(conn):
    repo = Repository(conn)
    cid = repo.resolve_company(Company(name="Apple", cik="320193", source="edgar"))
    sid = repo.upsert_security(Security(market="US", symbol="AAPL", currency="USD", source="edgar"), cid)
    prices = [Price(symbol="AAPL", market="US", trade_date="2024-06-03", close=194.0,
                    currency="USD", source="yfinance")]
    assert repo.upsert_prices(sid, prices) == 1
    repo.upsert_prices(sid, prices)  # run again
    repo.commit()
    n = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    assert n == 1  # no duplicate row


def test_security_unique_per_market_symbol(conn):
    repo = Repository(conn)
    cid = repo.resolve_company(Company(name="Tencent", isin="KYG875721634", source="hkexnews"))
    s1 = repo.upsert_security(Security(market="HK", symbol="00700", currency="HKD", source="hkexnews"), cid)
    s2 = repo.upsert_security(Security(market="HK", symbol="00700", currency="HKD", source="yfinance"), cid)
    assert s1 == s2


def _two_companies(repo):
    return (repo.resolve_company(Company(name="Samsung Electronics", isin="KR7005930003",
                                         source="dart")),
            repo.resolve_company(Company(name="SK hynix", isin="KR7000660001",
                                         source="dart")))


def test_one_pair_can_hold_two_relations(conn):
    """The edge that the old key silently ate.

    Samsung and SK hynix compete in DRAM and sell to each other elsewhere, so
    both edges are true at once and both come from the same source. Keyed on
    the pair alone, the second INSERT hit the first and DO NOTHING dropped it.
    """
    repo = Repository(conn)
    cid, peer = _two_companies(repo)
    repo.upsert_peer(cid, peer, "competitor", "manual", direction=-1, weight=0.8)
    repo.upsert_peer(cid, peer, "customer", "manual", direction=1, weight=0.3)
    repo.commit()
    rows = conn.execute(
        "SELECT relation, direction, weight FROM company_peers "
        "WHERE company_id=%s AND peer_company_id=%s ORDER BY relation", (cid, peer)
    ).fetchall()
    assert [(r["relation"], r["direction"], r["weight"]) for r in rows] == [
        ("competitor", -1, 0.8), ("customer", 1, 0.3),
    ]


def test_rescoring_an_edge_updates_it(conn):
    """DO NOTHING pinned an edge to whatever was written first."""
    repo = Repository(conn)
    cid, peer = _two_companies(repo)
    repo.upsert_peer(cid, peer, "supplier", "manual", weight=0.2, evidence="guess")
    repo.upsert_peer(cid, peer, "supplier", "manual", weight=0.9,
                     evidence="20260814000123")
    repo.commit()
    row = conn.execute(
        "SELECT weight, evidence, count(*) OVER () AS n FROM company_peers"
    ).fetchone()
    assert row["n"] == 1
    assert (row["weight"], row["evidence"]) == (0.9, "20260814000123")


def test_a_blank_sweep_does_not_erase_a_hand_scored_edge(conn):
    """An FMP-style sweep knows no direction, weight or evidence.

    Re-running it must not blank the three columns a person filled in, which is
    what a plain EXCLUDED assignment would do.
    """
    repo = Repository(conn)
    cid, peer = _two_companies(repo)
    repo.upsert_peer(cid, peer, "fmp_peer", "fmp", direction=-1, weight=0.7,
                     evidence="https://example.com/a")
    repo.upsert_peer(cid, peer, "fmp_peer", "fmp")  # the sweep, carrying nothing
    repo.commit()
    row = conn.execute(
        "SELECT direction, weight, evidence FROM company_peers"
    ).fetchone()
    assert (row["direction"], row["weight"], row["evidence"]) == (
        -1, 0.7, "https://example.com/a")


def test_direction_is_only_ever_plus_or_minus_one(conn):
    """Every consumer multiplies a score by this, so a 0 or a 2 cannot land."""
    import psycopg

    repo = Repository(conn)
    cid, peer = _two_companies(repo)
    with pytest.raises(psycopg.errors.CheckViolation):
        repo.upsert_peer(cid, peer, "competitor", "manual", direction=0)
    conn.rollback()
