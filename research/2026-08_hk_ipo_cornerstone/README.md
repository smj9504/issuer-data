# 홍콩거래소 IPO 코너스톤 투자자 현황

**Date:** 2026-08-19
**Status:** in progress

## Question

25년 1월 1일부터 26년 6월 30일까지 홍콩 거래소에 상장된 모든 IPO 건을 모집단으로 하여,
IPO 기업명/IPO일자/공모총액(USD기준)/공모시총(USD기준)/참여 코너스톤 투자자 기관수/코너스톤
투자자 배정 총액(USD기준)/전체 공모총액 중 코너스톤 투자자 배정 총액 비율(단위: %, 소수점
둘째자리까지)/참여 코너스톤 투자자명(코너스톤 배정비중순으로) 의 헤더로 표를 만든다.

## Population / scope

- **거래소/보드:** HKEX **Main Board**만. GEM 보드는 별도 시장 세그먼트라 제외(요청이 "홍콩
  거래소"로 일반적으로 지칭한 것을 대형 시장인 Main Board로 해석).
- **기간:** 상장일(Listing Date) 기준 2025-01-01 ~ 2026-06-30. 프로스펙터스 발행일이 아니라
  실제 상장일 기준으로 필터링.
- **포함 기준:** 신주 공모(fresh-capital offering)를 통한 신규 상장만. 아래는 같은 New Listing
  Report에 함께 실리지만 "공모"가 아니라서 모집단에서 **제외**(사유가 있는 채로
  `.cache/excluded_non_offering.json`에 기록됨, 8건):
  - GEM → Main Board **보드 이전(Transfer of Listing)** — 이미 상장돼 있던 종목의 보드 변경.
    신주 발행이 없어 공모총액/코너스톤 개념 자체가 없음. (Pizu Group 09893, Ocean One 09876,
    Youzan Technology 06051, Fameglow Holdings 03774)
  - **De-SPAC 합병** — SPAC과의 합병을 통한 상장. (ZG Group 06676, Seyond Holdings 02665)
  - **Introduction 방식 상장** — 신주 발행 없이 기존 주주 지분만으로 상장(홍콩 상장규정상 별도
    상장 방식). 공모가/공모총액이 원천적으로 존재하지 않음. (Sunshine Lake Pharma 06887,
    VOYAH Automotive 07489)
- **최종 모집단:** 196건 (전체 204건 중 8건 제외). `.cache/population.json` 참고.

## Method

### 1. 모집단 확정 — HKEX New Listing Report

HKEXnews의 filing 검색 API(`titleSearchServlet.do`)는 "이미 아는 종목코드"의 공시를 찾는
용도라, "이 기간에 신규 상장한 종목 전체"를 처음부터 발견하는 데는 쓸 수 없다(파일럿 단계에서
확인). 대신 `www2.hkexnews.hk/New-Listings/New-Listing-Information/Main-Board`가 연도별로
제공하는 **New Listing Report** 워크북(`NLR2025_Eng.xlsx`, `NLR2026_Eng.xlsx`)을 원천으로
사용 — 종목코드/기업명/프로스펙터스일자/상장일/공모가/공모총액(HK$)이 이미 구조화된 표로
제공됨. 각 IPO는 2행(홍콩공모/국제공모)으로 나뉘어 있어 "Funds Raised (HK$)"를 합산해 총
공모총액을 계산.

### 2. 상장설명서(프로스펙터스) 위치 확인

종목코드를 알면, 기존 HKEXnews filing 검색으로 `TITLE="GLOBAL OFFERING"` +
`LONG_TEXT`에 `"Listing Documents"`가 포함된 filing이 상장설명서 PDF임을 파일럿에서 확인
(Bloks Group 00325 표본으로 검증).

### 3. 코너스톤 투자자 섹션 추출 — 표 파싱이 아닌 LLM 직독 방식

`research/README.md`의 "Irregular per-document data" 규칙에 따름: 딜마다 요약 표의 레이아웃이
다르고(증권사/로펌 템플릿 차이), 일부는 표 자체가 **90도 회전되어 렌더링**되어 `pdfplumber`가
문자 순서를 거꾸로 반환하는 경우도 확인됨(Bloks Group 00325 표본, 331~332페이지 —
`direction: 'ttb'`). 이 회전 이슈는 이번 작업 범위 밖에서(다른 세션) 근본 수정 중이라고
전달받아, 여기서는 표 geometry 파싱을 시도하지 않고 **서술형 "CORNERSTONE INVESTORS" 섹션**을
직접 읽는 방식으로 우회함 — 회전된 표에 담긴 정보(투자자명/배정금액)는 예외 없이 앞뒤
서술형 문단("Name of Investor" 소제목 + 소개 문단)에도 반복되어 있음을 3개 표본(Bloks Group
00325 — 5개 기관/표 회전, Numans Health Food 02530 — 코너스톤 없음, Mixue Group 02097 — 5개
기관/표 정상)으로 교차검증함.

`run.py`가 하는 일:
1. 각 프로스펙터스 PDF에서 "CORNERSTONE INVESTOR" 문구가 나오는 모든 페이지를 찾음
2. 가장 큰 연속 페이지 클러스터(같은 챕터)만 골라 앞뒤 1페이지씩 패딩해 발췌
   (초기 버전은 목차의 챕터명 언급 1건과 본문 챕터 사이 전체 300+ 페이지를 통째로 끌어오는
   버그가 있었음 — largest-cluster 방식으로 수정, `_largest_cluster` 참고)
3. 발췌 텍스트를 `.cache/cornerstone_text/<종목코드>.json`에 캐시

이후 **Claude가 각 캐시 파일을 직접 읽고** 아래 스키마로 구조화(`cornerstone_structured.json`에
수기 조립) — regex 파서를 새로 짜지 않은 이유는 위 레이아웃 다양성 때문:

```json
{
  "<종목코드>": {
    "investors": [
      {"name": "Greenwoods Asset Management Hong Kong Limited", "amount_usd_million": 20.0},
      {"name": "Fullgoal Fund Management Co., Ltd.", "amount_usd_million": 7.0}
    ]
  }
}
```

`investors`가 빈 배열이면 "코너스톤 투자자 없음"(실제로 흔한 케이스 — 소형 IPO 다수).

### 4. 공모총액 USD 환산

New Listing Report는 공모총액을 HKD로만 제공. 상장일 기준 HKD/USD spot rate로 환산 —
`issuer_data.services._fetch_ccy_to_usd`(기존 `collect_fx`가 쓰는 것과 동일한 Yahoo 시세 헬퍼)를
DB에 쓰지 않고 재사용(`research/README.md`의 "reuse what exists, DB is read-only" 원칙).
HKD/USD는 커런시보드 페그(~7.75~7.85)라 상장일 당일 시세가 없으면(주말/공휴일) 직전 최대 7일
이내 가장 가까운 거래일 시세로 대체 — 페그 특성상 이 근사가 표에 필요한 정밀도에 충분함.

코너스톤 배정금액은 표본 확인 결과 프로스펙터스 본문에 이미 USD로 명시되어 있어(예: "aggregate
amount of US$50.00 million") 별도 환산 불필요.

### 5. 비율 계산

코너스톤 배정 비율(%) = 코너스톤 투자자 배정 총액(USD) ÷ 공모총액(USD) × 100, 소수점 둘째자리.

## Caveats / known limitations

- **공모시총(offer-time market cap)은 이번 산출물에서 제외.** New Listing Report에 상장 시
  총발행주식수가 없어 계산이 불가능하고(각 프로스펙터스 표지에서 개별 확인 필요), 사용자와
  합의하여 이번 패스에서는 다른 열을 먼저 채우고 이 열은 공란으로 둠. 필요시 후속 작업으로 추가.
- **표 회전/깨짐 이슈는 이 프로젝트의 다른 세션에서 근본 수정 중**이라고 전달받음 — 이 작업은
  그 이슈를 우회(서술형 텍스트 직독)했을 뿐 수정하지 않았음. `pdf_columns.py`/geometry 추출기가
  고쳐지면, 표 기반 교차검증으로 신뢰도를 더 높일 수 있음.
- **코너스톤 투자자명은 프로스펙터스 원문 표기 그대로** 사용(법인명 축약/공식 영문명 통일 등의
  후처리 없음). 같은 운용사의 여러 펀드가 별도 "투자자"로 등재되는 경우(예: Fullgoal Fund와
  Fullgoal HK)는 프로스펙터스가 실제로 별도 계약 주체로 구분한 대로 각각 별도 행 취급.
- **투자자명 추출은 3개 표본 파일럿으로 검증**했으나, 전체 196건에 대한 100% 정확도는 보장하지
  않음 — 특히 영문명이 매우 길거나 복잡한 법인 구조(다단계 SPV, 여러 펀드의 공동 서명 등)는
  개별 확인이 필요할 수 있음. 스팟체크 권장 대상은 아래 "Output" 섹션 참고.
- **HKD/USD 환산은 상장일 spot rate 단일 값** 사용(공모기간 평균이 아님) — 페그 통화라 실질적
  영향은 미미하나, 정밀한 재무 분석 목적이라면 재검토 필요.

## Output

`output/hk_ipo_cornerstone_2025_2026H1.csv` — 196행(1건당 1 IPO), 컬럼:

| 컬럼 | 설명 |
|---|---|
| IPO 기업명 | New Listing Report 원문 기업명 |
| IPO일자 | 상장일 (YYYY-MM-DD) |
| 공모총액(USD) | 홍콩+국제 공모 합계, 상장일 spot rate로 USD 환산, 반올림 |
| 공모시총(USD) | **공란** — 위 caveats 참고 |
| 참여 코너스톤 투자자 기관수 | 프로스펙터스 "CORNERSTONE INVESTORS" 섹션에서 식별된 기관 수 (0 = 코너스톤 없음) |
| 코너스톤 투자자 배정 총액(USD) | 개별 투자자 배정액 합계 |
| 코너스톤 배정 비율(%) | 배정총액 ÷ 공모총액 × 100, 소수점 둘째자리 |
| 참여 코너스톤 투자자명(배정비중순) | 배정금액 내림차순, 쉼표로 구분 |
| stock_code | HKEXnews 5자리 종목코드 (추적용, 요청 헤더 외 참고 컬럼) |

원본 raw 데이터(캐시):
- `.cache/population.json` — 모집단 196건 원본 필드
- `.cache/excluded_non_offering.json` — 제외 8건 + 사유
- `.cache/cornerstone_text/<종목코드>.json` — 종목별 발췌 텍스트(코너스톤 섹션)
- `.cache/prospectus_pdfs/<종목코드>.pdf` — 원본 프로스펙터스 PDF (용량 큼, 재현용)
