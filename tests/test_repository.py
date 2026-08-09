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
