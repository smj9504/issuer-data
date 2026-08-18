"""Run the structured PDF engine over gold cases and score accuracy.

For each case the extracted tables are matched to the gold tables (each gold
table paired to the predicted table with the highest TEDS), then TEDS / GriTS /
numeric-EM are averaged over gold tables and paragraph-continuity is scored on
the reflowed text. Results aggregate per category and overall. Passing an
``escalator`` (see ``pdf_escalate``) reports the escalated-table count and
estimated cost alongside the scores, so accuracy lift can be measured against cost.
"""

from __future__ import annotations

from ..pdf_extract import extract_structured
from . import metrics
from .gold import GoldCase, load_gold_dir, synthetic_cases


def _match_and_score(pred_tables, gold_tables) -> dict:
    if not gold_tables:
        return {"teds": None, "grits": None, "numeric_em": None}
    pred_grids = [t.rows for t in pred_tables]
    teds_s = grits_s = num_s = 0.0
    for gold in gold_tables:
        if pred_grids:
            best = max(pred_grids, key=lambda p: metrics.teds(p, gold))
        else:
            best = []
        teds_s += metrics.teds(best, gold)
        grits_s += metrics.grits_con(best, gold)
        num_s += metrics.numeric_exact_match(best, gold)
    n = len(gold_tables)
    return {"teds": teds_s / n, "grits": grits_s / n, "numeric_em": num_s / n}


def score_case(case: GoldCase, *, escalator=None, threshold: float = 0.66,
               cost_per_page: float = 0.0) -> dict:
    sdoc = extract_structured(case.pdf_bytes, escalator=escalator, threshold=threshold,
                              cost_per_page=cost_per_page)
    result = {
        "name": case.name,
        "category": case.category,
        "continuity": metrics.paragraph_continuity(sdoc.text, case.paragraphs)
        if case.paragraphs else None,
        "escalated": sdoc.escalated_count,
        "est_cost": sdoc.est_cost,
    }
    result.update(_match_and_score(sdoc.tables, case.tables))
    return result


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def run_eval(cases: list[GoldCase], *, escalator=None, threshold: float = 0.66,
             cost_per_page: float = 0.0) -> dict:
    per_case = [score_case(c, escalator=escalator, threshold=threshold,
                           cost_per_page=cost_per_page) for c in cases]
    by_cat: dict[str, list[dict]] = {}
    for r in per_case:
        by_cat.setdefault(r["category"], []).append(r)
    categories = {
        cat: {metric: _mean(r[metric] for r in rows)
              for metric in ("teds", "grits", "numeric_em", "continuity")}
        for cat, rows in by_cat.items()
    }
    overall = {metric: _mean(r[metric] for r in per_case)
               for metric in ("teds", "grits", "numeric_em", "continuity")}
    return {
        "cases": per_case,
        "categories": categories,
        "overall": overall,
        "escalated_total": sum(r["escalated"] for r in per_case),
        "est_cost_total": round(sum(r["est_cost"] for r in per_case), 4),
        "n_cases": len(per_case),
    }


def default_cases(gold_dir=None) -> list[GoldCase]:
    """Synthetic matrix + any real labelled cases under gold_dir."""
    cases = synthetic_cases()
    if gold_dir:
        cases += load_gold_dir(gold_dir)
    return cases
