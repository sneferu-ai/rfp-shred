"""Shared fixtures: SQLite-backed app, TestClient, PDF/account/matrix factories."""

from __future__ import annotations

import io
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.db import create_schema, make_engine, make_session_factory
from app.core.models import Account, ExtractionRun, Job, Requirement, Rfp
from app.core.security import hash_password
from app.web.app import create_app


@pytest.fixture()
def settings(tmp_path) -> Settings:
    files_dir = tmp_path / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        app_secret="test-secret-test-secret-test-secret!",
        database_url=f"sqlite:///{tmp_path}/test.db",
        files_dir=str(files_dir),
        public_url="http://testserver",
        model_provider="mock",
        model_name="mock-extractor-1",
        environment="test",
    )


@pytest.fixture()
def engine(settings):
    eng = make_engine(settings.database_url)
    create_schema(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def db(settings, engine):
    factory = make_session_factory(engine)
    with factory() as session:
        yield session


@pytest.fixture()
def client(settings) -> TestClient:
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------

def make_account(db, email: str | None = None, staff: bool = False, password: str = "test-password-123") -> Account:
    account = Account(
        email=email or f"user-{uuid.uuid4().hex[:8]}@example.com",
        pass_hash=hash_password(password),
        is_staff=staff,
    )
    db.add(account)
    db.flush()
    return account


def make_rfp(db, account: Account, *, state: str = "ready", origin: str = "user", **kwargs) -> Rfp:
    rfp = Rfp(account_id=account.id, orig_name=kwargs.pop("orig_name", "test.pdf"), state=state, origin=origin, **kwargs)
    db.add(rfp)
    db.flush()
    return rfp


def add_requirements(db, rfp: Rfp, count: int, *, clause_prefix: str = "l") -> list[Requirement]:
    rows = []
    for i in range(1, count + 1):
        row = Requirement(
            rfp_id=rfp.id,
            seq=i,
            clause_id=f"{clause_prefix}.{i}",
            section="Section L" if clause_prefix == "l" else "Section M",
            page=(i % 5) + 1,
            body=f"The offeror shall meet requirement number {i} of this solicitation.",
            excerpt=f"shall meet requirement number {i}",
            tags=[],
        )
        db.add(row)
        rows.append(row)
    db.flush()
    return rows


def make_pdf(path: Path, pages: list[list[str]]) -> Path:
    """Write a real text-native PDF via reportlab (one page per line list)."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=letter)
    for lines in pages:
        text = c.beginText(72, 720)
        for line in lines:
            text.textLine(line)
        c.drawText(text)
        c.showPage()
    c.save()
    return path


def pdf_bytes(pages: list[list[str]]) -> bytes:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    for lines in pages:
        text = c.beginText(72, 720)
        for line in lines:
            text.textLine(line)
        c.drawText(text)
        c.showPage()
    c.save()
    return buf.getvalue()


def signup_json(client: TestClient, email: str | None = None, password: str = "test-password-123") -> tuple[str, str]:
    """Create an account through the JSON API. Returns (email, csrf_token)."""
    email = email or f"user-{uuid.uuid4().hex[:8]}@example.com"
    resp = client.post(
        "/signup",
        json={"email": email, "password": password},
        headers={"Accept": "application/json"},
    )
    assert resp.status_code == 201, resp.text
    return email, resp.json()["csrf_token"]


def csrf_headers(client: TestClient) -> dict[str, str]:
    resp = client.get("/api/csrf")
    assert resp.status_code == 200, resp.text
    return {"X-CSRF-Token": resp.json()["csrf_token"], "Accept": "application/json"}


def make_run_and_job(db, rfp: Rfp, *, priority: int = 10) -> tuple[ExtractionRun, Job]:
    run = ExtractionRun(rfp_id=rfp.id, stage="reading")
    db.add(run)
    db.flush()
    job = Job(rfp_id=rfp.id, priority=priority, status="queued")
    db.add(job)
    db.flush()
    return run, job


def refresh_db(db) -> None:
    """Expire cached objects so the next access reloads data committed by
    the app's own session (tests mix direct-db + HTTP). NOTE: with
    expire_on_commit=False, rollback() after a commit is a pass-through and
    does NOT expire — expire_all() is the correct refresh."""
    db.expire_all()
