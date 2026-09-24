"""S10 console gating, FR-024 health, FR-045 reprocess, FR-042 amendment,
FR-038 annotations, FR-020 maintenance, FR-043 provider degradation,
FR-030 rate limits, FR-036 staff bootstrap, script exports."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from app.core import maintenance, provider_health
from app.core.models import (
    Account,
    CorpusAnnotation,
    ExtractionRun,
    InferenceLog,
    Job,
    Requirement,
    RetentionLog,
    Rfp,
    SweepRun,
)
from scripts.create_staff import create_staff
from scripts.export_annotations import export_annotations
from scripts.export_corrections import export_corrections
from tests.conftest import (
    add_requirements,
    csrf_headers,
    make_account,
    make_pdf,
    make_rfp,
    make_run_and_job,
    pdf_bytes,
    refresh_db,
    signup_json,
)


# ---------------------------------------------------------------------------
# S10
# ---------------------------------------------------------------------------

def test_public_pages_render(client, db):
    """Every anonymous surface renders (template syntax + context sanity)."""
    assert client.get("/").status_code == 200
    assert client.get("/signup").status_code == 200
    assert client.get("/login").status_code == 200
    assert client.get("/forgot-password").status_code == 200
    assert client.get("/reset-password?token=x").status_code == 200
    # unauthenticated app surfaces redirect to login
    resp = client.get("/app", follow_redirects=False)
    assert resp.status_code == 303
    resp = client.get("/app/new", follow_redirects=False)
    assert resp.status_code == 303
    # authenticated surfaces render
    signup_json(client)
    assert client.get("/app").status_code == 200
    assert client.get("/app/new").status_code == 200
    assert client.get("/app/account").status_code == 200


def test_ops_console_staff_only(client, db):
    email, _ = signup_json(client)
    assert client.get("/ops/sweeps").status_code == 404
    assert client.get("/ops/audit").status_code == 404
    acct = db.query(Account).filter(Account.email == email).one()
    acct.is_staff = True
    db.commit()
    db.add(SweepRun(found=3, matched=1, errors=[{"kind": "call_failed"}]))
    db.commit()
    assert client.get("/ops/sweeps").status_code == 200
    assert "call_failed" in client.get("/ops/sweeps").text
    assert client.get("/ops/quality").status_code == 200
    assert client.get("/ops/audit").status_code == 200


def test_staff_bootstrap_script(db, settings):
    password, created = create_staff(settings.database_url, "founder@example.com")
    assert created and len(password) == 16
    again, created_again = create_staff(settings.database_url, "founder@example.com")
    assert created_again is False and again == ""


# ---------------------------------------------------------------------------
# FR-024
# ---------------------------------------------------------------------------

def test_healthz_dynamic(client, settings):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["db"] == "ok" and payload["jobs"] == "ok" and payload["model"] == "ok"
    # break the jobs table -> 503 naming the failed dependency
    with settings_engine(settings) as eng:
        eng.execute(__import__("sqlalchemy").text("DROP TABLE jobs"))
    resp = client.get("/healthz")
    assert resp.status_code == 503
    assert resp.json()["jobs"] == "error"


class settings_engine:  # tiny context manager helper
    def __init__(self, settings):
        from app.core.db import make_engine

        self.eng = make_engine(settings.database_url)

    def __enter__(self):
        return _EngineCtx(self.eng)

    def __exit__(self, *exc):
        self.eng.dispose()


class _EngineCtx:
    def __init__(self, eng):
        self.eng = eng

    def execute(self, stmt):
        with self.eng.begin() as conn:
            return conn.execute(stmt)


def test_healthz_reports_degraded_model(client, settings):
    path = Path(settings.files_dir) / "provider_state.json"
    path.write_text(json.dumps({"openai": "down", "anthropic": "down", "checked_at": "now"}))
    settings.model_provider = "openai"
    resp = client.get("/healthz")
    assert resp.json()["model"] == "degraded"


# ---------------------------------------------------------------------------
# FR-043
# ---------------------------------------------------------------------------

def test_provider_health_probe_and_degradation(settings, tmp_path):
    state = provider_health.probe_once(
        settings,
        probe_openai=lambda s: "down",
        probe_anthropic=lambda s: "down",
    )
    assert state["openai"] == "down"
    settings.model_provider = "openai"
    assert provider_health.is_degraded(settings) is True
    # mock provider is never degraded (dev/test stack)
    settings.model_provider = "mock"
    assert provider_health.is_degraded(settings) is False


def test_probe_reports_down_on_4xx(settings, monkeypatch):
    """FR-043: a 401/403 bad key must report 'down', not 'ok' — extraction
    calls would fail too. Only <400 counts as reachable (the implementer's
    round-2 hardening; pin it so a future '>= 500' regression cannot return)."""
    import httpx

    class _Resp:
        def __init__(self, status_code):
            self.status_code = status_code

    settings.openai_api_key = "sk-test"
    settings.anthropic_api_key = "sk-test"
    for code in (401, 403, 400, 404, 422):
        monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(code))
        assert provider_health._probe_openai(settings) == "down", f"code {code}"
        assert provider_health._probe_anthropic(settings) == "down", f"code {code}"

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(200))
    assert provider_health._probe_openai(settings) == "ok"
    assert provider_health._probe_anthropic(settings) == "ok"


def test_probe_reports_down_on_network_error(settings, monkeypatch):
    """A connection error (DNS / timeout / refused) is 'down', not a crash."""
    import httpx

    settings.openai_api_key = "sk-test"
    settings.anthropic_api_key = "sk-test"

    def _boom(*a, **k):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "get", _boom)
    assert provider_health._probe_openai(settings) == "down"
    assert provider_health._probe_anthropic(settings) == "down"


def test_probe_unknown_when_no_key(settings):
    """No key configured → 'unknown' (not 'down'): the provider was never
    armed, so FR-043 degradation (which needs BOTH down) should not trigger
    on an unconfigured provider."""
    settings.openai_api_key = ""
    settings.anthropic_api_key = ""
    assert provider_health._probe_openai(settings) == "unknown"
    assert provider_health._probe_anthropic(settings) == "unknown"


# ---------------------------------------------------------------------------
# FR-045 / FR-042 / FR-038
# ---------------------------------------------------------------------------

def test_reprocess_within_grace_and_410_after_destruction(client, db, settings, tmp_path):
    email, _ = signup_json(client)
    acct = db.query(Account).filter(Account.email == email).one()
    pdf = make_pdf(tmp_path / "sol.pdf", [["Section L", "L.1 The offeror shall submit."]])
    rfp = make_rfp(db, acct, stored_path=str(pdf))
    run, job = make_run_and_job(db, rfp)
    run.stage = "ready"
    db.commit()

    resp = client.post(f"/app/matrices/{rfp.id}/reprocess", headers=csrf_headers(client), follow_redirects=False)
    assert resp.status_code == 303
    refresh_db(db)
    runs = db.query(ExtractionRun).filter(ExtractionRun.rfp_id == rfp.id).order_by(ExtractionRun.created_at).all()
    assert len(runs) == 2
    assert runs[-1].attempt == run.attempt + 1
    # original job (never processed) + the fresh re-process job, priority 10
    queued = db.query(Job).filter(Job.rfp_id == rfp.id, Job.status == "queued").order_by(Job.enqueued_at).all()
    assert len(queued) == 2
    assert queued[-1].priority == 10

    # destroy the source -> 410
    rfp.stored_path = None
    db.commit()
    resp = client.post(f"/app/matrices/{rfp.id}/reprocess", headers=csrf_headers(client))
    assert resp.status_code == 410


def test_amendment_linking(client, db):
    email, _ = signup_json(client)
    acct = db.query(Account).filter(Account.email == email).one()
    parent = make_rfp(db, acct, orig_name="base.pdf")
    db.commit()
    data = pdf_bytes([["Section L", "L.1 The offeror shall comply."]])
    resp = client.post(
        "/app/new",
        files={"file": ("amendment.pdf", data, "application/pdf")},
        data={"amends_rfp_id": str(parent.id)},
        headers=csrf_headers(client),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    refresh_db(db)
    child = db.query(Rfp).filter(Rfp.id != parent.id).one()
    assert child.amends_rfp_id == parent.id
    # S3 shows the Amendment badge
    resp = client.get("/app")
    assert "Amendment of" in resp.text


def test_annotation_mode_and_export(client, db, settings, tmp_path):
    email, _ = signup_json(client)
    acct = db.query(Account).filter(Account.email == email).one()
    acct.is_staff = True
    rfp = make_rfp(db, acct)
    rows = add_requirements(db, rfp, 3)
    db.commit()
    headers = csrf_headers(client)

    resp = client.post(
        f"/app/matrices/{rfp.id}/annotate",
        data={"action": "correct_binding", "requirement_id": str(rows[0].id)},
        headers=headers, follow_redirects=False,
    )
    assert resp.status_code == 303
    client.post(
        f"/app/matrices/{rfp.id}/annotate",
        data={"action": "not_requirement", "requirement_id": str(rows[1].id)},
        headers=headers,
    )
    client.post(
        f"/app/matrices/{rfp.id}/annotate",
        data={"action": "add_missing", "text": "The offeror shall bring donuts.", "clause_id": "L.99", "section": "Section L", "page": "4"},
        headers=headers,
    )
    refresh_db(db)
    annotations = db.query(CorpusAnnotation).filter(CorpusAnnotation.rfp_id == rfp.id).all()
    assert len(annotations) == 3
    assert {a.is_binding for a in annotations} == {True, False}

    out = tmp_path / "gt.json"
    payload = export_annotations(settings.database_url, str(rfp.id), str(out))
    assert out.exists()
    assert payload["requirements"][0]["is_binding"] in (True, False)
    assert set(payload["requirements"][0]) == {"clause_id", "section", "page", "text", "is_binding"}


def test_annotation_forbidden_for_non_staff(client, db):
    email, _ = signup_json(client)
    acct = db.query(Account).filter(Account.email == email).one()
    rfp = make_rfp(db, acct)
    (row,) = add_requirements(db, rfp, 1)
    db.commit()
    resp = client.post(
        f"/app/matrices/{rfp.id}/annotate",
        data={"action": "correct_binding", "requirement_id": str(row.id)},
        headers=csrf_headers(client),
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# FR-020 maintenance + FR-039 reprioritize + FR-037 export
# ---------------------------------------------------------------------------

def test_retention_grace_and_failed_expiry(db, settings, tmp_path):
    acct = make_account(db)
    f1 = tmp_path / "a.pdf"
    f1.write_bytes(b"x")
    rfp = make_rfp(db, acct, stored_path=str(f1))
    run, _ = make_run_and_job(db, rfp)
    run.stage = "ready"
    run.heartbeat_at = datetime.now(timezone.utc) - timedelta(hours=25)
    old_failed = make_rfp(db, acct, state="failed", stored_path=str(tmp_path / "b.pdf"))
    (tmp_path / "b.pdf").write_bytes(b"y")
    old_failed.created_at = datetime.now(timezone.utc) - timedelta(days=8)
    db.commit()

    actions = maintenance.enforce_source_retention(db)
    db.commit()
    assert any("24h grace" in a for a in actions)
    assert rfp.stored_path is None
    assert not f1.exists()
    assert old_failed.state == "expired"
    assert db.query(RetentionLog).count() == 2


def test_retention_covers_clone_path_without_run(db, tmp_path):
    """FR-033 clones reach `ready` with NO extraction run — the grace clock
    falls back to the upload's created_at (no file leak)."""
    from app.core import maintenance as maint

    acct = make_account(db)
    f = tmp_path / "clone.pdf"
    f.write_bytes(b"x")
    clone = make_rfp(db, acct, state="ready", stored_path=str(f))
    clone.created_at = datetime.now(timezone.utc) - timedelta(hours=25)
    db.commit()
    actions = maint.enforce_source_retention(db)
    db.commit()
    assert any("24h grace" in a for a in actions)
    assert not f.exists()


def test_retention_respects_opt_in_and_grace_window(db, tmp_path):
    acct = make_account(db)
    f = tmp_path / "keep.pdf"
    f.write_bytes(b"x")
    kept = make_rfp(db, acct, stored_path=str(f), retain_source=True)
    run, _ = make_run_and_job(db, kept)
    run.stage = "ready"
    run.heartbeat_at = datetime.now(timezone.utc) - timedelta(hours=25)
    db.commit()
    assert maintenance.enforce_source_retention(db) == []
    assert f.exists()


def test_failed_retention_respects_keep_source_opt_in(db, tmp_path):
    """The seven-day abandoned-failure sweep must not override an explicit
    keep-source choice; opt-in files persist until the user deletes them."""
    acct = make_account(db)
    source = tmp_path / "keep-failed.pdf"
    source.write_bytes(b"x")
    kept = make_rfp(
        db, acct, state="failed", stored_path=str(source), retain_source=True
    )
    kept.created_at = datetime.now(timezone.utc) - timedelta(days=8)
    db.commit()

    assert maintenance.enforce_source_retention(db) == []
    assert kept.state == "failed"
    assert kept.stored_path == str(source)
    assert source.exists()


def test_reprioritize_after_month_reset(db, settings):
    acct = make_account(db)
    rfp = make_rfp(db, acct)
    job = Job(rfp_id=rfp.id, priority=0, status="queued")
    db.add(job)
    db.commit()
    assert maintenance.reprioritize_capped_jobs(db, settings.monthly_inference_cap_usd)
    assert job.priority == 10


def test_cap_priority_at_upload(db, settings):
    """FR-039: over-cap account's new upload parks at priority 0."""
    from app.core import usage as usage_mod

    acct = make_account(db)
    settings.monthly_inference_cap_usd = 5.0
    db.add(InferenceLog(account_id=acct.id, tokens_in=0, tokens_out=0, estimated_cost_usd=Decimal("9.99")))
    db.commit()
    assert usage_mod.priority_for_new_job(db, acct.id, "user", settings.monthly_inference_cap_usd) == 0
    # sweep origin is exempt
    assert usage_mod.priority_for_new_job(db, acct.id, "sweep", settings.monthly_inference_cap_usd) == 1
    # prior-month spend does not count (cap resets on the 1st)
    old = InferenceLog(
        account_id=acct.id, tokens_in=0, tokens_out=0, estimated_cost_usd=Decimal("99.0"),
        created_at=datetime.now(timezone.utc) - timedelta(days=40),
    )
    db.add(old)
    db.commit()
    assert usage_mod.is_over_cap(db, acct.id, 5.0) is True  # this month's 9.99 still counts
    # a fresh account is under
    fresh = make_account(db)
    assert usage_mod.priority_for_new_job(db, fresh.id, "user", 5.0) == 10


def test_corrections_export_script(db, settings):
    from app.core.models import Correction

    acct = make_account(db)
    rfp = make_rfp(db, acct)
    db.add(Correction(account_id=acct.id, rfp_id=rfp.id, correction_type="uncheck", old_value="a", new_value="b"))
    db.commit()
    rows = export_corrections(settings.database_url, "2020-01-01", "2030-01-01")
    assert len(rows) == 1
    assert rows[0]["correction_type"] == "uncheck"


def test_upload_rate_limit_429(client, settings):
    signup_json(client)
    headers = csrf_headers(client)
    data = pdf_bytes([["Section L", "L.1 The offeror shall comply with everything."]])
    last = None
    for _ in range(11):
        last = client.post("/app/new", files={"file": ("x.pdf", data, "application/pdf")}, headers=headers)
    assert last.status_code == 429
    assert "retry-after" in {k.lower(): v for k, v in last.headers.items()}
