"""Unpack a ZIP of statements safely.

Credit Lens receive a borrower's documents as one archive (the sample arrived
as `Creditlens.zip`: case folders, statements, a CAM sheet). The consolidation
endpoint therefore accepts a ZIP and treats every statement-like file inside as
part of the batch. Everything else (spreadsheets that turn out not to be
statements are reported per file, not fatal).

Defences, because an archive is attacker-shaped input:
  * member names are never used as paths — each is written under a generated
    name, so `../../etc/passwd` and absolute paths go nowhere;
  * member count, per-member size and total uncompressed size are capped, and
    the uncompressed size is counted while reading, not trusted from the
    header (zip bombs lie in the header);
  * nested archives are not opened.
"""
from __future__ import annotations

import os
import uuid
import zipfile
from typing import List, Tuple

from app.b2b import errors
from app.b2b.errors import ApiError

STATEMENT_EXTENSIONS = {".pdf", ".csv", ".tsv", ".txt", ".xlsx", ".xlsm", ".xls",
                        ".json", ".ofx", ".qfx", ".xml"}
MAX_MEMBERS = 200


def extract_zip(zip_path: str, out_dir: str, *, max_member_bytes: int,
                max_total_bytes: int) -> List[Tuple[str, str]]:
    """Return [(path on disk, display name)] for statement-like members."""
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise ApiError(errors.FILE_CORRUPT, "The ZIP archive could not be opened.")
    out: List[Tuple[str, str]] = []
    total = 0
    with zf:
        members = [m for m in zf.infolist() if not m.is_dir()]
        if len(members) > MAX_MEMBERS:
            raise ApiError(errors.INVALID_PARAMETER,
                           f"The archive holds more than {MAX_MEMBERS} files.")
        for m in members:
            name = m.filename.replace("\\", "/")
            base = os.path.basename(name)
            if not base or base.startswith(".") or "__MACOSX" in name:
                continue
            ext = os.path.splitext(base)[1].lower()
            if ext not in STATEMENT_EXTENSIONS:
                continue
            if m.flag_bits & 0x1:
                raise ApiError(errors.INVALID_PARAMETER,
                               "Encrypted ZIP archives are not supported; send the files unzipped.")
            target = os.path.join(out_dir, f"{uuid.uuid4().hex}{ext}")
            written = 0
            with zf.open(m) as src, open(target, "wb") as dst:
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    written += len(chunk)
                    total += len(chunk)
                    if written > max_member_bytes:
                        raise ApiError(errors.FILE_TOO_LARGE,
                                       f"'{base}' in the archive exceeds the per-file limit.")
                    if total > max_total_bytes:
                        raise ApiError(errors.FILE_TOO_LARGE,
                                       "The archive's contents exceed the total size limit.")
                    dst.write(chunk)
            out.append((target, name))
    if not out:
        raise ApiError(errors.NO_TRANSACTIONS_FOUND,
                       "The archive contains no statement files (PDF, Excel, CSV, JSON, OFX, CAMT).")
    return out
