"""Content-addressed file storage and upload validation.

05_SECURITY.md §10.4 and §10.5, and 01_REQUIREMENTS.md § Evidence Document
Ingestion. Three controls, each here for a stated reason:

* **Content-type inspection, not extension.** A `.exe` renamed to `.pdf` must be
  rejected. The declared MIME type and the filename are both attacker-controlled,
  so neither is consulted for the decision — only the file's own magic bytes.
* **Size limit enforced while streaming.** Reading a 25MB-capped upload fully
  into memory before checking its length would let a 2GB upload exhaust the
  server first.
* **Content-addressed paths.** The stored path is derived entirely from the
  SHA-256 of the content, so a hostile filename cannot influence where bytes
  land. Path traversal is not filtered out of the filename — the filename is
  simply never used to build a path.

The original filename is still recorded, because it is evidence about the
evidence, but it is sanitised before storage and never used for I/O.
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from pathlib import Path
from typing import BinaryIO, NamedTuple

from app.config.settings import settings
from app.errors import PayloadTooLargeError, UnsupportedFileTypeError

logger = logging.getLogger(__name__)

# 01_REQUIREMENTS.md § Inputs: PDF, DOCX, XLSX, PNG, JPG.
ALLOWED_MIME_TYPES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "image/png",
        "image/jpeg",
    }
)

# Magic-byte signatures. DOCX and XLSX are both ZIP containers, so they share a
# signature and are told apart by inspecting the archive's contents below.
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"PK\x03\x04", "application/zip"),
)

_CHUNK = 64 * 1024


class StoredFile(NamedTuple):
    content_hash: str
    storage_path: str
    mime_type: str
    size_bytes: int
    original_filename: str


def sanitize_filename(filename: str) -> str:
    """Reduce a filename to something safe to store and display.

    Not used to build a path — see the module docstring — but a filename
    containing control characters or path separators would still be unpleasant
    in a report, a log line, or a Content-Disposition header.
    """
    name = unicodedata.normalize("NFKD", filename or "")
    name = name.replace("\\", "/").split("/")[-1]  # discard any directory part
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(". ")
    if not name:
        name = "unnamed"
    return name[:255]


def detect_mime_type(head: bytes, *, full_content: bytes | None = None) -> str | None:
    """Identify a file from its own bytes. Returns None if unrecognised.

    `full_content` is needed only to distinguish DOCX from XLSX, which requires
    reading the ZIP central directory rather than just the header.
    """
    for signature, mime in _SIGNATURES:
        if head.startswith(signature):
            if mime != "application/zip":
                return mime
            return _classify_ooxml(full_content) if full_content is not None else None
    return None


def _classify_ooxml(content: bytes) -> str | None:
    """Tell DOCX from XLSX from any other ZIP by what the archive contains.

    A plain ZIP — or a ZIP with an executable inside — resolves to None and is
    therefore rejected, which is the behaviour we want: the allow-list is of
    document formats, not of container formats.
    """
    import io
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = set(archive.namelist())
    except zipfile.BadZipFile:
        return None

    if "word/document.xml" in names:
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if "xl/workbook.xml" in names:
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return None


def read_and_validate(upload: BinaryIO, original_filename: str) -> tuple[bytes, str, str]:
    """Read an upload under the size cap and identify its true type.

    Returns (content, mime_type, sanitised filename). Raises before reading
    anything beyond the cap.
    """
    max_bytes = settings.max_upload_bytes
    buffer = bytearray()
    while True:
        chunk = upload.read(_CHUNK)
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > max_bytes:
            # Stop reading immediately rather than buffering the whole thing to
            # discover it was oversized.
            raise PayloadTooLargeError(f"File exceeds the {settings.MAX_UPLOAD_SIZE_MB}MB limit.")

    content = bytes(buffer)
    if not content:
        raise UnsupportedFileTypeError("The uploaded file is empty.")

    mime_type = detect_mime_type(content[:8], full_content=content)
    if mime_type is None or mime_type not in ALLOWED_MIME_TYPES:
        # The message names the accepted formats but not what was detected —
        # echoing a detected type back is a small oracle for probing the filter.
        raise UnsupportedFileTypeError(
            "Unsupported file type. Accepted formats: PDF, DOCX, XLSX, PNG, JPG."
        )

    return content, mime_type, sanitize_filename(original_filename)


def store(content: bytes, subdirectory: str = "evidence") -> tuple[str, str]:
    """Write content to its content-addressed location.

    Returns (content_hash, storage_path). Storing an identical file twice is a
    no-op that returns the same path — the bytes are already there and are
    immutable, so rewriting them would be pointless work at best.
    """
    digest = hashlib.sha256(content).hexdigest()
    root = Path(settings.FILE_STORAGE_PATH) / subdirectory
    # Two-level fan-out keeps directory sizes manageable on a small server.
    target = root / digest[:2] / digest[2:4] / digest
    target.parent.mkdir(parents=True, exist_ok=True)

    if not target.exists():
        # Write to a temporary name and rename, so a crash mid-write cannot
        # leave a truncated file at a path that claims to be that hash.
        staging = target.with_suffix(".partial")
        staging.write_bytes(content)
        staging.replace(target)

    return digest, str(target)


def read_stored(storage_path: str) -> bytes:
    """Read a stored file back.

    The path comes from a database column written by `store`, never from user
    input, so there is no traversal surface here — but the containment check
    below makes that structural rather than merely true today.
    """
    path = Path(storage_path).resolve()
    root = Path(settings.FILE_STORAGE_PATH).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Refusing to read a file outside the storage root.")
    return path.read_bytes()
