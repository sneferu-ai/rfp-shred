"""S2 auth: signup/login/logout/reset (FR-001/002/028/031, AC-004, AC-050)."""

import re
from pathlib import Path

from app.core.models import ResetToken, SessionRow
from tests.conftest import signup_json


def _outbox_files(settings) -> list[Path]:
    outbox = Path(settings.files_dir) / "outbox"
    return sorted(outbox.glob("*.eml")) if outbox.exists() else []


def _refresh(db) -> None:
    """Expire cached objects so the next access reloads data committed by
    the app's own session (rollback() after commit is a pass-through)."""
    db.expire_all()


def _form(client, **values):
    response = client.get("/api/csrf")
    if response.status_code == 200:
        token = response.json()["csrf_token"]
    else:
        html = client.get("/forgot-password").text
        match = re.search(r'name="csrf_token" value="([^"]+)"', html)
        assert match, html
        token = match.group(1)
    return {"csrf_token": token, **values}


def test_signup_json_returns_csrf_and_sets_session(client, settings):
    email, token = signup_json(client)
    assert token
    resp = client.get("/api/csrf")
    assert resp.status_code == 200
    assert resp.json()["csrf_token"] == token


def test_signup_duplicate_email_409(client):
    email, _ = signup_json(client)
    resp = client.post("/signup", json={"email": email, "password": "another-password-1"}, headers={"Accept": "application/json"})
    assert resp.status_code == 409


def test_signup_weak_password_422(client):
    resp = client.post("/signup", json={"email": "weak@example.com", "password": "short"}, headers={"Accept": "application/json"})
    assert resp.status_code == 422


def test_login_uniform_401_and_throttle_429(client):
    email, _ = signup_json(client)
    client.post("/logout", headers={"X-CSRF-Token": client.get("/api/csrf").json()["csrf_token"]})
    for _ in range(10):
        resp = client.post("/login", json={"email": email, "password": "wrong-password-xx"}, headers={"Accept": "application/json"})
        assert resp.status_code == 401
        assert resp.json()["detail"] == "invalid credentials"
    resp = client.post("/login", json={"email": email, "password": "wrong-password-xx"}, headers={"Accept": "application/json"})
    assert resp.status_code == 429
    assert "retry-after" in {k.lower(): v for k, v in resp.headers.items()}


def test_login_success_and_logout(client):
    email, _ = signup_json(client)
    client.post("/logout", headers={"X-CSRF-Token": client.get("/api/csrf").json()["csrf_token"]})
    resp = client.post("/login", json={"email": email, "password": "test-password-123"}, headers={"Accept": "application/json"})
    assert resp.status_code == 200
    assert resp.json()["csrf_token"]
    assert client.get("/app").status_code == 200


def test_forgot_password_never_enumerates(client, settings):
    resp = client.post(
        "/forgot-password", data=_form(client, email="ghost@example.com")
    )
    assert resp.status_code == 200
    assert _outbox_files(settings) == []


def _capture_reset_token(settings) -> str:
    files = _outbox_files(settings)
    assert files, "expected a reset email in the outbox"
    import email as email_mod

    message = email_mod.message_from_string(files[-1].read_text())
    payload = message.get_payload(decode=True)
    body = payload.decode() if payload else message.get_payload()
    match = re.search(r"token=([A-Za-z0-9_\-\.]+)", body)
    assert match, body
    return match.group(1)


def test_reset_password_flow_and_session_revocation(client, settings, db):
    email, _ = signup_json(client)
    client.post("/forgot-password", data=_form(client, email=email))
    token = _capture_reset_token(settings)
    _refresh(db)
    assert db.query(SessionRow).count() >= 1

    resp = client.post(
        "/reset-password",
        data=_form(client, token=token, password="brand-new-password-1"),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    # all sessions revoked (FR-028)
    _refresh(db)
    assert db.query(SessionRow).count() == 0
    # token is single-use now
    resp = client.post(
        "/reset-password",
        data=_form(client, token=token, password="another-new-password-1"),
    )
    assert resp.status_code == 410
    # new password works
    resp = client.post("/login", json={"email": email, "password": "brand-new-password-1"}, headers={"Accept": "application/json"})
    assert resp.status_code == 200


def test_forged_reset_token_410_no_side_effects(client, settings, db):
    """AC-050a: tampered HMAC -> 410, no sessions revoked, no password change."""
    email, _ = signup_json(client)
    client.post("/forgot-password", data=_form(client, email=email))
    token = _capture_reset_token(settings)
    _refresh(db)
    sessions_before = db.query(SessionRow).count()
    # flip one byte in the middle of the token
    mid = len(token) // 2
    forged = token[:mid] + ("A" if token[mid] != "A" else "B") + token[mid + 1:]
    resp = client.post(
        "/reset-password",
        data=_form(client, token=forged, password="attacker-password-1"),
    )
    assert resp.status_code == 410
    _refresh(db)
    assert db.query(SessionRow).count() == sessions_before
    resp = client.post("/login", json={"email": email, "password": "test-password-123"}, headers={"Accept": "application/json"})
    assert resp.status_code == 200


def test_signed_but_unissued_token_410(client, settings, db):
    """AC-050b: correctly signed token that matches no stored row -> 410,
    no side effects on any account."""
    from app.core.security import make_reset_token
    import uuid as uuid_mod

    email, _ = signup_json(client)
    _refresh(db)
    sessions_before = db.query(SessionRow).count()
    # sign a token for a *different* (nonexistent) account id — HMAC is valid
    # but no reset_tokens row exists for it.
    ghost_token, _hash = make_reset_token(str(uuid_mod.uuid4()), settings.app_secret)
    resp = client.post(
        "/reset-password",
        data=_form(client, token=ghost_token, password="attacker-password-1"),
    )
    assert resp.status_code == 410
    _refresh(db)
    assert db.query(SessionRow).count() == sessions_before


def test_cross_site_browser_login_is_refused(client):
    email, _ = signup_json(client)
    client.post(
        "/logout",
        headers={"X-CSRF-Token": client.get("/api/csrf").json()["csrf_token"]},
    )
    response = client.post(
        "/login",
        data=_form(client, email=email, password="test-password-123"),
        headers={
            "Origin": "https://attacker.invalid",
            "Sec-Fetch-Site": "cross-site",
        },
    )
    assert response.status_code == 403
    assert client.get("/app", follow_redirects=False).status_code == 303
