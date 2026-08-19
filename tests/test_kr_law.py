"""Offline tests: LawCollector parses 국가법령정보 OpenAPI JSON into models.

Fixtures below are trimmed real responses captured live against open.law.go.kr
(target=law/elaw/oldAndNew/admrul/prec), not hand-guessed shapes.
"""

from issuer_data.collectors.kr_law import LawCollector
from issuer_data.config import Settings
from issuer_data.storage.repository import Repository

LAW_SEARCH = {
    "LawSearch": {
        "law": [
            {
                "현행연혁코드": "현행",
                "법령일련번호": "283193",
                "법령상세링크": "/DRF/lawService.do?OC=test&target=law&MST=283193",
                "법령명한글": "자본시장과 금융투자업에 관한 법률",
                "법령구분명": "법률",
                "소관부처명": "금융위원회",
                "공포번호": "21324",
                "제개정구분명": "일부개정",
                "법령ID": "010513",
                "시행일자": "20260804",
                "공포일자": "20260203",
                "법령약칭명": "자본시장법",
            },
            {"id": "2"},  # malformed (no 법령ID/법령일련번호) -> must be skipped
        ],
        "totalCnt": "2",
    }
}

LAW_DETAIL = {
    "법령": {
        "기본정보": {
            "법령명_한글": "자본시장과 금융투자업에 관한 법률",
            "공포번호": "21324",
            "법령ID": "010513",
            "소관부처": {"content": "금융위원회", "소관부처코드": "1160100"},
            "시행일자": "20260203",
            "공포일자": "20260203",
        },
        "조문": {
            "조문단위": [
                {"조문번호": "1", "조문내용": "제1편 총칙", "조문여부": "전문"},
                {
                    "조문번호": "1",
                    "조문내용": "제1조(목적) 이 법은 자본시장의 발전을 목적으로 한다.",
                    "조문제목": "목적",
                    "조문여부": "조문",
                },
                {
                    "조문번호": "3",
                    "조문내용": "제3조(금융투자상품)",
                    "조문제목": "금융투자상품",
                    "조문여부": "조문",
                    "항": [
                        {
                            "항번호": "①",
                            "항내용": "① 금융투자상품이란 다음과 같다.",
                            "호": [{"호번호": "1.", "호내용": "1. 증권"}],
                            "목": [{"목번호": "가.", "목내용": "가. 지분증권"}],
                        }
                    ],
                },
            ]
        },
    }
}

EFLAW_HISTORY = {
    "LawSearch": {
        "law": [
            {
                "현행연혁코드": "시행예정",
                "법령일련번호": "285957",
                "법령ID": "010513",
                "법령명한글": "자본시장과 금융투자업에 관한 법률",
                "시행일자": "20261113",
                "공포일자": "20260512",
                "제개정구분명": "일부개정",
            },
            {
                "현행연혁코드": "현행",
                "법령일련번호": "283193",
                "법령ID": "010513",
                "법령명한글": "자본시장과 금융투자업에 관한 법률",
                "시행일자": "20260804",
                "공포일자": "20260203",
                "제개정구분명": "일부개정",
            },
        ],
        "totalCnt": "273",
    }
}

ELAW_DETAIL = {
    "Law": {
        "InfSection": {"lsNmEng": "CAPITAL MARKETS ACT", "lsId": "010513"},
        "JoSection": {
            "Jo": [
                {"joNo": "0001", "joCts": "PART I GENERAL PROVISIONS"},
                {"joNo": "0001001", "joCts": "Article 1 (Purpose) This Act aims to..."},
            ]
        },
    }
}

OLD_AND_NEW = {
    "OldAndNewService": {
        "신조문_기본정보": {"법령ID": "010513"},
        "구조문_기본정보": {"법령ID": "010513"},
        "신조문목록": {"조문": [{"no": "1", "content": "new text"}]},
        "구조문목록": {"조문": [{"no": "1", "content": "old text"}]},
    }
}

ADMRUL_SEARCH = {
    "AdmRulSearch": {
        "admrul": {
            "행정규칙명": "고난도금융투자상품심사위원회 설치 및 운영에 관한 규정",
            "행정규칙일련번호": "2100000200919",
            "행정규칙ID": "77408",
            "소관부처명": "금융위원회",
        },
        "totalCnt": "1",
    }
}

PREC_SEARCH = {
    "PrecSearch": {
        "prec": {
            "데이터출처명": "대법원",
            "사건명": "자본시장법위반",
            "판례일련번호": "601495",
            "법원명": "대법원",
        }
    }
}


def _collector(monkeypatch, by_target: dict) -> LawCollector:
    c = LawCollector(Settings(law_api_oc="test"))

    def fake_call(url, target, **params):
        return by_target.get(target, {})

    monkeypatch.setattr(c, "_call", fake_call)
    return c


def test_law_collector_requires_oc():
    from issuer_data.collectors.base import NotSupportedError

    try:
        LawCollector(Settings(law_api_oc=None))
        assert False, "expected NotSupportedError"
    except NotSupportedError:
        pass


def test_search_statutes_parses_and_skips_malformed(monkeypatch):
    c = _collector(monkeypatch, {"law": LAW_SEARCH})
    recs = c.search_statutes("자본시장법")
    assert len(recs) == 1  # the id-only item was skipped
    r = recs[0]
    assert r.law_id == "010513"
    assert r.law_serial_no == "283193"
    assert r.name == "자본시장과 금융투자업에 관한 법률"
    assert r.name_abbrev == "자본시장법"
    assert r.enforcement_date == "2026-08-04"
    assert r.detail_url == "https://www.law.go.kr/DRF/lawService.do?OC=test&target=law&MST=283193"


def test_get_statute_detail_parses_articles_and_skips_headers(monkeypatch):
    c = _collector(monkeypatch, {"law": LAW_DETAIL})
    detail = c.get_statute_detail(mst="283193")
    assert detail.law_id == "010513"
    assert detail.name == "자본시장과 금융투자업에 관한 법률"
    assert detail.department == "금융위원회"
    assert detail.enforcement_date == "2026-02-03"
    # the 전문(header) unit is dropped; only the two real articles remain
    assert len(detail.articles) == 2
    art1, art3 = detail.articles
    assert art1.article_no == "1"
    assert "제1조" in art1.content
    assert art3.article_no == "3"
    # 항/호/목 text is flattened into content in document order
    assert "금융투자상품이란" in art3.content
    assert "1. 증권" in art3.content
    assert "가. 지분증권" in art3.content
    assert detail.raw_text  # full payload always preserved


def test_search_history_resolves_abbreviation_via_eflaw(monkeypatch):
    # 'eflaw' only matches the exact name, so search_history must resolve
    # "자본시장법" (abbreviation) -> "자본시장과 금융투자업에 관한 법률" via a 'law'
    # search first (LAW_SEARCH), then query 'eflaw' (EFLAW_HISTORY) with that name.
    c = _collector(monkeypatch, {"law": LAW_SEARCH, "eflaw": EFLAW_HISTORY})
    entries = c.search_history("자본시장법")
    assert len(entries) == 2
    statuses = {e.law_serial_no: e.status for e in entries}
    assert statuses["283193"] == "현행"
    assert statuses["285957"] == "시행예정"
    assert all(e.law_id == "010513" for e in entries)


def test_get_english_detail_joins_articles(monkeypatch):
    c = _collector(monkeypatch, {"elaw": ELAW_DETAIL})
    tr = c.get_english_detail(mst="284145")
    assert tr.law_id == "010513"
    assert tr.name_en == "CAPITAL MARKETS ACT"
    assert "PART I GENERAL PROVISIONS" in tr.content_en
    assert "Article 1 (Purpose)" in tr.content_en


def test_get_old_and_new_zips_by_sequence_no(monkeypatch):
    c = _collector(monkeypatch, {"oldAndNew": OLD_AND_NEW})
    entries = c.get_old_and_new(mst="283193")
    assert len(entries) == 1
    assert entries[0].law_id == "010513"
    assert entries[0].old_text == "old text"
    assert entries[0].new_text == "new text"


def test_search_raw_picks_serial_and_title_over_department(monkeypatch):
    c = _collector(monkeypatch, {"admrul": ADMRUL_SEARCH, "prec": PREC_SEARCH})
    admrul_items = c.search_raw("admrul", "금융투자")
    assert len(admrul_items) == 1
    assert admrul_items[0].item_key == "2100000200919"
    assert admrul_items[0].title == "고난도금융투자상품심사위원회 설치 및 운영에 관한 규정"

    prec_items = c.search_raw("prec", "자본시장법")
    assert len(prec_items) == 1
    # must pick 사건명 ("case name"), not 데이터출처명 ("source": "대법원")
    assert prec_items[0].title == "자본시장법위반"
    assert prec_items[0].item_key == "601495"


def test_repository_statute_roundtrip_and_company_link(conn, monkeypatch):
    from issuer_data.models import Company

    repo = Repository(conn)
    c = _collector(monkeypatch, {"law": LAW_SEARCH})
    recs = c.search_statutes("자본시장법")
    assert len(recs) == 1

    company_id = repo.resolve_company(Company(name="Test Co", source="test"))
    n = repo.upsert_statutes(recs, company_id=company_id)
    repo.commit()
    assert n == 1
    row = conn.execute("SELECT law_id, name, company_id FROM statutes").fetchone()
    assert row["law_id"] == "010513"
    assert row["company_id"] == company_id
