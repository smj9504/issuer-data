"""Offline tests for the DART 원문 parser, on markup trimmed from real filings.

The fixture keeps DART's real shape: the relationship/entity-type codes sit on a
separate 특별관계자 roster table, NOT on the 변동 rows, so a parser that reads
them off the change row itself silently produces NULL for exactly the fields the
FI-vs-대주주 classification depends on. That regression is pinned below.
"""

import sqlite3

import pytest

from issuer_data.kr_disclosure_parse import (
    HLD_MTH,
    parse_holder_roster,
    parse_report_meta,
    parse_stake_changes,
    parse_treasury_disposal,
)

# Trimmed from 005930 rcept 20260828001916 — roster rows first, then 변동 rows.
D001 = """<DOCUMENT>
<TABLE>
<TR><TE ACODE="RPT_NM">주식등의대량보유상황보고서</TE>
<TU AUNIT="RPT_DST1" AUNITVALUE="4">변동ㆍ변경</TU></TR>
<TR><TE ACODE="SEQ_NO">1</TE><TE ACODE="SPC_NM">삼성생명보험</TE>
<TE ACODE="SPC_ID2">104-81-26688</TE>
<TU AUNIT="SPC_TP" AUNITVALUE="K">금융기관</TU>
<TU AUNIT="FLT_CRP_RLT" AUNITVALUE="10">최대주주</TU></TR>
<TR><TE ACODE="SEQ_NO">2</TE><TE ACODE="SPC_NM">삼성복지재단</TE>
<TE ACODE="SPC_ID2">104-82-05958</TE>
<TU AUNIT="SPC_TP" AUNITVALUE="I">국내법인</TU>
<TU AUNIT="FLT_CRP_RLT" AUNITVALUE="16">기타</TU></TR>
</TABLE>
<TABLE>
<TR ACOPY="Y"><TE ACODE="SPC_NM">삼성생명보험</TE>
<TE ACODE="SPC_ID2">104-81-26688</TE>
<TU AUNIT="MDF_DT" AUNITVALUE="20260729">2026년 07월 29일</TU>
<TU AUNIT="HLD_MTH" AUNITVALUE="02" ENG="Disposal">장내매도(-)</TU>
<TU AUNIT="STK_KND" AUNITVALUE="11">의결권있는 주식</TU>
<TE ACODE="BFR_MDF_CNT">501,235,043</TE>
<TE ACODE="MDF_SDK_CNT">-247</TE></TR>
<TR ACOPY="Y"><TE ACODE="SPC_NM">삼성복지재단</TE>
<TE ACODE="SPC_ID2">104-82-05958</TE>
<TU AUNITVALUE="20260731" AUNIT="MDF_DT">2026년 07월 31일</TU>
<TU AUNIT="HLD_MTH" AUNITVALUE="12">장외매도(-)</TU>
<TE ACODE="BFR_MDF_CNT">4,484,150</TE>
<TE ACODE="MDF_SDK_CNT">-1,000,000</TE></TR>
<TR ACOPY="Y"><TE ACODE="SPC_NM">삼성생명보험</TE>
<TE ACODE="SPC_ID2">104-81-26688</TE>
<TU AUNIT="MDF_DT" AUNITVALUE="20260730">2026년 07월 30일</TU>
<TU AUNIT="HLD_MTH" AUNITVALUE="01">장내매수(+)</TU>
<TE ACODE="BFR_MDF_CNT">501,234,796</TE>
<TE ACODE="MDF_SDK_CNT">249</TE></TR>
</TABLE>
</DOCUMENT>"""

TREASURY = """<DOCUMENT><TABLE>
<TR><TE ACODE="SEL_OSTK">1,132,477</TE>
<TE ACODE="SEL_OSTK_SPRC">285,000</TE>
<TE ACODE="SEL_OSTK_PRC">322,755,945,000</TE>
<TE ACODE="DSPS_PARN">임원 등 928명</TE>
<TE ACODE="SEL_PPS">임원 등 성과급의 자기주식 지급</TE>
<TE ACODE="HLD_OSTK">-</TE></TR>
</TABLE></DOCUMENT>"""


def test_roster_is_keyed_by_business_number():
    roster = parse_holder_roster(D001)
    assert roster["104-81-26688"]["relation_label"] == "최대주주"
    assert roster["104-81-26688"]["holder_type_label"] == "금융기관"
    assert roster["104-82-05958"]["relation_label"] == "기타"


def test_change_rows_are_joined_to_roster_codes():
    """The regression: these come from a different table than the 변동 row."""
    rows = parse_stake_changes(D001)
    seoul_life = [r for r in rows if r["holder_name"] == "삼성생명보험"]
    assert seoul_life, "변동 rows were not found at all"
    assert all(r["relation"] == "10" for r in seoul_life)
    assert all(r["holder_type"] == "K" for r in seoul_life)
    assert all(r["relation_label"] == "최대주주" for r in seoul_life)


def test_quantities_dates_and_methods():
    rows = parse_stake_changes(D001)
    sale = next(r for r in rows if r["method"] == "02")
    assert sale["change_date"] == "2026-07-29"
    assert sale["shares_before"] == 501235043.0
    assert sale["shares_delta"] == -247.0        # signed, thousands separators stripped
    assert sale["stock_kind"] == "11"


def test_attribute_order_variation_is_tolerated():
    """AUNITVALUE sometimes precedes AUNIT; both orders must parse."""
    rows = parse_stake_changes(D001)
    off_market = next(r for r in rows if r["method"] == "12")
    assert off_market["change_date"] == "2026-07-31"     # parsed from reversed attrs
    assert off_market["shares_delta"] == -1000000.0


def test_report_meta_reads_the_report_kind():
    assert parse_report_meta(D001)["report_type"] == "변동ㆍ변경"


def test_treasury_disposal_fields():
    t = parse_treasury_disposal(TREASURY)
    assert t["shares"] == 1132477.0
    assert t["unit_price"] == 285000.0            # the actual execution price
    assert t["total_amount"] == 322755945000.0
    assert t["counterparty"] == "임원 등 928명"


def test_dash_means_absent_not_zero():
    """'-' is DART's 'not applicable'; parsing it as 0 would fake a real value."""
    t = parse_treasury_disposal(TREASURY)
    assert t.get("method") is None


@pytest.mark.parametrize("code,label", [("01", "장내매수(+)"), ("02", "장내매도(-)"),
                                        ("11", "장외매수(+)"), ("12", "장외매도(-)")])
def test_method_dictionary_covers_the_trading_codes(code, label):
    assert HLD_MTH[code] == label


def _view_db(rows):
    """Apply the shipped schema and load parsed rows, to exercise the views."""
    import pathlib

    sql = pathlib.Path("src/issuer_data/storage/schema.sql").read_text(encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    conn.executescript(sql)
    conn.execute("INSERT INTO companies(company_id, name, source) VALUES (1,'t','dart')")
    for r in rows:
        conn.execute(
            "INSERT INTO kr_stake_changes(company_id, rcept_no, change_date, holder_name,"
            " holder_id, holder_type, holder_type_label, relation, relation_label, method,"
            " method_label, stock_kind, shares_before, shares_delta, source)"
            " VALUES (1,'rc1',?,?,?,?,?,?,?,?,?,?,?,?,'dart')",
            (r["change_date"], r["holder_name"], r["holder_id"], r["holder_type"],
             r["holder_type_label"], r["relation"], r["relation_label"], r["method"],
             r["method_label"], r["stock_kind"], r["shares_before"], r["shares_delta"]),
        )
    conn.commit()
    return conn


def test_view_classifies_side_and_venue():
    conn = _view_db(parse_stake_changes(D001))
    got = dict(conn.execute(
        "SELECT method, side || '/' || venue FROM v_kr_stake_sales").fetchall())
    assert got["02"] == "sell/on_market"
    assert got["12"] == "sell/off_market"      # a block deal is off-market
    assert got["01"] == "buy/on_market"


def test_view_buckets_holders_from_dart_codes():
    """The FI-vs-대주주 call lives in the view, so it is testable without refetching."""
    conn = _view_db(parse_stake_changes(D001))
    got = dict(conn.execute(
        "SELECT DISTINCT holder_name, holder_bucket FROM v_kr_stake_sales").fetchall())
    assert got["삼성생명보험"] == "controlling"   # relation 10 wins over type K
    assert got["삼성복지재단"] == "other"


def test_deal_view_rolls_up_by_receipt_and_side():
    conn = _view_db(parse_stake_changes(D001))
    deals = conn.execute(
        "SELECT side, venue, holders, shares_total FROM v_kr_stake_deals"
        " ORDER BY side, venue").fetchall()
    assert ("sell", "off_market", 1, 1000000.0) in deals
    assert ("buy", "on_market", 1, 249.0) in deals


def test_price_check_view_does_not_fan_out_across_price_sources():
    """One disposal must stay one row even when several sources price that day.

    krx and yfinance both carry a close for the same KR trading day, so a naive
    join emitted one disposal row per source and would inflate any count taken
    over this view.
    """
    import pathlib

    sql = pathlib.Path("src/issuer_data/storage/schema.sql").read_text(encoding="utf-8")
    conn = sqlite3.connect(":memory:")
    conn.executescript(sql)
    conn.execute("INSERT INTO companies(company_id, name, source) VALUES (1,'t','dart')")
    conn.execute(
        "INSERT INTO securities(security_id, company_id, market, symbol, source)"
        " VALUES (1,1,'KR','005930','dart')"
    )
    for src, close in (("krx", 254500.0), ("yfinance", 254500.0)):
        conn.execute(
            "INSERT INTO prices(security_id, trade_date, close, source)"
            " VALUES (1,'2026-07-11',?,?)", (close, src)
        )
    conn.execute(
        "INSERT INTO kr_treasury_disposals(company_id, rcept_no, report_date,"
        " report_kind, shares, unit_price, total_amount, source)"
        " VALUES (1,'rc1','2026-07-13','처분결정',1132477,285000,322755945000,'dart')"
    )
    conn.commit()

    rows = conn.execute("SELECT premium_pct, amount_residual"
                        " FROM v_kr_treasury_price_check").fetchall()
    assert len(rows) == 1, f"disposal fanned out to {len(rows)} rows"
    assert rows[0][0] == pytest.approx(11.98, abs=0.01)
    assert rows[0][1] == 0          # shares * unit_price reconciles to the total


def test_same_holder_same_day_different_stock_kind_are_distinct_rows():
    """보통주와 기타 주식은 별개 변동이다 — 키가 좁으면 하나가 사라진다.

    Real case: 20260805000440 filed 이석희's 임원퇴임 as two lines on 2022-03-30,
    stock_kind 11 (의결권있는 주식, 11,236주) and 10 (기타, 191,684주). With
    stock_kind outside the primary key the second overwrote the first and 191,684
    shares vanished — 64 parsed rows became 60 stored.
    """
    xml = """<DOCUMENT><TABLE>
<TR><TE ACODE="SPC_NM">이석희</TE><TE ACODE="SPC_ID2">650623</TE>
<TU AUNIT="MDF_DT" AUNITVALUE="20220330">2022년 03월 30일</TU>
<TU AUNIT="HLD_MTH" AUNITVALUE="97">임원퇴임(-)</TU>
<TU AUNIT="STK_KND" AUNITVALUE="11">의결권있는 주식</TU>
<TE ACODE="BFR_MDF_CNT">11,236</TE><TE ACODE="MDF_SDK_CNT">-11,236</TE></TR>
<TR><TE ACODE="SPC_NM">이석희</TE><TE ACODE="SPC_ID2">650623</TE>
<TU AUNIT="MDF_DT" AUNITVALUE="20220330">2022년 03월 30일</TU>
<TU AUNIT="HLD_MTH" AUNITVALUE="97">임원퇴임(-)</TU>
<TU AUNIT="STK_KND" AUNITVALUE="10">기타</TU>
<TE ACODE="BFR_MDF_CNT">191,684</TE><TE ACODE="MDF_SDK_CNT">-191,684</TE></TR>
</TABLE></DOCUMENT>"""
    rows = parse_stake_changes(xml)
    assert len(rows) == 2

    conn = _view_db(rows)
    stored = conn.execute(
        "SELECT stock_kind, shares_delta FROM kr_stake_changes ORDER BY stock_kind"
    ).fetchall()
    assert stored == [("10", -191684.0), ("11", -11236.0)], "a 변동 line was overwritten"


def test_stock_kind_is_never_null_so_dedup_still_works():
    """stock_kind is in the primary key, where NULL <> NULL would defeat dedup."""
    xml = """<DOCUMENT><TABLE>
<TR><TE ACODE="SPC_NM">홍길동</TE><TE ACODE="SPC_ID2">700101</TE>
<TU AUNIT="MDF_DT" AUNITVALUE="20260101">2026년 01월 01일</TU>
<TU AUNIT="HLD_MTH" AUNITVALUE="02">장내매도(-)</TU>
<TE ACODE="MDF_SDK_CNT">-100</TE></TR>
</TABLE></DOCUMENT>"""
    row = parse_stake_changes(xml)[0]
    assert row["stock_kind"] == "", "absent STK_KND must be '' so the PK still collides"
