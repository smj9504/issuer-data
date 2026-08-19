# Research (one-off investigations)

This directory is for questions that need data issuer-data doesn't already have
modeled/collected, and that are unlikely to be asked again in the same shape —
as opposed to data that belongs in the durable `collectors/` -> `schema.sql`
pipeline. See "One-off research vs. reusable collectors" in the top-level
README for the short version of this triage rule.

## Triage: where does a new question go?

1. **Already-collected data** — no new fetching needed. Use
   `issuer-data query --sql ...` or read `data/issuer_data.sqlite` with
   pandas directly. Nothing to add here.
2. **One-off research** — new data, unlikely to be asked again in this shape.
   → a task folder in this directory (see below).
3. **Reusable collection** — the same source/shape will clearly be queried
   again, or needs to join against `companies`/`securities`. → extend
   `collectors/` + `models.py` + `schema.sql` + `tests/` instead. If it's not
   scoped to a market/symbol (like statutes), follow
   `collectors/kr_law.py`'s convention: a standalone class, its own CLI
   subcommand, its own namespaced tables with a nullable `company_id` FK, and
   a raw-JSON catch-all table for anything not individually modeled.

   Also bucket 3: if a collector already exists and the data is already
   modeled, but it's missing a filter/narrowing parameter you need — extend
   that collector or the orchestrator directly instead of working around the
   gap here. Already fixed this way: `Orchestrator.collect(...,
   filing_types=[...])` / `collect --type filings --filing-type 8-K,10-K` —
   EDGAR, DART, HKEXnews and FMP all already return a `filing_type` on every
   `Filing`, they just had no way to filter by it before.

Rule of thumb for 2 vs 3: **would a different future question also want this
data kept centrally?** Reference data (FX rates, statutes, LEI, symbol
masters) is worth persisting via the existing services even when a one-off
task is what triggered fetching it — e.g. call `services.collect_fx()` for
just the narrow window you need; it's cheap and the next task benefits too.
The specific *answer* to one question (e.g. "these 40 deals had these
investors") is not — that's what this directory is for.

## Starting a new task

Copy `_template/` to `<YYYY-MM>_<slug>/` (e.g. `2026-08_hk_ipo_cornerstone/`)
and fill in `run.py` + `README.md`.

```
research/
  <YYYY-MM>_<slug>/
    run.py        # tracked — the actual fetch/transform/output logic
    README.md     # tracked — question, population, method, caveats
    output/       # tracked — final result (usually small: dozens/hundreds of rows)
    .cache/       # gitignored — raw downloads, intermediate scratch (can be large)
```

`run.py` and `output/` are tracked in git on purpose: the method and the
answer are both cheap (a script is a few KB; a final result table is usually
dozens to hundreds of rows) and are the actual point of doing the research —
losing them means redoing the work. `.cache/` (raw downloaded documents,
anything bulky and re-fetchable) is gitignored — that's the part that's
genuinely wasteful to keep around forever.

If a task is well and truly done and won't be revisited, it's fine to delete
the whole folder — it's still in git history if you need it back. But the
default is to keep it; delete only when you're sure.

## Reuse what already exists — don't build a new abstraction layer

No `research_lib` package. Import directly from the main package:

- **HTTP**: `issuer_data.http.client.HttpClient(rate_limit=...)` — the same
  rate-limited client every collector uses.
- **Document text extraction**: `issuer_data.documents.extract_text(content,
  fmt)` — PDF/HTML/XML/ZIP already handled, already a public function.
- **Point-in-time FX**: `issuer_data.services.collect_fx(repo, settings,
  start, end)` to backfill just the narrow window you need into `fx_rates`,
  then look up the nearest-prior spot rate yourself — same SQL pattern as
  `v_latest_price` in `storage/schema.sql`. No new helper needed.
- **Existing DB**: `issuer_data.storage.db.connect()` + `Repository` to join
  against `companies`/`securities` etc. Treat it as read-only by convention
  (don't call `upsert_*` methods) unless you're deliberately filling in
  reference data per the rule of thumb above.

## Irregular per-document data: LLM-assisted extraction, not regex parsing

When each document has a different shape and there aren't many of them (e.g.
allotment/allocation tables that differ deal by deal), don't write bespoke
regex/heuristic parsing code — the way `collectors/hk_hkexnews.py`'s
`_parse_annual_report` does for the much more uniform shape of financial
statements. Instead:

1. `extract_text()` the document down to plain text (no change needed).
2. Have Claude read the extracted text directly and structure it against a
   schema you've defined up front (fan out with the Agent tool if there are
   many documents) — no per-document parsing code to maintain.
3. Spot-check a sample of the results by hand, especially numbers, currency
   units, and name spellings.

The reusable asset here is the **extraction schema/field definitions**, not
parsing code — write down what you asked for and how in the task's
`README.md`. If the same document type turns out to need this repeatedly at
volume, that's when it's worth graduating to real parsing code (below).

## Graduating to a real collector

If a task's fetch/parse logic (or an LLM-extraction schema that's proven
stable) turns out to be needed a second time: move the logic into
`src/issuer_data/collectors/<source>.py` (write a proper parsing function if
it started as LLM-assisted extraction), add a typed model to `models.py`, add
a table to `storage/schema.sql` with `source` in its primary key (and a
nullable `company_id` FK if relevant) — `statutes`/`law_api_raw` in
`schema.sql` are the template — and add tests under `tests/` following
`tests/test_kr_law.py`'s style (trimmed real-response fixtures as module
constants, monkeypatched HTTP calls). The one-off script can then be deleted,
or kept as a thin caller of the new collector.
