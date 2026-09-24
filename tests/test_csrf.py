"""FR-029 CSRF protection (AC-021): positive, negative, JSON token acquisition."""

from datetime import datetime, timezone

from tests.conftest import add_requirements, csrf_headers, make_rfp, signup_json


def test_csrf_matrix(client, settings, db):
    email, token = signup_json(client)
    # seed an entitled matrix with a row to PATCH
    from app.core.models import Account

    acct = db.query(Account).filter(Account.email == email).one()
    rfp = make_rfp(db, acct)
    rfp.unlocked_at = datetime.now(timezone.utc)
    (row,) = add_requirements(db, rfp, 1)
    db.commit()

    headers = csrf_headers(client)

    # (a) form with valid token
    resp = client.post(
        "/app/account/watchlist",
        data={"csrf_token": headers["X-CSRF-Token"], "watch_naics": "541511"},
    )
    assert resp.status_code in (200, 303)

    # (b) form without token -> 403
    resp = client.post("/app/account/watchlist", data={"watch_naics": "541511"})
    assert resp.status_code == 403

    # (c) form with mismatched token -> 403
    resp = client.post(
        "/app/account/watchlist",
        data={"csrf_token": "bogus-token", "watch_naics": "541511"},
    )
    assert resp.status_code == 403

    # (d) htmx-style PATCH with X-CSRF-Token header -> 200
    resp = client.patch(
        f"/api/rows/{row.id}",
        json={"verified": True},
        headers=headers,
    )
    assert resp.status_code == 200

    # (e) htmx-style PATCH without header -> 403
    resp = client.patch(f"/api/rows/{row.id}", json={"verified": True})
    assert resp.status_code == 403

    # (f) GET /api/csrf with session cookie -> token JSON
    resp = client.get("/api/csrf")
    assert resp.status_code == 200
    api_token = resp.json()["csrf_token"]
    assert api_token == headers["X-CSRF-Token"]

    # (g) PATCH with JSON body using token from (f) -> 200 + JSON response
    resp = client.patch(
        f"/api/rows/{row.id}",
        json={"verified": True, "response": "Test response"},
        headers={"X-CSRF-Token": api_token, "Accept": "application/json", "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["verified"] is True
    assert resp.json()["response"] == "Test response"


def test_login_returns_fresh_csrf_token(client):
    email, first_token = signup_json(client)
    client.post("/logout", headers={"X-CSRF-Token": first_token})
    resp = client.post(
        "/login",
        json={"email": email, "password": "test-password-123"},
        headers={"Accept": "application/json"},
    )
    assert resp.status_code == 200
    second_token = resp.json()["csrf_token"]
    # rotated on login
    assert second_token != first_token
    assert client.get("/api/csrf").json()["csrf_token"] == second_token
