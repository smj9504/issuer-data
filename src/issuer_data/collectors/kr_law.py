"""Korean statutes via 국가법령정보 공동활용 OpenAPI (open.law.go.kr).

Standalone REST client — not a BaseCollector. Statutes are national reference
data (법령), not scoped to a market/symbol the way prices or financials are, so
this doesn't plug into the market/data_type collect pipeline. Callers query it
directly (see `cli.py`'s `law` subcommand) and may optionally attach a
company_id at storage time to link a statute to one issuer.

Requires a free OC value (ISSUER_LAW_API_OC) — the id-part of the email you
register with at open.law.go.kr, not a generated API key. Without it, the
client raises NotSupportedError.

Most `target`s must also be individually enabled for your OC under "OPEN API
신청" on the site — an OC missing a target gets an HTML error page back
instead of JSON; `_call` detects that and raises a NotSupportedError naming
the target, rather than a raw JSON-parse crash. There is no separate "법령연혁"
grant, though: it isn't its own line item in the API catalog, and the
seemingly-obvious target name for it ('lsHistory') errors as unapproved even
on a fully-approved OC — `search_history()` gets the same data (매 시행일자
버전, 과거/현행/시행예정 전부) from `target='eflaw'` instead, which the base
현행법령 grant already covers (verified live).

Field mappings for `law` (목록/본문), `elaw` (영문법령, list + detail incl. the
eflaw-based history reuse), and `oldAndNew` (신구법) were verified against live
responses. `thdCmp` (3단비교) consistently returned `{}` for every
statute/param combo tried against a real account, so its parsing reuses the
same shape as `oldAndNew` on a best-effort basis and may need adjusting once a
response with actual data is seen.
"""

from __future__ import annotations

import hashlib
import json

from ..config import Settings
from ..http.client import HttpClient
from ..logging import get_logger
from ..models import (
    LawApiRawItem,
    StatuteArticle,
    StatuteComparisonEntry,
    StatuteDetail,
    StatuteHistoryEntry,
    StatuteRecord,
    StatuteTranslation,
)
from .base import NotSupportedError

log = get_logger(__name__)

_HOST = "https://www.law.go.kr"
_SEARCH_URL = f"{_HOST}/DRF/lawSearch.do"
_SERVICE_URL = f"{_HOST}/DRF/lawService.do"

# Every category under the 국가법령정보 공동활용 OpenAPI umbrella that this client
# can reach generically via search_raw()/fetch_raw() (see module docstring — each
# target must be separately enabled for your OC). 'law'/'elaw'/'lsHistory'/
# 'oldAndNew'/'thdCmp' have dedicated, typed methods above instead.
RAW_TARGETS: dict[str, str] = {
    "admrul": "행정규칙",
    "ordin": "자치법규",
    "prec": "판례",
    "expc": "법령해석례",
    "detc": "헌재결정례",
    "trty": "조약",
    "licbyl": "별표·서식",
    "lsStmd": "법령체계도",
    "lsAbrv": "법령명 약칭",
}


class LawCollector:
    source = "law_go_kr"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.oc = settings.law_api_oc
        if not self.oc:
            raise NotSupportedError(
                "Korean statute lookup requires ISSUER_LAW_API_OC "
                "(free registration at https://open.law.go.kr)"
            )
        self.client = HttpClient(rate_limit=settings.law_rate_limit)

    # ------------------------------------------------------------------- http
    def _call(self, url: str, target: str, **params) -> dict:
        params = {k: v for k, v in params.items() if v is not None}
        params.update(OC=self.oc, target=target, type="JSON")
        resp = self.client.get(url, params=params)
        try:
            data = resp.json()
        except ValueError:
            text = resp.text
            if "미신청" in text:
                raise NotSupportedError(
                    f"OC '{self.oc}' isn't approved for target='{target}' yet — apply for it "
                    "at https://open.law.go.kr (OPEN API 신청 > 활용신청 > 해당 법령종류 체크)"
                ) from None
            log.warning("law.go.kr returned non-JSON for target=%s (%d bytes)", target, len(text))
            return {}
        return data if isinstance(data, dict) else {}

    def _search(self, target: str, **params) -> dict:
        return self._call(_SEARCH_URL, target, **params)

    def _service(self, target: str, **params) -> dict:
        return self._call(_SERVICE_URL, target, **params)

    # ------------------------------------------------------ 현행법령 목록/본문
    def search_statutes(
        self, query: str, *, search: int = 1, display: int = 20, page: int = 1
    ) -> list[StatuteRecord]:
        """목록조회: statutes whose name (search=1) or body (search=2) matches `query`."""
        payload = self._search("law", query=query, search=search, display=display, page=page)
        root = _unwrap(payload)
        out: list[StatuteRecord] = []
        for it in _find_list(root, "law", "Law"):
            rec = _parse_statute_item(it)
            if rec:
                out.append(rec)
        return out

    def get_statute_detail(
        self, *, law_id: str | None = None, mst: str | None = None
    ) -> StatuteDetail:
        """본문조회: full text of one statute, by 법령ID or 법령일련번호(MST)."""
        if not law_id and not mst:
            raise ValueError("get_statute_detail requires law_id or mst")
        payload = self._service("law", MST=mst, ID=law_id)
        root = _unwrap(payload)
        info = root.get("기본정보") if isinstance(root.get("기본정보"), dict) else root
        return StatuteDetail(
            law_id=str(law_id or _pick(info, "법령ID") or ""),
            law_serial_no=str(mst or _pick(info, "법령일련번호") or ""),
            name=str(_pick(info, "법령명_한글", "법령명한글", "법령명") or ""),
            department=_text(_pick(info, "소관부처", "소관부처명")),
            promulgation_date=_pick(info, "공포일자"),
            enforcement_date=_pick(info, "시행일자"),
            articles=_extract_articles(root),
            raw_text=json.dumps(payload, ensure_ascii=False),
        )

    # ------------------------------------------------------------- 법령 연혁
    def search_history(
        self,
        query: str,
        *,
        law_id: str | None = None,
        display: int = 20,
        page: int = 1,
    ) -> list[StatuteHistoryEntry]:
        """연혁: every enforcement-date version (past/current/upcoming) of statutes
        matching `query` — via target='eflaw' (시행일 법령), not a dedicated 'history'
        target. There is no separately-gated "법령연혁" API: the official target name
        for it (tried as 'lsHistory') consistently errored as unapproved even with a
        fully-approved OC, and doesn't appear as its own line item in the account's
        API catalog either — 'eflaw' (already covered by 대한민국현행법령 access) is
        confirmed live to return the full 현행/연혁/시행예정 version history instead,
        each with its own MST (법령일련번호) usable with get_statute_detail(mst=...).
        `law_id` narrows results client-side when `query` matches more than one law.

        Unlike target='law', 'eflaw' only matches a statute's exact registered name —
        not an abbreviation like "자본시장법" — so `query` is first resolved to that
        exact name via a `law` search (confirmed live: searching 'eflaw' directly with
        an abbreviation returns totalCnt=0).
        """
        exact_name = self._resolve_exact_name(query, law_id)
        payload = self._search("eflaw", query=exact_name, display=display, page=page)
        root = _unwrap(payload)
        out: list[StatuteHistoryEntry] = []
        for it in _find_list(root, "law", "Law"):
            lid = str(_pick(it, "법령ID") or "")
            mst = str(_pick(it, "법령일련번호") or "")
            if not lid or not mst:
                log.warning("law history item missing law_id/mst; skipped (keys=%s)", list(it))
                continue
            if law_id and lid != str(law_id):
                continue
            out.append(
                StatuteHistoryEntry(
                    law_id=lid,
                    law_serial_no=mst,
                    name=str(_pick(it, "법령명한글", "법령명") or ""),
                    enforcement_date=_pick(it, "시행일자"),
                    promulgation_date=_pick(it, "공포일자"),
                    revision_type=_pick(it, "제개정구분명"),
                    status=_pick(it, "현행연혁코드"),
                )
            )
        return out

    def _resolve_exact_name(self, query: str, law_id: str | None) -> str:
        candidates = self.search_statutes(query, display=10)
        if law_id:
            filtered = [c for c in candidates if c.law_id == str(law_id)]
            candidates = filtered or candidates
        return candidates[0].name if candidates else query

    # ------------------------------------------------------------- 영문법령
    def search_english(
        self, query: str, *, display: int = 20, page: int = 1
    ) -> list[StatuteRecord]:
        """영문법령 목록조회: statutes that have an official English translation."""
        payload = self._search("elaw", query=query, display=display, page=page)
        root = _unwrap(payload)
        out: list[StatuteRecord] = []
        for it in _find_list(root, "law", "Law"):
            rec = _parse_statute_item(it)
            if rec:
                out.append(rec)
        return out

    def get_english_detail(
        self, *, law_id: str | None = None, mst: str | None = None
    ) -> StatuteTranslation:
        """영문법령 본문조회: the English translation body (InfSection + JoSection.Jo)."""
        if not law_id and not mst:
            raise ValueError("get_english_detail requires law_id or mst")
        payload = self._service("elaw", MST=mst, ID=law_id)
        root = _unwrap(payload)
        info = root.get("InfSection") if isinstance(root.get("InfSection"), dict) else {}
        jo = root.get("JoSection")
        articles = _as_list(jo.get("Jo")) if isinstance(jo, dict) else []
        content_en = "\n".join(
            str(a["joCts"]) for a in articles if a.get("joCts")
        ) or None
        return StatuteTranslation(
            law_id=str(law_id or _pick(info, "lsId") or ""),
            law_serial_no=str(mst) if mst else None,
            name_en=str(_pick(info, "lsNmEng") or ""),
            content_en=content_en,
        )

    # ---------------------------------------------------- 신구법 / 3단 비교
    def get_old_and_new(self, *, mst: str) -> list[StatuteComparisonEntry]:
        """신구법 비교: old-vs-new article text for the revision identified by `mst`."""
        return self._comparison("oldAndNew", mst)

    def get_three_way(self, *, mst: str) -> list[StatuteComparisonEntry]:
        """3단 비교: 법률/시행령/시행규칙 aligned side-by-side for the revision `mst`.

        Best-effort: unlike `oldAndNew`, no live response with data was available
        to confirm this target's exact JSON shape (see module docstring).
        """
        return self._comparison("thdCmp", mst)

    def _comparison(self, target: str, mst: str) -> list[StatuteComparisonEntry]:
        payload = self._service(target, MST=mst)
        root = _unwrap(payload)
        new_info = root.get("신조문_기본정보") or {}
        old_info = root.get("구조문_기본정보") or {}
        law_id = str(_pick(new_info, "법령ID") or _pick(old_info, "법령ID") or mst)
        new_items = _as_list((root.get("신조문목록") or {}).get("조문"))
        old_items = _as_list((root.get("구조문목록") or {}).get("조문"))
        new_by_no = {str(it.get("no")): it.get("content") for it in new_items}
        old_by_no = {str(it.get("no")): it.get("content") for it in old_items}
        out: list[StatuteComparisonEntry] = []
        for no in sorted(set(new_by_no) | set(old_by_no), key=lambda x: (len(x), x)):
            old_text, new_text = old_by_no.get(no), new_by_no.get(no)
            if old_text is None and new_text is None:
                continue
            out.append(
                StatuteComparisonEntry(
                    law_id=law_id,
                    article_no=no,
                    old_text=str(old_text) if old_text is not None else None,
                    new_text=str(new_text) if new_text is not None else None,
                )
            )
        return out

    # --------------------------------------------------- generic (any target)
    def search_raw(
        self, target: str, query: str | None = None, *, display: int = 20, page: int = 1,
        **extra,
    ) -> list[LawApiRawItem]:
        """목록조회 for any target not given a dedicated method above (see RAW_TARGETS) —
        each item kept as raw JSON since these categories' exact fields aren't modeled.
        """
        payload = self._search(target, query=query, display=display, page=page, **extra)
        root = _unwrap(payload)
        return [_raw_item(target, it) for it in _find_list(root)]

    def fetch_raw(
        self, target: str, *, item_id: str | None = None, mst: str | None = None, **extra
    ) -> LawApiRawItem:
        """본문조회 for any target not given a dedicated method above (see RAW_TARGETS).

        Most non-법령 categories (판례/행정규칙/자치법규/...) key their body lookup off
        `item_id` (their own serial number, sent as `ID=`) — `mst` (`MST=`) only applies
        to 법령/영문법령-style targets, which have their own typed methods above anyway.
        """
        if not item_id and not mst and not extra:
            raise ValueError("fetch_raw requires item_id, mst, or another id kwarg")
        payload = self._service(target, ID=item_id, MST=mst, **extra)
        root = _unwrap(payload)
        return _raw_item(target, root, key_hint=item_id or mst)


# --------------------------------------------------------------------- parsing
def _unwrap(payload: dict) -> dict:
    """Peel off the single root key the API wraps every response in."""
    if not isinstance(payload, dict):
        return {}
    if len(payload) == 1:
        ((_, only_val),) = payload.items()
        if isinstance(only_val, dict):
            return only_val
    return payload


def _as_list(value) -> list[dict]:
    """A result key holds a dict for one match, a list for many — normalize to a list."""
    if value is None:
        return []
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _find_list(d: dict, *candidate_keys: str) -> list[dict]:
    for k in candidate_keys:
        if k in d:
            return _as_list(d[k])
    for v in d.values():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return v
    # display=1 (or a genuinely single match) comes back as one bare dict rather
    # than a one-element list — accept a dict value with enough keys to be an
    # item rather than a metadata field like {"page": "1"}.
    for v in d.values():
        if isinstance(v, dict) and len(v) >= 4:
            return [v]
    return []


def _pick(d: dict, *keys: str, default=None):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default


def _pick_contains(d: dict, substr: str):
    for k, v in d.items():
        if substr in str(k) and v not in (None, ""):
            return v
    return None


def _text(value):
    """Some fields are plain strings; others are {'content': ..., '...코드': ...} dicts."""
    if isinstance(value, dict):
        return value.get("content")
    return value


def _parse_statute_item(it: dict) -> StatuteRecord | None:
    law_id = str(_pick(it, "법령ID") or "")
    mst = str(_pick(it, "법령일련번호") or "")
    if not law_id or not mst:
        log.warning("law search item missing law_id/mst; skipped (keys=%s)", list(it))
        return None
    detail_url = _pick(it, "법령상세링크")
    if detail_url and str(detail_url).startswith("/"):
        detail_url = _HOST + detail_url
    return StatuteRecord(
        law_id=law_id,
        law_serial_no=mst,
        name=str(_pick(it, "법령명한글", "법령명") or ""),
        name_abbrev=_pick(it, "법령약칭명"),
        law_type=_pick(it, "법령구분명"),
        department=_pick(it, "소관부처명"),
        promulgation_date=_pick(it, "공포일자"),
        promulgation_no=_pick(it, "공포번호"),
        enforcement_date=_pick(it, "시행일자"),
        revision_type=_pick(it, "제개정구분명"),
        detail_url=detail_url,
    )


def _collect_texts(node: dict, parts: list[str]) -> None:
    """Recursively gather 항내용/호내용/목내용 in document order (항 -> 호 -> 목)."""
    for key in ("항내용", "호내용", "목내용"):
        v = node.get(key)
        if v:
            parts.append(str(v).strip())
    for sub_key in ("항", "호", "목"):
        for child in _as_list(node.get(sub_key)):
            _collect_texts(child, parts)


_RAW_TITLE_KEYS = (
    "사건명", "법령명한글", "법령명", "행정규칙명", "자치법규명", "조약명",
    "별표명", "서식명", "제목",
)


def _raw_title(it: dict) -> str | None:
    for k in _RAW_TITLE_KEYS:
        v = it.get(k)
        if v:
            return str(v)
    # fallback: longest string among any other '...명'/'...제목' field (title-ish
    # fields tend to be far longer than short labels like a department name)
    candidates = [
        str(v) for k, v in it.items()
        if isinstance(v, str) and v and k.endswith(("명", "제목"))
    ]
    return max(candidates, key=len) if candidates else None


def _raw_item(target: str, it: dict, *, key_hint: str | None = None) -> LawApiRawItem:
    key = (
        key_hint
        or _pick_contains(it, "일련번호")
        or _pick_contains(it, "ID")
        or _pick_contains(it, "번호")
    )
    if not key:
        key = hashlib.sha1(
            json.dumps(it, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
    title = _raw_title(it)
    return LawApiRawItem(
        target=target,
        item_key=str(key),
        title=str(title) if title else None,
        payload_json=json.dumps(it, ensure_ascii=False),
    )


def _extract_articles(root: dict) -> list[StatuteArticle]:
    """조문(article) extraction from 조문.조문단위 — a flat list mixing 편/장/절
    headers (조문여부='전문', skipped) and real articles (조문여부='조문'), whose
    body text lives either directly in 조문내용 or nested under 항/호/목.
    """
    jo = root.get("조문")
    units = _as_list(jo.get("조문단위")) if isinstance(jo, dict) else _as_list(jo)
    out: list[StatuteArticle] = []
    for u in units:
        if u.get("조문여부") != "조문":
            continue
        no = u.get("조문번호")
        parts: list[str] = []
        head = u.get("조문내용")
        if head:
            parts.append(str(head).strip())
        _collect_texts(u, parts)
        content = "\n".join(p for p in parts if p)
        if not content and not no:
            continue
        title = u.get("조문제목")
        out.append(
            StatuteArticle(
                article_no=str(no or ""),
                article_title=str(title) if title else None,
                content=content,
            )
        )
    return out
