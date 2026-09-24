"""Intake file validation (FR-004, FR-041) — shared by the web intake and
the sweep attachment validator (FR-022).

Uploads are verified by header signature, never by extension. Content-Length
is checked by the route before the body streams (early 413); everything here
operates on already-spooled bytes. Nothing is persisted on rejection.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import dataclass

PDF_MAGIC = b"%PDF"
ZIP_MAGIC = b"PK\x03\x04"
BOMB_RATIO = 100.0


class IntakeRejection(RuntimeError):
    """Maps to an HTTP rejection (413/415/422). ``status`` + ``detail`` are
    surfaced verbatim; no bytes are kept."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass
class ZipEntry:
    name: str
    kind: str  # "pdf" | "docx"
    data: bytes


def content_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sniff_type(data: bytes) -> str | None:
    """Header-signature sniffing over the FULL upload bytes: 'pdf' |
    'zip' | 'docx' | None. (ZIP structure — central directory — lives at
    the file tail, so a head slice is not parseable.)"""
    if data.startswith(PDF_MAGIC):
        return "pdf"
    if data.startswith(ZIP_MAGIC):
        # DOCX is a ZIP container: distinguish by its content types part.
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = set(zf.namelist())
                if "word/document.xml" in names or "[Content_Types].xml" in names:
                    return "docx"
        except zipfile.BadZipFile:
            return None
        return "zip"
    return None


def probe_encrypted_pdf(path: str) -> None:
    """Raise IntakeRejection(422) for PDFs that resist an empty-password
    open (FR-004). Empty-owner-password files open and process (AC-033a)."""
    try:
        import pikepdf  # production path

        try:
            with pikepdf.open(path, password=""):
                return
        except pikepdf.PasswordError:
            raise IntakeRejection(422, "This PDF is encrypted and cannot be processed")
        except pikepdf.PdfError as exc:
            raise IntakeRejection(422, f"This PDF could not be read: {exc}") from exc
    except ImportError:
        pass
    from pypdf import PdfReader

    try:
        reader = PdfReader(path)
        if reader.is_encrypted:
            outcome = reader.decrypt("")
            if outcome == 0:
                raise IntakeRejection(422, "This PDF is encrypted and cannot be processed")
    except IntakeRejection:
        raise
    except Exception as exc:
        raise IntakeRejection(422, f"This PDF could not be read: {exc}") from exc


def validate_zip_bytes(
    data: bytes, *, max_entries: int, max_upload_bytes: int
) -> list[ZipEntry]:
    """FR-041 intake checks: entry type, entry count, decompression bomb,
    encrypted entries. Returns extracted PDF/DOCX entries (filename-sorted
    merge happens in the worker)."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise IntakeRejection(422, "This ZIP file could not be read")
    infos = [
        i
        for i in zf.infolist()
        if not i.is_dir() and not i.filename.startswith("__MACOSX")
    ]
    if len(infos) > max_entries:
        raise IntakeRejection(422, f"ZIP contains too many files (max {max_entries})")
    total_uncompressed = sum(i.file_size for i in infos)
    if total_uncompressed > max_upload_bytes * 5:
        raise IntakeRejection(422, "ZIP expands beyond the allowed size")
    compressed = max(1, sum(max(1, i.compress_size) for i in infos))
    if total_uncompressed / compressed > BOMB_RATIO:
        raise IntakeRejection(422, "ZIP rejected: decompression bomb check failed")
    entries: list[ZipEntry] = []
    for info in infos:
        if info.flag_bits & 0x1:
            raise IntakeRejection(422, "ZIP contains encrypted entries")
        lower = info.filename.lower()
        if lower.endswith(".pdf"):
            kind = "pdf"
        elif lower.endswith(".docx"):
            kind = "docx"
        else:
            raise IntakeRejection(422, "ZIP must contain only PDF or DOCX files")
        payload = zf.read(info)
        entries.append(ZipEntry(name=info.filename, kind=kind, data=payload))
    if not entries:
        raise IntakeRejection(422, "ZIP contains no PDF or DOCX files")
    return entries
