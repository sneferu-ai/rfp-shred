"""Scheduled maintenance tasks driven by the sweep container's cron loop:

- FR-020 source lifecycle: 24-hour grace destruction after ``ready``;
  7-day TTL on abandoned failures (sets rfps.state='expired'); opt-in
  retention exempt. Matrix rows always survive.
- FR-039: re-prioritize parked (priority 0) jobs once the account is under
  its monthly cap again.
- SAM.gov cached notices older than 30 days are purged (section 5 retention).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core import usage as usage_mod
from app.core.models import ExtractionRun, Job, RetentionLog, Rfp, SamNotice
from app.core.queue import PRIORITY_SWEEP, PRIORITY_USER

log = logging.getLogger(__name__)

GRACE_HOURS = 24
FAILED_TTL_DAYS = 7
SAM_NOTICE_TTL_DAYS = 30


def _destroy_file(rfp: Rfp) -> bool:
    if not rfp.stored_path:
        return False
    path = Path(rfp.stored_path)
    try:
        if path.exists():
            path.unlink()
    except OSError:
        log.warning("could not destroy source file %s", path)
        return False
    rfp.stored_path = None
    return True


def _ready_clock(session: Session, rfp: Rfp) -> datetime | None:
    """The FR-020 grace clock: the final heartbeat of the ready run (the
    ready timestamp), or — for matrices that reached ready WITHOUT an
    extraction run (the FR-033 clone path) — the upload's created_at."""
    from app.core.models import as_aware

    run = session.execute(
        select(ExtractionRun)
        .where(ExtractionRun.rfp_id == rfp.id, ExtractionRun.stage == "ready")
        .order_by(ExtractionRun.heartbeat_at.desc())
    ).scalars().first()
    if run is not None:
        return as_aware(run.heartbeat_at)
    return as_aware(rfp.created_at)


def enforce_source_retention(session: Session, now: datetime | None = None) -> list[str]:
    """FR-020. Returns actions taken (observable in tests + logs)."""
    now = now or datetime.now(timezone.utc)
    actions: list[str] = []
    grace_cutoff = now - timedelta(hours=GRACE_HOURS)
    # 24h grace after ready for non-retained files
    candidates = session.execute(
        select(Rfp)
        .where(Rfp.retain_source.is_(False))
        .where(Rfp.stored_path.is_not(None))
        .where(Rfp.state.in_(["ready", "unlocked", "cleared", "exported"]))
    ).scalars().all()
    for rfp in candidates:
        clock = _ready_clock(session, rfp)
        if clock is not None and clock < grace_cutoff and _destroy_file(rfp):
            session.add(RetentionLog(rfp_id=rfp.id, action="destroyed_after_24h_grace"))
            actions.append(f"rfp {rfp.id}: destroyed after 24h grace")
    # 7-day TTL on abandoned failures
    failed_cutoff = now - timedelta(days=FAILED_TTL_DAYS)
    failed = session.execute(
        select(Rfp)
        .where(Rfp.state == "failed")
        .where(Rfp.retain_source.is_(False))
        .where(Rfp.created_at < failed_cutoff)
    ).scalars().all()
    for rfp in failed:
        destroyed = _destroy_file(rfp)
        rfp.state = "expired"
        session.add(RetentionLog(rfp_id=rfp.id, action="expired_failed_upload"))
        actions.append(f"rfp {rfp.id}: expired abandoned failure (file_destroyed={destroyed})")
    session.flush()
    return actions


def reprioritize_capped_jobs(session: Session, cap_usd: float) -> list[str]:
    """Un-park priority-0 jobs for accounts now under their cap (month
    reset). Returns actions taken."""
    actions: list[str] = []
    parked = session.execute(
        select(Job, Rfp)
        .join(Rfp, Rfp.id == Job.rfp_id)
        .where(Job.status == "queued", Job.priority == 0)
    ).all()
    for job, rfp in parked:
        if not usage_mod.is_over_cap(session, rfp.account_id, cap_usd):
            job.priority = PRIORITY_SWEEP if rfp.origin == "sweep" else PRIORITY_USER
            actions.append(f"job {job.id}: un-parked (priority {job.priority})")
    session.flush()
    return actions


def purge_old_sam_notices(session: Session, now: datetime | None = None) -> int:
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=SAM_NOTICE_TTL_DAYS)
    old = session.execute(
        select(SamNotice).where(SamNotice.posted_at < cutoff)
    ).scalars().all()
    count = 0
    for notice in old:
        session.delete(notice)
        count += 1
    session.flush()
    return count


def run_daily_maintenance(session: Session, cap_usd: float) -> dict[str, list[str] | int]:
    """CLEANUP_CRON entrypoint."""
    retention = enforce_source_retention(session)
    unparked = reprioritize_capped_jobs(session, cap_usd)
    purged = purge_old_sam_notices(session)
    return {"retention": retention, "unparked": unparked, "sam_notices_purged": purged}
