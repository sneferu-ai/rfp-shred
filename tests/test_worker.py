"""FR-006 worker: claim order, full processing, heartbeat stale re-queue,
attempt increments, max-3 attempts (AC-016/034/039)."""

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.model_client import ModelResponse, ProviderRateLimited
from app.core.models import ExtractionRun, Job, Requirement, Rfp
from app.core.queue import MAX_RETRIES_CAUSE, claim_next_job, enqueue, requeue_stale_runs
from app.worker.main import process_one_job
from tests.conftest import make_account, make_pdf, make_rfp, make_run_and_job, refresh_db


def test_claim_priority_order(db):
    acct = make_account(db)
    sweep_rfp = make_rfp(db, acct, origin="sweep")
    user_rfp = make_rfp(db, acct)
    user_rfp2 = make_rfp(db, acct)
    enqueue(db, sweep_rfp.id, 1)
    enqueue(db, user_rfp.id, 10)
    enqueue(db, user_rfp2.id, 10)
    db.commit()
    first = claim_next_job(db, "w1")
    assert first.rfp_id in (user_rfp.id, user_rfp2.id)
    db.commit()
    second = claim_next_job(db, "w1")
    assert second.rfp_id in (user_rfp.id, user_rfp2.id)
    db.commit()
    third = claim_next_job(db, "w1")
    assert third.rfp_id == sweep_rfp.id


def test_priority_zero_never_claimed(db):
    acct = make_account(db)
    rfp = make_rfp(db, acct)
    enqueue(db, rfp.id, 0)
    db.commit()
    assert claim_next_job(db, "w1") is None


def test_process_one_job_end_to_end(db, settings, tmp_path):
    acct = make_account(db)
    pdf = make_pdf(tmp_path / "sol.pdf", [["Section L", "L.1 The offeror shall submit a technical volume."]])
    rfp = make_rfp(db, acct, state="received", stored_path=str(pdf))
    run, job = make_run_and_job(db, rfp)
    db.commit()

    processed = process_one_job(db, settings, "w1", sleep_fn=lambda s: None)
    assert processed is not None and processed.id == job.id
    refresh_db(db)
    assert rfp.state == "ready"
    assert run.stage == "ready"
    assert job.status == "completed"
    rows = db.execute(select(Requirement).where(Requirement.rfp_id == rfp.id)).scalars().all()
    assert rows and rows[0].clause_id == "l.1"


def test_process_one_job_failure_marks_failed(db, settings, tmp_path):
    """A NON-provider failure (corrupt file) fails immediately with cause —
    FR-005 tool errors are not retried; only provider 429/5xx are (FR-006)."""
    acct = make_account(db)
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"garbage")
    rfp = make_rfp(db, acct, state="received", stored_path=str(bad))
    run, job = make_run_and_job(db, rfp)
    db.commit()
    process_one_job(db, settings, "w1", sleep_fn=lambda s: None)
    refresh_db(db)
    assert rfp.state == "failed"
    assert run.stage == "failed"
    assert run.error
    assert job.status == "failed"


class _FlakyProviderClient:
    """Provider that 429s `fail_times` times, then serves a valid extraction."""

    def __init__(self, fail_times: int):
        self.calls = 0
        self.fail_times = fail_times

    def complete(self, *, system, user, max_tokens, json_schema=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ProviderRateLimited("429")
        return ModelResponse(text=json.dumps({"requirements": []}), tokens_in=1, tokens_out=1)


def test_process_one_job_provider_flaky_recovers(db, settings, tmp_path):
    """FR-006: provider 429/5xx -> three backed-off retries against the
    CONFIGURED provider (A-023: no failover). Recovery within the budget
    completes the job normally."""
    acct = make_account(db)
    pdf = make_pdf(tmp_path / "sol.pdf", [["Section L", "L.1 The offeror shall submit a technical volume."]])
    rfp = make_rfp(db, acct, state="received", stored_path=str(pdf))
    run, job = make_run_and_job(db, rfp)
    db.commit()
    client = _FlakyProviderClient(fail_times=2)
    processed = process_one_job(db, settings, "w1", model_client=client, sleep_fn=lambda s: None)
    assert processed is not None and processed.id == job.id
    refresh_db(db)
    # the first chunk burned its retry budget (2 fails + success) and later
    # chunks succeed immediately — retries absorbed, job completes normally
    assert client.calls > 2
    assert rfp.state == "ready"
    assert run.stage == "ready"
    assert job.status == "completed"


def test_process_one_job_provider_down_fails_after_backed_off_retries(db, settings, tmp_path):
    """FR-006 lifecycle: provider 429/5xx -> backed-off retries -> `failed`
    with cause + manual Retry on S5. The job is NOT silently re-enqueued."""
    acct = make_account(db)
    pdf = make_pdf(tmp_path / "sol.pdf", [["Section L", "L.1 The offeror shall submit a technical volume."]])
    rfp = make_rfp(db, acct, state="received", stored_path=str(pdf))
    run, job = make_run_and_job(db, rfp)
    db.commit()
    client = _FlakyProviderClient(fail_times=99)
    process_one_job(db, settings, "w1", model_client=client, sleep_fn=lambda s: None)
    refresh_db(db)
    # every chunk got exactly its 3-attempt budget, no more (the per-chunk
    # count itself is pinned in tests/test_mine.py)
    assert client.calls >= 3 and client.calls % 3 == 0
    assert rfp.state == "failed"
    assert run.stage == "failed"
    assert "model provider error" in run.error
    assert job.status == "failed"


def _stale_run(db, attempt: int = 1):
    acct = make_account(db)
    rfp = make_rfp(db, acct, state="extracting")
    run, job = make_run_and_job(db, rfp)
    run.stage = "extracting"
    run.attempt = attempt
    run.heartbeat_at = datetime.now(timezone.utc) - timedelta(minutes=11)
    job.status = "processing"
    job.locked_by = "dead-worker"
    db.commit()
    return rfp, run, job


def test_stale_requeue_increments_attempt_and_cleans(db):
    """AC-039: reset + increment + cleanup in one transaction."""
    rfp, run, job = _stale_run(db)
    # partial output from the dead attempt
    from tests.conftest import add_requirements

    add_requirements(db, rfp, 2)
    db.commit()
    actions = requeue_stale_runs(db)
    db.commit()
    assert actions and "re-queued" in actions[0]
    assert run.attempt == 2
    assert job.status == "queued"
    assert job.locked_by is None
    assert db.execute(select(Requirement).where(Requirement.rfp_id == rfp.id)).scalars().all() == []
    assert rfp.state == "received"


def test_max_attempts_marks_failed_with_cause(db):
    """AC-034: after the third attempt the run fails with the exact cause."""
    rfp, run, job = _stale_run(db, attempt=3)
    actions = requeue_stale_runs(db)
    db.commit()
    assert run.stage == "failed"
    assert run.error == MAX_RETRIES_CAUSE
    assert rfp.state == "failed"
    assert any("max retries exceeded" in a for a in actions)


def test_live_run_not_requeued(db):
    _rfp, run, job = _stale_run(db)
    run.heartbeat_at = datetime.now(timezone.utc)  # fresh heartbeat
    db.commit()
    assert requeue_stale_runs(db) == []
    assert run.attempt == 1
    assert job.status == "processing"
