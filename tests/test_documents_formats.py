"""Format detection, archive recursion and 한글 HWP extraction.

The regression these guard: DART serves a ZIP from a URL ending in `.xml` under a
Content-Type of `application/x-msdownload`, so anything that trusts the name
extracts nothing and stores it as success.
"""

import io
import logging
import struct
import zipfile
import zlib

import pytest

from issuer_data import documents
from issuer_data.config import Settings
from issuer_data.documents import _filename_from, _format_from, extract_text, sniff_format
from issuer_data.hwp_extract import is_hwp5, is_hwpx
from issuer_data.models import Company, Filing, Security
from issuer_data.storage.repository import Repository

# --------------------------------------------------------------- HWP fixtures
_FREE, _ENDOFCHAIN, _FATSECT, _NOSTREAM = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFF
_SECTOR = 512
_HWPTAG_PARA_TEXT = 0x10 + 51


def _para_record(text: str) -> bytes:
    """One HWPTAG_PARA_TEXT record holding `text` as UTF-16LE."""
    payload = text.encode("utf-16-le")
    header = _HWPTAG_PARA_TEXT | (0 << 10) | ((len(payload) & 0xFFF) << 20)
    return struct.pack("<I", header) + payload


def _dir_entry(name, obj_type, left=_NOSTREAM, right=_NOSTREAM, child=_NOSTREAM,
               start=0, size=0) -> bytes:
    raw = name.encode("utf-16-le") + b"\0\0"
    return (
        raw.ljust(64, b"\0")[:64]
        + struct.pack("<H", len(raw))
        + struct.pack("<BB", obj_type, 1)
        + struct.pack("<III", left, right, child)
        + b"\0" * 36
        + struct.pack("<I", start)
        + struct.pack("<Q", size)
    )


def make_hwp5(sections: list[bytes], compressed: bool = True, flags: int = 0) -> bytes:
    """A real (minimal) HWP 5.x OLE container holding `sections`.

    Built rather than committed as a binary blob so the bytes under test are
    readable. Streams are padded past the 4096-byte mini-stream cutoff so the
    container needs no mini-FAT.
    """
    file_header = (
        b"HWP Document File".ljust(32, b"\0")
        + struct.pack("<I", 0x05000300)
        + struct.pack("<I", (1 if compressed else 0) | flags)
    )
    streams = [("FileHeader", file_header)]
    for i, body in enumerate(sections):
        streams.append((f"Section{i}", zlib.compress(body)[2:-4] if compressed else body))
    streams = [(n, d.ljust(4096, b"\0")) for n, d in streams]

    n_entries = 3 + len(sections)
    dir_sectors = -(-n_entries * 128 // _SECTOR)      # directory spans a chain
    placed, fat_links, nxt = [], {}, 1 + dir_sectors  # sector 0 = FAT, then directory
    for name, data in streams:
        nsec = (len(data) + _SECTOR - 1) // _SECTOR
        for k in range(nsec):
            fat_links[nxt + k] = _ENDOFCHAIN if k == nsec - 1 else nxt + k + 1
        placed.append((name, data, nxt, len(data)))
        nxt += nsec

    fat = [_FREE] * (_SECTOR // 4)
    assert nxt <= len(fat), "fixture outgrew a single FAT sector"
    fat[0] = _FATSECT
    for d in range(1, 1 + dir_sectors):
        fat[d] = _ENDOFCHAIN if d == dir_sectors else d + 1
    for sec, link in fat_links.items():
        fat[sec] = link

    at = {n: (s, z) for n, _, s, z in placed}
    entries = [
        _dir_entry("Root Entry", 5, child=1),
        _dir_entry("BodyText", 1, right=2, child=3),
        _dir_entry("FileHeader", 2, start=at["FileHeader"][0], size=at["FileHeader"][1]),
    ]
    for i in range(len(sections)):
        entries.append(_dir_entry(
            f"Section{i}", 2,
            right=(4 + i if i + 1 < len(sections) else _NOSTREAM),
            start=at[f"Section{i}"][0], size=at[f"Section{i}"][1],
        ))

    head = bytearray(_SECTOR)
    head[0:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    for offset, fmt, value in (
        (24, "<H", 0x003E), (26, "<H", 0x0003), (28, "<H", 0xFFFE),
        (30, "<H", 9), (32, "<H", 6), (44, "<I", 1), (48, "<I", 1),
        (56, "<I", 4096), (60, "<I", _ENDOFCHAIN), (64, "<I", 0),
        (68, "<I", _ENDOFCHAIN), (72, "<I", 0), (76, "<I", 0),
    ):
        struct.pack_into(fmt, head, offset, value)
    for i in range(1, 109):
        struct.pack_into("<I", head, 76 + 4 * i, _FREE)

    fat_bytes = b"".join(struct.pack("<I", v) for v in fat)
    directory = b"".join(entries).ljust(dir_sectors * _SECTOR, b"\0")
    out = bytearray(bytes(head) + fat_bytes + directory)
    for _name, data, start, _size in placed:
        offset = _SECTOR * (start + 1)
        out.extend(b"\0" * max(0, offset - len(out)))
        out[offset:offset + len(data)] = data
    return bytes(out)


def make_hwpx(body: str = "한글 문서 본문") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", "application/hwp+zip")
        zf.writestr("Contents/header.xml", "<head><fontface>바탕</fontface></head>")
        zf.writestr("Contents/section0.xml", f"<sec><p><t>{body}</t></p></sec>")
    return buf.getvalue()


def make_zip(members: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


# ------------------------------------------------------- detection by content
def test_zip_served_as_xml_is_detected_as_zip():
    """The DART case: URL says .xml, Content-Type says msdownload, bytes say ZIP."""
    blob = make_zip({"20260310002820.xml": "<doc><title>사업보고서</title></doc>"})
    url = "https://opendart.fss.or.kr/api/document.xml?crtfc_key=k&rcept_no=1"
    fmt = _format_from(url, "application/x-msdownload;charset=UTF-8", blob)
    assert fmt == "zip"
    assert "사업보고서" in extract_text(blob, fmt)


def test_zip_served_as_xml_without_bytes_still_guesses_xml():
    """Without the body there is nothing to go on — which is why bytes are passed."""
    url = "https://opendart.fss.or.kr/api/document.xml?rcept_no=1"
    assert _format_from(url, "application/x-msdownload;charset=UTF-8") == "xml"


def test_saved_filename_follows_the_real_format():
    url = "https://opendart.fss.or.kr/api/document.xml?rcept_no=1"
    assert _filename_from(url, 0, "zip") == "document.zip"
    assert _filename_from("https://x/a.pdf", 0, "pdf") == "a.pdf"  # already honest
    assert _filename_from("https://x/download?id=7", 3, "pdf") == "doc3.pdf"


@pytest.mark.parametrize("content,expected", [
    (b"%PDF-1.7\nstuff", "pdf"),
    (b"PK\x03\x04rest", "zip"),
    (b"\x1f\x8b\x08\x00", "gzip"),
    (b'<?xml version="1.0"?><doc/>', "xml"),
    (b"<!DOCTYPE html><html></html>", "html"),
    (b"just some prose", None),
])
def test_sniff_format(content, expected):
    assert sniff_format(content) == expected


def test_ole_that_is_not_hwp_is_reported_not_guessed(caplog):
    """A legacy .doc/.xls is an OLE file too; it must not be mistaken for HWP."""
    fake_doc = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 2048
    assert sniff_format(fake_doc) == "ole"
    assert not is_hwp5(fake_doc)
    with caplog.at_level(logging.WARNING):
        assert extract_text(fake_doc, "ole") is None
    assert "no text extractor" in caplog.text


# -------------------------------------------------------- archives, recursive
def test_zip_member_dispatched_by_content_not_name():
    """The same name-trusting bug, one level in: a .bin member that is really XML."""
    blob = make_zip({"payload.bin": "<doc><title>주요사항보고서</title></doc>"})
    assert "주요사항보고서" in extract_text(blob, "zip")


def test_zip_of_hwp_is_extracted():
    inner = make_hwp5([_para_record("첨부 한글 문서")])
    assert "첨부 한글 문서" in extract_text(make_zip({"attach.hwp": inner}), "zip")


def test_nested_archive_is_extracted():
    inner = make_zip({"body.xml": "<d>안쪽 본문</d>"})
    assert "안쪽 본문" in extract_text(make_zip({"inner.zip": inner}), "zip")


def test_archive_recursion_is_depth_capped(caplog):
    blob = make_zip({"body.xml": "<d>깊은 본문</d>"})
    for _ in range(6):
        blob = make_zip({"nested.zip": blob})
    with caplog.at_level(logging.WARNING):
        extract_text(blob, "zip")
    assert "nested deeper" in caplog.text


def test_unreadable_members_are_skipped_not_fatal():
    blob = make_zip({"logo.png": b"\x89PNG\r\n\x1a\n\0\0", "body.xml": "<d>본문</d>"})
    assert extract_text(blob, "zip").strip() == "본문"


# ------------------------------------------------------------------ HWP / HWPX
def test_hwp5_roundtrip():
    blob = make_hwp5([_para_record("첫 번째 문단입니다."), _para_record("두 번째 문단.")])
    assert is_hwp5(blob)
    assert sniff_format(blob) == "hwp"
    assert extract_text(blob, "hwp") == "첫 번째 문단입니다.\n두 번째 문단."


def test_hwp5_uncompressed():
    blob = make_hwp5([_para_record("비압축 본문")], compressed=False)
    assert extract_text(blob, "hwp") == "비압축 본문"


def test_hwp5_multiple_sections_in_numeric_order():
    sections = [_para_record(f"구역 {i}") for i in range(12)]
    text = extract_text(make_hwp5(sections), "hwp")
    assert text.splitlines() == [f"구역 {i}" for i in range(12)]  # not 0,1,10,11,2...


def test_hwp5_control_characters_do_not_corrupt_text():
    """Wide controls span 8 UTF-16 units; skipping the wrong width garbles the rest."""
    payload = (
        "앞".encode("utf-16-le")
        + struct.pack("<H", 4) + b"\0" * 14     # inline control: 8 units total
        + struct.pack("<H", 9) + b"\0" * 14     # tab, also 8 units
        + "뒤".encode("utf-16-le")
    )
    header = _HWPTAG_PARA_TEXT | ((len(payload) & 0xFFF) << 20)
    record = struct.pack("<I", header) + payload
    assert extract_text(make_hwp5([record]), "hwp") == "앞\t뒤"


def test_hwp5_password_protected_yields_no_text(caplog):
    blob = make_hwp5([_para_record("비밀")], flags=0x02)
    with caplog.at_level(logging.WARNING):
        assert extract_text(blob, "hwp") is None
    assert "password-protected" in caplog.text


def test_hwpx_detected_and_body_extracted():
    blob = make_hwpx("한글 문서 본문")
    assert is_hwpx(blob)
    assert sniff_format(blob) == "hwpx"
    text = extract_text(blob, "hwpx")
    assert "한글 문서 본문" in text
    assert "바탕" not in text  # header.xml is not body text


def test_plain_zip_is_not_mistaken_for_hwpx():
    assert sniff_format(make_zip({"a.xml": "<d/>"})) == "zip"


# ------------------------------------------------- silence is the real defect
class _Resp:
    def __init__(self, content, content_type):
        self.content = content
        self.headers = {"Content-Type": content_type}


def test_document_with_no_text_is_logged_and_counted(conn, tmp_path, monkeypatch, caplog):
    """A download that extracts nothing must be visible, not a quiet text_chars=0."""
    settings = Settings(docs_dir=tmp_path)
    repo = Repository(conn)
    cid = repo.resolve_company(Company(name="ACME", cik="1", source="edgar"))
    repo.upsert_security(Security(market="US", symbol="ACME", source="edgar"), cid)
    repo.upsert_filings(cid, [Filing(
        symbol="ACME", market="US", filing_id="f1", url="http://x/doc.xml",
        source="edgar", doc_urls=["http://x/doc.xml"])])
    repo.commit()

    class _FakeClient:
        def get(self, url, headers=None):
            # An OLE file we cannot read: downloads fine, extracts nothing.
            return _Resp(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 2048, "text/xml")

    monkeypatch.setattr(documents, "_client", lambda s: _FakeClient())
    with caplog.at_level(logging.INFO):
        assert documents.backfill_documents(conn, settings, ["US:ACME"], None, None) == 1

    assert "no text extracted" in caplog.text
    assert "1 with NO text extracted" in caplog.text
    row = conn.execute("SELECT doc_format, text_chars FROM filing_documents").fetchone()
    assert (row["doc_format"], row["text_chars"]) == ("ole", 0)
