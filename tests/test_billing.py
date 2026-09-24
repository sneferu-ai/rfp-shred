"""Billing: checkout guards, webhook signature + idempotency + lifecycle
(FR-013/014/015, AC-010)."""

import json
import uuid

import pytest
from datetime import datetime, timedelta, timezone

from app.billing.checkout import CheckoutRejected, create_checkout
from app.billing.entitlement import is_entitled
from app.billing.webhook import handle_webhook, sign_payload
from app.core.models import Account, Entitlement, StripeEvent, as_aware
from tests.conftest import add_requirements, make_account, make_rfp, refresh_db, signup_json, csrf_headers

SECRET = "whsec_test"


def _checkout_completed_event(rfp, account, kind="single", event_id=None, session_id=None, subscription=None):
    obj = {
        "id": session_id or f"cs-{uuid.uuid4()}",
        "metadata": {"rfp_id": str(rfp.id), "account_id": str(account.id), "kind": kind},
    }
    if subscription:
        # Real Stripe subscription-mode checkouts carry the subscription id
        # on the session object — distinct from the session id.
        obj["subscription"] = subscription
    return {
        "id": event_id or f"evt-{uuid.uuid4()}",
        "type": "checkout.session.completed",
        "data": {"object": obj},
    }


def test_checkout_guards(db, settings):
    acct = make_account(db)
    zero = make_rfp(db, acct)
    small = make_rfp(db, acct)
    add_requirements(db, small, 7)
    big = make_rfp(db, acct)
    add_requirements(db, big, 42)
    db.commit()
    with pytest.raises(CheckoutRejected) as e1:
        create_checkout(db, settings, account=acct, rfp=zero, kind="single")
    assert e1.value.status == 409
    with pytest.raises(CheckoutRejected) as e2:
        create_checkout(db, settings, account=acct, rfp=small, kind="single")
    assert e2.value.status == 409
    session = create_checkout(db, settings, account=acct, rfp=big, kind="single")
    assert session.url and session.id
    # already-entitled -> 409
    big.unlocked_at = datetime.now(timezone.utc)
    db.commit()
    with pytest.raises(CheckoutRejected) as e3:
        create_checkout(db, settings, account=acct, rfp=big, kind="single")
    assert e3.value.status == 409


def test_webhook_rejects_unsigned(db, settings):
    status, outcome = handle_webhook(db, b"{}", "bad-header", secret=SECRET)
    assert status == 400


def test_checkout_completed_grants_entitlement_once(db, settings):
    """(a) entitlement + unlocked_at; (b) replay -> still one entitlement."""
    acct = make_account(db)
    rfp = make_rfp(db, acct)
    add_requirements(db, rfp, 20)
    db.commit()

    event = _checkout_completed_event(rfp, acct)
    payload = json.dumps(event).encode()
    status, outcome = handle_webhook(db, payload, sign_payload(payload, SECRET), secret=SECRET)
    assert status == 200 and outcome == "entitlement_granted"
    db.commit()
    refresh_db(db)
    assert is_entitled(db, acct.id, rfp) is True
    assert rfp.unlocked_at is not None
    assert db.query(Entitlement).count() == 1

    # replay the identical event: idempotent (one entitlement only)
    status, outcome = handle_webhook(db, payload, sign_payload(payload, SECRET), secret=SECRET)
    assert status == 200 and outcome == "already_processed"
    refresh_db(db)
    assert db.query(Entitlement).count() == 1


def test_charge_succeeded_grants_nothing(db):
    acct = make_account(db)
    rfp = make_rfp(db, acct)
    add_requirements(db, rfp, 20)
    db.commit()
    event = {"id": f"evt-{uuid.uuid4()}", "type": "charge.succeeded", "data": {"object": {"id": "ch-1", "metadata": {}}}}
    payload = json.dumps(event).encode()
    status, outcome = handle_webhook(db, payload, sign_payload(payload, SECRET), secret=SECRET)
    assert status == 200
    refresh_db(db)
    assert db.query(Entitlement).count() == 0
    assert is_entitled(db, acct.id, rfp) is False


def test_plan_lifecycle(db):
    """(d) invoice.paid extends; (e) subscription.deleted closes; (f) matrices
    opened during the plan stay open after expiry. Uses DISTINCT checkout
    session / subscription ids, exactly as real Stripe delivers them — the
    plan must be keyed on the subscription or every later webhook misses it."""
    acct = make_account(db)
    rfp = make_rfp(db, acct)
    add_requirements(db, rfp, 20)
    sub_id = f"sub-{uuid.uuid4()}"
    session_id = f"cs-{uuid.uuid4()}"
    assert sub_id != session_id
    db.commit()

    # plan purchase via checkout (session id != subscription id)
    event = _checkout_completed_event(rfp, acct, kind="plan", session_id=session_id, subscription=sub_id)
    payload = json.dumps(event).encode()
    status, _ = handle_webhook(db, payload, sign_payload(payload, SECRET), secret=SECRET)
    assert status == 200
    db.commit()
    refresh_db(db)
    plan = db.query(Entitlement).filter(Entitlement.kind == "plan").one()
    # keyed on the subscription id so invoice/subscription webhooks find it
    assert plan.stripe_ref == sub_id
    assert as_aware(plan.period_end) > datetime.now(timezone.utc)
    first_end = as_aware(plan.period_end)

    # The first invoice.paid confirms the checkout's initial window; it must
    # not grant a second free month for the same billing period.
    paid = {"id": f"evt-{uuid.uuid4()}", "type": "invoice.paid", "data": {"object": {"id": f"in-{uuid.uuid4()}", "subscription": sub_id}}}
    payload = json.dumps(paid).encode()
    handle_webhook(db, payload, sign_payload(payload, SECRET), secret=SECRET)
    db.commit()
    refresh_db(db)
    assert as_aware(plan.period_end) == first_end

    # A renewal adopts Stripe's canonical paid-through timestamp.
    renewal_end = datetime.now(timezone.utc) + timedelta(days=61)
    renewal = {
        "id": f"evt-{uuid.uuid4()}",
        "type": "invoice.paid",
        "data": {"object": {
            "id": f"in-{uuid.uuid4()}", "subscription": sub_id,
            "lines": {"data": [{"period": {"end": int(renewal_end.timestamp())}}]},
        }},
    }
    payload = json.dumps(renewal).encode()
    handle_webhook(db, payload, sign_payload(payload, SECRET), secret=SECRET)
    db.commit()
    refresh_db(db)
    assert abs(as_aware(plan.period_end).timestamp() - renewal_end.timestamp()) < 2

    # subscription.deleted closes the plan
    deleted = {"id": f"evt-{uuid.uuid4()}", "type": "customer.subscription.deleted", "data": {"object": {"id": sub_id}}}
    payload = json.dumps(deleted).encode()
    handle_webhook(db, payload, sign_payload(payload, SECRET), secret=SECRET)
    db.commit()
    refresh_db(db)
    assert plan.active is False

    # Out-of-order events for the deleted subscription are terminally ignored.
    late_paid = {
        "id": f"evt-{uuid.uuid4()}", "type": "invoice.paid",
        "data": {"object": {"id": f"in-{uuid.uuid4()}", "subscription": sub_id}},
    }
    payload = json.dumps(late_paid).encode()
    status, outcome = handle_webhook(db, payload, sign_payload(payload, SECRET), secret=SECRET)
    db.commit()
    refresh_db(db)
    assert status == 200 and outcome == "stored_canceled_plan"
    assert plan.active is False

    late_update = {
        "id": f"evt-{uuid.uuid4()}", "type": "customer.subscription.updated",
        "data": {"object": {
            "id": sub_id, "status": "active",
            "current_period_end": int((datetime.now(timezone.utc) + timedelta(days=31)).timestamp()),
        }},
    }
    payload = json.dumps(late_update).encode()
    status, outcome = handle_webhook(db, payload, sign_payload(payload, SECRET), secret=SECRET)
    db.commit()
    refresh_db(db)
    assert status == 200 and outcome == "stored_canceled_plan"
    assert plan.active is False

    # (f) the matrix opened during the plan stays open (unlocked_at set)
    assert is_entitled(db, acct.id, rfp) is True
    # ...but a fresh matrix is locked again
    fresh = make_rfp(db, acct)
    add_requirements(db, fresh, 20)
    db.commit()
    assert is_entitled(db, acct.id, fresh) is False


def test_plan_payment_failed_and_synced_with_distinct_ids(db):
    """invoice.payment_failed deactivates and customer.subscription.updated
    re-syncs — both locate the plan by SUBSCRIPTION id (distinct from the
    checkout session id, as in real Stripe)."""
    acct = make_account(db)
    rfp = make_rfp(db, acct)
    add_requirements(db, rfp, 20)
    sub_id = f"sub-{uuid.uuid4()}"
    db.commit()
    event = _checkout_completed_event(rfp, acct, kind="plan", session_id=f"cs-{uuid.uuid4()}", subscription=sub_id)
    payload = json.dumps(event).encode()
    status, _ = handle_webhook(db, payload, sign_payload(payload, SECRET), secret=SECRET)
    assert status == 200
    db.commit()
    refresh_db(db)
    plan = db.query(Entitlement).filter(Entitlement.kind == "plan").one()

    failed = {"id": f"evt-{uuid.uuid4()}", "type": "invoice.payment_failed",
              "data": {"object": {"id": f"in-{uuid.uuid4()}", "subscription": sub_id}}}
    payload = json.dumps(failed).encode()
    status, outcome = handle_webhook(db, payload, sign_payload(payload, SECRET), secret=SECRET)
    assert status == 200 and outcome == "plan_marked_inactive"
    db.commit()
    refresh_db(db)
    assert plan.active is False

    period_unix = int((datetime.now(timezone.utc) + timedelta(days=31)).timestamp())
    updated = {"id": f"evt-{uuid.uuid4()}", "type": "customer.subscription.updated",
               "data": {"object": {"id": sub_id, "status": "active", "current_period_end": period_unix}}}
    payload = json.dumps(updated).encode()
    status, outcome = handle_webhook(db, payload, sign_payload(payload, SECRET), secret=SECRET)
    assert status == 200 and outcome == "plan_synced"
    db.commit()
    refresh_db(db)
    assert plan.active is True
    assert abs(as_aware(plan.period_end).timestamp() - period_unix) < 2


def test_processing_failure_dead_letters_after_three(db):
    acct = make_account(db)
    bad_event = {
        "id": f"evt-{uuid.uuid4()}",
        "type": "checkout.session.completed",
        "data": {"object": {"id": "cs-x", "metadata": {"rfp_id": str(uuid.uuid4()), "account_id": str(acct.id), "kind": "single"}}},
    }
    payload = json.dumps(bad_event).encode()
    for _ in range(3):
        status, outcome = handle_webhook(db, payload, sign_payload(payload, SECRET), secret=SECRET)
        assert status == 500
        db.commit()
    refresh_db(db)
    record = db.get(StripeEvent, bad_event["id"])
    assert record.status == "dead_lettered"
    assert record.attempts == 3
    status, outcome = handle_webhook(db, payload, sign_payload(payload, SECRET), secret=SECRET)
    assert status == 200 and outcome == "dead_lettered"


def test_dev_stub_checkout_flow(client, settings, db):
    """Full loop: stub checkout -> dev completion -> matrix entitled."""
    email, _ = signup_json(client)
    acct = db.query(Account).filter(Account.email == email).one()
    rfp = make_rfp(db, acct)
    add_requirements(db, rfp, 33)
    db.commit()

    resp = client.post(
        f"/app/matrices/{rfp.id}/unlock", data={"kind": "single"},
        headers=csrf_headers(client), follow_redirects=False,
    )
    assert resp.status_code == 303
    # stub URL points back at /billing/return; the dev endpoint completes it
    resp = client.post(
        "/dev/stub-checkout-complete",
        data={"rfp_id": str(rfp.id), "kind": "single"},
        headers=csrf_headers(client), follow_redirects=False,
    )
    assert resp.status_code == 303
    refresh_db(db)
    assert is_entitled(db, acct.id, rfp) is True
    payload = client.get(f"/api/matrices/{rfp.id}").json()
    assert payload["unlocked"] is True
    assert len(payload["rows"]) == 33
