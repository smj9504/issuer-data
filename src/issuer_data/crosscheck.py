"""Cross-source validation: check a PDF's figures against data collected elsewhere.

This project already collects the same companies' financials from structured
sources — DART, EDGAR, FMP, Alpha Vantage, yfinance — into the ``financials``
table. That makes it the cheapest validator available for a PDF: a number that
also arrives through an independent API needs no reviewer at all.

The three outcomes are deliberately not two:

- **confirmed** — the figure is present in the document at some unit scale. Real
  positive evidence, from a source that never saw this PDF.
- **conflict** — the document has the row (its label matches the account) but no
  cell that agrees at any scale. This is the strong failure signal: either the
  parse shifted a cell or the two sources genuinely disagree, and both deserve a
  human.
- **unmatched** — the figure simply is not in this document. Not evidence of
  anything: an interim report does not carry every annual line item. Counting
  these as errors would drown the signal, so they are excluded from the score.

Unit scale is the whole difficulty: a Korean filing prints 백만원 while the API
stores 원, so raw equality would fail on every correct figure. Candidate scales
cover the KR (천/백만/억/조) and US (thousand/million/billion) conventions, and
the tolerance widens with scale so a figure rounded to its printed unit still
matches.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .logging import get_logger
from .pdf_validate import parse_number

log = get_logger(__name__)

# 원/천원/만원/백만원/억원/조원 and thousand/million/billion. Applied to the cell,
# because the document is the side that abbreviates.
_SCALES = (1, 1e3, 1e4, 1e6, 1e8, 1e9, 1e12)

CONFIRMED = "confirmed"
CONFLICT = "conflict"
UNMATCHED = "unmatched"


@dataclass
class FactCheck:
    account: str
    value: float
    fiscal_year: int | None
    fiscal_period: str | None
    source: str
    status: str
    scale: float | None = None
    evidence: str = ""

    def to_dict(self) -> dict:
        return {"account": self.account, "value": self.value,
                "fiscal_year": self.fiscal_year, "fiscal_period": self.fiscal_period,
                "source": self.source, "status": self.status,
                "scale": self.scale, "evidence": self.evidence[:200]}


@dataclass
class CrossCheckReport:
    checks: list[FactCheck] = field(default_factory=list)

    @property
    def confirmed(self) -> list[FactCheck]:
        return [c for c in self.checks if c.status == CONFIRMED]

    @property
    def conflicts(self) -> list[FactCheck]:
        return [c for c in self.checks if c.status == CONFLICT]

    @property
    def unmatched(self) -> list[FactCheck]:
        return [c for c in self.checks if c.status == UNMATCHED]

    @property
    def score(self) -> float | None:
        """Agreement among the facts actually located. None when none were."""
        decided = len(self.confirmed) + len(self.conflicts)
        return None if not decided else len(self.confirmed) / decided

    def to_dict(self) -> dict:
        return {
            "score": None if self.score is None else round(self.score, 4),
            "confirmed": len(self.confirmed),
            "conflicts": [c.to_dict() for c in self.conflicts[:20]],
            "unmatched": len(self.unmatched),
            "facts_checked": len(self.checks),
        }


def _norm_label(text) -> str:
    return re.sub(r"[\s:：·.()\[\]_/-]+", "", str(text or "")).lower()


def _numeric_cells(tables) -> list[tuple[float, str]]:
    """Every numeric cell in the document, with the row it sits in as evidence."""
    cells: list[tuple[float, str]] = []
    for table in tables or []:
        for row in table.rows:
            evidence = " | ".join(str(c or "") for c in row)
            for cell in row:
                value = parse_number(cell)
                if value is not None:
                    cells.append((value, evidence))
    return cells


def _rows_labelled(tables, names: list[str]) -> list[tuple[list, str]]:
    """Rows whose leading cells name one of `names` (so a conflict is provable)."""
    wanted = {_norm_label(n) for n in names if n}
    hits = []
    for table in tables or []:
        for row in table.rows:
            for cell in row[:2]:
                label = _norm_label(cell)
                if label and label in wanted:
                    hits.append((row, " | ".join(str(c or "") for c in row)))
                    break
    return hits


def _agrees(cell_value: float, fact_value: float, rel_tol: float) -> float | None:
    """The scale at which the cell equals the fact, or None.

    Tolerance is the looser of a relative band and half the printed unit: a filing
    that prints 1,234 백만원 for 1,234,567,890 원 is correct, not off by 567,890.
    """
    for scale in _SCALES:
        scaled = cell_value * scale
        tol = max(rel_tol * abs(fact_value), 0.5 * scale)
        if abs(scaled - fact_value) <= tol:
            return scale
    return None


def load_facts(conn, company_id: int, *, fiscal_year: int | None = None,
               exclude_sources: list[str] | None = None, limit: int = 200) -> list[dict]:
    """Financial facts collected for this company by *other* sources."""
    sql = ["SELECT account, account_local, fiscal_year, fiscal_period, value, source",
           "FROM financials WHERE company_id = ? AND value IS NOT NULL"]
    params: list = [company_id]
    if fiscal_year is not None:
        sql.append("AND fiscal_year = ?")
        params.append(fiscal_year)
    for source in exclude_sources or []:
        sql.append("AND source <> ?")
        params.append(source)
    sql.append("ORDER BY fiscal_year DESC, account LIMIT ?")
    params.append(limit)
    cur = conn.execute(" ".join(sql), params)
    return [dict(row) for row in cur.fetchall()]


def crosscheck(conn, company_id: int, tables, *, fiscal_year: int | None = None,
               exclude_sources: list[str] | None = None, rel_tol: float = 0.01,
               limit: int = 200) -> CrossCheckReport:
    """Score a document's tables against independently collected financial facts.

    ``exclude_sources`` should name the source the PDF itself came from — a filing
    checked against figures parsed out of that same filing proves nothing.
    """
    report = CrossCheckReport()
    facts = load_facts(conn, company_id, fiscal_year=fiscal_year,
                       exclude_sources=exclude_sources, limit=limit)
    if not facts:
        return report
    cells = _numeric_cells(tables)
    if not cells:
        return report

    for fact in facts:
        value = float(fact["value"])
        names = [fact.get("account_local"), fact.get("account")]
        check = FactCheck(
            account=fact.get("account_local") or fact.get("account") or "?",
            value=value, fiscal_year=fact.get("fiscal_year"),
            fiscal_period=fact.get("fiscal_period"),
            source=fact.get("source") or "?", status=UNMATCHED,
        )
        hit = next(((scale, evidence) for cell, evidence in cells
                    if (scale := _agrees(cell, value, rel_tol)) is not None), None)
        if hit:
            check.status, check.scale, check.evidence = CONFIRMED, hit[0], hit[1]
        else:
            labelled = _rows_labelled(tables, names)
            if labelled:
                # The document has this line and none of its numbers agree — that
                # is a real disagreement, not a figure the filing left out.
                check.status = CONFLICT
                check.evidence = labelled[0][1]
        report.checks.append(check)
    return report
