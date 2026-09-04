# 클라우드 DB 설정 — Supabase

작성: 2026-09-04
목적: 전 종목 스윕(A 컴퓨터)과 분석(B 컴퓨터)이 같은 DB를 보게 한다.

계정 생성과 DSN 발급은 **직접** 해야 한다(브라우저 작업). 나머지는 아래 명령으로 검증된다.

---

## 0. 규모 — 무료 티어로 충분하다

| 항목 | 값 |
|---|---|
| 빈 스키마 | 11 MB |
| 스윕 후 예상 | 30만 행 내외, 수백 MB |
| Supabase 무료 | 500 MB |

여유는 있지만 넉넉하지는 않다. `filing_documents.text_content`(원문 본문)까지 채우면
빠르게 늘어나므로, 스윕은 `--type stake`만 돌리고 원문 백필은 나중에 판단할 것.

## 1. 프로젝트 생성

1. https://supabase.com → 가입 → **New project**
2. **Region: Northeast Asia (Seoul)** — DART가 한국이라 왕복이 짧다. 스윕은 종목당
   왕복이 발생하므로 3,988회분 지연이 그대로 누적된다.
3. Database Password를 **강한 값으로 생성하고 따로 보관**한다. 이 비밀번호가 DSN에
   들어가고, 나중에 화면에서 다시 볼 수 없다.

## 2. DSN 확인

Project Settings → **Database** → Connection string → **URI**.

두 가지가 보인다. **어느 쪽을 쓰는지가 중요하다:**

| | 포트 | 용도 |
|---|---|---|
| **Direct connection** | 5432 | **A(수집)에 이걸 쓴다.** 세션이 그대로 유지된다 |
| Transaction pooler | 6543 | 짧은 요청 다수용. prepared statement가 제한된다 |

스윕은 연결 하나를 몇 시간 잡고 트랜잭션을 직접 관리하므로 **direct(5432)** 가 맞다.
B(분석)는 어느 쪽이든 무방하다.

```
postgresql://postgres:<PASSWORD>@db.<ref>.supabase.co:5432/postgres
```

## 3. `.env`에 기록

```bash
# .env  (git에 올라가지 않는다 — .gitignore 2행)
ISSUER_DB_DSN=postgresql://postgres:<PASSWORD>@db.<ref>.supabase.co:5432/postgres
```

`sslmode`를 적지 않아도 된다. 로컬이 아닌 호스트에는 코드가 `sslmode=verify-full`을
자동으로 붙인다 (libpq 기본값 `prefer`는 조용히 평문으로 내려가므로 그대로 두면 안 된다).

## 4. 스키마 적용 + 점검

```bash
python -m issuer_data init-db
python -m issuer_data check-db
```

`check-db`가 이렇게 나와야 한다:

```
DSN        postgres@db.<ref>.supabase.co:5432/postgres
Server     PostgreSQL 15.x (UTF8)
Connect    <ms>   TLS: yes            ← yes 여야 한다
Schema     2026-09-04                 ← MISSING이면 init-db
Role       postgres   writes: yes
Data       KR securities 0, ...
```

**TLS가 no로 나오면 멈추고 DSN을 확인할 것.**

## 5. B 컴퓨터용 읽기 전용 롤

A가 되돌릴 수 없는 이틀 수집을 돌리는 동안 B가 같은 DB에 붙는다. 실수 한 번이
수집분을 날리므로 **B는 읽기 전용 롤로 붙인다.** CLI의 `query` 검사는 접두어만 보므로
`WITH t AS (DELETE ... RETURNING *) SELECT * FROM t`에 뚫린다 — 방어선이 아니라 난간이다.

Supabase 대시보드의 **SQL Editor**에 그대로 붙여넣는다:

```sql
-- 비밀번호는 A의 것과 다른 값으로
CREATE ROLE analyst LOGIN PASSWORD '<ANALYST_PASSWORD>';

GRANT CONNECT ON DATABASE postgres TO analyst;
GRANT USAGE ON SCHEMA public TO analyst;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO analyst;

-- 이 줄을 빠뜨리기 쉽다: 이후에 생기는 테이블은 위 GRANT에 안 걸린다
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO analyst;

-- 이중 안전장치: 이 롤의 트랜잭션은 읽기 전용으로 시작한다
ALTER ROLE analyst SET default_transaction_read_only = on;

-- B의 폭주 쿼리가 수집을 방해하지 못하게
ALTER ROLE analyst SET statement_timeout = '120s';
ALTER ROLE analyst SET idle_in_transaction_session_timeout = '60s';
```

B의 `.env`:
```bash
ISSUER_DB_DSN=postgresql://analyst:<ANALYST_PASSWORD>@db.<ref>.supabase.co:5432/postgres
```

B에서 확인 — `writes: no`가 나와야 한다:
```bash
python -m issuer_data check-db
```

## 6. 스윕 시작

```bash
# 1) 종목 마스터 (DART 호출 0회)
python -m issuer_data collect --market kr --type master --source dart
python -m issuer_data query --sql "SELECT COUNT(*) FROM securities WHERE market='KR'"
#    → 3,988 근처가 나와야 한다. 2면 마스터가 안 채워진 것이니 멈출 것

# 2) 스윕 첫날
python -m issuer_data collect --market kr --type stake \
    --start 2026-01-01 --end 2026-12-31 --max-api-calls 18000

# 3) 다음 날
python -m issuer_data collect --market kr --type stake \
    --start 2026-01-01 --end 2026-12-31 --max-api-calls 18000 --resume
```

## 7. 장시간 연결 — 이미 처리돼 있다

스윕은 연결 하나를 몇 시간 잡고 있고 대부분의 시간을 API 호출에 쓴다. 그 사이 트래픽이
없으면 중간의 NAT나 로드밸런서가 연결을 끊는다.

- **TCP keepalive**: 유휴 60초 후 프로브 (`db.py`의 `_KEEPALIVE`). 끊기기 전에 클라이언트가
  먼저 트래픽을 만든다.
- **재접속**: 연결이 죽으면 종목 단위 에러 핸들러가 다시 연다
  (`orchestrator._recover_connection`). 스윕이 죽지 않고 다음 종목으로 넘어간다.
  회귀 테스트: `test_a_dropped_connection_does_not_end_the_sweep`.

즉 연결이 한 번 끊겨도 스윕은 이어진다. 다만 **재접속조차 실패하면** 예외를 올려
멈춘다 — 그때는 `--resume`으로 이어가면 커서부터 재개된다.

## 8. 함정

- **Supabase 무료는 1주일 미사용 시 일시정지**된다(수동 재개). 스윕 중에는 매일 쓰므로
  문제없지만, 분석을 한동안 쉬다 돌아오면 대시보드에서 재개해야 한다.
- **`postgres` 롤을 B에 주지 말 것.** §5의 `analyst`를 쓴다.
- **테스트는 절대 이 DB를 가리키지 않게 한다.** 테스트는 스키마를 통째로 지우고
  TRUNCATE를 돌린다. 테스트용은 `docker compose -f docker-compose.test.yml up -d`
  (포트 55432)이고, `conftest.py`의 기본값이 그쪽이라 실수로 섞일 일은 없다.
- **비밀번호가 로그에 남지 않는다** — `redact()`가 DSN에서 지운다. 다만 직접
  `echo $ISSUER_DB_DSN` 같은 걸 붙여넣지 않도록 주의.
