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

# Filter filings by type (comma-separated, case-insensitive substring match on filing_type)
python -m issuer_data collect --market us --type filings --symbols AAPL --filing-type 8-K,10-K

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
  (original file + extracted text), **`fx_rates`**, **`company_peers`**, `collection_runs`.
- Views: **`v_latest_price`** (latest close, local + USD via nearest-prior FX),
  **`v_company_overview`** (one row per company with all its listings).

Every fact table carries a `source` column, so the same entity can hold data from multiple
providers; `source` is part of the composite primary keys to avoid collisions.

## Filing documents

`--type filings --download-docs` (or `download-docs` to backfill) downloads each filing's
original document into `data/documents/{market}/{symbol}/{filing_id}/` and extracts body
text into `filing_documents.text_content`:

- PDF (HKEXnews) → `pdfplumber`
- HTML/XML (SEC EDGAR / DART) → `BeautifulSoup` + `lxml`
- DART XBRL ZIP → unzip then parse inner XML

Scanned/image-only PDFs yield no text (`text_content` is NULL, `text_chars`=0); OCR is a
possible future add-on.

## Development

```bash
pip install -e ".[dev]"
pytest        # offline tests (schema, resolver, normalization, EDGAR parsing, docs)
ruff check src
```

## One-off research vs. reusable collectors

Not every question belongs in the durable `collectors/` -> `schema.sql`
pipeline above. If the data's already collected, just `query`/pandas it. If
it's new but unlikely to be asked again in the same shape, it goes in
[`research/`](research/) instead of a new schema table — see
[`research/README.md`](research/README.md) for the full triage rule
(including what to do when an existing collector is just missing a filter,
rather than the data being genuinely new).

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
| identifiers / LEI | SEC, DART, GLEIF | LEI via GLEIF (no key) |
| market cap / shares | FMP, yfinance | foreign-ownership % = KRX login only |
| ratios / valuation | FMP (+ computed) | US strongest |
| ownership (major/5%) | DART, FMP | US 13D/G structured = partial |
| institutional (13F) | FMP, EDGAR | |
| corporate actions | yfinance, FMP, DART | free via Yahoo |
| analyst / targets | FMP | US-centric |
| insider trades | **SEC Form 4 (free)**, DART | fully parsed (name/role/shares/price) |
| earnings / transcripts | FMP, AV | transcripts → documents |
| news / sentiment | FMP, AV | |
| index membership | FMP (US indices) | KR/HK not free |
| ESG | FMP (tier-gated) | sustainability reports → documents |

Honest gaps (land as empty columns, logged — never fabricated): **foreign-ownership %**
(KRX login), **KR/HK index-membership history**, granular ESG grades.

## Notes & limitations

- **Yahoo/yfinance** and **HKEXnews** use undocumented endpoints; they can rate-limit or
  change shape. Requests go through the standard `requests` client (proxy-aware), not the
  `yfinance` library's `curl_cffi` transport.
- **Alpha Vantage** free tier is ~5 req/min, ~25/day; the collector detects throttle
  responses and warns.
- **Cross-listing** auto-resolution prefers strong identifiers (LEI/CIK/corp_code/ISIN)
  and only then names; use the `link` command / overrides CSV for anything it misses.
- **FX/USD** conversion is a daily approximation (markets close at different times).
