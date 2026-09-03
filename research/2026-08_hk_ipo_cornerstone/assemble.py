"""Assemble output/result.csv from population.json + FX + Claude-structured
cornerstone data (cornerstone_structured.json, hand-written after reading each
.cache/cornerstone_text/<code>.json — see README.md's Method section for the
extraction schema and research/README.md's "Irregular per-document data" rule
for why this step isn't automated).

Run: python assemble.py   (from inside this task's own folder, after run.py
and after cornerstone_structured.json exists)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

TASK_DIR = Path(__file__).parent
OUTPUT_DIR = TASK_DIR / "output"
CACHE_DIR = TASK_DIR / ".cache"

sys.path.insert(0, str(TASK_DIR))
from run import fetch_ccy_usd_rates, nearest_prior_rate


# Each prospectus states its cornerstone table in whatever currency that
# deal's Cornerstone Placing section used — USD, HKD, or RMB across the
# pilot sample (see cornerstone_structured.json's per-investor "amount_*"
# keys). Normalize every investor row to USD before summing/ranking.
def _investor_usd(inv: dict, hkd_rate: float | None, rmb_rate: float | None) -> float | None:
    if "amount_usd" in inv:
        return inv["amount_usd"]
    if "amount_usd_million" in inv:
        return inv["amount_usd_million"] * 1_000_000
    if "amount_hkd_million" in inv:
        return inv["amount_hkd_million"] * 1_000_000 * hkd_rate if hkd_rate else None
    if "amount_rmb_million" in inv:
        return inv["amount_rmb_million"] * 1_000_000 * rmb_rate if rmb_rate else None
    return None


def main() -> None:
    population = json.loads((CACHE_DIR / "population.json").read_text(encoding="utf-8"))
    structured_path = TASK_DIR / "cornerstone_structured.json"
    structured = json.loads(structured_path.read_text(encoding="utf-8")) if structured_path.exists() else {}

    dates = [r["listing_date"] for r in population if r["listing_date"]]
    hkd_rates = fetch_ccy_usd_rates("HKD", min(dates), max(dates)) if dates else {}
    # Yahoo has no "RMB=X" ticker (RMB isn't the ISO code) — CNY is what
    # actually resolves, even though cornerstone_structured.json's field name
    # (matching how prospectuses themselves label the currency) says "rmb".
    rmb_rates = fetch_ccy_usd_rates("CNY", min(dates), max(dates)) if dates else {}

    rows = []
    for rec in population:
        code = rec["stock_code"]
        listing_date = rec["listing_date"]
        hkd_fx = nearest_prior_rate(hkd_rates, listing_date) if listing_date else None
        rmb_fx = nearest_prior_rate(rmb_rates, listing_date) if listing_date else None
        offer_usd = rec["total_funds_raised_hkd"] * hkd_fx if hkd_fx else None

        cs = structured.get(code, {})
        investors = cs.get("investors", [])  # [{name, amount_<ccy>[_million]}]
        for inv in investors:
            inv["_usd"] = _investor_usd(inv, hkd_fx, rmb_fx)
        ranked = sorted(investors, key=lambda x: -(x["_usd"] or 0))
        cs_total_usd = sum((inv["_usd"] or 0) for inv in investors) or None
        cs_pct = (cs_total_usd / offer_usd * 100) if (cs_total_usd and offer_usd) else 0.0

        rows.append(
            {
                "IPO 기업명": rec["company_name"],
                "IPO일자": listing_date,
                "공모총액(USD)": round(offer_usd) if offer_usd else None,
                "공모시총(USD)": None,  # out of scope for this pass — see README.md
                "참여 코너스톤 투자자 기관수": len(investors),
                "코너스톤 투자자 배정 총액(USD)": round(cs_total_usd) if cs_total_usd else 0,
                "코너스톤 배정 비율(%)": round(cs_pct, 2),
                "참여 코너스톤 투자자명(배정비중순)": ", ".join(inv["name"] for inv in ranked),
                "stock_code": code,  # traceability, not part of the requested headers
            }
        )

    OUTPUT_DIR.mkdir(exist_ok=True)
    df = pd.DataFrame(rows)
    out_path = OUTPUT_DIR / "hk_ipo_cornerstone_2025_2026H1.csv"
    df.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"Wrote {len(df)} rows to {out_path}")


if __name__ == "__main__":
    main()
