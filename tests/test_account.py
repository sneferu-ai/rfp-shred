"""S9 account: watchlist roundtrip, usage widget, delete matrix, delete
account cascade (FR-027/038/039, AC-025, AC-038 partial)."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.core import usage as usage_mod
from app.core.models import (
    Account,
    AuditEvent,
    CorpusAnnotation,
    Correction,
    Entitlement,
    FilteredLine,
    InferenceLog,
    Requirement,
    Rfp,
    SessionRow,
)
from tests.conftest import (
    add_requirements,
    csrf_headers,
    make_rfp,
    refresh_db,
    signup_json,
)


def test_account_page_with_active_plan_renders(client, db):
    """Regression: the plan section must render on SQLite (naive datetimes
    must never reach a template comparison)."""
    from datetime import timedelta

    email, _ = signup_json(client)
    acct = db.query(Account).filter(Account.email == email).one()
    db.add(
        Entitlement(
            account_id=acct.id, kind="plan", active=True,
            period_end=datetime.now(timezone.utc) + timedelta(days=20),
        )
    )
    db.commit()
    resp = client.get("/app/account")
    assert resp.status_code == 200
    assert "Unlimited monthly — active" in resp.text


def test_watchlist_roundtrip(client, db):
    email, _ = signup_json(client)
    headers = csrf_headers(client)
    resp = client.post(
        "/app/account/watchlist",
        data={"csrf_token": headers["X-CSRF-Token"], "watch_naics": "541511, 541512", "watch_set_asides": ["SDVOSB", "8A"]},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    refresh_db(db)
    acct = db.query(Account).filter(Account.email == email).one()
    assert acct.watch_naics == ["541511", "541512"]
    assert set(acct.watch_set_asides) == {"SDVOSB", "8A"}


def test_usage_widget_value(client, db):
    email, _ = signup_json(client)
    acct = db.query(Account).filter(Account.email == email).one()
    db.add(InferenceLog(account_id=acct.id, rfp_id=None, tokens_in=1_000_000, tokens_out=1_000_000, estimated_cost_usd=Decimal("18.00")))
    db.commit()
    resp = client.get("/app/account")
    assert resp.status_code == 200
    assert "$18.00 of $100.00 used" in resp.text
    # same query backs the widget (S9 SQL contract)
    assert usage_mod.monthly_usage_usd(db, acct.id) == 18.0


def test_delete_matrix_removes_rows_source_and_keeps_account(client, db, tmp_path):
    email, _ = signup_json(client)
    acct = db.query(Account).filter(Account.email == email).one()
    source = tmp_path / "matrix-source.pdf"
    source.write_bytes(b"source")
    rfp = make_rfp(db, acct, stored_path=str(source))
    add_requirements(db, rfp, 5)
    db.commit()
    resp = client.post(f"/app/account/matrices/{rfp.id}/delete", headers=csrf_headers(client), follow_redirects=False)
    assert resp.status_code == 303
    refresh_db(db)
    assert db.query(Rfp).count() == 0
    assert db.query(Requirement).count() == 0
    assert db.query(Account).count() == 1
    assert not source.exists()


def test_delete_account_cascade(client, db, tmp_path):
    """AC-025: everything cascades; audit_events retained with actor nulled;
    inference_log retained with account nulled; email reusable."""
    email, _ = signup_json(client)
    acct = db.query(Account).filter(Account.email == email).one()
    source = tmp_path / "account-source.pdf"
    source.write_bytes(b"source")
    rfp = make_rfp(db, acct, stored_path=str(source))
    add_requirements(db, rfp, 3)
    db.add(FilteredLine(rfp_id=rfp.id, seq=1, page=1, text_preview="p", full_text="f", filter_reason="r"))
    db.add(Entitlement(account_id=acct.id, kind="single", rfp_id=rfp.id, active=True))
    db.add(InferenceLog(account_id=acct.id, rfp_id=rfp.id, tokens_in=5, tokens_out=5, estimated_cost_usd=Decimal("0.01")))
    db.add(Correction(account_id=acct.id, rfp_id=rfp.id, correction_type="uncheck", old_value=None, new_value=None))
    db.add(CorpusAnnotation(rfp_id=rfp.id, clause_id="l.1", section="L", page=1, text="t", is_binding=True, created_by=acct.id))
    db.add(AuditEvent(actor_id=acct.id, action="export_delivered", entity="rfp", entity_id=str(rfp.id)))
    db.commit()

    resp = client.post("/app/account/delete", headers=csrf_headers(client), follow_redirects=False)
    assert resp.status_code == 303
    refresh_db(db)

    assert db.query(Account).count() == 0
    assert db.query(Rfp).count() == 0
    assert db.query(Requirement).count() == 0
    assert db.query(FilteredLine).count() == 0
    assert db.query(Entitlement).count() == 0
    assert db.query(SessionRow).count() == 0
    assert db.query(CorpusAnnotation).count() == 0  # cascades with rfp
    # corrections cascade with the account's rfps (FR-027 lists them in the
    # cascade set; corrections.rfp_id is ON DELETE CASCADE per section 5)
    assert db.query(Correction).count() == 0
    # retained with nulled references
    log = db.query(InferenceLog).one()
    assert log.account_id is None and log.rfp_id is None
    event = db.query(AuditEvent).one()
    assert event.actor_id is None and event.action == "export_delivered"
    assert not source.exists()

    # email reusable for a new signup
    resp = client.post("/signup", json={"email": email, "password": "second-account-123"}, headers={"Accept": "application/json"})
    assert resp.status_code == 201


@pytest.mark.parametrize("delete_account", [False, True], ids=["matrix", "account"])
def test_source_file_survives_failed_delete_transaction(
    client, db, tmp_path, monkeypatch, delete_account
):
    """Irreversible bytes are retained whenever request commit rolls back."""
    email, _ = signup_json(client)
    acct = db.query(Account).filter(Account.email == email).one()
    source = tmp_path / f"rollback-{'account' if delete_account else 'matrix'}.pdf"
    source.write_bytes(b"source must survive")
    rfp = make_rfp(db, acct, stored_path=str(source))
    db.commit()
    headers = csrf_headers(client)

    class CommitFailureSession(Session):
        def commit(self):
            raise RuntimeError("forced commit failure")

    failing_factory = sessionmaker(
        bind=client.app.state.engine,
        class_=CommitFailureSession,
        expire_on_commit=False,
        future=True,
    )
    monkeypatch.setattr(client.app.state, "session_factory", failing_factory)
    endpoint = "/app/account/delete" if delete_account else f"/app/account/matrices/{rfp.id}/delete"

    with pytest.raises(RuntimeError, match="forced commit failure"):
        client.post(endpoint, headers=headers, follow_redirects=False)

    refresh_db(db)
    assert source.exists()
    assert db.get(Account, acct.id) is not None
    assert db.get(Rfp, rfp.id) is not None
    assert db.get(Rfp, rfp.id).stored_path == str(source)
