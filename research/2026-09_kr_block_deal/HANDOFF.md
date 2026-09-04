# 인수인계 — 전 종목 스윕 실행 대기

작성: 2026-09-03 · 갱신: 2026-09-04 (스윕 전 점검에서 데이터 유실 발견·수정,
마스터 소스·종목 수 정정, PostgreSQL 이전)
대상: 다른 세션에서 이어받을 사람
전제: `research/2026-09_kr_block_deal/README.md`(설계·실측)를 먼저 읽을 것.

**다른 머신에서 시작한다면 먼저:** `.env`는 git에 없다(`.gitignore`). `.env.example`을
복사해 `ISSUER_DART_API_KEY`와 `ISSUER_DB_DSN`을 채워야 스윕이 돈다. **DB는 PostgreSQL로
옮겼으므로 파일을 복사할 것이 없다** — 두 머신이 같은 DSN을 보면 된다 (§5). 새 DB라면
`init-db` 후 마스터부터. 환경 구축은 `HANDOFF_VENV.md` 참고.

## 한 줄 요약

파이프라인은 완성됐고 검증도 끝났다. **남은 건 전 종목 스윕을 실제로 돌리는 것뿐이고,
그건 구현 문제가 아니라 예산 판단이다.** 코드로 고칠 수 없는 한계 1건은 아래 §3.

---

## 0. 지금 물려받는 상태 (2026-09-04 기준 실측)

| 항목 | 값 |
|---|---|
| DB의 KR 종목 수 | **2개** (005930, 000660) — 마스터 미수집 |
| `kr_stake_changes` | 189행 / 접수번호 5건 |
| `kr_treasury_disposals` | 8행 |
| `scan_progress` 커서 | 5행 (테스트 스윕 흔적) |
| 오늘 쓴 DART 호출 | 11회 (`api_call_budget`) |
| 테스트 | 248 passed, 1 skipped · ruff clean (PostgreSQL 기준) |

즉 **실데이터는 검증용 표본 수준**이다. 본 스윕은 아직 안 돌렸다.

## 1. 바로 시작하려면

```bash
# 1) 종목 마스터 먼저 — 순회 대상이 securities 테이블에서 나온다
#    --source dart 를 붙일 것. KR master의 기본 소스는 yfinance인데(registry.py),
#    yfinance에는 "시장 전 종목 열거"가 없어 명단이 안 채워진다. DART 소스는
#    OpenDartReader가 하루 1회 받아 캐시하는 corp_codes(docs_cache/*.pkl)에서
#    상장사 전체를 읽으므로 DART 호출 0회이고, corp_code·한글명까지 채운다.
python -m issuer_data collect --market kr --type master --source dart

# 2) 스윕 (첫날)
python -m issuer_data collect --market kr --type stake \
    --start 2026-01-01 --end 2026-12-31 --max-api-calls 18000

# 3) 다음 날 이어서
python -m issuer_data collect --market kr --type stake \
    --start 2026-01-01 --end 2026-12-31 --max-api-calls 18000 --resume

# 진행률 / 오늘 사용량
python -m issuer_data status
python -m issuer_data query --sql "SELECT status, COUNT(*) FROM scan_progress GROUP BY status"
```

비용 추정: 종목당 평균 5.75회(대형주 8종목 실측, 표본이 대형주라 **상한값**),
전 종목 1년치 → OpenDART 일일 한도 20,000회 기준 **이틀**.

종목 수는 **3,988개**다 (2026-09-04, DART corp_codes 중 6자리 종목코드 보유 법인 실측).
이 문서가 원래 쓴 ≈2,600은 과소치였다. 3,988 × 5.75 ≈ 22,900회 — 이틀인 것은 같지만
둘째 날 여유가 거의 없다. 다만 5.75가 대형주 표본 상한이라 실제로는 더 낮을 여지가 있다
(중소형주는 대량보유 공시가 적다).

`corp_code`는 추가 호출을 만들지 않는다. `dart.list(symbol, ...)`이 내부에서
`find_corp_code`를 부르지만 그건 캐시된 DataFrame 조회이고, 과금되는 호출은
`fetch_filings`의 목록 1회와 `_document_xml`의 원문 1회뿐이다 (`_spend()` 호출 지점).

**돌리기 전에 §2를 반드시 읽을 것.** 실행 순서가 결과를 바꾼다.

## 2. 스윕 전 반드시 알아야 할 것

### 2.1 재수집은 싸다 — 22,000회는 한 번만 낸다

접수번호 단위로 중복 제거되므로:

| 재실행 상황 | 비용 | 실측 |
|---|---|---|
| 같은 기간 재실행 | 종목당 **1회** (목록만) | ✅ |
| 기간 확장 (8월→7~8월) | 목록 1 + **신규 원문만** | ✅ 005930이 4회 |
| `--resume` | **0회** | ✅ |

따라서 1년치를 한 번 채워두면 이후는 증분만 나간다. 매달 이어붙이는 운영이 가능하다.

### 2.2 `--reparse`를 언제 써야 하는가 (중요)

접수번호 스킵은 **파서를 고친 뒤에는 독이 된다.** 저장된 행은 옛 파서의 *출력*이라,
접수번호를 건너뛰면 잘못된 결과가 영구히 남는다. `--restart`는 스캔 커서만 지우므로
이걸 못 푼다.

```bash
# 파서/스키마를 고친 뒤 기존 데이터를 다시 파싱해야 할 때만
python -m issuer_data collect --market kr --type stake --symbols 000660 \
    --start ... --end ... --reparse --restart
```

전액 재수집 비용이 드니 평소엔 쓰지 말 것.

### 2.3 `--start/--end`는 접수일이지 변동일이 아니다

한 보고서가 수년치 이력을 담을 수 있다. 실측: 2026-06~08만 요청했는데
`20260805000440`이 2022-02-25부터를 담아 **185행 중 43행이 2026년 이전**이었다.

중복은 아니므로 정합성 문제는 없다. 다만 **기간별 집계는 반드시 `change_date`로
필터해야 하고, 수집 범위를 신뢰하면 안 된다.**

```sql
-- 맞는 방식
SELECT ... FROM v_kr_stake_sales WHERE change_date BETWEEN '2026-01-01' AND '2026-12-31'
```

### 2.4 스윕 후 확인할 쿼리 2개

**(a) 정정공시 중복** — 표본에서는 0건이었으나 전 종목에서 재확인할 것.
0행이면 그대로 두면 된다 (dedup 뷰 불필요).

```sql
SELECT company_id, holder_name, change_date, method, shares_delta,
       COUNT(DISTINCT rcept_no) n, string_agg(DISTINCT rcept_no, ',')
FROM kr_stake_changes
GROUP BY company_id, holder_name, change_date, method, shares_delta
HAVING COUNT(DISTINCT rcept_no) > 1;
```

**(b) 미지 `HLD_MTH` 코드** — 사전은 22건 표본 기반 16개라 불완전하다. 새 코드가
나오면 `kr_disclosure_parse.py`의 `HLD_MTH`와 `v_kr_stake_sales`의 side/venue CASE에
반영해야 한다 (미지 코드는 버려지지 않고 원값 보존되므로 유실은 없다).

```sql
SELECT method, method_label, COUNT(*) FROM kr_stake_changes
WHERE method NOT IN ('01','02','11','12','33','59','69','75','86','90','96','97','98','99','104')
GROUP BY method, method_label;
```

**현재 표본에서 이미 1건 나온다**: `method='-'`(공란)인 행이 있다 — 박정호,
2024-03-27, -22,114주. 방법을 신고하지 않은 변동이다. 뷰의 폴백이 의도대로 동작해
`shares_delta < 0`으로 `side=sell`을 주고 `venue`는 정직하게 `other`로 둔다. **이건
버그가 아니라 설계된 동작이니 고치지 말 것** — 장내/장외를 모르는 걸 아는 척하면
안 된다. 전 종목에서 이런 행의 비중이 커지면 그때 별도 처리를 검토하면 된다.

## 3. 지분 변동 단가 — 고칠 수 없는 항목 (제도적 한계)

### 결론: 코드로 해결할 수 없다. 재조사하지 말 것.

D001 변동명세에는 **거래 단가 필드가 존재하지 않는다.** 파서 버그가 아니라 서식에 없다.

이미 확인한 것:
- 단가 후보 ACODE(`MDF_UNT_PRC` 등): **0/14건**
- 단가/금액 계열 전수 스캔에서 나온 3개가 전부:
  - `HLD_UNT_PRJ`/`HLD_UNT_PRG` — 보유주식 **수량** (단가 아님)
  - `PRH_AMT` — 8/14건 존재하나 **취득자금 조달내역**(자기자금·차입금). 원문 문맥
    확인: `차입금(I) 기타(J) 계(H+I+J) 삼성생명보험 ... 16,699,220,100`. 보고서
    **전체 합계**라 개별 행에 배분 불가, 애초에 체결가가 아님

**대안(현행 유지):** 지분 변동 가격은 `prices` 당일 종가로 근사. 실제 체결가는 자기주식
처분(`SEL_OSTK_SPRC`)에서만 나오고 `v_kr_treasury_price_check`가 이미 검증한다.

굳이 개선한다면 KRX 시간외 대량매매 체결 데이터를 별도로 받아 매칭하는 방법이 있으나,
같은 날 여러 건이면 귀속이 모호해 근사에 그친다. **문서화된 한계로 두는 게 맞다.**

## 4. 건드리면 안 되는 설계 결정

"정리"처럼 보이지만 되돌리면 문제가 되는 것들. 각각 고정 테스트가 있다.

1. **파서는 FI/대주주를 판정하지 않는다.** 분류는 `v_kr_stake_sales`의 `holder_bucket`
   CASE 문에만 있다. 파서에 넣으면 기준이 바뀔 때마다 전량 재수집이다.
2. **관계 코드는 변동 행이 아니라 별도 특별관계자 명부에 있다.** 사업자번호로 조인해야
   하며, 변동 행에서 직접 읽으면 분류 필드가 조용히 전부 NULL이 된다
   (`test_change_rows_are_joined_to_roster_codes`).
3. **`stock_kind`는 PK에 있어야 한다.** 같은 보고자·같은 날·같은 방법이라도 보통주와
   기타주식은 별개 변동이다. 빼면 한쪽이 덮어써져 조용히 사라진다 — 실제로
   `20260805000440`에서 64행 중 4행(191,684주 포함)이 유실됐었다
   (`test_same_holder_same_day_different_stock_kind_are_distinct_rows`).
4. **PK 컬럼은 NULL이 아니라 `''`를 쓴다** (`holder_id`, `stock_kind`, `method`).
   NULL은 서로 같지 않아 dedup이 조용히 깨진다. 예전엔 SQLite의 `INSERT OR REPLACE`가
   NULL을 조용히 `''`로 바꿔줘서 버텼지만 PostgreSQL은 거부하므로, 이제 모델의
   validator가 보장한다 (`test_model_turns_absent_key_fields_into_empty_strings`,
   `test_stock_kind_is_never_null_so_dedup_still_works`).
5. **미지 `HLD_MTH` 코드는 버리지 않고 원값 보존한다.**
6. **가격 조인은 (company, date)당 1건으로 접어야 한다.** krx·yfinance가 같은 날 종가를
   둘 다 갖고 있어 조인이 갈라지면 집계가 부풀려진다
   (`test_price_check_view_does_not_fan_out_across_price_sources`).
7. **`fetch_stake_changes`는 `"대량보유"`가 아닌 D 목록 항목의 원문을 열지 않는다.**
   임원·주요주주 보고서가 D 목록의 대다수라 이 필터가 호출량 대부분을 걸러낸다.
   전 종목 스캔이 예상보다 싼 이유가 이것이다.
8. **커서 키에 날짜 범위가 들어간다.** 2025년치를 끝낸 종목은 2026년 스윕에서 완료로
   치지 않는다 (`test_cursor_is_scoped_to_the_date_range`).
9. **호출 예산은 DB에 있다.** 메모리 카운터로 바꾸면 재실행마다 0으로 돌아가 실제
   한도를 지나친다 (`test_budget_survives_a_process_restart`).
10. **예산 소진은 종목 실패로 기록하지 않는다.** 실행 기회조차 없었으므로 `error`로
    남기면 데이터에 대한 거짓말이 된다. 커서에서 아예 빠진다.

## 5. 스키마 적용 동작 (알아둘 것)

**DB는 이제 PostgreSQL이다** (2026-09-04 이전 완료). 컴퓨터 A가 스윕을 돌리는 동안
컴퓨터 B가 같은 DB를 읽어야 해서 서버형으로 옮겼다. `.env`의 `ISSUER_DB_DSN`이 접속
문자열이며, 로컬이 아닌 호스트는 DSN이 직접 정하지 않는 한 `sslmode=verify-full`이 붙는다.

**스키마는 `init-db`에서만 적용된다.** 예전에는 `connect()`가 열 때마다 자동 적용했지만,
공유 서버에서는 접속하는 모든 프로세스가 DROP/CREATE VIEW를 치게 되어 서로 뷰를 지운다.
대신 `schema_meta`에 버전이 있고 `connect()`가 이를 **읽어서 경고만** 한다 — DDL은 치지 않는다.
경고가 뜨면 `init-db`를 돌릴 것.

3층 자동 마이그레이션(`_ADDED_COLUMNS`/`_REKEYED`/테이블 재생성)은 **삭제됐다.** SQLite가
PK를 못 바꿔서 있던 장치이고, PostgreSQL은 `ALTER TABLE ... DROP CONSTRAINT ... ADD
PRIMARY KEY`가 된다. 스키마를 바꾸면 `db.py`의 `SCHEMA_VERSION`을 올리고 `init-db`를 돌린다.

스윕과 분석을 다른 머신에서 돌린다면 **읽기 전용 롤**을 쓸 것 (README 참고). CLI의
`query` 읽기 전용 검사는 접두어만 보므로 `WITH ... DELETE RETURNING`에 뚫린다.

## 6. 관련 파일

- `src/issuer_data/kr_disclosure_parse.py` — 원문 파서 (코드 사전 포함)
- `src/issuer_data/collectors/kr_dart.py` — `fetch_stake_changes` / `fetch_treasury_disposals`,
  호출 과금(`_spend`), 접수번호 스킵
- `src/issuer_data/collectors/base.py` — `CallBudget` / `QuotaExceededError`
- `src/issuer_data/orchestrator.py` — `collect_coverage`의 재개·예산·`reparse` 루프
- `src/issuer_data/storage/db.py` — 접속 팩토리 · `init_db` · 스키마 버전 확인
- `src/issuer_data/storage/schema.sql` — 테이블 4개 + 뷰 3개
- `tests/test_kr_disclosure_parse.py` — 파서·뷰 회귀 14건
- `tests/test_market_scan_resume.py` — 재개·예산 회귀 20건
- `tests/test_schema_apply.py` — 스키마 적용/버전 경고 회귀
- `tests/test_no_sqlite_isms.py` — 방언 실수 grep (플레이스홀더·REAL 등)
- `docker-compose.test.yml` — 테스트용 Postgres (포트 55432, 비영속)
