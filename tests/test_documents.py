import io
import zipfile

from issuer_data import documents
from issuer_data.config import Settings
from issuer_data.documents import _format_from, extract_text
from issuer_data.models import Company, Filing, Security
from issuer_data.storage.repository import Repository


def test_extract_html_text():
    html = b"<html><body><h1>Title</h1><p>Hello <b>world</b></p><script>x=1</script></body></html>"
    text = extract_text(html, "html")
    assert "Hello" in text and "world" in text
    assert "x=1" not in text  # script stripped


def test_extract_txt():
    assert extract_text(b"plain text body", "txt") == "plain text body"


def test_extract_zip_of_xml():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("report.xml", "<doc><title>사업보고서</title><body>내용</body></doc>")
    text = extract_text(buf.getvalue(), "zip")
    assert "사업보고서" in text


def test_format_detection():
    assert _format_from("https://x/y.pdf", None) == "pdf"
    assert _format_from("https://x/y", "application/pdf") == "pdf"
    assert _format_from("https://sec.gov/a.htm", None) == "html"
    assert _format_from("https://x/a.zip", None) == "zip"
    assert _format_from("https://x/a.xml", None) == "xml"


class _Resp:
    def __init__(self, url):
        self.content = f"<html><body>{url}</body></html>".encode()
        self.headers = {"Content-Type": "text/html"}


def test_backfill_downloads_all_persisted_doc_urls(conn, tmp_path, monkeypatch):
    settings = Settings(docs_dir=tmp_path)
    repo = Repository(conn)
    cid = repo.resolve_company(Company(name="ACME", cik="1", source="edgar"))
    repo.upsert_security(Security(market="US", symbol="ACME", source="edgar"), cid)
    repo.upsert_filings(cid, [Filing(
        symbol="ACME", market="US", filing_id="f1", url="http://x/primary.htm",
        source="edgar", doc_urls=["http://x/primary.htm", "http://x/ex99.htm"])])
    repo.commit()

    calls: list[str] = []

    class _FakeClient:
        def get(self, url, headers=None):
            calls.append(url)
            return _Resp(url)

    monkeypatch.setattr(documents, "_client", lambda s: _FakeClient())

    n = documents.backfill_documents(conn, settings, ["US:ACME"], None, None)
    assert n == 2  # primary + exhibit, not just doc_seq=0
    assert set(calls) == {"http://x/primary.htm", "http://x/ex99.htm"}
    seqs = {r["doc_seq"] for r in conn.execute("SELECT doc_seq FROM filing_documents")}
    assert seqs == {0, 1}

    # a second backfill is incremental: nothing left to fetch
    calls.clear()
    assert documents.backfill_documents(conn, settings, ["US:ACME"], None, None) == 0
    assert calls == []
