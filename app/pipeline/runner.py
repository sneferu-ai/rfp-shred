"""Pipeline runner: drives one extraction run through its stages.

received -> reading -> extracting -> auditing -> ready (or failed).

The page-map function and model client are injectable so the suite can run
the loop against synthetic page maps and the deterministic mock adapter;
production uses pdfplumber/ocrmypdf + the configured provider adapter.
"""

from __future__ import annotations

import logging
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.model_client import (
    ModelClient,
    ProviderUnavailable,
    estimate_cost_usd,
    get_model_client,
)
from app.core.models import ExtractionRun, InferenceLog, RetentionLog, Rfp
from app.pipeline import pagemap as pagemap_mod
from app.pipeline.audit import audit_items, locate_source_pages
from app.pipeline.chunking import ChunkCapExceeded, build_chunks
from app.pipeline.filter import split_requirements
from app.pipeline.intake_files import validate_zip_bytes
from app.pipeline.mine import mine_chunks
from app.pipeline.pagemap import PageMap, PipelineError
from app.pipeline.transform import persist_mine_result

log = logging.getLogger(__name__)

PagemapFn = Callable[[str | Path], PageMap]


class ExtractionFailed(RuntimeError):
    """The run fails with ``cause`` recorded on the run + rfp (FR-005/006)."""

    def __init__(self, cause: str) -> None:
        super().__init__(cause)
        self.cause = cause


def _touch(run: ExtractionRun) -> None:
    run.heartbeat_at = datetime.now(timezone.utc)


def _default_pagemap(path: str | Path) -> PageMap:
    return pagemap_mod.build_page_map(path)


def _prepare_processing_pdf(
    session: Session,
    settings: Settings,
    rfp: Rfp,
    run: ExtractionRun,
    work_dir: Path,
) -> Path:
    """Resolve the single processing PDF for any intake shape (FR-041):

    - .pdf  -> the upload itself
    - .docx -> LibreOffice conversion (advisory-lock serialized)
    - .zip  -> entries extracted at intake are re-read from the stored zip,
               DOCX entries converted, then everything merged via pypdf in
               filename-sorted order into one processing PDF (merge logged
               to retention_log; merge order limitation is A-012).
    """
    stored = Path(rfp.stored_path or "")
    suffix = stored.suffix.lower()
    if suffix == ".pdf":
        return stored
    if suffix == ".docx":
        return pagemap_mod.convert_docx_to_pdf(stored, work_dir, session)
    if suffix == ".zip":
        from pypdf import PdfWriter

        data = stored.read_bytes()
        entries = validate_zip_bytes(
            data, max_entries=settings.max_zip_entries, max_upload_bytes=settings.max_upload_bytes
        )
        writer = PdfWriter()
        for entry in sorted(entries, key=lambda e: e.name.lower()):
            if entry.kind == "pdf":
                src = work_dir / f"entry-{entry.name}"
                src.write_bytes(entry.data)
                writer.append(str(src))
            else:  # docx -> convert first
                src = work_dir / f"entry-{entry.name}"
                src.write_bytes(entry.data)
                converted = pagemap_mod.convert_docx_to_pdf(src, work_dir, session)
                writer.append(str(converted))
        merged = work_dir / "merged.pdf"
        with open(merged, "wb") as fh:
            writer.write(fh)
        session.add(RetentionLog(rfp_id=rfp.id, action="zip_merged"))
        session.flush()
        return merged
    raise ExtractionFailed(f"unsupported stored file type: {suffix or '(none)'}")


def run_extraction(
    session: Session,
    settings: Settings,
    rfp: Rfp,
    run: ExtractionRun,
    *,
    model_client: ModelClient | None = None,
    pagemap_fn: PagemapFn | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> int:
    """Run the full extraction. Returns the number of surviving requirement
    rows. On failure raises ExtractionFailed (caller records the cause)."""
    client = model_client or get_model_client(settings)
    pmap_fn = pagemap_fn or _default_pagemap

    # reading
    run.stage = "reading"
    rfp.state = "reading"
    _touch(run)
    session.flush()

    with tempfile.TemporaryDirectory(dir=settings.files_dir) as tmp:
        work_dir = Path(tmp)
        try:
            processing_pdf = _prepare_processing_pdf(session, settings, rfp, run, work_dir)
        except (PipelineError, ExtractionFailed) as exc:
            raise ExtractionFailed(str(exc)) from exc

        try:
            page_map = pmap_fn(processing_pdf)
        except PipelineError as exc:
            raise ExtractionFailed(str(exc)) from exc
        except Exception as exc:  # tool errors fail the run with cause (FR-005)
            raise ExtractionFailed(f"page-map build failed: {exc}") from exc

    rfp.page_count = page_map.page_count
    if not page_map.pages or not any(t.strip() for t in page_map.pages.values()):
        # zero text at all -> still lands in ready with the empty state
        _finish_ready(session, rfp, run)
        return 0

    # extracting
    run.stage = "extracting"
    rfp.state = "extracting"
    _touch(run)
    session.flush()

    try:
        chunks = build_chunks(page_map.pages)
    except ChunkCapExceeded as exc:
        raise ExtractionFailed(str(exc)) from exc

    try:
        mine = mine_chunks(
            client, chunks, max_tokens=settings.model_max_tokens, sleep_fn=sleep_fn
        )
    except ProviderUnavailable as exc:
        raise ExtractionFailed(f"model provider error: {exc}") from exc
    if mine.failed_chunks:
        raise ExtractionFailed(
            "model extraction was incomplete: "
            f"{mine.failed_chunks} chunk{'s' if mine.failed_chunks != 1 else ''} failed; "
            "no partial compliance matrix was published"
        )

    # FR-039: per-call cost logging
    for call in mine.calls:
        session.add(
            InferenceLog(
                account_id=rfp.account_id,
                rfp_id=rfp.id,
                tokens_in=call.tokens_in,
                tokens_out=call.tokens_out,
                estimated_cost_usd=estimate_cost_usd(settings, call.tokens_in, call.tokens_out),
            )
        )
    session.flush()

    # auditing (noise filtering is part of the auditing stage)
    run.stage = "auditing"
    rfp.state = "auditing"
    _touch(run)
    session.flush()

    filtered = split_requirements(mine.items)
    binding = locate_source_pages(list(filtered.requirements), page_map.pages)
    audit = audit_items(binding, page_map.pages)
    outcome = persist_mine_result(
        session, rfp_id=rfp.id, run=run, mine=mine, audit=audit, filtered=filtered
    )
    log.info(
        "rfp %s: %d requirements, %d filtered, %d audit-dropped, %d raw outputs",
        rfp.id, outcome.requirements, outcome.filtered_lines,
        outcome.audit_drops, outcome.raw_outputs,
    )
    _finish_ready(session, rfp, run)
    return outcome.requirements


def _finish_ready(session: Session, rfp: Rfp, run: ExtractionRun) -> None:
    run.stage = "ready"
    rfp.state = "ready"
    # final heartbeat doubles as the ready timestamp: the FR-020 24-hour
    # grace clock runs from this moment.
    _touch(run)
    session.flush()
