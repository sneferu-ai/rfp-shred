"""Page-map build (FR-005).

Per-page text via pdfplumber (falling back to pypdf). Pages with fewer than
50 extractable characters are flagged for OCR; all flagged pages are
extracted into a temporary sub-PDF, OCR'd in a single ``ocrmypdf`` pass, and
merged back — OCR'd text is mapped to the ORIGINAL page numbers via the
``sub_pdf_page -> original_page`` mapping, never the sub-PDF's 1..N.

DOCX converts to PDF through headless LibreOffice serialized by a Postgres
advisory lock (OBL-28), so citations match the rendered PDF's pagination.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.queue import advisory_unlock, try_advisory_lock

OCR_CHAR_THRESHOLD = 50
OCR_FAILED_THRESHOLD = 10
LIBREOFFICE_LOCK = "libreoffice_conversion"


class PipelineError(RuntimeError):
    """Tool-level failure; fails the run with a cause (FR-005)."""


@dataclass
class PageMap:
    pages: dict[int, str]
    page_count: int
    ocr_flagged: list[int] = field(default_factory=list)
    ocr_failed: list[int] = field(default_factory=list)


def extract_pages_pdfplumber(pdf_path: str | Path) -> dict[int, str]:
    import pdfplumber  # lazy: production dependency

    pages: dict[int, str] = {}
    with pdfplumber.open(str(pdf_path)) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            pages[i] = page.extract_text() or ""
    return pages


def extract_pages_pypdf(pdf_path: str | Path) -> dict[int, str]:
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    pages: dict[int, str] = {}
    for i, page in enumerate(reader.pages, start=1):
        try:
            pages[i] = page.extract_text() or ""
        except Exception:
            pages[i] = ""
    return pages


def extract_pages(pdf_path: str | Path) -> dict[int, str]:
    try:
        return extract_pages_pdfplumber(pdf_path)
    except ImportError:
        return extract_pages_pypdf(pdf_path)


def ocr_subpdf(pdf_path: str | Path, flagged_pages: list[int]) -> dict[int, str]:
    """One ocrmypdf pass over a sub-PDF of the flagged pages.

    Returns {original_page: ocr_text}. Raises PipelineError when the tool is
    unavailable so callers can decide (pages counted as OCR-failed).
    """
    from pypdf import PdfReader, PdfWriter

    if shutil.which("ocrmypdf") is None:
        raise PipelineError("ocrmypdf not available on this host")
    reader = PdfReader(str(pdf_path))
    writer = PdfWriter()
    for p in flagged_pages:
        writer.add_page(reader.pages[p - 1])
    with tempfile.TemporaryDirectory() as tmp:
        sub_in = Path(tmp) / "sub.pdf"
        sub_out = Path(tmp) / "sub_ocr.pdf"
        with open(sub_in, "wb") as fh:
            writer.write(fh)
        proc = subprocess.run(
            ["ocrmypdf", "--skip-text", str(sub_in), str(sub_out)],
            capture_output=True,
            timeout=1800,
        )
        if proc.returncode != 0:
            raise PipelineError(f"ocrmypdf failed: {proc.stderr.decode()[:300]}")
        ocr_pages = extract_pages(sub_out)
    # sub_pdf_page -> original_page mapping
    return {flagged_pages[i - 1]: text for i, text in ocr_pages.items() if i - 1 < len(flagged_pages)}


def build_page_map(pdf_path: str | Path, ocr_enabled: bool = True) -> PageMap:
    pages = extract_pages(pdf_path)
    page_count = len(pages)
    flagged = [p for p, text in pages.items() if len((text or "").strip()) < OCR_CHAR_THRESHOLD]
    ocr_failed: list[int] = []
    if flagged and ocr_enabled:
        try:
            ocr_text = ocr_subpdf(pdf_path, flagged)
        except PipelineError:
            ocr_text = {}
            ocr_failed.extend(flagged)
        for p in flagged:
            text = (ocr_text.get(p) or "").strip()
            if len(text) < OCR_FAILED_THRESHOLD:
                if p not in ocr_failed:
                    ocr_failed.append(p)
            else:
                pages[p] = text
    return PageMap(pages=pages, page_count=page_count, ocr_flagged=flagged, ocr_failed=ocr_failed)


def convert_docx_to_pdf(
    docx_path: str | Path,
    out_dir: str | Path,
    session: Session | None = None,
) -> Path:
    """Headless LibreOffice DOCX->PDF under the advisory lock (FR-005/OBL-28).

    The lock is cross-container safe on Postgres (both API and worker share
    the instance). Outside Postgres (tests) the lock is a no-op.
    """
    soffice = shutil.which("libreoffice") or shutil.which("soffice")
    if soffice is None:
        raise PipelineError("LibreOffice is not available on this host")
    got_lock = True
    if session is not None:
        got_lock = try_advisory_lock(session, LIBREOFFICE_LOCK)
        if not got_lock:
            raise PipelineError("could not acquire libreoffice_conversion lock")
    try:
        proc = subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(out_dir), str(docx_path)],
            capture_output=True,
            timeout=900,
        )
        if proc.returncode != 0:
            raise PipelineError(f"LibreOffice conversion failed: {proc.stderr.decode()[:300]}")
    finally:
        if session is not None and got_lock:
            advisory_unlock(session, LIBREOFFICE_LOCK)
    produced = Path(out_dir) / (Path(docx_path).stem + ".pdf")
    if not produced.exists():
        raise PipelineError("LibreOffice produced no output PDF")
    return produced
