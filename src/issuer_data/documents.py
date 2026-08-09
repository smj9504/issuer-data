"""Download original filing documents and extract their body text.

Formats: PDF (pdfplumber), HTML/XML (BeautifulSoup+lxml), DART ZIP-of-XML
(unzip then parse), plain text. Scanned/image-only PDFs yield no text — the file
is still recorded with text_content=NULL (OCR is a possible later add-on).
"""

from __future__ import annotations

import io
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


def _format_from(url: str, content_type: str | None) -> str:
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
    return "html"  # EDGAR primary docs are usually HTML


def _filename_from(url: str, doc_seq: int, fmt: str) -> str:
    name = Path(unquote(urlparse(url).path)).name
    if not name or "." not in name:
        name = f"doc{doc_seq}.{fmt}"
    return name


# ------------------------------------------------------------------ extraction
def extract_text(content: bytes, fmt: str) -> str | None:
    try:
        if fmt == "pdf":
            return _pdf_text(content)
        if fmt == "zip":
            return _zip_text(content)
        if fmt in ("html", "xml"):
            return _markup_text(content, fmt)
        if fmt == "txt":
            return content.decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        log.warning("text extraction (%s) failed: %s", fmt, exc)
    return None


def _pdf_text(content: bytes) -> str | None:
    import pdfplumber

    parts: list[str] = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            if t:
                parts.append(t)
    text = "\n".join(parts).strip()
    return text or None  # None => likely scanned/image PDF


def _markup_text(content: bytes, fmt: str) -> str | None:
    from bs4 import BeautifulSoup

    parser = "lxml-xml" if fmt == "xml" else "lxml"
    soup = BeautifulSoup(content, parser)
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    return text or None


def _zip_text(content: bytes) -> str | None:
    parts: list[str] = []
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        for name in zf.namelist():
            lower = name.lower()
            if lower.endswith((".xml", ".html", ".htm")):
                data = zf.read(name)
                t = _markup_text(data, "xml" if lower.endswith(".xml") else "html")
                if t:
                    parts.append(t)
            elif lower.endswith(".txt"):
                parts.append(zf.read(name).decode("utf-8", errors="replace"))
    text = "\n".join(parts).strip()
    return text or None


# ------------------------------------------------------------------ downloading
def _download_one(
    client: HttpClient, settings: Settings, company_id: int, filing_id: str,
    source: str, doc_seq: int, url: str, market: str, symbol: str,
) -> FilingDocument | None:
    try:
        resp = client.get(url, headers=_headers_for(url, settings))
    except Exception as exc:  # noqa: BLE001
        log.warning("download failed %s: %s", url, exc)
        return None
    content = resp.content
    fmt = _format_from(url, resp.headers.get("Content-Type"))
    filename = _filename_from(url, doc_seq, fmt)
    dest_dir = Path(settings.docs_dir) / market / symbol / filing_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename
    dest.write_bytes(content)
    text = extract_text(content, fmt)
    if text and len(text) > _MAX_TEXT:
        text = text[:_MAX_TEXT]
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
    )


def download_filing_documents(
    repo: Repository, settings: Settings, company_id: int, filings: list[Filing]
) -> int:
    """Download originals + extract text for a list of freshly collected filings."""
    client = _client(settings)
    sym = repo.symbol_for_company(company_id)
    market, symbol = (sym or ("NA", str(company_id)))
    n = 0
    for fl in filings:
        urls = fl.doc_urls or ([fl.url] if fl.url else [])
        for seq, url in enumerate(urls):
            if not url:
                continue
            doc = _download_one(client, settings, company_id, fl.filing_id, fl.source,
                                seq, url, market, symbol)
            if doc is not None:
                repo.upsert_filing_document(doc)
                n += 1
        repo.commit()
    log.info("documents: downloaded %d files for company %s (%s:%s)", n, company_id, market, symbol)
    return n


def backfill_documents(
    conn: sqlite3.Connection,
    settings: Settings,
    symbols: list[str] | None,
    markets: list[str] | None,
    limit: int | None,
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
    for row in rows:
        cid = row["company_id"]
        sym = repo.symbol_for_company(cid)
        market, symbol = (sym or ("NA", str(cid)))
        url = row["url"]
        if not url:
            continue
        doc = _download_one(client, settings, cid, row["filing_id"], row["source"],
                            0, url, market, symbol)
        if doc is not None:
            repo.upsert_filing_document(doc)
            n += 1
            repo.commit()
    return n


def _pending_rows(conn, company_ids, markets, limit):
    sql = (
        "SELECT f.company_id, f.filing_id, f.source, f.url FROM filings f "
        "JOIN securities s ON s.company_id=f.company_id "
        "WHERE NOT EXISTS (SELECT 1 FROM filing_documents d WHERE d.company_id=f.company_id "
        "AND d.filing_id=f.filing_id AND d.source=f.source)"
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
