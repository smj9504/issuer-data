# 대주주·FI 지분 매각 추적 (DART 원문 파싱)

**Date:** 2026-09-03
**Status:** 구현 완료 (실데이터 검증됨)

## Question

D001(주식등의대량보유상황보고서)과 자기주식 처분결정/결과보고서 원문에서 매도·매수
거래를 추출해, 매도자/매수자를 분류하고 딜 단위로 집계하며 가격 정합성을 검증한다.

원래 요청은 "대주주-FI 매각"이었지만, 특정 종목·기간·판정기준을 미리 못 박는 대신
**어떤 종목·기간·분류기준에도 적용되는 범용 파이프라인**으로 설계한다.

## 설계 원칙 — 분류를 코드에 넣지 않는다

핵심 결정: **"FI냐 대주주냐"를 파서가 판정하지 않는다.**

실측해보니 DART 원문에 발행사가 신고한 관계 코드가 이미 들어있다:

| 필드 | 코드 예시 | 의미 |
|---|---|---|
| `FLT_CRP_RLT` | `10` / `14` / `16` | 최대주주 / 계열회사등 / 기타 |
| `SPC_TP` | `K` / `I` / `D` | 금융기관 / 국내법인 / 개인(국내) |
| `IFR_RLT` | `I` / `C` / `L` | 최대주주 / 계열회사 / 기타 |
| `HLD_MTH` | `01` / `02` | 장내매수(+) / 장내매도(-) |

보고자명으로 PE/VC를 문자열 추측하면 규칙이 코드에 굳어버리고, 기준이 바뀔 때마다
재수집해야 한다. 대신 **원문의 코드값을 그대로 저장**하고, "FI 판정"은 저장된 뒤
SQL/뷰 층에서 한다. 그러면:

- 기준을 바꿔도 재수집이 필요 없다 (쿼리만 수정)
- 같은 데이터에 여러 정의를 동시에 적용할 수 있다 (엄격/느슨 기준 비교)
- 판정 근거가 원문 코드로 남아 감사 가능하다

이름 기반 휴리스틱이 필요하면 그건 **별도 매핑 테이블**로 두고 파서 밖에 둔다.

## 실측으로 확인한 것

`005930`(삼성전자) `20260828001916`, `000660`(SK하이닉스) 2건 교차 확인:

**D001 원문 — 발행사가 달라도 필드 구조 동일**
```
SPC_NM        삼성생명보험              보고자/특별관계자명
SPC_ID2       104-81-26688             사업자등록번호 (법인 식별 키)
SPC_TP        K = 금융기관              보고자 유형
MDF_DT        AUNITVALUE=20260729       변동일
HLD_MTH       AUNITVALUE=02 장내매도(-)  취득/처분 방법  ← 매도/매수 분류
STK_KND       AUNITVALUE=11 의결권있는주식 주식 종류
BFR_MDF_CNT   501,235,043               변동 전 수량
MDF_SDK_CNT   -247                      변동 수량 (부호 포함)
```

**자기주식 처분결정 원문 (`20260713000395`)**
```
SEL_OSTK        1,132,477            처분 수량
SEL_OSTK_SPRC   285,000              처분 단가  ← 실제 체결가
SEL_OSTK_PRC    322,755,945,000      처분 총액
DSPS_PARN       임원 등 928명         처분 상대방  ← 매수자
SEL_PPS         임원 등 성과급 지급    처분 목적
```

`AUNITVALUE`에 정규화된 코드가 있어 한글 라벨 매칭 없이 파싱된다. HK 코너스톤 건과
정반대 상황 — DART 서식은 고정이라 **결정론적 코드 파싱이 맞고, LLM 추출은 불필요**하다.

## 선결 과제 (블로커)

`kr_dart.py`의 `fetch_filings`가 `kind="A"`(정기공시)로 하드코딩되어 있어 지분공시(D)와
주요사항보고(B)를 **구조적으로 가져올 수 없다**. `--filing-type`으로도 우회 불가 —
애초에 목록에 담기지 않는다. EDGAR `filing_types` 때와 같은 유형의 갭(수집기는 있는데
파라미터가 없음)이므로, 우회 스크립트가 아니라 `kind` 파라미터화로 해결한다.

## 범용화 설계

### 1. 수집 계층 — 파라미터로 열어둔다

- `fetch_filings(..., kind=...)` — A/B/D 등 공시유형 선택 (기본값은 현행 유지)
- 대상 종목은 인자로 받되, 전체 시장 스캔은 `securities` 테이블에서 종목을 얻어
  순회하는 방식 (DART는 회사별 조회가 기본)

### 2. 저장 계층 — 원문 코드를 그대로 보존

새 테이블 2개. 판정 결과가 아니라 **관측된 사실**만 담는다:

- `kr_stake_changes` — D001 변동 명세 1행 = 1거래
  `company_id, rcept_no, reporter_name, reporter_id(사업자번호), reporter_type(SPC_TP),
   relation(FLT_CRP_RLT), change_date, method(HLD_MTH), stock_kind, shares_before,
   shares_delta, price, source` + `source`를 PK에 포함 (기존 컨벤션)
- `kr_treasury_disposals` — 자기주식 처분결정/결과
  `company_id, rcept_no, decided_date, shares, unit_price, total_amount,
   counterparty(DSPS_PARN), purpose(SEL_PPS), report_kind(결정/결과), source`

### 3. 분류·집계 계층 — 뷰로 분리

- 매도/매수 분류: `method` 코드 기반 (`02`=매도 등)
- 딜 단위 집계: `(company_id, change_date, reporter_id)` 로 묶되, 블록딜은 여러
  특별관계자가 같은 날 동시 매도하므로 `rcept_no` 단위 롤업도 병행
- 가격 정합성: 자기주식 `unit_price`와 같은 날 시장가(`prices.close`) 대비 할인율,
  D001 매도와 처분결과의 수량·일자 교차 확인
- "FI 판정"은 여기서 정의 — 파서가 아니라 뷰에서

## 한계 / 열린 질문

- **가격**: 확인 완료 — D001 변동명세에는 단가 필드가 **아예 없다** (12건 전수에서
  `MDF_UNT_PRC` 0건, `PRH_AMT`(취득자금 총액)만 6/12건). 따라서 지분변동 가격은
  `prices`의 당일 종가로 근사하고, 실제 체결가 검증은 단가가 명시되는 자기주식
  처분(`SEL_OSTK_SPRC`) 쪽에서만 가능하다.

- **관계 코드의 위치**: `FLT_CRP_RLT`/`SPC_TP`는 변동 행이 아니라 **별도 특별관계자
  명부 표**에 있다. 사업자번호(`SPC_ID2`)로 조인해야 하며, 변동 행에서 직접 읽으면
  분류에 필요한 필드가 전부 NULL이 된다 (회귀 테스트로 고정).
- **코드 사전**: 22건 표본으로 넓혀 16개 `HLD_MTH` 코드를 확보했다 (`01`/`02` 장내,
  `11`/`12` 장외, 그 외 신규보고·자사주상여금·주식분할 등). 부호는 라벨 접미사
  `(+)`/`(-)`에 실려 있다. 여전히 완전하지 않으므로 미지 코드는 원값 보존한다.
- **전체 시장 스캔**: DART는 회사별 조회가 기본이라 "전 종목 블록딜"은 종목 수 × API 호출이
  된다. 호출량·레이트리밋 고려 필요.

## 사용법

```bash
# 지분 변동 명세 (D001 원문 파싱)
python -m issuer_data collect --market kr --type stake --symbols 005930     --start 2026-08-01 --end 2026-08-31

# 자기주식 취득/처분 (주요사항보고 B)
python -m issuer_data collect --market kr --type treasury --symbols 005930     --start 2025-01-01 --end 2026-08-31

# 분류 / 딜 집계 / 가격 정합성
python -m issuer_data query --sql "SELECT * FROM v_kr_stake_sales"
python -m issuer_data query --sql "SELECT * FROM v_kr_stake_deals"
python -m issuer_data query --sql "SELECT * FROM v_kr_treasury_price_check"
```

FI/대주주 기준을 바꾸려면 `schema.sql`의 `v_kr_stake_sales`에 있는 `holder_bucket`
CASE 문만 고치고 `init-db`를 다시 돌리면 된다 — 재수집 불필요.

## 실데이터 검증 (005930)

- `--type stake` 2026-08 → 변동 32행 (매도 18 / 매수 14), 전부 `controlling` 분류
  (삼성생명보험은 `SPC_TP=K` 금융기관이지만 `FLT_CRP_RLT=10` 최대주주가 우선)
- `--type treasury` 2025-01~2026-08 → 8건. `amount_residual`이 전 행 0 —
  수량×단가가 신고 총액과 정확히 일치하므로 파싱이 맞다는 교차 검증이 된다
- `premium_pct` 실측: 2026-07-13 처분단가 285,000원 vs 종가 254,500원 = **+11.98%**,
  2026-03-18은 **-7.0%**. 자기주식 처분은 임직원 성과급 지급이 많아 시장가 대비
  프리미엄/할인이 목적(`purpose`)에 따라 갈린다.

## Output

없음 — 이 파이프라인 자체가 산출물. 특정 조사 결과가 필요하면 위 뷰를 쿼리해서
`output/`에 저장한다.
