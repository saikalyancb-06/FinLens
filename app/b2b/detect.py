"""Decide what a file *is* from its bytes, and say so out loud when the name lied.

The rule this module exists to enforce: **the extension is a claim, the content
is evidence.** A customer integration will eventually POST a CSV called
`statement.pdf`, an HTML table called `export.xls`, or a ZIP called
`statement.xlsx`, and the wrong answer to "what is this?" is not a cosmetic
problem — it routes the file to a parser that will either produce nonsense or
throw an exception the client cannot act on.

So every decision here starts from magic bytes or a structural probe, and the
extension is consulted only for the two questions bytes genuinely cannot answer:

  * OFX vs QFX — Quicken's QFX is OFX with a vendor block; the transaction
    payload is byte-identical, so only the filename distinguishes them.
  * A ZIP container whose member list could not be read from a truncated head.

When the extension and the content disagree, `DetectedFormat.mismatch` is set
and both answers are reported. This module does not decide what to do about
that — the caller may reject with FORMAT_MISMATCH or proceed on content — but
it never silently papers over it.

`app/statements/document_text.py::sniff_kind` solves a narrower version of the
same problem for the email-ingestion path. This is deliberately a separate
implementation rather than a call into it: that one collapses everything it
does not recognise into `binary`/`text` and has no concept of OFX, CAMT, JSON
or a delimiter, all of which the B2B parser registry must dispatch on.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from app.b2b.parsers.delimited import sniff_delimiter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- format ids
# Stable lowercase ids. These are the keys the parser registry dispatches on and
# the ids `GET /v1/formats` publishes, so they are part of the public contract.
F_PDF = "pdf"
F_XLSX = "xlsx"
F_XLS = "xls"
F_CSV = "csv"
F_TSV = "tsv"
F_TXT = "txt"
F_JSON = "json"
F_CAMT = "xml_camt"
F_OFX = "ofx"
F_QFX = "qfx"
F_ZIP = "zip"
F_UNKNOWN = "unknown"

MIME_BY_FORMAT: Dict[str, str] = {
    F_PDF: "application/pdf",
    F_XLSX: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    F_XLS: "application/vnd.ms-excel",
    F_CSV: "text/csv",
    F_TSV: "text/tab-separated-values",
    F_TXT: "text/plain",
    F_JSON: "application/json",
    F_CAMT: "application/xml",
    F_OFX: "application/x-ofx",
    F_QFX: "application/vnd.intu.qfx",
    F_ZIP: "application/zip",
    F_UNKNOWN: "application/octet-stream",
}

#: What an extension *claims*. Never the final answer.
EXTENSION_FORMATS: Dict[str, str] = {
    ".pdf": F_PDF,
    ".xlsx": F_XLSX, ".xlsm": F_XLSX,
    ".xls": F_XLS,
    ".csv": F_CSV,
    ".tsv": F_TSV, ".tab": F_TSV,
    ".txt": F_TXT, ".dat": F_TXT,
    ".json": F_JSON,
    ".xml": F_CAMT,          # the only XML dialect this API parses
    ".ofx": F_OFX,
    ".qfx": F_QFX,
    ".zip": F_ZIP,
}

#: Formats grouped so that a *within-family* difference is not called a lie.
#: `.csv` holding semicolon-separated rows is a delimiter detail, not a
#: misnamed file, and flagging it as a mismatch would make the signal useless.
#: OFX and QFX are one family for the same reason — same bytes, different name.
_FAMILIES: Dict[str, str] = {
    F_CSV: "delimited", F_TSV: "delimited", F_TXT: "delimited",
    F_OFX: "ofx", F_QFX: "ofx",
}

#: How much of the file the text probes are allowed to look at. A CAMT header
#: with a long GrpHdr, or an OFX SGML header block, can push the first
#: `<STMTTRN>` or `BkToCstmrStmt` past a small head buffer.
PROBE_BYTES = 65_536


@dataclass
class DetectedFormat:
    """What the file is, how sure we are, and what gave it away."""

    format: str
    confidence: float
    reason: str
    mime: str
    extension: str = ""                        # normalised, with the dot
    extension_format: Optional[str] = None     # what the name claimed
    mismatch: bool = False                     # name and content disagree
    delimiter: Optional[str] = None            # delimited text only
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_supported(self) -> bool:
        return self.format not in (F_UNKNOWN, F_ZIP)

    def to_api(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "format": self.format,
            "confidence": round(self.confidence, 3),
            "reason": self.reason,
            "mime": self.mime,
        }
        if self.mismatch:
            out["extension_format"] = self.extension_format
            out["mismatch"] = True
        return out


def _family(fmt: str) -> str:
    return _FAMILIES.get(fmt, fmt)


def _decode(data: bytes) -> Optional[str]:
    """Best-effort text view of a byte head. None means 'not text'."""
    for enc in ("utf-8-sig", "utf-8", "utf-16", "cp1252", "latin-1"):
        try:
            text = data.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
        # latin-1 decodes literally anything, so a NUL-heavy result is binary
        # that merely survived the decode rather than text.
        if text.count("\x00") > max(2, len(text) // 200):
            return None
        return text
    return None


def _probe_bytes(head_bytes: bytes, full_path: Optional[str]) -> bytes:
    """Prefer a larger window off disk when we have the file."""
    if full_path and os.path.isfile(full_path):
        try:
            with open(full_path, "rb") as handle:
                data = handle.read(PROBE_BYTES)
            if len(data) >= len(head_bytes or b""):
                return data
        except OSError:
            pass
    return head_bytes or b""


def _zip_members(head_bytes: bytes, full_path: Optional[str]) -> Optional[list]:
    """Member names of a ZIP, or None when the container could not be listed.

    A truncated head cannot be listed at all — the central directory lives at
    the *end* of a ZIP — so this is only reliable with `full_path`.
    """
    if full_path and os.path.isfile(full_path):
        try:
            with zipfile.ZipFile(full_path) as archive:
                return archive.namelist()
        except (zipfile.BadZipFile, OSError):
            return None
    try:
        with zipfile.ZipFile(io.BytesIO(head_bytes)) as archive:
            return archive.namelist()
    except (zipfile.BadZipFile, OSError):
        return None


_RE_OFX_TAG = re.compile(r"<\s*OFX\s*>", re.IGNORECASE)
_RE_CAMT_NS = re.compile(r"camt\.053", re.IGNORECASE)
_RE_XML_DECL = re.compile(r"^\s*<\?xml", re.IGNORECASE)
_RE_FIRST_ELEMENT = re.compile(r"<\s*([A-Za-z_][\w.:-]*)")


def detect_format(filename: str,
                  head_bytes: bytes,
                  full_path: Optional[str] = None) -> DetectedFormat:
    """Identify a file from its content, with the filename as a tiebreak only.

    `head_bytes` is enough for every magic-byte decision. `full_path`, when
    given, buys three things nothing else can supply: the ZIP central directory
    (xlsx vs plain archive), a JSON parse of a document longer than the head,
    and a text probe deep enough to reach a CAMT root element.
    """
    ext = os.path.splitext(os.path.basename(filename or ""))[1].lower()
    ext_format = EXTENSION_FORMATS.get(ext)

    def result(fmt: str, confidence: float, reason: str,
               delimiter: Optional[str] = None,
               detail: Optional[Dict[str, Any]] = None) -> DetectedFormat:
        mismatch = bool(
            ext_format
            and fmt != F_UNKNOWN
            and _family(fmt) != _family(ext_format)
        )
        if mismatch:
            reason = f"{reason}; extension '{ext}' claimed {ext_format}"
        return DetectedFormat(
            format=fmt,
            confidence=confidence,
            reason=reason,
            mime=MIME_BY_FORMAT.get(fmt, MIME_BY_FORMAT[F_UNKNOWN]),
            extension=ext,
            extension_format=ext_format,
            mismatch=mismatch,
            delimiter=delimiter,
            detail=detail or {},
        )

    head = head_bytes or b""
    if not head:
        return result(F_UNKNOWN, 0.0, "file is empty")

    # -------------------------------------------------------------- magic bytes
    if head.startswith(b"%PDF-"):
        return result(F_PDF, 1.0, "magic bytes '%PDF-'")

    if head.startswith(b"\xd0\xcf\x11\xe0"):
        # OLE2 compound document. Genuine legacy .xls lives here — so do .doc
        # and .msg, which will parse to zero transactions rather than crash.
        return result(F_XLS, 0.9, "OLE2 compound document header (legacy .xls)")

    if head.startswith(b"PK\x03\x04"):
        names = _zip_members(head, full_path)
        if names is None:
            # Could not open the container. Believe the extension only as far
            # as saying so in the reason, and keep the confidence honest.
            if ext_format == F_XLSX:
                return result(F_XLSX, 0.5,
                              "ZIP container; member list unreadable, extension claims xlsx")
            return result(F_ZIP, 0.6, "ZIP container; member list unreadable")
        if any(n.startswith("xl/") for n in names):
            return result(F_XLSX, 1.0, "ZIP container with an 'xl/' member (OOXML workbook)")
        if any(n.startswith("word/") or n.startswith("ppt/") for n in names):
            return result(F_ZIP, 0.9, "OOXML container that is not a workbook")
        return result(F_ZIP, 0.95, "ZIP archive with no OOXML workbook member")

    if head[:4] in (b"\x89PNG", b"GIF8") or head[:3] == b"\xff\xd8\xff":
        return result(F_UNKNOWN, 0.95, "image file; this API analyses statements, not images")

    # ------------------------------------------------------------------- text
    probe = _probe_bytes(head, full_path)
    text = _decode(probe)
    if text is None:
        return result(F_UNKNOWN, 0.8, "binary content with no recognised signature")

    stripped = text.lstrip("﻿ \t\r\n")
    upper_head = stripped[:4096].upper()

    # OFX before generic XML: OFX 2.x *is* XML and would otherwise be caught by
    # the XML branch and rejected as a non-CAMT dialect.
    if "OFXHEADER" in upper_head or _RE_OFX_TAG.search(stripped[:8192]):
        is_xml = bool(_RE_XML_DECL.match(stripped))
        version = "2.x" if is_xml else "1.x"
        # Only the filename separates QFX from OFX; the payload is the same.
        fmt = F_QFX if ext == ".qfx" else F_OFX
        return result(
            fmt, 0.95,
            f"OFX {version} markers ({'XML declaration + <OFX>' if is_xml else 'OFXHEADER/SGML <OFX>'})",
            detail={"ofx_version": version},
        )

    if stripped.startswith("<"):
        if _RE_CAMT_NS.search(text) or "BkToCstmrStmt" in text:
            return result(F_CAMT, 0.95,
                          "ISO 20022 markers (camt.053 namespace or BkToCstmrStmt)")
        root = _RE_FIRST_ELEMENT.search(stripped.replace("<?xml", "", 1) if _RE_XML_DECL.match(stripped) else stripped)
        root_name = root.group(1) if root else "?"
        return result(
            F_UNKNOWN, 0.7,
            f"XML/HTML document rooted at <{root_name}>; only CAMT.053 XML is supported",
            detail={"xml_root": root_name},
        )

    if stripped[:1] in ("{", "["):
        parsed_ok, why = _json_probe(stripped, full_path)
        if parsed_ok:
            return result(F_JSON, 0.98, "parsed as JSON")
        # It opens like JSON and is not anything else; call it JSON at low
        # confidence so the JSON parser can return a precise FILE_CORRUPT
        # rather than the caller guessing from a bare "unknown".
        return result(F_JSON, 0.5, f"starts with a JSON opener but did not parse ({why})")

    delimiter, delim_conf, delim_reason = sniff_delimiter(text)
    if delimiter == "\t":
        return result(F_TSV, delim_conf, f"tab-delimited text ({delim_reason})",
                      delimiter="\t")
    if delimiter:
        name = {";": "semicolon", "|": "pipe", ",": "comma"}.get(delimiter, repr(delimiter))
        return result(F_CSV, delim_conf, f"{name}-delimited text ({delim_reason})",
                      delimiter=delimiter)

    return result(F_TXT, 0.4,
                  "plain text with no consistent delimiter; no tabular structure found")


def _json_probe(text: str, full_path: Optional[str]) -> tuple:
    """Try hardest to answer 'is this JSON?' without loading a huge file twice."""
    try:
        json.loads(text)
        return True, ""
    except ValueError as exc:
        head_error = str(exc)
    if not (full_path and os.path.isfile(full_path)):
        return False, head_error
    try:
        # The head almost certainly truncated a valid document. Re-read whole,
        # but only when the file is small enough that doing so is not itself a
        # denial-of-service; anything larger stays a low-confidence guess.
        if os.path.getsize(full_path) > 32 * 1024 * 1024:
            return False, "document larger than the 32MB re-probe limit"
        with open(full_path, "rb") as handle:
            whole = handle.read()
        decoded = _decode(whole)
        if decoded is None:
            return False, "not decodable as text"
        json.loads(decoded)
        return True, ""
    except (OSError, ValueError) as exc:
        return False, str(exc)
