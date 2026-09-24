"""Intake pipeline (FR-004, FR-033, FR-041, FR-042) as a testable service.

Rejection keeps zero bytes: the upload is spooled to a temp location,
validated, and only moved into FILES_DIR/incoming on acceptance.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import usage as usage_mod
from app.core.config import Settings
from app.core.models import Account, AuditDrop, ExtractionRun, FilteredLine, Requirement, Rfp
from app.core.queue import enqueue
from app.pipeline.intake_files import (
    IntakeRejection,
    content_sha256,
    probe_encrypted_pdf,
    sniff_type,
    validate_zip_bytes,
)


def process_upload(
    db: Session,
    settings: Settings,
    *,
    account: Account,
    filename: str,
    data: bytes,
    retain_source: bool,
    amends_rfp_id: uuid.UUID | None = None,
) -> Rfp:
    if len(data) > settings.max_upload_bytes:
        raise IntakeRejection(413, "File exceeds the maximum upload size")
    kind = sniff_type(data)
    if kind is None:
        raise IntakeRejection(415, "Unsupported file type — upload a PDF, DOCX, or ZIP")

    tmp_dir = Path(settings.files_dir) / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / f"{uuid.uuid4()}.{kind}"
    try:
        tmp_path.write_bytes(data)
        if kind == "pdf":
            probe_encrypted_pdf(str(tmp_path))
        if kind == "zip":
            validate_zip_bytes(
                data, max_entries=settings.max_zip_entries, max_upload_bytes=settings.max_upload_bytes
            )

        digest = content_sha256(data)
        prior = db.execute(
            select(Rfp)
            .where(Rfp.account_id == account.id, Rfp.content_hash == digest, Rfp.unlocked_at.is_not(None))
            .order_by(Rfp.created_at.desc())
        ).scalars().first()

        incoming = Path(settings.files_dir) / "incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        stored = incoming / f"{uuid.uuid4()}.{kind}"
        shutil.move(str(tmp_path), stored)

        rfp = Rfp(
            account_id=account.id,
            orig_name=filename,
            stored_path=str(stored),
            content_hash=digest,
            state="received",
            retain_source=retain_source,
            origin="user",
            amends_rfp_id=amends_rfp_id,
        )
        if prior is not None:
            # FR-033: same-account re-upload of an unlocked file inherits the
            # unlock — no payment.
            rfp.unlocked_at = prior.unlocked_at
        db.add(rfp)
        db.flush()

        if prior is not None and prior.state == "ready":
            # clone the finished matrix; no extraction runs (AC-022)
            _clone_matrix_rows(db, prior, rfp)
            rfp.state = "ready"
            db.flush()
            return rfp

        run = ExtractionRun(rfp_id=rfp.id, stage="reading")
        db.add(run)
        db.flush()
        priority = usage_mod.priority_for_new_job(
            db, account.id, "user", settings.monthly_inference_cap_usd
        )
        enqueue(db, rfp.id, priority)
        db.flush()
        return rfp
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _clone_matrix_rows(db: Session, prior: Rfp, new: Rfp) -> None:
    requirements = db.execute(
        select(Requirement).where(Requirement.rfp_id == prior.id).order_by(Requirement.seq)
    ).scalars().all()
    for row in requirements:
        db.add(
            Requirement(
                rfp_id=new.id, seq=row.seq, clause_id=row.clause_id, section=row.section,
                page=row.page, body=row.body, excerpt=row.excerpt, response="",
                verified_at=None, tags=list(row.tags or []), model_output_id=None,
            )
        )
    for line in db.execute(select(FilteredLine).where(FilteredLine.rfp_id == prior.id)).scalars():
        db.add(
            FilteredLine(
                rfp_id=new.id, seq=line.seq, page=line.page, text_preview=line.text_preview,
                full_text=line.full_text, filter_reason=line.filter_reason,
            )
        )
    for drop in db.execute(select(AuditDrop).where(AuditDrop.rfp_id == prior.id)).scalars():
        db.add(
            AuditDrop(
                rfp_id=new.id, seq=drop.seq, page=drop.page, excerpt=drop.excerpt,
                text=drop.text, audit_failure_reason=drop.audit_failure_reason,
            )
        )
    db.flush()
