"""Download original filing documents and extract their body text.

Formats: PDF (pdfplumber), HTML/XML (BeautifulSoup+lxml), 한글 HWP 5.x and HWPX
(hwp_extract), archives (recursively, by content), plain text.

The format is decided by sniffing the bytes, never by the URL or the declared
Content-Type: DART serves a ZIP from a URL ending in `.xml` under a Content-Type
of `application/x-msdownload`, so every name-based guess gets it wrong and the
whole body of every Korean filing is silently lost. The same rule applies inside
archives, whose members are dispatched by content too.

A document that downloads but yields no text is logged and counted, not stored
silently: an unknown format has to announce itself on the first run rather than
sit in the database as text_chars=0. Scanned/image-only PDFs are the known-benign
case of this (OCR is opt-in via ocr_enabled).
"""

from __future__ import annotations

import gzip
import io
import json
import sqlite3
import warnings
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlparse

# BeautifulSoup emits noisy warnings when markup looks like XML/URL; ignore them.
warnings.filterwarnings("ignore", module="bs4")

from .config import Settings
from .http.client import HttpClient
from .logging import get_logger
from .models import Filing, FilingDocument
from .storage.repository import Repository
from .utils.dates import today_iso

log = get_logger(__name__)

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)
_MAX_TEXT = 5_000_000  # cap stored text at ~5M chars
_MAX_ARCHIVE_DEPTH = 3  # archives inside archives; a guard against zip bombs


def _client(settings: Settings) -> HttpClient:
    # A single client; UA is chosen per-request by host in _headers_for.
    return HttpClient(rate_limit=settings.edgar_rate_limit)


def _headers_for(url: str, settings: Settings) -> dict:
    host = urlparse(url).netloc.lower()
    if "sec.gov" in host:
        return {"User-Agent": settings.sec_user_agent}
    if "hkexnews" in host:
        return {"User-Agent": _BROWSER_UA, "Referer": "https://www1.hkexnews.hk/"}
    return {"User-Agent": _BROWSER_UA}


# Leading bytes that identify a format regardless of what the URL or the server
# claims. OLE and ZIP are containers, so they need a second look to tell an HWP
# from a legacy .doc, or an HWPX from an ordinary archive.
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "pdf"),
    (b"PK\x03\x04", "zip"),
    (b"PK\x05\x06", "zip"),   # empty archive
    (b"PK\x07\x08", "zip"),   # spanned archive
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "ole"),
    (b"\x1f\x8b", "gzip"),
    (b"%!PS", "ps"),
)


def sniff_format(content: bytes) -> str | None:
    """Identify `content` by its leading bytes, or None if nothing matches."""
    from .hwp_extract import is_hwp5, is_hwpx

    for magic, fmt in _MAGIC:
        if not content.startswith(magic):
            continue
        if fmt == "ole":
            return "hwp" if is_hwp5(content) else "ole"
        if fmt == "zip":
            return "hwpx" if is_hwpx(content) else "zip"
        return fmt
    head = content[:1024].lstrip()[:64].lower()
    if head.startswith(b"<?xml"):
        return "xml"
    if head.startswith((b"<!doctype html", b"<html")):
        return "html"
    if head.startswith(b"<"):
        return "html"  # a bare markup fragment; the HTML parser reads both
    return None


def _format_from_name(name: str) -> str | None:
    """Fall back to the file extension when the bytes are not self-identifying."""
    lower = name.lower()
    for suffix, fmt in (
        (".pdf", "pdf"), (".zip", "zip"), (".xml", "xml"), (".htm", "html"),
        (".html", "html"), (".txt", "txt"), (".hwp", "hwp"), (".hwpx", "hwpx"),
    ):
        if lower.endswith(suffix):
            return fmt
    return None


def _format_from(url: str, content_type: str | None, content: bytes = b"") -> str:
    sniffed = sniff_format(content)
    if sniffed:
        return sniffed
    path = urlparse(url).path.lower()
    ct = (content_type or "").lower()
    if path.endswith(".pdf") or "pdf" in ct:
        return "pdf"
    if path.endswith(".zip") or "zip" in ct:
        return "zip"
    if path.endswith((".xml",)) or "xml" in ct:
        return "xml"
    if path.endswith((".htm", ".html")) or "html" in ct:
        return "html"
    if path.endswith(".txt") or "text/plain" in ct:
        return "txt"
    if path.endswith(".hwp"):
        return "hwp"
    if path.endswith(".hwpx"):
        return "hwpx"
    return "html"  # EDGAR primary docs are usually HTML


def _filename_from(url: str, doc_seq: int, fmt: str) -> str:
    name = Path(unquote(urlparse(url).path)).name
    if not name or "." not in name:
        return f"doc{doc_seq}.{fmt}"
    # Keep the on-disk extension honest: DART's document.xml is really a zip, and
    # saving it as .xml makes every later reader repeat the original mistake.
    if _format_from_name(name) != fmt:
        name = f"{Path(name).stem}.{fmt}"
    return name


# ------------------------------------------------------------------ extraction
def extract_text(content: bytes, fmt: str, depth: int = 0) -> str | None:
    from .hwp_extract import hwp5_text, hwpx_text

    try:
        if fmt == "pdf":
            return _pdf_text(content)
        if fmt in ("zip", "gzip"):
            return _archive_text(content, fmt, depth)
        if fmt == "hwp":
            return hwp5_text(content)
        if fmt == "hwpx":
            return hwpx_text(content)
        if fmt in ("html", "xml"):
            return markup_text(content, fmt)
        if fmt == "txt":
            return content.decode("utf-8", errors="replace")
        log.warning("no text extractor for format %r", fmt)
    except Exception as exc:  # noqa: BLE001
        log.warning("text extraction (%s) failed: %s", fmt, exc)
    return None


def _pdf_page_texts(content: bytes) -> list[str]:
    """Text layer of each page, "" where the page has none."""
    import pdfplumber

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        return [(page.extract_text() or "").strip() for page in pdf.pages]


def _pdf_text(content: bytes) -> str | None:
    pages = [t for t in _pdf_page_texts(content) if t]
    return "\n".join(pages).strip() or None  # None => likely scanned/image PDF


def markup_text(content: bytes, fmt: str) -> str | None:
    from bs4 import BeautifulSoup

    parser = "lxml-xml" if fmt == "xml" else "lxml"
    soup = BeautifulSoup(content, parser)
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    return text or None


def _archive_text(content: bytes, fmt: str, depth: int = 0) -> str | None:
    """Text of an archive's members, each dispatched by its own leading bytes.

    Members are identified the same way the outer document is — by content, with
    the name only as a fallback — so a zip of PDFs, a mislabelled member or a
    nested archive all work without adding a case here.
    """
    if depth >= _MAX_ARCHIVE_DEPTH:
        log.warning("archive nested deeper than %d levels; not descending further",
                    _MAX_ARCHIVE_DEPTH)
        return None
    if fmt == "gzip":
        return _member_text(gzip.decompress(content), "", depth)

    parts: list[str] = []
    total = 0
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            text = _member_text(zf.read(info), info.filename, depth)
            if not text:
                continue
            parts.append(text)
            total += len(text)
            if total >= _MAX_TEXT:  # stop reading a pathologically large archive
                break
    text = "\n".join(parts).strip()
    return text or None


def _member_text(data: bytes, name: str, depth: int) -> str | None:
    fmt = sniff_format(data) or _format_from_name(name)
    if fmt is None:
        return None  # an image, a font, a signature blob: nothing to read
    return extract_text(data, fmt, depth + 1)


# ------------------------------------------------------------------ downloading
def _download_one(
    client: HttpClient, settings: Settings, company_id: int, filing_id: str,
    source: str, doc_seq: int, url: str, market: str, symbol: str,
    extract_tables: bool = False,
) -> tuple[FilingDocument | None, list]:
    try:
        resp = client.get(url, headers=_headers_for(url, settings))
    except Exception as exc:  # noqa: BLE001
        log.warning("download failed %s: %s", url, exc)
        return None, []
    content = resp.content
    fmt = _format_from(url, resp.headers.get("Content-Type"), content)
    filename = _filename_from(url, doc_seq, fmt)
    dest_dir = Path(settings.docs_dir) / market / symbol / filing_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename
    dest.write_bytes(content)
    tables: list = []
    text: str | None = None
    if extract_tables and fmt == "pdf":
        # Structured pass: cross-page-stitched tables + reflowed narrative, with
        # optional (config-gated) escalation of the low-confidence tail.
        try:
            from .pdf_escalate import build_escalator
            from .pdf_extract import extract_structured

            escalator = build_escalator(settings)
            doc = extract_structured(
                content, escalator=escalator,
                threshold=settings.pdf_confidence_threshold,
                cost_per_page=settings.escalation_cost_per_page,
                ml_engine=(settings.pdf_ml_engine if settings.pdf_ml_tables else None),
                ml_dpi=settings.pdf_ml_dpi,
            )
            tables = doc.tables
            text = doc.text or None
            if doc.escalated_count:
                log.info("escalated %d low-confidence table(s) for %s (est. cost $%.3f)",
                         doc.escalated_count, url, doc.est_cost)
        except Exception as exc:  # noqa: BLE001
            log.warning("structured PDF extract failed %s: %s", url, exc)
    if text is None:
        text = extract_text(content, fmt)
    if fmt == "pdf" and getattr(settings, "ocr_enabled", True) and not tables:
        # Pages with no text layer, whether that is the whole document or the
        # three chart pages inside an otherwise born-digital filing. Gating OCR
        # on the *document* being empty was how a chart page's numbers vanished
        # while the surrounding prose still referred to them.
        text = _ocr_pages_without_text(content, text, settings, url)
    if text and len(text) > _MAX_TEXT:
        text = text[:_MAX_TEXT]
    if not text:
        # Downloaded fine but nothing came out. Say so loudly: this is how a
        # format we do not really handle stays invisible for months.
        log.warning("no text extracted: %s (format=%s, %d bytes)", url, fmt, len(content))
    return FilingDocument(
        company_id=company_id,
        filing_id=filing_id,
        source=source,
        doc_seq=doc_seq,
        doc_url=url,
        local_path=str(dest),
        doc_format=fmt,
        file_size=len(content),
        text_content=text,
        text_chars=len(text) if text else 0,
        downloaded_at=today_iso(),
    ), tables


def _ocr_pages_without_text(content: bytes, text: str | None,
                            settings: Settings, url: str) -> str | None:
    """Fill in pages that carry no text layer, keeping the pages that do."""
    from .pdf_ocr import ocr_pages, ocr_ready

    try:
        pages = _pdf_page_texts(content)
    except Exception as exc:  # noqa: BLE001
        log.debug("could not read per-page text for %s: %s", url, exc)
        return text
    missing = {i for i, t in enumerate(pages) if not t}
    if not missing:
        return text
    if not ocr_ready():
        log.warning("%d of %d page(s) have no text layer and OCR is unavailable: %s",
                    len(missing), len(pages), url)
        return text
    try:
        recovered = ocr_pages(content, settings.ocr_languages, settings.ocr_dpi,
                              settings.ocr_max_pages, only=missing)
    except Exception as exc:  # noqa: BLE001
        log.warning("OCR failed %s: %s", url, exc)
        return text
    for index, page_text in recovered.items():
        pages[index] = page_text
    still_empty = len(missing) - len(recovered)
    if recovered:
        log.info("OCR recovered %d of %d text-less page(s) for %s",
                 len(recovered), len(missing), url)
    if still_empty:
        log.warning("%d page(s) yielded no text even after OCR: %s", still_empty, url)
    return "\n".join(t for t in pages if t).strip() or None


def download_filing_documents(
    repo: Repository, settings: Settings, company_id: int, filings: list[Filing],
    extract_tables: bool = False,
) -> int:
    """Download originals + extract text for a list of freshly collected filings."""
    client = _client(settings)
    sym = repo.symbol_for_company(company_id)
    market, symbol = (sym or ("NA", str(company_id)))
    n = 0
    empty = 0
    for fl in filings:
        urls = fl.doc_urls or ([fl.url] if fl.url else [])
        for seq, url in enumerate(urls):
            if not url:
                continue
            doc, tables = _download_one(client, settings, company_id, fl.filing_id, fl.source,
                                        seq, url, market, symbol, extract_tables)
            if doc is not None:
                repo.upsert_filing_document(doc)
                if tables:
                    repo.upsert_filing_tables(company_id, fl.filing_id, fl.source, seq, tables)
                n += 1
                empty += not doc.text_chars
        repo.commit()
    log.info("documents: downloaded %d files for company %s (%s:%s)%s",
             n, company_id, market, symbol,
             f" — {empty} with NO text extracted" if empty else "")
    return n


def backfill_documents(
    conn: sqlite3.Connection,
    settings: Settings,
    symbols: list[str] | None,
    markets: list[str] | None,
    limit: int | None,
    extract_tables: bool = False,
) -> int:
    """Download originals for stored filings that have no documents yet."""
    repo = Repository(conn)
    company_ids: list[int] | None = None
    if symbols:
        company_ids = []
        for token in symbols:
            m, s = (token.split(":", 1) if ":" in token else ("US", token))
            cid = repo.get_company_id_for_symbol(m.upper(), s)
            if cid is not None:
                company_ids.append(cid)

    rows = _pending_rows(conn, company_ids, markets, limit)
    client = _client(settings)
    n = 0
    empty = 0
    for row in rows:
        cid = row["company_id"]
        filing_id, source = row["filing_id"], row["source"]
        sym = repo.symbol_for_company(cid)
        market, symbol = (sym or ("NA", str(cid)))
        # Prefer the persisted document list (exhibits included); fall back to the
        # single primary url. Skip doc_seqs already downloaded so re-runs are incremental.
        urls = _parse_doc_urls(row["doc_urls"]) or ([row["url"]] if row["url"] else [])
        done = _downloaded_seqs(conn, cid, filing_id, source)
        for seq, url in enumerate(urls):
            if not url or seq in done:
                continue
            doc, tables = _download_one(client, settings, cid, filing_id, source,
                                        seq, url, market, symbol, extract_tables)
            if doc is not None:
                repo.upsert_filing_document(doc)
                if tables:
                    repo.upsert_filing_tables(cid, filing_id, source, seq, tables)
                n += 1
                empty += not doc.text_chars
                repo.commit()
    log.info("backfill: downloaded %d files%s", n,
             f" — {empty} with NO text extracted" if empty else "")
    return n


def _parse_doc_urls(raw) -> list[str]:
    if not raw:
        return []
    try:
        val = json.loads(raw)
        return [u for u in val if u] if isinstance(val, list) else []
    except (ValueError, TypeError):
        return []


def _downloaded_seqs(conn, company_id, filing_id, source) -> set[int]:
    rows = conn.execute(
        "SELECT doc_seq FROM filing_documents WHERE company_id=? AND filing_id=? AND source=?",
        (company_id, filing_id, source),
    ).fetchall()
    return {r["doc_seq"] for r in rows}


def _pending_rows(conn, company_ids, markets, limit):
    # A filing is pending while it has fewer downloaded documents than its
    # persisted doc_urls list (>= 1 for the primary), so exhibits are picked up on
    # a later backfill even after doc_seq=0 was fetched.
    sql = (
        "SELECT f.company_id, f.filing_id, f.source, f.url, f.doc_urls FROM filings f "
        "JOIN securities s ON s.company_id=f.company_id "
        "WHERE (SELECT COUNT(*) FROM filing_documents d WHERE d.company_id=f.company_id "
        "  AND d.filing_id=f.filing_id AND d.source=f.source) "
        "  < MAX(1, COALESCE(json_array_length(f.doc_urls), 1))"
    )
    params: list = []
    if company_ids:
        sql += " AND f.company_id IN (%s)" % ",".join("?" * len(company_ids))
        params += company_ids
    if markets:
        sql += " AND s.market IN (%s)" % ",".join("?" * len(markets))
        params += markets
    sql += " GROUP BY f.company_id, f.filing_id, f.source"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, tuple(params)).fetchall()
