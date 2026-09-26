"""Read the *content* of a downloaded document, whatever its filename claims.

The type is decided by magic bytes first and the extension second. That ordering
is the point: ``monthly_document.pdf`` may really be a PDF bank statement, and
``statement.pdf`` may really be a JPEG a bank's mailer renamed. Classification
downstream is only as trustworthy as this layer's refusal to believe filenames.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import zipfile
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)

KIND_PDF = "pdf"
KIND_CSV = "csv"
KIND_XLSX = "xlsx"
KIND_XLS = "xls"
KIND_HTML = "html"
KIND_TEXT = "text"
KIND_BINARY = "binary"
KIND_UNKNOWN = "unknown"

#: Text extraction is capped so a 400-page annual report cannot pin a worker.
MAX_PDF_PAGES = 40
MAX_TEXT_CHARS = 400_000
MAX_TABLE_ROWS = 5_000


@dataclass
class DocumentContent:
    """What could be read out of a document, plus why not when nothing could."""

    kind: str = KIND_UNKNOWN
    text: str = ""
    rows: List[List[str]] = field(default_factory=list)
    is_encrypted: bool = False
    unlocked: bool = False
    password_required: bool = False
    password_invalid: bool = False
    error: Optional[str] = None

    @property
    def has_content(self) -> bool:
        return bool(self.text.strip() or self.rows)


def sniff_kind(data: bytes, filename: str = "") -> str:
    """Identify a document from its leading bytes, falling back to its name."""
    head = data[:8] if data else b""

    if head.startswith(b"%PDF-"):
        return KIND_PDF
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        # OLE2 compound file: legacy .xls (also .doc, which will simply yield no
        # ledger structure and be classified NOT_FINANCIAL further down).
        return KIND_XLS
    if head.startswith(b"PK\x03\x04"):
        # A ZIP container. OOXML workbooks carry xl/workbook.xml; anything else
        # is an archive we do not open.
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                names = archive.namelist()
            if any(n.startswith("xl/") for n in names):
                return KIND_XLSX
        except (zipfile.BadZipFile, OSError):
            pass
        return KIND_BINARY
    if head[:4] in (b"\x89PNG", b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1", b"GIF8"):
        return KIND_BINARY

    ext = os.path.splitext((filename or "").lower())[1]

    try:
        sample = data[:8192].decode("utf-8")
    except UnicodeDecodeError:
        try:
            sample = data[:8192].decode("latin-1")
        except Exception:
            return KIND_BINARY

    stripped = sample.lstrip().lower()
    if stripped.startswith(("<!doctype html", "<html", "<?xml")) or "<table" in stripped:
        return KIND_HTML
    if ext in (".csv", ".tsv"):
        return KIND_CSV
    if ext in (".xlsx", ".xlsm"):
        return KIND_XLSX
    if ext == ".xls":
        return KIND_XLS
    if ext in (".htm", ".html"):
        return KIND_HTML

    # Comma/tab/semicolon separated content that no extension claimed.
    first_lines = [ln for ln in sample.splitlines()[:5] if ln.strip()]
    if first_lines:
        for sep in (",", "\t", ";", "|"):
            counts = [ln.count(sep) for ln in first_lines]
            if min(counts) >= 2 and max(counts) - min(counts) <= 1:
                return KIND_CSV
    return KIND_TEXT


def read_document(path: str, filename: str = "", password: Optional[str] = None) -> DocumentContent:
    """Extract text (and a table where one exists) from a document on disk."""
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        return DocumentContent(error=f"could not read '{filename or path}': {exc}")

    if not data:
        return DocumentContent(error=f"'{filename or path}' is empty")

    kind = sniff_kind(data, filename or os.path.basename(path))

    if kind == KIND_PDF:
        return _read_pdf(path, filename, password)
    if kind == KIND_XLSX:
        return _read_xlsx(data, filename)
    if kind == KIND_XLS:
        return _read_xls(path, data, filename)
    if kind == KIND_HTML:
        return _read_html(data, filename)
    if kind == KIND_CSV:
        return _read_csv(data, filename)
    if kind == KIND_TEXT:
        return _read_text(data)
    return DocumentContent(kind=kind,
                           error=f"'{filename}' is a binary format this system cannot read")


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _read_pdf(path: str, filename: str, password: Optional[str]) -> DocumentContent:
    try:
        import fitz  # PyMuPDF
    except ImportError:  # pragma: no cover - dependency is in requirements.txt
        return DocumentContent(kind=KIND_PDF, error="PyMuPDF is not installed")

    result = DocumentContent(kind=KIND_PDF)
    try:
        doc = fitz.open(path)
    except Exception as exc:
        result.error = f"'{filename}' could not be opened as a PDF ({exc})"
        return result

    try:
        if doc.is_encrypted:
            result.is_encrypted = True
            supplied = (password or "").strip()
            if supplied and doc.authenticate(supplied) > 0:
                result.unlocked = True
            elif doc.authenticate("") > 0:
                # Encrypted with an empty owner password — readable as-is.
                result.unlocked = True
            elif supplied:
                result.password_invalid = True
                result.error = (f"The document password for '{filename}' is invalid. "
                                "Check the password and try again.")
                return result
            else:
                result.password_required = True
                result.error = (f"'{filename}' is password-protected and needs a document "
                                "password to open.")
                return result

        pages = []
        for index, page in enumerate(doc):
            if index >= MAX_PDF_PAGES:
                break
            try:
                pages.append(page.get_text())
            except Exception:
                continue
        result.text = "\n".join(p for p in pages if p)[:MAX_TEXT_CHARS]
    finally:
        try:
            doc.close()
        except Exception:
            pass

    if not result.text.strip():
        # A scanned statement: no text layer. Reported rather than silently
        # treated as "not a statement", because the two need different actions.
        result.error = (f"'{filename}' contains no extractable text — it is most likely a "
                        "scanned image and needs OCR.")
    return result


def _read_csv(data: bytes, filename: str) -> DocumentContent:
    text = _decode(data)
    result = DocumentContent(kind=KIND_CSV, text=text[:MAX_TEXT_CHARS])
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    try:
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        rows = []
        for index, row in enumerate(reader):
            if index >= MAX_TABLE_ROWS:
                break
            cleaned = [(c or "").strip() for c in row]
            if any(cleaned):
                rows.append(cleaned)
        result.rows = rows
    except csv.Error as exc:
        result.error = f"'{filename}' is not readable as delimited text ({exc})"
    return result


def _read_text(data: bytes) -> DocumentContent:
    return DocumentContent(kind=KIND_TEXT, text=_decode(data)[:MAX_TEXT_CHARS])


def _read_html(data: bytes, filename: str) -> DocumentContent:
    from app.statements.html_text import html_to_text, html_tables

    markup = _decode(data)
    tables = html_tables(markup)
    rows: List[List[str]] = []
    for table in tables:
        if len(table) > len(rows):
            rows = table
    return DocumentContent(kind=KIND_HTML,
                           text=html_to_text(markup)[:MAX_TEXT_CHARS],
                           rows=rows[:MAX_TABLE_ROWS])


def _read_xlsx(data: bytes, filename: str) -> DocumentContent:
    result = DocumentContent(kind=KIND_XLSX)
    try:
        import openpyxl
    except ImportError:  # pragma: no cover
        result.error = "openpyxl is not installed"
        return result
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        message = str(exc).lower()
        if "encrypt" in message or "password" in message:
            result.is_encrypted = True
            result.password_required = True
            result.error = f"'{filename}' is a password-protected workbook."
        else:
            result.error = f"'{filename}' could not be opened as a workbook ({exc})"
        return result

    rows: List[List[str]] = []
    try:
        for sheet in workbook.worksheets:
            for row in sheet.iter_rows(values_only=True):
                cleaned = ["" if v is None else str(v).strip() for v in row]
                if any(cleaned):
                    rows.append(cleaned)
                if len(rows) >= MAX_TABLE_ROWS:
                    break
            if len(rows) >= MAX_TABLE_ROWS:
                break
    finally:
        try:
            workbook.close()
        except Exception:
            pass

    result.rows = rows
    result.text = "\n".join("\t".join(r) for r in rows)[:MAX_TEXT_CHARS]
    return result


def _read_xls(path: str, data: bytes, filename: str) -> DocumentContent:
    """Legacy .xls via pandas, which routes to whichever engine is installed."""
    result = DocumentContent(kind=KIND_XLS)
    try:
        import pandas as pd
    except ImportError:  # pragma: no cover
        result.error = "pandas is not installed"
        return result
    try:
        sheets = pd.read_excel(io.BytesIO(data), sheet_name=None, header=None, dtype=str)
    except Exception as exc:
        result.error = (f"'{filename}' could not be read as a legacy Excel workbook ({exc}). "
                        "Installing `xlrd` adds support for this format.")
        return result

    rows: List[List[str]] = []
    for frame in sheets.values():
        for record in frame.fillna("").astype(str).values.tolist():
            cleaned = [str(v).strip() for v in record]
            if any(cleaned):
                rows.append(cleaned)
            if len(rows) >= MAX_TABLE_ROWS:
                break
        if len(rows) >= MAX_TABLE_ROWS:
            break

    result.rows = rows
    result.text = "\n".join("\t".join(r) for r in rows)[:MAX_TEXT_CHARS]
    return result
