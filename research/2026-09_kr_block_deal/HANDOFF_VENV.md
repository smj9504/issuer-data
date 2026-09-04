# 인수인계 — 프로젝트 전용 venv 구축

작성: 2026-09-04
대상: venv 작업을 이어받을 다음 세션
선행 문서: 같은 폴더의 `HANDOFF.md`(전 종목 스윕 대기 상태) — **본 작업과 독립적이다.**

## 한 줄 요약

지금은 시스템 파이썬 하나에 **265개 패키지**가 뒤섞여 있고, 그 탓에 이미 두 번 깨졌다
(한 번은 고쳤고, 하나는 고아로 남아 있다). **venv를 만드는 것이 근본 해법이지만,
지금 당장 깨진 것은 없다.** 이건 예방 작업이지 장애 대응이 아니다.

---

## 0. 현재 환경 실측 (2026-09-04)

| 항목 | 값 |
|---|---|
| Python | 3.12.6 — `C:\Users\user\AppData\Local\Programs\Python\Python312\python.exe` |
| venv | **없음** (`.venv`/`venv`/`env` 모두 부재) |
| 설치 패키지 | **265개** (프로젝트 선언 의존성은 16개 + extras) |
| 테스트 | **240 passed, 1 skipped, 0 failed** · ruff clean |
| `.gitignore` | `.venv/`, `venv/` 이미 등록됨 (29~30행) |

핵심 버전:

```
numpy 2.5.1      scipy 1.18.1     numba 0.61.2 (깨짐, 고아)
pandas 2.3.3     pykrx 1.2.8      torch 2.14.0 (0.53GB)
transformers 4.57.1   timm 1.0.29   docling 미설치
```

## 1. 왜 이 작업이 필요한가 — 실제로 있었던 일

시스템 파이썬 공유 때문에 **이번 세션에서만 두 건**이 드러났다.

### 1.1 고쳤음: scipy/numpy ABI 충돌 (테스트 2건 실패)

`tests/test_pdf_ml_tables.py` 2건이 실패하고 있었다. 원인은 **둘**이었다:

1. **scipy 1.12.0이 numpy 1.x ABI로 빌드됨** → numpy 2.5.1에서
   `ImportError: numpy.core.multiarray failed to import`.
   `transformers`가 detection 경로에서 `scipy.optimize`/`scipy.sparse`를 끌어오는데,
   이 예외가 `pdf_extract.py:405`의 `except Exception`에 삼켜져 **표가 조용히 빈 채로**
   반환됐다. → `scipy 1.18.1`로 해결.
2. **`timm` 미설치** — scipy를 고친 *뒤에야* 드러났다. Table Transformer의 backbone이
   요구한다. `pyproject.toml`의 `ml` extra는 `timm>=1.0`을 **이미 올바르게 선언**하고
   있었고, 이 환경에 extra가 반쪽만 설치돼 있었을 뿐이다. → `timm 1.0.29` 설치로 해결.

**교훈: 코드 결함이 아니라 전부 환경 결함이었다.** venv였다면 `pip install '.[ml]'`
한 번으로 둘 다 안 생겼다.

이 세션의 코드 변경은 **의존성 핀 2줄뿐**이다 (아직 커밋 안 함, §5 참고):
- `pyproject.toml` — `ml` extra에 `scipy>=1.13` + 이유 주석
- `requirements.txt` — 동일 미러

### 1.2 안 고쳤음: numba 고아 패키지

```
numba 0.61.2 요구: numpy<2.3,>=1.24    ←  설치된 numpy 2.5.1은 범위 밖
$ python -c "import numba"
ImportError: Numba needs NumPy 2.2 or less. Got NumPy 2.5.
```

**의도적으로 두었다.** 검증 결과 도달 불가능하기 때문:

| 확인 | 결과 |
|---|---|
| 우리 소스가 쓰는가 (`grep numba\|njit\|@jit`) | 0건 |
| 설치 패키지 중 요구하는 것이 있는가 (전 배포판 스캔) | 0건 |
| `pyproject.toml`에 있는가 | 없음 (main/dev/ocr/ml 어디에도) |
| 프로젝트 import 후 `sys.modules`에 뜨는가 | False |

즉 **아무도 요구하지 않는 고아**다. venv를 만들면 애초에 안 들어오므로 **이 작업으로
자동 해소된다.** 별도 조치 불필요.

## 2. 하면 되는 일

```bash
cd c:/projects_2026/issuer-data

# 1) 생성 — .gitignore에 이미 있으므로 .venv 이름을 쓸 것
python -m venv .venv

# 2) 활성화 (PowerShell)
.venv\Scripts\Activate.ps1
#   Git Bash라면: source .venv/Scripts/activate

# 3) 설치 — editable + 필요한 extras
python -m pip install --upgrade pip
pip install -e ".[dev,ocr,ml]"

# 4) 검증 — 이 수치와 같아야 한다
python -m pytest          # 기대: 240 passed, 1 skipped, 0 failed
#   (docling까지 깔리면 241 passed, 0 skipped)
ruff check .              # 기대: All checks passed!
```

### 검증 시 반드시 확인할 것

```bash
# venv를 쓰고 있는지 (시스템 파이썬이면 작업이 무의미)
python -c "import sys; print(sys.executable)"   # .venv 경로가 나와야 함

# 고아가 안 따라왔는지
pip list | grep -i numba                        # 아무것도 안 나와야 정상

# 패키지 수 — 265개에서 대폭 줄어야 한다
pip list --format=freeze | wc -l
```

## 3. 함정 — 미리 알고 시작할 것

### 3.1 `numpy>=2`는 협상 불가

`pykrx 1.2.8`이 `numpy>=2.0`을 요구한다. **pykrx는 전 종목 스윕 1단계(종목 마스터
수집)의 핵심**이라 뺄 수 없다. 따라서:

- **numpy를 2 미만으로 내리는 해법은 전부 오답이다.** 충돌이 나면 상대 패키지를
  올려야 한다 (scipy에서 실제로 그렇게 했다).
- `pandas>=2.2.2` 핀도 같은 이유다 — 2.2.2가 `numpy<2` 상한을 없앤 버전.
  `pyproject.toml:13`과 `requirements.txt:2`에 주석으로 남아 있다.

### 3.2 `ml` extra는 무겁다 — 나눠 깔아도 된다

`torch`만 0.53GB이고 Table Transformer 가중치는 첫 실행 때 HF에서 추가로 받는다.
급하면 `.[dev,ocr]`만 깔고 시작해도 된다. 그 경우 ML 테스트는 **실패가 아니라 스킵**
으로 빠진다 (`requires_tatr` 가드). 단 §1.1의 함정 재현을 원치 않으면 `ml`을 깔 때
**extra 전체를 한 번에** 깔 것 — 반쪽 설치가 바로 그 사고의 원인이었다.

### 3.3 남은 스킵은 1건뿐이다

```
SKIPPED [1] tests/test_pdf_ml_tables.py:122: ML extra not installed
```

docling 미설치. `ml` extra에 선언돼 있으므로 **`.[ml]`을 깔면 사라진다** (`pip install
--dry-run`으로 확인: numpy 2.5.1·torch 2.14.0과 충돌 없음).

**OCR 6건은 더 이상 스킵되지 않는다 — 이번에 고쳤다.** 원래는 폰트 경로가 리눅스
2개만 하드코딩돼 있어서(`/usr/share/fonts/truetype/dejavu/...`) 윈도우에서는 폰트가
`C:\Windows\Fonts`에 가득한데도 "no scalable font available"로 빠졌다. 즉 **윈도우에서
OCR 폴백 로직이 한 번도 테스트된 적이 없었다.** `_font_path()`가 플랫폼별 폰트
디렉터리를 실제로 탐색하도록 바꿔서 6건 모두 통과한다 (§7 참고).

### 3.4 `.env`는 건드리지 말 것

`.env`에 `ISSUER_DART_API_KEY` 등 실제 키가 들어 있고 `.gitignore` 2행에 등록돼 있다.
venv를 만들어도 `.env`는 CWD 기준으로 읽히므로 **그대로 두면 된다.** 새로 만들거나
덮어쓰지 말 것.

### 3.5 DB는 venv와 무관하다

DB는 PostgreSQL 서버이므로(2026-09-04 이전) venv와 무관하다. `.env`의
`ISSUER_DB_DSN`만 맞으면 되고, venv 작업이 DB를 건드릴 이유가 없다. 테스트는 별도의
일회용 컨테이너를 쓴다: `docker compose -f docker-compose.test.yml up -d`.
현재 상태는 `HANDOFF.md` §0 참고 (KR 종목 2개, `kr_stake_changes` 189행 —
검증용 표본 수준이며 본 스윕은 아직 안 돌렸다).

## 4. 하지 않아도 되는 일

- **numba 처리** — venv를 만들면 자동으로 빠진다 (§1.2).
- **시스템 파이썬 정리** — 265개 중 상당수는 다른 프로젝트 것일 수 있다.
  `pip uninstall`로 청소하려 들지 말 것. **다른 프로젝트가 무엇을 쓰는지 확인되지
  않았다.** venv를 만드는 목적 자체가 시스템 파이썬을 안 건드리는 것이다.
- **`ml_available` 가드 개선** — `pdf_ml_tables.py:42`가 최상위 패키지만 검사해서
  "반쪽 설치"일 때 스킵 대신 실패로 나타난다. 이번에 논의됐으나 **손대지 않기로
  했다**: 프로덕션 동작은 의도대로고(예외를 삼키고 내장 detector로 폴백), venv에서는
  반쪽 설치 자체가 안 생긴다. 고치고 싶어지면 그때 별도로 판단할 것.

## 5. 커밋 안 된 변경 (이어받을 때 확인)

```
 M pyproject.toml      # ml extra에 scipy>=1.13 + 주석
 M requirements.txt    # 동일 미러
 M tests/test_pdf_ocr.py            # 폰트 탐색 이식성 (§7)
?? research/2026-09_kr_block_deal/HANDOFF_VENV.md   # 이 문서
```

HEAD는 `549bf66`. 전부 **아직 커밋하지 않았다.**
venv 검증이 끝나면 함께 커밋하는 게 자연스럽다 — 모두 같은 뿌리(환경 의존성)다.

## 6. 관련 파일

- `pyproject.toml` — 의존성/extras 정의 (`dev`, `ocr`, `ml`), `requires-python >=3.10`
- `requirements.txt` — pyproject의 미러 (ml/ocr는 주석 처리된 opt-in)
- `.gitignore` — `.env`(2행), `.venv/`(29행), `venv/`(30행)
- `src/issuer_data/pdf_ml_tables.py` — `ml_available`/`ml_ready` 가드, TATR 로딩
- `src/issuer_data/pdf_extract.py:405` — ML 실패를 삼키고 폴백하는 지점
- `research/2026-09_kr_block_deal/HANDOFF.md` — **본 작업과 별개인** 전 종목 스윕 인수인계

## 7. 이번에 고친 것: OCR 테스트 폰트 탐색

`tests/test_pdf_ocr.py`가 폰트를 이렇게 찾고 있었다:

```python
for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
```

리눅스 경로 2개 하드코딩. 윈도우에는 없으니 6건이 전부 스킵됐고, 스킵은 초록색이라
아무도 안 들여다봤다. 실측하니 **문제는 폰트 부재가 아니었다**:

| 확인 | 결과 |
|---|---|
| `ocr_available()` | True — OCR 스택 정상 |
| tesseract 바이너리 | `C:\Program Files\Tesseract-OCR\tesseract` 존재 |
| `C:/Windows/Fonts/arial.ttf` | 열림 |

**쓸 수 있는 폰트가 있는데 테스트가 못 찾은 것**이다. `_font_path()`를 넣어
(1) Pillow의 bare-name 조회 → (2) 플랫폼별 폰트 디렉터리 재귀 탐색 순으로 아무 폰트나
찾게 했다. 결과: **6건 전부 통과.** OCR 로직 자체에는 버그가 없었다 — 검증만 안 되고
있었을 뿐이다.

`@cache`로 첫 탐색 결과를 재사용하므로 glob 비용은 세션당 한 번이다.
폰트가 정말 없는 머신에서는 여전히 스킵된다 (그때는 진짜 스킵이 맞다).
