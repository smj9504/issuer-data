"""Accuracy metrics for the structured PDF engine — dependency-free.

- ``teds``  — Tree-Edit-Distance-based Similarity (Pawlik/Zhang–Shasha) over the
  ``table → row → cell`` tree. Grid tables are exactly depth-3, so the general
  tree edit reduces to a two-level sequence alignment (rows, then cells), which is
  implemented exactly here — 1.0 for identical tables, degrading with edits.
- ``grits_con`` — a factored GriTS content score: the optimal ordered row
  alignment maximizing per-row cell LCS, scored as a cell-content F-measure.
- ``numeric_exact_match`` — F1 over the multiset of numeric tokens (digits only,
  so locale/format differences don't matter).
- ``paragraph_continuity`` — fraction of gold paragraphs that survive reflow as a
  contiguous (whitespace-normalized) substring — catches wrong splits/joins.

All return a float in [0, 1]; comparing two empty inputs returns 1.0.
"""

from __future__ import annotations

import re

Grid = list[list[str]]


def _norm(cell) -> str:
    return re.sub(r"\s+", " ", str("" if cell is None else cell)).strip().lower()


def _cells(grid: Grid) -> int:
    return sum(len(r) for r in grid)


# ------------------------------------------------------------------------ TEDS
def _cell_edit(a: list[str], b: list[str]) -> int:
    """Edit distance between two cell-label sequences (sub cost 0 if equal else 1)."""
    a = [_norm(c) for c in a]
    b = [_norm(c) for c in b]
    n, m = len(a), len(b)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        dp[i][0] = i
    for j in range(1, m + 1):
        dp[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            sub = 0 if a[i - 1] == b[j - 1] else 1
            dp[i][j] = min(dp[i - 1][j] + 1, dp[i][j - 1] + 1, dp[i - 1][j - 1] + sub)
    return dp[n][m]


def _tree_size(grid: Grid) -> int:
    # 1 (table node) + per row (1 row node + one leaf per cell)
    return 1 + sum(1 + len(r) for r in grid)


def teds(pred: Grid, gold: Grid) -> float:
    """TEDS over the table/row/cell tree (table & row relabels are free)."""
    n, m = len(pred), len(gold)
    if n == 0 and m == 0:
        return 1.0
    # sequence alignment over rows; deleting/inserting a whole row costs its node count
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        dp[i][0] = dp[i - 1][0] + 1 + len(pred[i - 1])
    for j in range(1, m + 1):
        dp[0][j] = dp[0][j - 1] + 1 + len(gold[j - 1])
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            drop = dp[i - 1][j] + 1 + len(pred[i - 1])
            add = dp[i][j - 1] + 1 + len(gold[j - 1])
            sub = dp[i - 1][j - 1] + _cell_edit(pred[i - 1], gold[j - 1])
            dp[i][j] = min(drop, add, sub)
    ted = dp[n][m]
    return 1.0 - ted / max(_tree_size(pred), _tree_size(gold))


# ------------------------------------------------------------------- GriTS-Con
def _lcs_len(a: list[str], b: list[str]) -> int:
    a = [_norm(c) for c in a]
    b = [_norm(c) for c in b]
    n, m = len(a), len(b)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            dp[i][j] = (dp[i - 1][j - 1] + 1) if a[i - 1] == b[j - 1] \
                else max(dp[i - 1][j], dp[i][j - 1])
    return dp[n][m]


def grits_con(pred: Grid, gold: Grid) -> float:
    """Factored GriTS content F-score: best ordered row alignment × per-row cell LCS."""
    tp, tg = _cells(pred), _cells(gold)
    if tp == 0 and tg == 0:
        return 1.0
    if tp == 0 or tg == 0:
        return 0.0
    n, m = len(pred), len(gold)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            dp[i][j] = max(dp[i - 1][j], dp[i][j - 1],
                           dp[i - 1][j - 1] + _lcs_len(pred[i - 1], gold[j - 1]))
    matches = dp[n][m]
    return 2 * matches / (tp + tg)


# ------------------------------------------------------------- numeric / prose
_NUM = re.compile(r"\d[\d,]*\.?\d*")


def _numbers(grid: Grid) -> list[str]:
    out: list[str] = []
    for row in grid:
        for cell in row:
            for tok in _NUM.findall(str(cell or "")):
                d = re.sub(r"\D", "", tok)
                if d:
                    out.append(d)
    return out


def _multiset_overlap(a: list[str], b: list[str]) -> int:
    from collections import Counter

    ca, cb = Counter(a), Counter(b)
    return sum((ca & cb).values())


def numeric_exact_match(pred: Grid, gold: Grid) -> float:
    """F1 over the multiset of numeric tokens (digits only)."""
    p, g = _numbers(pred), _numbers(gold)
    if not p and not g:
        return 1.0
    if not p or not g:
        return 0.0
    overlap = _multiset_overlap(p, g)
    if overlap == 0:
        return 0.0
    precision = overlap / len(p)
    recall = overlap / len(g)
    return 2 * precision * recall / (precision + recall)


def paragraph_continuity(pred_text: str, gold_paragraphs: list[str]) -> float:
    """Fraction of gold paragraphs recovered WITHIN a single reflowed paragraph.

    Splits the prediction on blank-line paragraph breaks first, so a gold
    paragraph that reflow left split across a spurious break is *not* counted —
    that is exactly the failure this metric exists to catch. Whitespace within a
    paragraph is normalized so intra-paragraph line wraps don't matter.
    """
    if not gold_paragraphs:
        return 1.0
    pred_paras = [re.sub(r"\s+", " ", chunk).strip().lower()
                  for chunk in re.split(r"\n\s*\n", pred_text or "")]
    hits = 0
    for para in gold_paragraphs:
        needle = re.sub(r"\s+", " ", para).strip().lower()
        if any(needle in pp for pp in pred_paras):
            hits += 1
    return hits / len(gold_paragraphs)
