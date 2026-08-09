"""Offline tests for the accounting-correct FX basis (period-average + view)."""

from issuer_data.config import Settings
from issuer_data.models import Company, FinancialFact, FxRate, Security
from issuer_data.services import _window_start, compute_period_average_fx
from issuer_data.storage.repository import Repository


def test_window_start_by_period():
    assert _window_start("2023-12-31", "FY") == "2022-12-31"
    # quarter window is ~92 days
    assert _window_start("2023-03-31", "Q1") < "2023-03-31"


def test_period_average_and_usd_view(conn, monkeypatch):
    repo = Repository(conn)
    cid = repo.resolve_company(Company(name="Samsung", corp_code="00126380", source="dart"))
    repo.upsert_security(Security(market="KR", symbol="005930", currency="KRW", source="dart"), cid)
    # one IS (flow) fact and one BS (stock) fact, both KRW, FY2023
    repo.upsert_financials(cid, [
        FinancialFact(symbol="005930", market="KR", fiscal_year=2023, fiscal_period="FY",
                      fs_scope="CFS", statement_type="IS", account="Revenue", value=1000.0,
                      currency="KRW", period_end="2023-12-31", source="dart"),
        FinancialFact(symbol="005930", market="KR", fiscal_year=2023, fiscal_period="FY",
                      fs_scope="CFS", statement_type="BS", account="Assets", value=2000.0,
                      currency="KRW", period_end="2023-12-31", source="dart"),
    ])
    # spot rates: cheap early in the year, richer at year-end
    repo.upsert_fx_rates([
        FxRate(rate_date="2023-01-31", base_ccy="KRW", quote_ccy="USD", rate_type="spot",
               rate=0.0008, source="test"),
        FxRate(rate_date="2023-12-29", base_ccy="KRW", quote_ccy="USD", rate_type="spot",
               rate=0.0010, source="test"),
    ])
    repo.commit()

    # patch collect_fx (network) to a no-op so only averaging runs
    monkeypatch.setattr("issuer_data.services.collect_fx", lambda *a, **k: 0)
    n = compute_period_average_fx(repo, Settings())
    assert n == 1  # one KRW/FY2023 average row
    avg = conn.execute(
        "SELECT rate FROM fx_rates WHERE rate_type='avg' AND base_ccy='KRW'"
    ).fetchone()["rate"]
    assert abs(avg - 0.0009) < 1e-9  # mean of the two spot rates

    rows = {r["statement_type"]: r["value_usd"] for r in conn.execute(
        "SELECT statement_type, value_usd FROM v_financials_usd WHERE company_id=?", (cid,))}
    # IS flow uses avg (0.0009); BS stock uses nearest-prior spot at period_end (0.0010)
    assert abs(rows["IS"] - 1000.0 * 0.0009) < 1e-6
    assert abs(rows["BS"] - 2000.0 * 0.0010) < 1e-6
