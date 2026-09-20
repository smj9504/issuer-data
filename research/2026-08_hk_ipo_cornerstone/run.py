"""홍콩거래소(Main Board) 2025-01-01~2026-06-30 IPO의 코너스톤 투자자 배정 현황 표를 만든다.

Copy of research/_template/run.py adapted for this task. See ./README.md for
the question, population, method, and caveats — including why cornerstone
investor names/amounts are read by Claude from extracted prospectus text
rather than parsed with regex (research/README.md's "Irregular per-document
data" rule: the summary table's layout differs deal to deal, and is even
90-degree-rotated in some prospectuses — see README.md's caveats section).

This script only does the *fetch* half (population + raw prospectus text into
.cache/); the *transform* half (reading each cache/<code>.txt and structuring
cornerstone investor rows) is done interactively by Claude reading the cache
files, not by this script, because that's the part that needs judgment per
research/README.md.

Run: python run.py   (from inside this task's own folder)
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import pandas as pd

TASK_DIR = Path(__file__).parent
OUTPUT_DIR = TASK_DIR / "output"
CACHE_DIR = TASK_DIR / ".cache"
PDF_DIR = CACHE_DIR / "prospectus_pdfs"
TEXT_DIR = CACHE_DIR / "cornerstone_text"

REPO_SRC = TASK_DIR.parent.parent / "src"
sys.path.insert(0, str(REPO_SRC))

# --------------------------------------------------------------------- PARAMS
PARAMS = {
    "start_date": "2025-01-01",
    "end_date": "2026-06-30",
    "board": "Main Board",  # GEM excluded — question says "홍콩 거래소" generically
    # New Listing Report workbooks (one per year; each covers Jan1-current for
    # the current year). 2025 is a full closed year; 2026 is filtered down to
    # end_date below since the workbook itself runs through whatever "today" was.
    "nlr_urls": {
        2025: "https://www2.hkexnews.hk/-/media/HKEXnews/Homepage/New-Listings/New-Listing-Information/New-Listing-Report/Main/NLR2025_Eng.xlsx",
        2026: "https://www2.hkexnews.hk/-/media/HKEXnews/Homepage/New-Listings/New-Listing-Information/New-Listing-Report/Main/NLR2026_Eng.xlsx",
    },
}


# ---------------------------------------------------------------------- fetch
def _http_client():
    from stock_data.http.client import HttpClient

    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
    )
    return HttpClient(rate_limit=2.0, headers={"User-Agent": ua}), ua


def fetch_population() -> tuple[list[dict], list[dict]]:
    """Main Board IPOs in [start_date, end_date] from HKEX's New Listing Report.

    Each workbook row-pair is (a) Hong Kong Offer / (b) International Offer —
    "Funds Raised (HK$)" is summed across both to get the true total offer size.

    Returns (population, excluded) — excluded holds admissions in the date
    window that had no fresh-capital offer (GEM-to-Main-Board transfers,
    de-SPAC mergers, listings "by Introduction"; see _na_reason), which by
    construction have no cornerstone tranche and none of the requested
    offer-size columns. They're reported, not dropped silently.
    """
    client, ua = _http_client()
    rows: list[dict] = []
    for year, url in PARAMS["nlr_urls"].items():
        cache_path = CACHE_DIR / f"NLR{year}.xlsx"
        if not cache_path.exists():
            r = client.session.get(url, headers={"User-Agent": ua}, timeout=30)
            r.raise_for_status()
            cache_path.write_bytes(r.content)
        df = pd.ExcelFile(cache_path).parse("NLR", header=None)
        rows.extend(_parse_nlr_sheet(df))
        time.sleep(0.3)

    start, end = PARAMS["start_date"], PARAMS["end_date"]
    in_window = [r for r in rows if start <= r["listing_date"] <= end]
    pop = [r for r in in_window if not r["non_offering_reason"]]
    excluded = [r for r in in_window if r["non_offering_reason"]]
    pop.sort(key=lambda r: r["listing_date"])
    excluded.sort(key=lambda r: r["listing_date"])
    return pop, excluded


def _parse_nlr_sheet(df) -> list[dict]:
    """Each IPO occupies two data rows: label row (a) has real values, the
    following row is a '"' ditto row except for its own Funds-Raised figure.

    A handful of rows carry a literal "N/A - <reason>" string in the funds/price
    cells instead of numbers — HKEX uses the same New Listing Report for every
    Main Board admission, not only fresh-capital IPOs, so GEM-to-Main-Board
    transfers, de-SPAC mergers, and listings "by Introduction" (no new shares
    offered) show up too. None of those had an offer/cornerstone tranche, so
    they're tagged non_offering_reason instead of silently left with
    offer_price_hkd=None indistinguishable from a real parsing gap.
    """
    out: list[dict] = []
    i = 0
    n = len(df)
    while i < n:
        seq = df.iat[i, 0]
        if isinstance(seq, (int, float)) and not pd.isna(seq):
            stock_code = str(df.iat[i, 1]).strip().zfill(5)
            name = re.sub(r"\s+", " ", str(df.iat[i, 2])).strip()
            prospectus_date = _excel_date(df.iat[i, 3])
            listing_date = _excel_date(df.iat[i, 4])
            funds_a_raw = df.iat[i, 8]
            offer_price_raw = df.iat[i, 9]
            non_offering_reason = _na_reason(funds_a_raw) or _na_reason(offer_price_raw)
            funds_a = _to_float(funds_a_raw)
            funds_b = _to_float(df.iat[i + 1, 8]) if i + 1 < n else None
            out.append(
                {
                    "stock_code": stock_code,
                    "company_name": name,
                    "prospectus_date": prospectus_date,
                    "listing_date": listing_date,
                    "offer_price_hkd": _to_float(offer_price_raw),
                    "funds_raised_hk_offer_hkd": funds_a,
                    "funds_raised_intl_offer_hkd": funds_b,
                    "total_funds_raised_hkd": (funds_a or 0) + (funds_b or 0),
                    "non_offering_reason": non_offering_reason,
                }
            )
            i += 2
        else:
            i += 1
    return out


def _na_reason(v) -> str | None:
    """'N/A - Transfer of Listing from GEM to Main Board' -> that reason string."""
    if not isinstance(v, str) or not v.strip().upper().startswith("N/A"):
        return None
    return re.sub(r"\s+", " ", v.split("-", 1)[1]).strip() if "-" in v else "N/A"


def _excel_date(v) -> str | None:
    if pd.isna(v):
        return None
    if hasattr(v, "strftime"):
        return v.strftime("%Y-%m-%d")
    return str(v)


def _to_float(v) -> float | None:
    if pd.isna(v):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_prospectus_text(pop: list[dict]) -> list[dict]:
    """For each IPO, locate its GLOBAL OFFERING prospectus PDF via HKEXnews
    filing search, download it, and cache the extracted text of pages
    mentioning "CORNERSTONE" (plus a same-count neighborhood) so Claude can
    read a short per-deal excerpt instead of a 500-page PDF. Deals with no
    "CORNERSTONE" hits at all get an explicit empty marker (real outcome —
    plenty of small-cap HK IPOs have no cornerstone tranche)."""
    from stock_data.http.client import HttpClient

    client, ua = _http_client()
    search_client = HttpClient(
        rate_limit=2.0,
        headers={
            "User-Agent": ua,
            "Referer": "https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=en",
        },
    )
    stocklist = search_client.get_json(
        "https://www1.hkexnews.hk/ncms/script/eds/activestock_sehk_e.json"
    )
    stock_id_by_code = {str(it.get("c", "")).zfill(5): it.get("i") for it in stocklist}

    results: list[dict] = []
    for rec in pop:
        code = rec["stock_code"]
        text_cache = TEXT_DIR / f"{code}.json"
        if text_cache.exists():
            results.append(json.loads(text_cache.read_text(encoding="utf-8")))
            continue

        outcome = {"stock_code": code, "company_name": rec["company_name"]}
        stock_id = stock_id_by_code.get(code)
        if stock_id is None:
            outcome["status"] = "no_stock_id"
            _write_json(text_cache, outcome)
            results.append(outcome)
            continue

        pdf_url = _find_prospectus_url(search_client, stock_id, rec["listing_date"])
        if not pdf_url:
            outcome["status"] = "prospectus_not_found"
            _write_json(text_cache, outcome)
            results.append(outcome)
            continue
        outcome["prospectus_url"] = pdf_url

        pdf_path = PDF_DIR / f"{code}.pdf"
        if not pdf_path.exists():
            r = client.session.get(
                pdf_url, headers={"User-Agent": ua, "Referer": "https://www1.hkexnews.hk/"}, timeout=90
            )
            r.raise_for_status()
            pdf_path.write_bytes(r.content)
            time.sleep(0.5)

        excerpt, total_pages = _extract_cornerstone_excerpt(pdf_path)
        outcome["total_pages"] = total_pages
        outcome["status"] = "has_cornerstone_section" if excerpt else "no_cornerstone_mention"
        outcome["cornerstone_text"] = excerpt
        _write_json(text_cache, outcome)
        results.append(outcome)
        print(f"  {code} {rec['company_name'][:40]:40s} -> {outcome['status']}")

    return results


def _find_prospectus_url(search_client, stock_id: int, listing_date: str | None) -> str | None:
    """The prospectus filing is titled GLOBAL OFFERING under the "Listing
    Documents" category, filed shortly before listing_date."""
    import datetime as dt

    if listing_date:
        end = dt.date.fromisoformat(listing_date)
        start = end - dt.timedelta(days=60)
    else:
        start = dt.date(2024, 6, 1)
        end = dt.date(2026, 7, 1)
    params = {
        "sortDir": 0, "sortByOptions": "DateTime", "category": 0, "market": "SEHK",
        "stockId": stock_id, "documentType": -1,
        "fromDate": start.strftime("%Y%m%d"), "toDate": end.strftime("%Y%m%d"),
        "title": "", "searchType": 1, "t": 1, "lang": "en",
    }
    data = search_client.get_json("https://www1.hkexnews.hk/search/titleSearchServlet.do", params=params)
    raw = data.get("result")
    if not raw:
        return None
    records = json.loads(raw)
    for r in records:
        title = (r.get("TITLE") or "").upper()
        long_text = r.get("LONG_TEXT") or ""
        link = r.get("FILE_LINK") or ""
        if "GLOBAL OFFERING" in title and "Listing Documents" in long_text and link.lower().endswith(".pdf"):
            return "https://www1.hkexnews.hk" + link if link.startswith("/") else link
    return None


def _extract_cornerstone_excerpt(pdf_path: Path) -> tuple[str, int]:
    """Concatenate the CORNERSTONE INVESTORS chapter, identified by its
    repeated page HEADER (first non-blank line is "CORNERSTONE INVESTOR" or
    "CORNERSTONE INVESTORS" — the exact wording is template-dependent: Bloks
    Group/Mixue Group/BrainAurora header every chapter page "CORNERSTONE
    INVESTORS" (plural), while ContiOcean/Beijing Saimo use the singular
    "CORNERSTONE INVESTOR". Matching only the plural silently dropped both of
    those deals' real chapters (confirmed by manual page dump — the plural
    match returned has_section=False for both, but they clearly have a
    normal 6-8 page Cornerstone Investors chapter under the singular
    heading), so the header check accepts either.

    A first version matched the phrase "CORNERSTONE INVESTOR" occurring
    ANYWHERE on the page (not just as the header), which also fires on
    unrelated chapters that mention cornerstone investors in passing — e.g.
    an "UNDERWRITING" chapter's force-majeure/termination clause routinely
    lists "a portion of the investment commitments made by any cornerstone
    investors under agreements signed with such cornerstone investors [being
    withdrawn]" as one of many termination triggers. On New Gonow
    Recreational Vehicles (00805) that produced a false positive: two stray
    hits in unrelated chapters (Underwriting, Appendix IV) with no real
    Cornerstone Investors chapter at all, one of which got returned as if it
    were the chapter. Requiring the page-HEADER match (not just presence
    anywhere) is what actually fixes that false positive — it fires only on
    pages that are part of the chapter, never on a passing mention; the
    singular/plural question above is orthogonal to that fix.

    Investor-name/amount prose reads fine even on pages whose *summary
    table* rendered rotated/garbled — see README.md caveats — so no
    table-geometry logic is used here.
    """
    import pdfplumber

    with pdfplumber.open(pdf_path) as pdf:
        n = len(pdf.pages)
        page_texts = [(p.extract_text() or "") for p in pdf.pages]

    header_hits = [
        i
        for i, t in enumerate(page_texts)
        if _first_line(t) in ("CORNERSTONE INVESTOR", "CORNERSTONE INVESTORS")
    ]
    if not header_hits:
        return "", n

    lo, hi = max(0, header_hits[0] - 1), min(n - 1, header_hits[-1] + 1)
    excerpt = "\n\n".join(f"--- page {i + 1} ---\n{page_texts[i]}" for i in range(lo, hi + 1))
    return excerpt, n


def _first_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line.upper()
    return ""


def _write_json(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_ccy_usd_rates(ccy: str, start: str, end: str) -> dict[str, float]:
    """Daily <ccy>->USD spot, {date: rate}. NLR states offer sizes in HKD;
    cornerstone amounts, read per-deal from each prospectus, come in whatever
    currency that deal's summary table used (USD, HKD, or RMB across the
    pilot sample) — so both HKD and RMB conversion are needed to normalize
    everything to USD before summing. Reuses services._fetch_ccy_to_usd — the
    same Yahoo-quotes helper collect_fx uses — without its DB upsert, per
    research/README.md's "reuse what exists, treat the DB as read-only"
    convention.
    """
    from stock_data.http.client import HttpClient
    from stock_data.services import _fetch_ccy_to_usd

    client = HttpClient(rate_limit=2.0, headers={"User-Agent": "stock-data research (fx)"})
    rates = _fetch_ccy_to_usd(client, ccy, start, end)
    return {r.rate_date: r.rate for r in rates}


def nearest_prior_rate(rates: dict[str, float], target_date: str) -> float | None:
    """HKD/USD is a currency-board peg (~7.75-7.85), so any nearby trading-day
    rate is accurate to the precision this table needs; exact-date is tried
    first, then the closest earlier date within a week (covers weekends/HK
    public holidays with no FX print)."""
    if target_date in rates:
        return rates[target_date]
    import datetime as dt

    d = dt.date.fromisoformat(target_date)
    for back in range(1, 8):
        candidate = (d - dt.timedelta(days=back)).isoformat()
        if candidate in rates:
            return rates[candidate]
    return None


# ------------------------------------------------------------------ transform
# NOT done in this script. Claude reads each .cache/cornerstone_text/<code>.json
# directly (schema: stock_code, company_name, status, cornerstone_text) and
# structures cornerstone investor name/amount against the schema documented in
# README.md's "Method" section, then hand-assembles output/result.csv. See
# research/README.md's "Irregular per-document data" rule for why.


def main() -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    PDF_DIR.mkdir(exist_ok=True)
    TEXT_DIR.mkdir(exist_ok=True)

    pop, excluded = fetch_population()
    print(f"Population: {len(pop)} Main Board IPOs in {PARAMS['start_date']}..{PARAMS['end_date']}")
    if excluded:
        print(f"Excluded (no fresh-capital offer): {len(excluded)}")
        for r in excluded:
            print(f"  {r['stock_code']} {r['company_name'][:45]:45s} -> {r['non_offering_reason']}")
    (CACHE_DIR / "population.json").write_text(
        json.dumps(pop, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (CACHE_DIR / "excluded_non_offering.json").write_text(
        json.dumps(excluded, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    results = fetch_prospectus_text(pop)
    n_has = sum(1 for r in results if r.get("status") == "has_cornerstone_section")
    n_none = sum(1 for r in results if r.get("status") == "no_cornerstone_mention")
    n_missing = len(results) - n_has - n_none
    print(f"Cornerstone section found: {n_has} | confirmed none: {n_none} | lookup failed: {n_missing}")
    print(f"Raw text cached under {TEXT_DIR} for Claude to read and structure into output/result.csv")


if __name__ == "__main__":
    main()
