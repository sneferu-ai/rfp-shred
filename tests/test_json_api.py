"""FR-032 JSON API independence (AC-035, AC-036)."""

from datetime import datetime, timezone

from app.core.models import Account, AuditDrop, FilteredLine
from tests.conftest import add_requirements, csrf_headers, make_rfp, signup_json


def test_api_get_matrix_matrix(client, db):
    """AC-035: 404 without auth; 10 rows without entitlement; all rows with."""
    email, _ = signup_json(client)
    acct = db.query(Account).filter(Account.email == email).one()
    rfp = make_rfp(db, acct)
    rows = add_requirements(db, rfp, 25)
    db.add(FilteredLine(rfp_id=rfp.id, seq=1, page=1, text_preview="x", full_text="x", filter_reason="y"))
    db.add(AuditDrop(rfp_id=rfp.id, seq=1, page=1, excerpt="x", text="y", audit_failure_reason="z"))
    db.commit()

    other_client = client.__class__(client.app)
    resp = other_client.get(f"/api/matrices/{rfp.id}")
    assert resp.status_code == 404

    payload = client.get(f"/api/matrices/{rfp.id}").json()
    assert len(payload["rows"]) == 10
    assert payload["locked_count"] == 15
    assert payload["unlocked"] is False
    assert payload["filtered_count"] == 1
    assert payload["audit_drop_count"] == 1
    for row in payload["rows"]:
        assert set(row) >= {"id", "clause_id", "section", "page", "body", "excerpt", "response", "verified", "tags"}

    rfp.unlocked_at = datetime.now(timezone.utc)
    db.commit()
    resp = client.get(f"/api/matrices/{rfp.id}")
    assert resp.headers["content-type"].startswith("application/json")
    payload = resp.json()
    assert payload["unlocked"] is True
    assert len(payload["rows"]) == 25


def test_api_patch_json_round_trip(client, db):
    """AC-036: CSRF from /api/csrf, JSON PATCH, JSON response, DB confirms."""
    email, _ = signup_json(client)
    acct = db.query(Account).filter(Account.email == email).one()
    rfp = make_rfp(db, acct)
    rfp.unlocked_at = datetime.now(timezone.utc)
    (row,) = add_requirements(db, rfp, 1)
    db.commit()

    token = client.get("/api/csrf").json()["csrf_token"]
    resp = client.patch(
        f"/api/rows/{row.id}",
        json={"verified": True, "response": "Test response"},
        headers={"X-CSRF-Token": token, "Content-Type": "application/json", "Accept": "application/json"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["verified"] is True
    assert resp.json()["response"] == "Test response"
    db.expire_all()
    assert row.verified_at is not None
    assert row.response == "Test response"
