"""Extract body text from 한글 (HWP) documents — the binary HWP 5.x format and
the newer XML-based HWPX.

HWP 5.x is an OLE/CFBF container. `FileHeader` identifies the file and carries a
flag telling whether the body streams are raw-deflate compressed; `BodyText/Section*`
hold the content as a flat record stream. Each record starts with a 32-bit header
packing (tag id, level, size), and the paragraph text lives in HWPTAG_PARA_TEXT
records as UTF-16LE, interleaved with control characters that must be skipped by
width or the text comes out full of garbage.

HWPX is an OWPML zip: the body is `Contents/section*.xml`, which the generic markup
path handles once the right members are picked out.

Distribution ("배포용") documents keep their body in an encrypted `ViewText` stream;
those yield no text and are reported as such rather than silently returning "".
"""

from __future__ import annotations

import io
import re
import struct
import zipfile
import zlib

from .logging import get_logger

log = get_logger(__name__)

_HWP5_SIGNATURE = b"HWP Document File"
_HWPX_MIMETYPE = b"application/hwp+zip"
_HWPX_SECTION_RE = re.compile(r"^Contents/section\d+\.xml$", re.IGNORECASE)

# Record header: tag id (10 bits) | level (10 bits) | size (12 bits). A size of
# 0xFFF means the real size is in the following uint32.
_TAG_MASK = 0x3FF
_SIZE_OVERFLOW = 0xFFF
_HWPTAG_BEGIN = 0x10
_HWPTAG_PARA_TEXT = _HWPTAG_BEGIN + 51

# Control characters inside PARA_TEXT. "char" controls occupy one UTF-16 unit;
# "inline" and "extended" ones occupy eight (the control plus 7 units of payload).
# Skipping the wrong width desynchronises the whole paragraph, so the table is
# spelled out rather than guessed at.
_CTRL_WIDE = frozenset({1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23})
_CTRL_WIDE_UNITS = 8
_CTRL_AS_TEXT = {9: "\t", 10: "\n", 13: "\n", 30: " ", 31: " "}


def is_hwp5(content: bytes) -> bool:
    """True if `content` is a binary HWP 5.x document (not just any OLE file)."""
    import olefile

    if not olefile.isOleFile(io.BytesIO(content)):
        return False
    try:
        with olefile.OleFileIO(io.BytesIO(content)) as ole:
            if not ole.exists("FileHeader"):
                return False
            with ole.openstream("FileHeader") as fh:
                return fh.read(len(_HWP5_SIGNATURE)) == _HWP5_SIGNATURE
    except Exception:  # noqa: BLE001 - a malformed OLE file is simply "not HWP"
        return False


def is_hwpx(content: bytes) -> bool:
    """True if `content` is an HWPX (OWPML) package rather than a plain zip."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            names = set(zf.namelist())
            if "mimetype" in names and zf.read("mimetype").strip() == _HWPX_MIMETYPE:
                return True
            return any(_HWPX_SECTION_RE.match(n) for n in names)
    except Exception:  # noqa: BLE001
        return False



def hwp5_text(content: bytes) -> str | None:
    """Body text of a binary HWP 5.x document, or None if nothing is readable."""
    import olefile

    with olefile.OleFileIO(io.BytesIO(content)) as ole:
        header = ole.openstream("FileHeader").read(40)
        if len(header) < 40:
            log.warning("HWP: FileHeader is truncated (%d bytes)", len(header))
            return None
        version = struct.unpack("<I", header[32:36])[0]
        flags = struct.unpack("<I", header[36:40])[0]
        compressed = bool(flags & 0x01)
        encrypted = bool(flags & 0x02)
        distributable = bool(flags & 0x04)
        if encrypted:
            log.warning("HWP: password-protected document, no text extracted")
            return None
        if distributable and not _sections(ole):
            # 배포용 문서: the body lives in an encrypted ViewText stream.
            log.warning("HWP: distribution document (ViewText), no text extracted")
            return None

        parts: list[str] = []
        for name in _sections(ole):
            raw = ole.openstream(name).read()
            if compressed:
                try:
                    raw = zlib.decompress(raw, -15)  # raw deflate, no zlib header
                except zlib.error as exc:
                    log.warning("HWP: cannot inflate %s: %s", name, exc)
                    continue
            text = _section_text(raw)
            if text:
                parts.append(text)
    if not parts:
        log.warning("HWP: no PARA_TEXT records found (version 0x%08x)", version)
        return None
    return "\n".join(parts).strip() or None


def hwpx_text(content: bytes) -> str | None:
    """Body text of an HWPX package: the section XML members, in order."""
    from .documents import markup_text

    parts: list[str] = []
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        sections = sorted(
            (n for n in zf.namelist() if _HWPX_SECTION_RE.match(n)),
            key=_section_sort_key,
        )
        for name in sections:
            text = markup_text(zf.read(name), "xml")
            if text:
                parts.append(text)
    return "\n".join(parts).strip() or None


def _sections(ole) -> list[str]:
    """`BodyText/Section*` stream paths, in numeric order."""
    found = [
        "/".join(entry)
        for entry in ole.listdir()
        if len(entry) == 2 and entry[0] == "BodyText" and entry[1].lower().startswith("section")
    ]
    return sorted(found, key=_section_sort_key)


def _section_sort_key(name: str) -> tuple[int, str]:
    """Sort Section2 before Section10 — plain string order would not."""
    match = re.search(r"(\d+)", name.rsplit("/", 1)[-1])
    return (int(match.group(1)) if match else 0, name)


def _section_text(buf: bytes) -> str:
    """Walk one decompressed section's record stream, collecting paragraph text."""
    parts: list[str] = []
    pos, end = 0, len(buf)
    while pos + 4 <= end:
        (header,) = struct.unpack_from("<I", buf, pos)
        pos += 4
        tag_id = header & _TAG_MASK
        size = (header >> 20) & _SIZE_OVERFLOW
        if size == _SIZE_OVERFLOW:
            if pos + 4 > end:
                break
            (size,) = struct.unpack_from("<I", buf, pos)
            pos += 4
        payload = buf[pos:pos + size]
        pos += size
        if tag_id == _HWPTAG_PARA_TEXT:
            parts.append(_para_text(payload))
    return "\n".join(p for p in parts if p.strip())


def _para_text(payload: bytes) -> str:
    """Decode one PARA_TEXT record, stepping over control characters by width."""
    out: list[str] = []
    units = len(payload) // 2
    i = 0
    while i < units:
        (code,) = struct.unpack_from("<H", payload, i * 2)
        if code >= 32:
            out.append(chr(code))
            i += 1
            continue
        out.append(_CTRL_AS_TEXT.get(code, ""))
        i += _CTRL_WIDE_UNITS if code in _CTRL_WIDE else 1
    return "".join(out)
