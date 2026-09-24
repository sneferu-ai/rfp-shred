"""Database-backed job queue (FR-006, DIS-7 — no Redis).

Workers claim jobs atomically with ``FOR UPDATE SKIP LOCKED`` on Postgres;
on SQLite (test-suite) the claim runs inside a transaction instead — the
semantics a single worker needs are preserved. Stale heartbeat re-queue uses
``pg_try_advisory_lock`` on Postgres and is a no-op lock on SQLite.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import Session

from app.core.models import (
    AuditDrop,
    ExtractionRun,
    FilteredLine,
    Job,
    RawModelOutput,
    Requirement,
    Rfp,
)

STALE_HEARTBEAT_S = 600  # ten minutes
MAX_ATTEMPTS = 3
MAX_RETRIES_CAUSE = "max retries exceeded — worker repeatedly died on this document"
PRIORITY_USER = 10
PRIORITY_SWEEP = 1
PRIORITY_CAPPED = 0


def enqueue(session: Session, rfp_id: uuid.UUID | None, priority: int) -> Job:
    job = Job(rfp_id=rfp_id, priority=priority, status="queued")
    session.add(job)
    session.flush()
    return job


def claim_next_job(session: Session, worker_id: str) -> Job | None:
    """Atomically claim the highest-priority queued job (FR-006 SQL)."""
    is_pg = session.get_bind().dialect.name == "postgresql"
    # priority 0 = parked by the FR-039 per-account cap: never claimed.
    stmt = select(Job).where(Job.status == "queued", Job.priority > 0).order_by(
        Job.priority.desc(), Job.enqueued_at.asc()
    ).limit(1)
    if is_pg:
        stmt = stmt.with_for_update(skip_locked=True)
    job = session.execute(stmt).scalars().first()
    if job is None:
        return None
    job.status = "processing"
    job.locked_at = datetime.now(timezone.utc)
    job.locked_by = worker_id
    job.started_at = datetime.now(timezone.utc)
    session.flush()
    return job


def complete_job(session: Session, job: Job, ok: bool, error: str | None = None) -> None:
    job.status = "completed" if ok else "failed"
    job.finished_at = datetime.now(timezone.utc)
    session.flush()


def try_advisory_lock(session: Session, name: str) -> bool:
    """pg_try_advisory_lock on Postgres; always-true no-op elsewhere."""
    if session.get_bind().dialect.name != "postgresql":
        return True
    row = session.execute(
        text("SELECT pg_try_advisory_lock(hashtextextended(:name, 0))"), {"name": name}
    ).scalar()
    return bool(row)


def advisory_unlock(session: Session, name: str) -> None:
    if session.get_bind().dialect.name != "postgresql":
        return
    session.execute(
        text("SELECT pg_advisory_unlock(hashtextextended(:name, 0))"), {"name": name}
    )


def requeue_stale_runs(session: Session, stale_after_s: int = STALE_HEARTBEAT_S) -> list[str]:
    """Re-queue runs whose heartbeat went silent (FR-006).

    For each stale run: under an advisory lock, in ONE transaction — reset the
    jobs row, increment ``extraction_runs.attempt``, and clean partial output.
    After MAX_ATTEMPTS the run is failed with the spec cause string and the
    file is retained under the 7-day failure-retention rule.
    Returns a list of human-readable actions taken.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_after_s)
    stale = session.execute(
        select(ExtractionRun, Rfp)
        .join(Rfp, Rfp.id == ExtractionRun.rfp_id)
        .where(ExtractionRun.stage.in_(["reading", "extracting", "auditing"]))
        .where(ExtractionRun.heartbeat_at < cutoff)
    ).all()
    actions: list[str] = []
    for run, rfp in stale:
        if not try_advisory_lock(session, str(run.id)):
            actions.append(f"run {run.id}: advisory lock held elsewhere, skipped")
            continue
        try:
            if run.attempt >= MAX_ATTEMPTS:
                run.stage = "failed"
                run.error = MAX_RETRIES_CAUSE
                rfp.state = "failed"
                session.execute(
                    update(Job)
                    .where(Job.rfp_id == rfp.id, Job.status.in_(["queued", "processing"]))
                    .values(status="failed", finished_at=datetime.now(timezone.utc))
                )
                session.flush()
                actions.append(f"run {run.id}: {MAX_RETRIES_CAUSE}")
                continue
            # cleanup partial output + reset queue row + increment attempt,
            # all in this one transaction.
            session.execute(
                delete(Requirement).where(
                    Requirement.rfp_id == rfp.id, Requirement.verified_at.is_(None)
                )
            )
            session.execute(delete(FilteredLine).where(FilteredLine.rfp_id == rfp.id))
            session.execute(delete(AuditDrop).where(AuditDrop.rfp_id == rfp.id))
            session.execute(
                delete(RawModelOutput).where(RawModelOutput.extraction_run_id == run.id)
            )
            run.attempt = run.attempt + 1
            run.heartbeat_at = datetime.now(timezone.utc)
            rfp.state = "received"
            job = session.execute(
                select(Job).where(
                    Job.rfp_id == rfp.id, Job.status.in_(["queued", "processing", "failed"])
                )
            ).scalars().first()
            if job is None:
                enqueue(session, rfp.id, PRIORITY_USER if rfp.origin == "user" else PRIORITY_SWEEP)
            else:
                job.status = "queued"
                job.locked_at = None
                job.locked_by = None
                job.started_at = None
                job.finished_at = None
            session.flush()
            actions.append(f"run {run.id}: re-queued (attempt {run.attempt})")
        finally:
            advisory_unlock(session, str(run.id))
    return actions
