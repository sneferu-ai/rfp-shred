"""Intake validation (FR-004/041, AC-005) + FR-033 re-upload bypass (AC-022)."""

import io
import zipfile
from pathlib import Path

from starlette.requests import Request

from app.core.models import Account, ExtractionRun, Job, Requirement, Rfp
from app.web.upload_guard import MULTIPART_OVERHEAD_BYTES
from tests.conftest import (
    add_requirements,
    csrf_headers,
    make_account,
    make_rfp,
    pdf_bytes,
    refresh_db,
    signup_json,
)

SIMPLE_PDF = pdf_bytes([["Section L", "L.1 The offeror shall submit a volume."]])


def _upload(client, headers, data: bytes, filename: str = "solicitation.pdf"):
    return client.post(
        "/app/new",
        files={"file": (filename, data, "application/octet-stream")},
        headers=headers,
        follow_redirects=False,
    )


def _zip_bytes(entries: list[tuple[str, bytes]], compresslevel: int = 9) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=compresslevel) as zf:
        for name, payload in entries:
            zf.writestr(name, payload)
    return buf.getvalue()


def test_valid_pdf_accepted(client, db):
    signup_json(client)
    resp = _upload(client, csrf_headers(client), SIMPLE_PDF)
    assert resp.status_code == 303
    refresh_db(db)
    rfp = db.query(Rfp).one()
    assert rfp.state == "received"
    assert rfp.content_hash
    assert db.query(ExtractionRun).count() == 1
    assert db.query(Job).one().priority == 10
    assert Path(rfp.stored_path).exists()


def test_oversize_rejected_413_before_stream(client, settings):
    signup_json(client)
    headers = csrf_headers(client)
    # content-length is checked before the body streams: send a declared
    # oversize payload; the app rejects without persisting anything.
    big = b"\x00" * (settings.max_upload_bytes + 10)
    resp = _upload(client, headers, big)
    assert resp.status_code == 413
    incoming = Path(settings.files_dir) / "incoming"
    assert not incoming.exists() or list(incoming.iterdir()) == []


def test_declared_oversize_never_reaches_multipart_parser(client, settings, monkeypatch):
    """The outer guard must fire before CSRF or route form parsing."""
    signup_json(client)
    headers = csrf_headers(client)
    headers.update(
        {
            "Content-Type": "multipart/form-data; boundary=test-boundary",
            "Content-Length": str(settings.max_upload_bytes + MULTIPART_OVERHEAD_BYTES + 1),
        }
    )

    def parser_must_not_run(*args, **kwargs):
        raise AssertionError("multipart parser was entered")

    monkeypatch.setattr(Request, "form", parser_must_not_run)
    response = client.post("/app/new", content=b"small-body", headers=headers)
    assert response.status_code == 413
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["code"] == "UPLOAD_TOO_LARGE"


def test_wrong_type_415(client):
    signup_json(client)
    resp = _upload(client, csrf_headers(client), b"\x89PNG\r\n\x1a\n" + b"\x00" * 100, "photo.png")
    assert resp.status_code == 415


def test_truncated_pdf_signature_only_415(client):
    signup_json(client)
    # random bytes with a PDF magic header but not a PDF container structure
    resp = _upload(client, csrf_headers(client), b"%PDF-" + b"\x00" * 64, "broken.pdf")
    # signature passes (it IS a pdf claim) — pypdf then fails to parse it at
    # the encrypted/corrupt probe -> 422
    assert resp.status_code in (415, 422)


def test_encrypted_pdf_422(client, tmp_path):
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt(user_password="hunter2", owner_password="owner-secret")
    enc = tmp_path / "enc.pdf"
    with open(enc, "wb") as fh:
        writer.write(fh)
    signup_json(client)
    resp = _upload(client, csrf_headers(client), enc.read_bytes(), "enc.pdf")
    assert resp.status_code == 422
    assert "encrypted" in resp.text.lower()


def test_empty_password_encrypted_pdf_accepted(client, tmp_path):
    """AC-033a: empty owner password opens via the empty-password probe."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt(user_password="", owner_password="")
    enc = tmp_path / "empty-enc.pdf"
    with open(enc, "wb") as fh:
        writer.write(fh)
    signup_json(client)
    resp = _upload(client, csrf_headers(client), enc.read_bytes(), "empty-enc.pdf")
    assert resp.status_code == 303


def test_zip_with_txt_rejected(client):
    signup_json(client)
    payload = _zip_bytes([("a.pdf", SIMPLE_PDF), ("notes.txt", b"hello")])
    resp = _upload(client, csrf_headers(client), payload, "pack.zip")
    assert resp.status_code == 422
    assert "only PDF or DOCX" in resp.text


def test_zip_too_many_entries(client, settings):
    signup_json(client)
    entries = [(f"f{i:03d}.pdf", SIMPLE_PDF) for i in range(settings.max_zip_entries + 1)]
    resp = _upload(client, csrf_headers(client), _zip_bytes(entries), "huge.zip")
    assert resp.status_code == 422
    assert "too many files" in resp.text


def test_zip_bomb_rejected(client, settings):
    signup_json(client)
    # highly compressible: 6MB of zeros at ratio >> 100:1
    payload = _zip_bytes([("b.pdf", b"\x00" * (6 * 1024 * 1024))])
    resp = _upload(client, csrf_headers(client), payload, "bomb.zip")
    assert resp.status_code == 422


def test_zip_valid_accepted(client):
    signup_json(client)
    payload = _zip_bytes([("a.pdf", SIMPLE_PDF), ("b.pdf", SIMPLE_PDF)])
    resp = _upload(client, csrf_headers(client), payload, "pack.zip")
    assert resp.status_code == 303


def test_reupload_bypass_clones_ready_matrix(client, settings, db):
    """FR-033/AC-022: same-account re-upload of an unlocked ready matrix
    clones rows and inherits the unlock — no extraction runs."""
    email, _ = signup_json(client)
    acct = db.query(Account).filter(Account.email == email).one()
    import hashlib
    from datetime import datetime, timezone

    prior = make_rfp(
        db, acct, state="ready",
        content_hash=hashlib.sha256(SIMPLE_PDF).hexdigest(),
        unlocked_at=datetime.now(timezone.utc),
    )
    add_requirements(db, prior, 15)
    db.commit()

    resp = _upload(client, csrf_headers(client), SIMPLE_PDF)
    assert resp.status_code == 303
    refresh_db(db)
    rfps = db.query(Rfp).order_by(Rfp.created_at).all()
    assert len(rfps) == 2
    new = rfps[-1]
    assert new.id != prior.id
    assert new.unlocked_at is not None
    assert new.state == "ready"
    assert new.content_hash == prior.content_hash
    # rows cloned, no extraction run or job for the new rfp
    assert db.query(Requirement).filter(Requirement.rfp_id == new.id).count() == 15
    assert db.query(ExtractionRun).filter(ExtractionRun.rfp_id == new.id).count() == 0
    assert db.query(Job).filter(Job.rfp_id == new.id).count() == 0


def test_reupload_bypass_fresh_extract_when_prior_not_ready(client, db):
    email, _ = signup_json(client)
    acct = db.query(Account).filter(Account.email == email).one()
    import hashlib
    from datetime import datetime, timezone

    prior = make_rfp(
        db, acct, state="failed",
        content_hash=hashlib.sha256(SIMPLE_PDF).hexdigest(),
        unlocked_at=datetime.now(timezone.utc),
    )
    db.commit()
    resp = _upload(client, csrf_headers(client), SIMPLE_PDF)
    assert resp.status_code == 303
    refresh_db(db)
    new = db.query(Rfp).filter(Rfp.id != prior.id).one()
    assert new.unlocked_at is not None  # entitlement still carried
    assert new.state == "received"      # fresh extraction runs
    assert db.query(Job).filter(Job.rfp_id == new.id).count() == 1


def test_reupload_different_account_no_bypass(client, db):
    email, _ = signup_json(client)
    other = make_account(db)
    import hashlib
    from datetime import datetime, timezone

    prior = make_rfp(
        db, other, state="ready",
        content_hash=hashlib.sha256(SIMPLE_PDF).hexdigest(),
        unlocked_at=datetime.now(timezone.utc),
    )
    db.commit()
    resp = _upload(client, csrf_headers(client), SIMPLE_PDF)
    assert resp.status_code == 303
    refresh_db(db)
    mine = db.query(Rfp).filter(Rfp.id != prior.id).one()
    assert mine.unlocked_at is None
    assert db.query(Job).filter(Job.rfp_id == mine.id).count() == 1
