"""<one-line description of the question this answers>

Copy this template to research/<YYYY-MM>_<slug>/run.py and fill in PARAMS /
fetch() / transform(). Runs as-is with dummy data — that's just to prove the
skeleton and imports work; replace the dummy bodies with real logic. See
research/README.md for conventions (what's tracked, reusing existing infra,
LLM-assisted extraction for irregular documents, when to graduate this into
a real collector).

Run: python run.py   (from inside this task's own folder)
"""

from __future__ import annotations

from pathlib import Path

TASK_DIR = Path(__file__).parent
OUTPUT_DIR = TASK_DIR / "output"
CACHE_DIR = TASK_DIR / ".cache"

# --------------------------------------------------------------------- PARAMS
# Whatever defines the population for this question (date range, market,
# symbols, ...) — keep it at the top so the scope is visible without reading
# the rest of the script.
PARAMS = {"example_param": "replace me"}


# ---------------------------------------------------------------------- fetch
# Reuse issuer_data.http.client.HttpClient(rate_limit=...) for new HTTP
# calls, issuer_data.documents.extract_text(content, fmt) for PDF/HTML/XML/
# ZIP text, and issuer_data.services.collect_fx(repo, settings, start, end)
# for point-in-time FX. Cache raw downloads under CACHE_DIR so re-running
# while iterating on parsing doesn't re-hit rate-limited sources.
def fetch() -> list[dict]:
    return [{"example_field": "replace fetch() with real logic"}]  # dummy


# ------------------------------------------------------------------ transform
# Plain code, or — for irregular per-document data (tables whose shape
# differs document to document, e.g. allotment tables) — read extract_text()
# output directly and have Claude structure it against a schema rather than
# writing regex parsing. See research/README.md's "Irregular per-document
# data" section.
def transform(raw: list[dict]) -> list[dict]:
    return raw  # dummy: passthrough


# --------------------------------------------------------------------- output
# Tracked in git on purpose — keep it small (the point is a readable answer,
# not a data dump). CSV via pandas is the default.
def write_output(result: list[dict]) -> Path:
    import pandas as pd

    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / "result.csv"
    pd.DataFrame(result).to_csv(path, index=False)
    return path


def main() -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    result = transform(fetch())
    path = write_output(result)
    print(f"Wrote {len(result)} row(s) to {path}")


if __name__ == "__main__":
    main()
