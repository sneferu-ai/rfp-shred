"""FR-012 preview gate + FR-015 entitlement (AC-009, AC-026)."""

from datetime import datetime, timedelta, timezone

from app.core.models import Account, Entitlement, Requirement
from tests.conftest import (
    add_requirements,
    csrf_headers,
    make_account,
    make_rfp,
    refresh_db,
    signup_json,
)


def _account_by_email(db, email):
    return db.query(Account).filter(Account.email == email).one()


def test_63_row_matrix_shows_exactly_ten_with_locked_count(client, db):
    email, _ = signup_json(client)
    acct = _account_by_email(db, email)
    rfp = make_rfp(db, acct)
    rows = add_requirements(db, rfp, 63)
    db.commit()

    # HTML surface
    resp = client.get(f"/app/matrices/{rfp.id}")
    assert resp.status_code == 200
    body = resp.text
    assert "53 further requirement" in body
    # rows 11+ are absent from the page source entirely (no hidden markup)
    assert "requirement number 11 " not in body
    assert "l.11" not in body
    for i in range(1, 11):
        assert f"l.{i}<" in body or f"l.{i}\"" in body or f"l.{i} " in body

    # JSON surface matches the DB's first ten rows by seq
    resp = client.get(f"/api/matrices/{rfp.id}")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["locked_count"] == 53
    assert len(payload["rows"]) == 10
    first_ten = [r for r in sorted(rows, key=lambda r: r.seq)[:10]]
    for got, want in zip(payload["rows"], first_ten):
        assert got["clause_id"] == want.clause_id
        assert got["section"] == want.section
        assert got["page"] == want.page
        assert got["body"] == want.body

    # PATCH on a locked row -> 404 (no existence leak)
    locked = sorted(rows, key=lambda r: r.seq)[10]
    resp = client.patch(
        f"/api/rows/{locked.id}", json={"verified": True}, headers=csrf_headers(client)
    )
    assert resp.status_code == 404


def test_7_row_matrix_is_fully_visible_and_free(client, db):
    email, _ = signup_json(client)
    acct = _account_by_email(db, email)
    rfp = make_rfp(db, acct)
    rows = add_requirements(db, rfp, 7)
    db.commit()

    resp = client.get(f"/app/matrices/{rfp.id}")
    assert resp.status_code == 200
    assert "Open this matrix" not in resp.text  # no unlock CTA
    for row in rows:
        assert row.body in resp.text

    payload = client.get(f"/api/matrices/{rfp.id}").json()
    assert payload["locked_count"] == 0
    assert len(payload["rows"]) == 7
    assert payload["unlocked"] is True  # <=10 rows are always entitled

    # checkout is refused: already-free matrix (AC-026b)
    resp = client.post(
        f"/app/matrices/{rfp.id}/unlock",
        data={"kind": "single"},
        headers=csrf_headers(client),
    )
    assert resp.status_code == 409


def test_zero_row_matrix_empty_state_no_cta(client, db):
    email, _ = signup_json(client)
    acct = _account_by_email(db, email)
    rfp = make_rfp(db, acct)
    db.commit()

    resp = client.get(f"/app/matrices/{rfp.id}")
    assert resp.status_code == 200
    assert "No binding requirements detected" in resp.text
    assert "Open this matrix" not in resp.text

    resp = client.post(
        f"/app/matrices/{rfp.id}/unlock",
        data={"kind": "single"},
        headers=csrf_headers(client),
    )
    assert resp.status_code == 409


def test_entitlement_resolution_matrix(db, settings):
    from app.billing.entitlement import is_entitled

    acct = make_account(db)
    other = make_account(db)
    rfp = make_rfp(db, acct)
    add_requirements(db, rfp, 42)
    db.commit()

    # locked by default
    assert is_entitled(db, acct.id, rfp) is False
    # unlocked_at opens and never reverts
    rfp.unlocked_at = datetime.now(timezone.utc)
    db.commit()
    assert is_entitled(db, acct.id, rfp) is True
    rfp.unlocked_at = datetime.now(timezone.utc) - timedelta(days=90)
    db.commit()
    assert is_entitled(db, acct.id, rfp) is True
    rfp.unlocked_at = None  # reset for the other branches

    # single entitlement
    db.add(Entitlement(account_id=acct.id, kind="single", rfp_id=rfp.id, active=True))
    db.commit()
    assert is_entitled(db, acct.id, rfp) is True
    assert is_entitled(db, other.id, rfp) is False

    # plan entitlement (active, future period_end)
    db.add(
        Entitlement(
            account_id=other.id, kind="plan", active=True,
            period_end=datetime.now(timezone.utc) + timedelta(days=10),
        )
    )
    db.commit()
    assert is_entitled(db, other.id, rfp) is True
    # expired plan closes
    for e in db.query(Entitlement).filter(Entitlement.kind == "plan"):
        e.period_end = datetime.now(timezone.utc) - timedelta(days=1)
    db.commit()
    assert is_entitled(db, other.id, rfp) is False

    # <=10 rows always entitled
    small = make_rfp(db, acct)
    add_requirements(db, small, 4)
    db.commit()
    assert is_entitled(db, acct.id, small) is True
