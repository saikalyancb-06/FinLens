"""Getting an upload onto disk without trusting any part of it.

Three things go wrong in the existing upload path, and this module exists to
not repeat them.

**It reads the whole file into memory before checking the size.**
`save_uploaded_file` does `file.file.read()` and *then* compares the length to
the cap. A 500 MB upload against a 50 MB limit is still 500 MB of resident
memory per concurrent request before the rejection — the limit protects disk,
not the process. Here the file is consumed in chunks and abandoned the moment
the cap is passed, so an oversized upload costs one chunk of memory and however
many bytes arrived before the abort.

**It accepts `.zip` and then fails obscurely.** `.zip` is in
`ALLOWED_EXTENSIONS`, passes content validation as a PK container, is written
to disk, and is then handed to a pipeline that dispatches on extension and
raises `Unsupported file format: '.zip'` from three layers down. The client
gets a 500-shaped failure for a file the API said it would take. Archives are
rejected here, at intake, with a 415 and a sentence saying why.

**Filenames are used with only partial sanitisation.** The stored name here is
never derived from the client's at all: it is a generated UUID inside a
per-request directory. The original is kept as a sanitised basename for
logging and error messages only, so a crafted name cannot reach the filesystem
even if a later change starts using it.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from app.b2b.detect import F_ZIP, detect_format
from app.b2b.errors import (
    ApiError,
    FILE_EMPTY,
    FILE_TOO_LARGE,
    MISSING_FILE,
    UNSUPPORTED_FILE_FORMAT,
)

logger = logging.getLogger(__name__)

#: Streamed in 1 MB slices: large enough that syscall overhead is irrelevant,
#: small enough that the memory cost of an abort is bounded by it.
CHUNK_BYTES = 1024 * 1024

#: How much of the head is retained for content detection. A CAMT GrpHdr or an
#: OFX SGML header fits comfortably; magic bytes need the first eight.
HEAD_BYTES = 8192

#: Anything outside this set is stripped from the remembered filename.
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._ -]")
_MAX_NAME_LEN = 180


@dataclass
class IngestedFile:
    """An upload that is on disk, measured, and safe to hand to a parser."""

    path: str
    filename: str            # sanitised basename, for logs and messages only
    size_bytes: int
    sha256: str
    head_bytes: bytes
    tmp_dir: str = ""        # the per-request directory `cleanup` removes
    detected: Any = None     # DetectedFormat, filled in by save_upload

    @property
    def extension(self) -> str:
        return os.path.splitext(self.filename)[1].lower()


def sanitise_filename(raw: Optional[str]) -> str:
    """Reduce a client-supplied name to a harmless basename.

    Path separators of both flavours are cut, `..` segments are removed, and
    the result is truncated. A name that sanitises to nothing becomes
    `upload`, because an empty filename in a log line is worse than a
    placeholder.
    """
    name = (raw or "").strip()
    # Windows clients send full paths; os.path.basename alone does not split on
    # a backslash when the server is POSIX.
    name = name.replace("\\", "/").split("/")[-1]
    name = name.replace("..", "")
    name = _UNSAFE_NAME_CHARS.sub("_", name).strip(". ")
    if len(name) > _MAX_NAME_LEN:
        stem, ext = os.path.splitext(name)
        name = stem[: _MAX_NAME_LEN - len(ext)] + ext
    return name or "upload"


def _reader(upload_file: Any):
    """A `read(n)` callable for a FastAPI UploadFile or a plain file object."""
    stream = getattr(upload_file, "file", None) or upload_file
    if not hasattr(stream, "read"):
        raise ApiError(MISSING_FILE, "no file was provided in the request")
    try:
        stream.seek(0)
    except (OSError, AttributeError, ValueError):
        # A non-seekable stream is fine; it simply has not been read yet.
        pass
    return stream.read


def save_upload(upload_file: Any, *, max_bytes: int, tmp_dir: str,
                allow_archive: bool = False) -> IngestedFile:
    """Stream an upload into a fresh temp directory, or fail before it fills one.

    The size check happens *during* the copy, not after it: as soon as the
    bytes written exceed `max_bytes` the partial file is deleted and
    `FILE_TOO_LARGE` is raised, so the process never holds more than one chunk
    plus what has already been written to disk.
    """
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")

    read = _reader(upload_file)
    filename = sanitise_filename(getattr(upload_file, "filename", None))
    extension = os.path.splitext(filename)[1].lower()

    request_dir = os.path.join(tmp_dir, uuid.uuid4().hex)
    os.makedirs(request_dir, exist_ok=True)
    # The stored name is generated, never derived from the client's. The
    # extension is carried across only as a hint for detection, which does not
    # trust it either.
    target = os.path.join(request_dir, f"{uuid.uuid4().hex}{extension}")

    digest = hashlib.sha256()
    head = b""
    written = 0

    try:
        with open(target, "wb") as handle:
            while True:
                chunk = read(CHUNK_BYTES)
                if not chunk:
                    break
                if isinstance(chunk, str):      # a text-mode stream
                    chunk = chunk.encode("utf-8", errors="replace")
                written += len(chunk)
                if written > max_bytes:
                    raise ApiError(
                        FILE_TOO_LARGE,
                        f"the file exceeds the {max_bytes // (1024 * 1024)} MB limit "
                        f"for this account",
                        detail={"max_bytes": max_bytes},
                    )
                if len(head) < HEAD_BYTES:
                    head += chunk[: HEAD_BYTES - len(head)]
                digest.update(chunk)
                handle.write(chunk)
    except ApiError:
        shutil.rmtree(request_dir, ignore_errors=True)
        raise
    except OSError as exc:
        shutil.rmtree(request_dir, ignore_errors=True)
        logger.error("[b2b.ingest] could not store upload %r: %s", filename, exc)
        raise ApiError(MISSING_FILE, "the uploaded file could not be stored")

    if written == 0:
        shutil.rmtree(request_dir, ignore_errors=True)
        raise ApiError(FILE_EMPTY, "the uploaded file is empty")

    ingested = IngestedFile(
        path=target,
        filename=filename,
        size_bytes=written,
        sha256=digest.hexdigest(),
        head_bytes=head,
        tmp_dir=request_dir,
    )

    detected = detect_format(filename, head, full_path=target)
    ingested.detected = detected

    if detected.format == F_ZIP and not allow_archive:
        # Refused at intake rather than at dispatch. The legacy allowlist takes
        # .zip and fails several layers deeper with a message the client cannot
        # act on; this is a 415 with an instruction.
        cleanup(ingested)
        raise ApiError(
            UNSUPPORTED_FILE_FORMAT,
            "archives are not accepted; upload one statement file per request "
            "rather than a .zip",
        )

    logger.info(
        "[b2b.ingest] stored %s (%d bytes, sha256=%s..., detected=%s%s)",
        filename, written, ingested.sha256[:12], detected.format,
        ", extension mismatch" if detected.mismatch else "",
    )
    return ingested


def cleanup(ingested: Optional[IngestedFile]) -> None:
    """Remove the per-request directory. Safe to call twice, or after a failure."""
    if ingested is None:
        return
    directory = ingested.tmp_dir or os.path.dirname(ingested.path or "")
    if not directory:
        return
    shutil.rmtree(directory, ignore_errors=True)
