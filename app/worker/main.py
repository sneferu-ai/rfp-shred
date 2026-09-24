"""Worker (FR-006): polls the jobs table and processes extraction jobs.

- claims jobs atomically (FOR UPDATE SKIP LOCKED on Postgres)
- spawns a heartbeat daemon thread with a dedicated SQLAlchemy engine using
  NullPool (no shared connection with the main thread) that updates
  extraction_runs.heartbeat_at every 15 seconds, survives blocking
  LibreOffice/OCR calls, and dies with the process
- on boot, heartbeat-stale runs are re-queued (app.core.queue.requeue_stale_runs)
- during FR-043 queued-degradation, rfp jobs are left queued (uploads keep
  being accepted; processing resumes in priority order after recovery)
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.core import provider_health
from app.core.config import Settings
from app.core.models import ExtractionRun, Job, Rfp
from app.core.queue import claim_next_job, complete_job, requeue_stale_runs
from app.pipeline.runner import ExtractionFailed, run_extraction

log = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_S = 15


class HeartbeatThread(threading.Thread):
    """FR-006 heartbeat: dedicated engine + NullPool, thread-safe."""

    def __init__(self, database_url: str, run_id: uuid.UUID) -> None:
        super().__init__(daemon=True)
        connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
        self._engine = create_engine(
            database_url, poolclass=NullPool, future=True, connect_args=connect_args
        )
        self._factory = sessionmaker(bind=self._engine, expire_on_commit=False, future=True)
        self._run_id = run_id
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                with self._factory() as session:
                    run = session.get(ExtractionRun, self._run_id)
                    if run is not None and run.stage in ("reading", "extracting", "auditing"):
                        run.heartbeat_at = datetime.now(timezone.utc)
                        session.commit()
            except Exception:
                log.exception("heartbeat update failed (run %s)", self._run_id)
            self._stop.wait(HEARTBEAT_INTERVAL_S)
        self._engine.dispose()


def process_one_job(
    session: Session,
    settings: Settings,
    worker_id: str,
    **extraction_kwargs,
) -> Job | None:
    """Claim + process one job. Returns the job (or None when idle)."""
    if provider_health.is_degraded(settings):
        log.info("provider degradation active — jobs remain queued")
        return None
    job = claim_next_job(session, worker_id)
    if job is None:
        return None
    session.commit()  # make the claim visible before the long work starts

    rfp = session.get(Rfp, job.rfp_id) if job.rfp_id else None
    if rfp is None:
        complete_job(session, job, ok=False, error="job has no rfp")
        session.commit()
        return job
    run = session.execute(
        select(ExtractionRun)
        .where(ExtractionRun.rfp_id == rfp.id)
        .order_by(ExtractionRun.created_at.desc())
    ).scalars().first()
    if run is None:
        complete_job(session, job, ok=False, error="no extraction run")
        session.commit()
        return job

    heartbeat = HeartbeatThread(settings.database_url, run.id)
    heartbeat.start()
    try:
        run_extraction(session, settings, rfp, run, **extraction_kwargs)
    except ExtractionFailed as exc:
        run.stage = "failed"
        run.error = exc.cause
        rfp.state = "failed"
        complete_job(session, job, ok=False, error=exc.cause)
    except Exception as exc:  # unexpected: fail loudly with cause
        log.exception("job %s crashed", job.id)
        run.stage = "failed"
        run.error = f"worker error: {exc}"
        rfp.state = "failed"
        complete_job(session, job, ok=False, error=str(exc))
    else:
        complete_job(session, job, ok=True)
    finally:
        heartbeat.stop()
    session.commit()
    return job


def run_worker(
    session_factory: sessionmaker,
    settings: Settings,
    *,
    worker_id: str | None = None,
    poll_s: float = 2.0,
    sleep_fn: Callable[[float], None] = time.sleep,
    max_cycles: int | None = None,
) -> None:
    worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
    # Boot: re-queue heartbeat-stale runs (FR-006).
    with session_factory() as session:
        actions = requeue_stale_runs(session)
        session.commit()
        for action in actions:
            log.info("boot requeue: %s", action)

    cycles = 0
    while True:
        try:
            with session_factory() as session:
                job = process_one_job(session, settings, worker_id)
        except Exception:
            log.exception("worker cycle failed")
            job = None
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            return
        if job is None:
            sleep_fn(poll_s)


def main() -> None:  # pragma: no cover - container entrypoint
    from app.core.db import make_engine, make_session_factory

    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env()
    engine = make_engine(settings.database_url)
    run_worker(make_session_factory(engine), settings)


if __name__ == "__main__":  # pragma: no cover
    main()
