"""Field extraction with provenance — the answerable half of "did this parse?".

"Was this PDF parsed correctly?" has no general answer: the layouts, languages,
charts and scans differ without limit. "Did we get the fields we came for, and
where does each one come from?" is bounded and checkable, so that is the question
this module answers.

A spec is data, not code — a label list per field — so a new document family costs
a JSON entry, not a parser. Labels are matched against table row headers first
(disclosure documents put their key/value pairs in two-column tables) and against
the narrative second.

Every value carries its provenance: which table, which page, and the exact line
it came from. That is what turns a review from "read this 80-page PDF" into
"check this one line", and it is why a REVIEW verdict is affordable enough to act
on rather than something the team learns to ignore.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .logging import get_logger
from .pdf_validate import parse_number

log = get_logger(__name__)


@dataclass
class FieldSpec:
    """One field to look for. ``labels`` are alternative names, any language."""

    name: str
    labels: list[str] = field(default_factory=list)
    kind: str = "text"          # text | number | date
    required: bool = False


@dataclass
class FieldValue:
    name: str
    value: str
    source: str                  # 'table' | 'text'
    page: int | None = None
    table_seq: int | None = None
    evidence: str = ""           # the row/line it was read from
    label: str = ""              # which label matched

    def to_dict(self) -> dict:
        return {"value": self.value, "source": self.source, "page": self.page,
                "table_seq": self.table_seq, "label": self.label,
                "evidence": self.evidence[:200]}


# Korean disclosures label the same concept several ways, and the same document
# family appears in three markets, so labels are listed per language rather than
# per template.
DEFAULT_SPECS: list[FieldSpec] = [
    FieldSpec("issuer", ["발행회사", "발행인", "회사명", "법인명", "상호",
                         "issuer", "company name", "name of issuer", "發行人", "公司名稱"]),
    FieldSpec("security_type", ["증권의 종류", "사채의 종류", "종목", "security type",
                                "type of security", "class of securities", "證券種類"]),
    FieldSpec("amount", ["발행금액", "모집총액", "발행총액", "권면총액", "모집가액",
                         "aggregate amount", "offering amount", "principal amount",
                         "發行金額", "發行總額"], kind="number"),
    FieldSpec("currency", ["통화", "표시통화", "currency", "貨幣"]),
    FieldSpec("issue_date", ["발행일", "납입일", "발행예정일", "issue date",
                             "closing date", "發行日"], kind="date"),
    FieldSpec("maturity", ["만기일", "상환기일", "만기", "maturity date", "maturity",
                           "到期日", "償還日"], kind="date"),
    FieldSpec("coupon", ["표면이자율", "이자율", "금리", "coupon", "interest rate",
                         "利率", "票面利率"], kind="number"),
    FieldSpec("isin", ["isin", "국제증권식별번호", "종목코드"]),
]


def load_specs(path) -> list[FieldSpec]:
    """Load a field schema from JSON: [{"name","labels","kind","required"}, ...]."""
    spec_path = Path(path)
    if not spec_path.is_file():
        raise FileNotFoundError(f"field schema not found: {spec_path}")
    raw = json.loads(spec_path.read_text(encoding="utf-8"))
    return [FieldSpec(name=item["name"], labels=list(item.get("labels", [])),
                      kind=item.get("kind", "text"),
                      required=bool(item.get("required", False)))
            for item in raw]


# ------------------------------------------------------------------- normalizing
def _norm(text) -> str:
    """Collapse whitespace and drop separators, so '증권의 종류'=='증권의종류'."""
    return re.sub(r"[\s:：·.()\[\]]+", "", str(text or "")).lower()


_DATEISH = re.compile(r"\d{4}\s*[-./년]\s*\d{1,2}\s*[-./월]?\s*\d{0,2}")


def _acceptable(value: str, kind: str) -> bool:
    """A value has to look like what the spec asked for, or it is not a hit."""
    if not value or not value.strip():
        return False
    if kind == "number":
        return parse_number(value) is not None
    if kind == "date":
        return bool(_DATEISH.search(value))
    return True


def _label_hit(cell: str, labels: list[str]) -> str | None:
    """The label this cell *is* (exact after normalizing), else None.

    Exact rather than substring: '이자율' is a substring of '연체이자율', and a
    label that merely appears inside another label reads the wrong row.
    """
    norm = _norm(cell)
    for label in labels:
        if norm == _norm(label):
            return label
    return None


# --------------------------------------------------------------- the extractors
def _from_tables(spec: FieldSpec, tables) -> FieldValue | None:
    """Key/value tables: a row whose first cells are the label, value alongside."""
    for seq, table in enumerate(tables or []):
        for row in table.rows:
            for idx, cell in enumerate(row[:2]):     # labels live at the row head
                label = _label_hit(cell, spec.labels)
                if not label:
                    continue
                for value in row[idx + 1:]:
                    text = str(value or "").strip()
                    if _acceptable(text, spec.kind):
                        return FieldValue(
                            name=spec.name, value=text, source="table",
                            page=table.page_start, table_seq=seq,
                            evidence=" | ".join(str(c or "") for c in row)[:200],
                            label=label,
                        )
    return None


def _from_text(spec: FieldSpec, text: str, page_text: dict | None) -> FieldValue | None:
    """Narrative: 'label : value' on one line, the commonest cover-page shape."""
    for label in spec.labels:
        pattern = re.compile(
            r"^\s*" + r"\s*".join(re.escape(ch) for ch in label.replace(" ", ""))
            + r"\s*[:：]\s*(?P<value>.+?)\s*$",
            re.IGNORECASE | re.MULTILINE,
        )
        for source_text, page in _sources(text, page_text):
            match = pattern.search(source_text)
            if match and _acceptable(match.group("value"), spec.kind):
                return FieldValue(
                    name=spec.name, value=match.group("value").strip(),
                    source="text", page=page,
                    evidence=match.group(0).strip()[:200], label=label,
                )
    return None


def _sources(text: str, page_text: dict | None):
    """Per-page text when we have it (so a hit carries a page), else the whole."""
    if page_text:
        return [(t, pg) for pg, t in sorted(page_text.items())]
    return [(text or "", None)]


def extract_fields(text: str, tables, specs: list[FieldSpec],
                   page_text: dict | None = None) -> dict[str, FieldValue]:
    """Find each spec'd field, tables first, narrative second.

    Missing fields are simply absent from the result — the caller decides whether
    that is fatal (``FieldSpec.required``), because the same document is a
    complete record for one task and irrelevant for another.
    """
    found: dict[str, FieldValue] = {}
    for spec in specs or []:
        hit = _from_tables(spec, tables) or _from_text(spec, text, page_text)
        if hit:
            found[spec.name] = hit
    return found
