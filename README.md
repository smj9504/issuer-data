# issuer-data

Multi-market issuer/security **data collector** into a unified **SQLite** database.

It pulls **issuer/security master**, **daily OHLCV prices**, **financial statements**,
and **disclosures/filings** (with original documents + extracted text) from Korea, Hong
Kong, and the US, and supports **cross-listing** (e.g. US ADRs), **multi-market views**,
and **domestic-vs-overseas peer comparison** with both local-currency and USD figures.

## Sources

| Market | Master | Prices | Financials | Filings |
|--------|--------|--------|------------|---------|
| Korea (KR) | KRX¹ / yfinance | KRX¹ / yfinance | **DART** | **DART** |
| Hong Kong (HK) | **HKEXnews** | yfinance / FMP | FMP | **HKEXnews** |
| US | **SEC EDGAR** | yfinance / FMP / Alpha Vantage | **SEC EDGAR** / FMP / AV | **SEC EDGAR** / FMP |

Cross-cutting: **FMP** (global profile/prices/financials/filings/peers), **Alpha Vantage**
(prices/overview/fundamentals), **FX** (USDKRW/USDHKD via Yahoo).

¹ **KRX note:** KRX's data portal now requires a *free member login* (`KRX_ID`/`KRX_PW`),
so `pykrx` returns nothing anonymously. To keep KR working out-of-the-box, the default KR
master/price source is **yfinance**. Set `KRX_ID`/`KRX_PW` and pass `--source krx` to use
pykrx instead.

## Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# or: pip install -e .
```

## Configure

Copy `.env.example` to `.env` and fill in what you have (every key is optional — a missing
key just skips that source):

```
ISSUER_DART_API_KEY=            # free: https://opendart.fss.or.kr
ISSUER_FMP_API_KEY=             # free tier: https://financialmodelingprep.com
ISSUER_ALPHAVANTAGE_API_KEY=    # free: https://www.alphavantage.co/support/#api-key
ISSUER_SEC_USER_AGENT=Your Name your-email@example.com   # required or EDGAR returns 403
ISSUER_DB_PATH=data/issuer_data.sqlite
```

No key is needed for SEC EDGAR (just a descriptive User-Agent), HKEXnews, or yfinance.

## Usage

```bash
python -m issuer_data init-db

# US (no key needed)
python -m issuer_data collect --market us --type master     --symbols AAPL,MSFT
python -m issuer_data collect --market us --type financials --symbols AAPL
python -m issuer_data collect --market us --type filings    --symbols AAPL --download-docs

# Prices (yfinance) for any market — symbol forms are normalized automatically
python -m issuer_data collect --market kr --type prices --symbols 005930 --start 2024-06-01 --end 2024-06-30
python -m issuer_data collect --market hk --type prices --symbols 0700.HK

# Hong Kong filings from HKEXnews (+ download original PDFs and extract text)
python -m issuer_data collect --market hk --type filings --symbols 00700 --download-docs

# FX (USDKRW/USDHKD) for currencies present in the DB, then compare across markets
python -m issuer_data collect --market all --type fx --start 2024-06-01 --end 2024-07-10
python -m issuer_data compare --symbols AAPL,005930,0700.HK

# Cross-listing: link a US ADR to its home listing (or use a curated overrides CSV)
python -m issuer_data link --symbols US:BABA,HK:9988
python -m issuer_data link --overrides            # applies data/company_overrides.csv

python -m issuer_data status
python -m issuer_data query --sql "SELECT * FROM v_company_overview LIMIT 10"
```

`--source` overrides the default source for any `(market, type)`; choices:
`krx, dart, hkexnews, edgar, yfinance, fmp, alphavantage`.

### Overrides CSV (`data/company_overrides.csv`)

Force known dual-listings onto one company (rows sharing a `group` are merged):

```csv
group,market,symbol
alibaba,US,BABA
alibaba,HK,9988
tencent,HK,00700
tencent,US,TCEHY
```

## Data model

Two-tier entity model for cross-listing:

- **`companies`** — the real-world issuer (financials, filings, peers attach here;
  carries LEI/CIK/corp_code/ISIN).
- **`securities`** — each listing (KRX 005930, HKEX 00700, NYSE BABA, ADR TCEHY); prices
  attach here; many securities → one company.
- **`identifier_xref`** — crosswalk so any source resolves to the right `company_id`.
- **`prices`**, **`financials`** (long/tidy), **`filings`**, **`filing_documents`**
  (original file + extracted text), **`filing_tables`** (structured PDF tables, one row per
  cell), **`fx_rates`**, **`company_peers`**, `collection_runs`.
- Views: **`v_latest_price`** (latest close, local + USD via nearest-prior FX),
  **`v_company_overview`** (one row per company with all its listings).

Every fact table carries a `source` column, so the same entity can hold data from multiple
providers; `source` is part of the composite primary keys to avoid collisions.

## Filing documents

`--type filings --download-docs` (or `download-docs` to backfill) downloads each filing's
original document into `data/documents/{market}/{symbol}/{filing_id}/` and extracts body
text into `filing_documents.text_content`:

- PDF (HKEXnews) → `pdfplumber`
- HTML/XML (SEC EDGAR) → `BeautifulSoup` + `lxml`
- 한글 HWP 5.x → `olefile` + the HWPTAG_PARA_TEXT records; HWPX → its section XML
- archives (DART filings, XBRL bundles) → each member extracted by the same rules,
  recursively

The format is decided by **sniffing the bytes**, not the URL or the Content-Type.
DART's `document.xml` endpoint returns a ZIP under a Content-Type of
`application/x-msdownload`, so every name-based guess is wrong; files are also saved
under the extension they really are. A document that downloads but yields no text is
logged and counted in the run summary rather than stored as a silent `text_chars=0`.

Pages with no text layer yield nothing to pdfplumber, so **OCR runs by default** — and it
runs **per page**, not only when the whole document is empty. A filing is often mostly
born-digital with a few scanned pages or a chart holding its numbers inside an image; those
pages are rendered and passed through Tesseract — multilingual (`eng+kor+chi_tra` by default),
so Korean/English/Traditional-Chinese scans all recover text into `text_content`. Turn it
off per run with `--no-ocr`, or globally with `ISSUER_OCR_ENABLED=false`.

PyMuPDF/pytesseract/Pillow install with the package, but the Tesseract engine is a system
package:

```bash
apt install tesseract-ocr tesseract-ocr-kor tesseract-ocr-chi-tra
```

Without it OCR is skipped and logs one warning saying so, naming how many pages of the
document have no text layer — a page that contributed nothing is reported rather than
quietly missing from `text_content`.

Charts are also where a line-based table detector goes wrong: a bar chart's gridlines look
like a grid, and the near-empty table it yields would ground at a perfect confidence because
there are no numbers in it to check. Detected grids that are almost entirely empty are
dropped.

### Structured PDF extraction (`--extract-tables`)

Add `--extract-tables` to `--download-docs` (or to `download-docs`) to run a
template-, country-, and language-agnostic structured pass on PDFs. The logic is
geometry-based, so it works on Korean/US/HK annual reports and government filings alike:

```bash
python -m issuer_data collect --market hk --type filings --symbols 00700 \
    --download-docs --extract-tables
```

- **Two table detectors** — filled-in forms (HKEX Monthly Returns, Next Day Disclosure
  Returns) are found by their ruling lines. Results announcements and annual reports draw
  no rules at all, holding their columns apart with whitespace, so pages the ruled pass
  leaves empty go through a geometry pass that rebuilds the grid from word positions:
  cells split at gaps wide relative to the font, rows kept only where a column grid holds
  for several lines and the columns past the first are mostly numeric (so justified prose
  is not mistaken for a table), and right-aligned header rows pulled back in. Each table
  records which found it in `filing_tables.source_engine` (`pdfplumber` / `column-geometry`).
- **Cross-page table stitching** — a table broken by a page break is rejoined when the
  previous page's table ends at the bottom margin, the next page's starts at the top
  margin, and their column x-signatures match; a repeated header row on the continuation
  is dropped. Cells land in **`filing_tables`** (one row per cell, with `page_start`/
  `page_end` and a `source_engine`).
- **Narrative reflow** — running headers/footers and page numbers (found by recurrence
  across pages, not a pattern list) are removed, line-end hyphenation is joined, and
  paragraphs split across lines/pages are merged into continuous text (CJK joined without
  inserted spaces). The reflowed text replaces the flat per-page blob in `text_content`.
- **Numeric grounding + grid consistency** — every number emitted in a table is checked
  against the raw text layer, an anti-hallucination guard on escalation output. Grounding
  alone cannot judge a *locally* read table, whose digits come from that same text layer
  and so always ground at 1.0, so a detection is also scored on how consistently its body
  holds one column count. The weaker of the two is stored as `filing_tables.confidence`
  and is the signal escalation keys on; tables below the threshold are marked
  `filing_tables.needs_review=1`.

#### Optional local ML detectors (free, no API)

The two built-in detectors are geometry only. For harder layouts, `--ml-tables` swaps the
geometry pass for a model on the pages the ruled pass left empty:

```bash
pip install '.[ml]'          # torch + transformers + docling, several GB
python -m issuer_data collect --market hk --type filings --symbols 00700 \
    --download-docs --extract-tables --ml-engine table-transformer
```

- `table-transformer` — Microsoft's [Table Transformer](https://github.com/microsoft/table-transformer)
  (MIT). Detects the table, then its rows and columns, per page.
- `docling` — IBM's [Docling](https://github.com/docling-project/docling) (MIT). Converts
  the whole document at once, so its tables are merged back in by page number.

Both are **free and run on this machine** — no API key, no per-page charge, unrelated to
the paid escalation below. The cost is weight: several GB of wheels and model files, and
inference measured in seconds per page instead of milliseconds. So the tier is off by
default, and if the extra is not installed the run logs one warning and falls back to the
built-in detectors rather than failing.

In both cases the **text still comes from the PDF's own text layer** — the model is asked
only where the cell boundaries are, so filed numbers stay byte-exact and nothing is
introduced by an OCR pass.

The front-end is `pdfplumber` plus the geometry pass; the ML tier above is opt-in.

#### Optional paid escalation (off + local-only by default)

Only the **low-confidence tail** is re-processed by a paid LLM, never the whole corpus.
Escalation is **disabled and local-only by default** — `ISSUER_PDF_LOCAL_ONLY=true`
hard-blocks any off-box call even when enabled, so documents never leave the machine
unless you opt in on **both** flags and provide a key:

```bash
ISSUER_PDF_ESCALATION_ENABLED=true ISSUER_PDF_LOCAL_ONLY=false \
ISSUER_ESCALATION_API_KEY=sk-... \
python -m issuer_data collect --market us --type filings --symbols AAPL \
    --download-docs --extract-tables
```

The `TextReconstructionEscalator` sends only the low-confidence page's **text layer** (no
image) plus the garbled rows to the LLM and re-grounds the JSON it returns — on failure it
returns nothing and the local result is kept with `needs_review=1` (never fabricated). A
`VisionEscalator` interface is stubbed for scanned pages (needs a page-render backend).
Cost is logged per run using `ISSUER_ESCALATION_COST_PER_PAGE`.

### Knowing whether a parse worked (`validate`)

PDFs differ without limit — layout, language, tables, charts, scans — so "was this parsed
correctly?" cannot be answered in general without a label. Two narrower questions can be,
and every document is checked against them automatically:

**Does the extraction contradict itself?** Four checks that need no ground truth:

| Check | Catches | Cost |
| --- | --- | --- |
| **Coverage** | content that silently vanished | free |
| **Arithmetic** | a shifted cell or merged column | free |
| **Engine consensus** | anything one detector reads ambiguously | a second parse |
| **Cross-source** | figures that disagree with other sources | a DB query |

Coverage is the half that grounding cannot see: `ground_numbers` asks "did we invent a
number?", and a locally-read table always answers 1.0 because its digits come from the same
text layer. It cannot ask "did we drop half the table?" — coverage does, by measuring how much
of the page's own content reached the output. Arithmetic is the strongest reference-free
signal in financial documents: when a 합계 / Total row equals the rows above it, the parse of
that column is almost certainly right, and a shifted cell breaks the identity immediately.

**Did we get the fields we came for?** Declare them as a JSON schema and every value carries
its page and the line it was read from, so a review is one line to check rather than an
80-page PDF.

```bash
python -m issuer_data validate --file filing.pdf            # verdict + the evidence for it
python -m issuer_data validate --file filing.pdf --agreement          # add a second detector
python -m issuer_data validate --file filing.pdf --crosscheck-symbol KR:005930
python -m issuer_data validate --file filing.pdf --fields schema.json # require these fields
python -m issuer_data review-queue                          # documents awaiting a human
```

Every document gets one of three verdicts, stored in `filing_extraction_reports`:

- **PASS** — every check that could run, ran clean. Not a guarantee of correctness: it means
  nothing contradicted the extraction.
- **REVIEW** — a signal fired. This is the human queue, and it is meant to be small.
- **FAIL** — nothing usable: no text layer, content lost wholesale, or a required field
  missing.

A check that could not run never counts against a document: a table with no total row reports
zero attempted checks, not a failed one. Runs report their verdict mix (`— extraction: 38
pass, 2 review`) so a bad parse cannot pass as a quiet success.

Cross-source validation exploits something this project already has: the same companies'
financials arrive from DART, EDGAR, FMP, Alpha Vantage and yfinance. A figure that also
arrives through an independent API needs no reviewer at all. Unit scale is handled (a filing
prints 백만원 where the API stores 원), and the three outcomes are deliberately not two —
*confirmed*, *conflict* (the document has that row and none of its cells agree — the strong
signal), and *unmatched*, which is excluded from the score because an interim report simply
does not carry every annual line item.

### Measuring extraction accuracy (`eval`)

```bash
python -m issuer_data eval                 # score the built-in synthetic gold matrix
python -m issuer_data eval --json          # full report as JSON
python -m issuer_data eval --escalate      # also run configured escalation, report lift/cost
```

Reports **TEDS**, **GriTS-Con**, numeric **exact-match**, and **paragraph-continuity** per
category (US/en · KR/ko · HK/zh × single-table · split-table · prose) and overall. Prose
continuity is scored in all three languages because the rejoining rule differs by script:
Chinese closes up a line break with no space, Korean keeps one (Hangul sits in the CJK block
but is space-delimited between 어절), and scoring English alone said nothing about either. The
synthetic cases are authored from known data so ground truth is exact. Add real labelled
documents under `data/eval/<name>/{doc.pdf, expected.json}` (see `data/eval/README.md`) and
they are scored automatically — no code change.

The eval also reports how well the label-free gate above tracks the labelled truth:

```
verdicts: fail 1, pass 7
gate (TEDS >= 0.95): caught 1, missed 0, false alarms 0, review rate 20%
```

**`missed`** is the number that matters — documents the gate passed while the labels say they
are wrong. That is its blind spot, and the only honest reason to keep auditing a random sample
of PASS documents by hand. The gold set deliberately includes an image-only PDF that the local
engine cannot read, so there is always at least one case that *must* be caught; it also drags
the overall TEDS down, which is the point — an eval containing only documents you already
handle tells you nothing.

Calibrating is the job the labels exist for: pick thresholds where the measured `missed` rate
is acceptable, rather than trusting the defaults in `.env.example`. Human effort then goes to
three places only — a small stratified gold set per document family (not per document), the
REVIEW queue, and a random audit of PASS documents to measure what the gate misses.

## Development

```bash
pip install -e ".[dev]"
pytest        # offline tests (schema, resolver, normalization, EDGAR parsing, docs)
ruff check src
```

## Extended coverage (Extension B)

Beyond the four core data types, the tool collects many more categories. `--type` accepts:
`metrics` (market-cap/shares), `ratios`, `ownership`, `institutional`, `actions`
(dividends/splits), `analyst`, `insiders`, `earnings`, `news`, `index`, `esg`, plus
`lei` (GLEIF Legal Entity Identifier enrichment).

```bash
python -m issuer_data collect --market us --type insiders --symbols AAPL   # Form 4 (free, EDGAR)
python -m issuer_data collect --market us --type actions  --symbols AAPL   # dividends/splits (free, Yahoo)
python -m issuer_data collect --market us --type lei      --symbols AAPL   # LEI via GLEIF (free)
python -m issuer_data collect --market us --type ratios   --symbols AAPL   # needs FMP key
```

### Statement scope & currency conversion

- **연결 vs 별도 / 분기 vs 연간:** `financials.fs_scope` is `CFS` (연결/consolidated) or `OFS`
  (별도/separate); both are stored along with quarterly and annual periods. Default
  queries/`compare` use `CFS` + `FY`.
- **Accounting-correct USD:** `v_financials_usd` converts each figure with the right FX —
  **period-average** rate for income-statement / cash-flow flows, **period-end closing**
  rate for balance-sheet stocks, daily spot for prices. `fx_rates.rate_type` is `spot`
  (daily) or `avg` (derived period-average); `collect --type fx` produces both.

### Coverage by source (free tiers)

| category | free source(s) | notes |
|----------|----------------|-------|
| category | free source(s) implemented | notes |
|----------|----------------------------|-------|
| identifiers / LEI | SEC, DART, GLEIF | LEI via GLEIF (no key) |
| market cap / shares | FMP; **KRX** (KR listed-shares + **foreign-ownership %**) | KRX needs free KRX_ID/KRX_PW |
| ratios / valuation | FMP (+ computed) | US strongest |
| ownership (major/5% + 13D/G) | **DART** (majorstock 5%+), **SEC 13D/13G** (free) | 13D/G = best-effort cover-page parse |
| institutional (13F) | FMP | via FMP institutional-holder |
| corporate actions | yfinance, FMP | free via Yahoo |
| analyst / targets | FMP | US-centric |
| insider trades | **SEC Form 4 (free)**, **DART** (임원·주요주주) | US: name/role/shares/price; KR: exec/major-holder reports |
| earnings | **FMP** (earnings calendar) | US-centric |
| news / sentiment | FMP | |
| index membership | **FMP** (S&P 500 / Nasdaq 100 / Dow), **KRX** (KOSPI200/KRX100/KOSDAQ150) | current snapshot; history not free |
| ESG | FMP (tier-gated) | sustainability reports → documents |

The first column lists only sources that are **actually wired** to a collector method; a
`--type` with no implemented source for a market records a `skipped` run (never `ok` with
0 rows) and writes nothing. Remaining honest gaps (empty, logged — never fabricated):
**index-membership history** (only the current snapshot is collected), earnings-call
transcripts, and granular ESG grades. KR foreign-ownership % and KR index membership need
a **free** KRX member login (`KRX_ID` / `KRX_PW`); US 13D/G is parsed heuristically from
filing cover pages, so treat its shares/percent as best-effort.

## Notes & limitations

- **Yahoo/yfinance** and **HKEXnews** use undocumented endpoints; they can rate-limit or
  change shape. Requests go through the standard `requests` client (proxy-aware), not the
  `yfinance` library's `curl_cffi` transport.
- **Alpha Vantage** free tier is ~5 req/min, ~25/day; the collector detects throttle
  responses and warns.
- **Cross-listing** auto-resolution prefers strong identifiers (LEI/CIK/corp_code/ISIN)
  and only then names; use the `link` command / overrides CSV for anything it misses.
- **FX/USD** conversion is a daily approximation (markets close at different times).
