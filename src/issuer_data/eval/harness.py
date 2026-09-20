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
               cost_per_page: float = 0.0, agreement: bool = False) -> dict:
    sdoc = extract_structured(case.pdf_bytes, escalator=escalator, threshold=threshold,
                              cost_per_page=cost_per_page)
    if agreement:
        from ..pdf_agreement import agreement as run_agreement
        from ..pdf_validate import decide

        consensus = run_agreement(case.pdf_bytes, reference=sdoc.tables)
        if sdoc.validation is not None:
            sdoc.validation.agreement = consensus.score
            decide(sdoc.validation)
    validation = sdoc.validation
    cov = getattr(validation, "coverage", None)
    result = {
        "name": case.name,
        "category": case.category,
        "continuity": metrics.paragraph_continuity(sdoc.text, case.paragraphs)
        if case.paragraphs else None,
        "escalated": sdoc.escalated_count,
        "est_cost": sdoc.est_cost,
        # The reference-free gate, scored side by side with the reference-based
        # metrics: this is how we learn whether the runtime alarm actually tracks
        # accuracy, instead of assuming it does.
        "verdict": getattr(validation, "verdict", "unknown"),
        "reasons": getattr(validation, "reasons", []),
        "token_recall": getattr(cov, "token_recall", None),
        "numeric_recall": getattr(cov, "numeric_recall", None),
        "agreement": getattr(validation, "agreement", None),
    }
    result.update(_match_and_score(sdoc.tables, case.tables))
    return result


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def gate_calibration(per_case: list[dict], *, accurate_at: float = 0.95) -> dict:
    """Does the reference-free verdict actually track accuracy?

    Confusion between what the gate said (pass vs review/fail) and what the gold
    labels show (TEDS at or above ``accurate_at``). The number that matters is
    ``missed`` — documents the gate passed while the labels say they are wrong.
    That is the alarm's blind spot, and the only honest reason to keep sampling
    PASS documents by hand.
    """
    scored = [r for r in per_case if r.get("teds") is not None]
    if not scored:
        return {"n": 0}
    passed_and_good = passed_and_bad = flagged_and_good = flagged_and_bad = 0
    for r in scored:
        good = r["teds"] >= accurate_at
        if r.get("verdict") == "pass":
            passed_and_good += good
            passed_and_bad += not good
        else:
            flagged_and_good += good
            flagged_and_bad += not good
    reviewed = flagged_and_good + flagged_and_bad
    return {
        "n": len(scored),
        "accurate_at": accurate_at,
        "passed_correct": passed_and_good,
        "missed": passed_and_bad,            # passed but wrong — the blind spot
        "caught": flagged_and_bad,           # flagged and wrong — the alarm working
        "false_alarms": flagged_and_good,    # flagged but fine — the review cost
        "recall": (flagged_and_bad / (flagged_and_bad + passed_and_bad)
                   if (flagged_and_bad + passed_and_bad) else None),
        "precision": (flagged_and_bad / reviewed) if reviewed else None,
        "review_rate": reviewed / len(scored),
    }


def run_eval(cases: list[GoldCase], *, escalator=None, threshold: float = 0.66,
             cost_per_page: float = 0.0, agreement: bool = False) -> dict:
    per_case = [score_case(c, escalator=escalator, threshold=threshold,
                           cost_per_page=cost_per_page, agreement=agreement)
                for c in cases]
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
    verdicts: dict[str, int] = {}
    for r in per_case:
        verdicts[r["verdict"]] = verdicts.get(r["verdict"], 0) + 1
    return {
        "cases": per_case,
        "categories": categories,
        "overall": overall,
        "verdicts": verdicts,
        "gate": gate_calibration(per_case),
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
