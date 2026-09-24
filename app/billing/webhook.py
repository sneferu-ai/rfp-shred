"""Stripe webhook (FR-014).

Signature verification uses stdlib HMAC (Stripe scheme: ``t.payload`` signed
with the endpoint secret) so the code path is fully exercised in tests.
Every event is stored by provider id for idempotency; entitlement creation
and ``rfps.unlocked_at`` happen in ONE transaction (a partial failure rolls
back entirely). Processing failures retry with backoff up to 3 attempts,
then dead-letter to ``audit_events``. Disputes/refunds are stored but not
auto-processed in beta (A-022).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import auditlog
from app.core.mailer import Mailer
from app.core.models import Account, AuditEvent, Entitlement, Rfp, StripeEvent, as_aware

SIGNATURE_TOLERANCE_S = 300
MAX_ATTEMPTS = 3
PLAN_PERIOD_DAYS = 30


class SignatureError(RuntimeError):
    pass


def verify_signature(payload: bytes, sig_header: str, secret: str, now: float | None = None) -> None:
    if not sig_header:
        raise SignatureError("missing Stripe-Signature header")
    parts: dict[str, str] = {}
    for piece in sig_header.split(","):
        if "=" in piece:
            k, v = piece.split("=", 1)
            parts.setdefault(k.strip(), v.strip())
    ts = parts.get("t")
    sig = parts.get("v1")
    if not ts or not sig:
        raise SignatureError("malformed Stripe-Signature header")
    now = now if now is not None else time.time()
    try:
        if abs(now - float(ts)) > SIGNATURE_TOLERANCE_S:
            raise SignatureError("stale timestamp")
    except ValueError:
        raise SignatureError("bad timestamp")
    expected = hmac.new(secret.encode(), f"{ts}.{payload.decode()}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise SignatureError("signature mismatch")


def sign_payload(payload: bytes, secret: str, ts: int | None = None) -> str:
    """Test/dev helper: build a valid Stripe-Signature header."""
    ts = ts or int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.{payload.decode()}".encode(), hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def _plan_extend(period_end: datetime | None) -> datetime:
    now = datetime.now(timezone.utc)
    base = period_end if as_aware(period_end) and as_aware(period_end) > now else now
    return base + timedelta(days=PLAN_PERIOD_DAYS)


def _event_period_end(obj: dict[str, Any]) -> datetime | None:
    """Return Stripe's canonical paid-through timestamp when present.

    Invoice payloads normally carry it on a line item's ``period.end``.  The
    direct keys keep the parser tolerant of fixture/proxy payloads that expose
    the same value at the top level.
    """
    candidates: list[Any] = [obj.get("period_end"), obj.get("current_period_end")]
    lines = obj.get("lines") or {}
    if isinstance(lines, dict):
        for line in lines.get("data") or []:
            if isinstance(line, dict):
                period = line.get("period") or {}
                if isinstance(period, dict):
                    candidates.append(period.get("end"))
    parsed: list[datetime] = []
    for value in candidates:
        try:
            if value:
                parsed.append(datetime.fromtimestamp(float(value), tz=timezone.utc))
        except (TypeError, ValueError, OSError):
            continue
    return max(parsed) if parsed else None


def _plan_was_canceled(session: Session, plan: Entitlement) -> bool:
    """A subscription deletion is terminal for that Stripe subscription id.

    Stripe may deliver older ``invoice.paid``/``subscription.updated`` events
    after a deletion.  The append-only audit event is a durable tombstone, so
    those late events cannot reactivate the entitlement.  A genuine resubscribe
    has a new Stripe subscription id and therefore a new entitlement.
    """
    return session.execute(
        select(AuditEvent.id).where(
            AuditEvent.action == "plan_canceled",
            AuditEvent.entity == "entitlement",
            AuditEvent.entity_id == str(plan.id),
        ).limit(1)
    ).first() is not None


def _apply_paid_period(plan: Entitlement, obj: dict[str, Any]) -> bool:
    """Advance a plan without double-counting the checkout's first invoice.

    Checkout grants an initial 30-day window immediately.  The initial
    ``invoice.paid`` confirms that same window and must not add another month.
    Renewal invoices use Stripe's canonical line period when available; the
    30-day fallback applies only when the stored window has actually elapsed.
    Returns whether ``period_end`` changed.
    """
    now = datetime.now(timezone.utc)
    current = as_aware(plan.period_end)
    canonical = _event_period_end(obj)
    replacement = current
    if canonical is not None and (current is None or canonical > current):
        replacement = canonical
    elif canonical is None and (current is None or current <= now):
        replacement = now + timedelta(days=PLAN_PERIOD_DAYS)
    if replacement != current:
        plan.period_end = replacement
        return True
    return False


def process_event(session: Session, event: dict[str, Any], mailer: Mailer | None = None) -> str:
    """Apply one Stripe event. Each operation is individually idempotent;
    replays and out-of-order delivery never duplicate or revoke incorrectly.
    Returns a short outcome string."""
    etype = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}
    metadata = obj.get("metadata") or {}
    now = datetime.now(timezone.utc)

    if etype == "checkout.session.completed":
        rfp_id = metadata.get("rfp_id")
        account_id = metadata.get("account_id")
        kind = metadata.get("kind", "single")
        if not rfp_id or not account_id:
            raise RuntimeError("checkout.session.completed missing metadata")
        rfp = session.get(Rfp, uuid.UUID(rfp_id))
        if rfp is None:
            raise RuntimeError(f"rfp {rfp_id} not found")
        session_id = obj.get("id", "")
        subscription_id = obj.get("subscription") or ""
        # Plan entitlements key on the SUBSCRIPTION id: invoice.paid and
        # customer.subscription.* events reference the subscription, never
        # the checkout session. Single purchases key on the session id.
        # Dedup matches either ref so a cross-event replay (new event id,
        # same checkout) still cannot double-grant.
        refs = [r for r in (session_id, subscription_id) if r]
        existing = session.execute(
            select(Entitlement).where(Entitlement.stripe_ref.in_(refs))
        ).scalars().first() if refs else None
        if existing is None:
            period_end = _plan_extend(None) if kind == "plan" else None
            session.add(
                Entitlement(
                    account_id=uuid.UUID(account_id),
                    kind=kind,
                    rfp_id=rfp.id if kind == "single" else None,
                    stripe_ref=subscription_id if kind == "plan" and subscription_id else session_id,
                    active=True,
                    period_end=period_end,
                )
            )
        if rfp.unlocked_at is None:
            rfp.unlocked_at = now
        auditlog.audit(
            session, action="entitlement_granted", entity="rfp", entity_id=rfp.id,
            actor_id=uuid.UUID(account_id),
        )
        return "entitlement_granted"

    if etype == "invoice.paid":
        sub = obj.get("subscription") or obj.get("id", "")
        plan = session.execute(
            select(Entitlement).where(
                Entitlement.kind == "plan", Entitlement.stripe_ref == sub
            ).with_for_update()
        ).scalars().first()
        if plan is not None:
            if _plan_was_canceled(session, plan):
                return "stored_canceled_plan"
            changed = _apply_paid_period(plan, obj)
            plan.active = True
            action = "plan_extended" if changed else "plan_payment_confirmed"
            auditlog.audit(session, action=action, entity="entitlement", entity_id=plan.id)
            return action
        return "stored_no_matching_plan"

    if etype == "invoice.payment_failed":
        sub = obj.get("subscription") or ""
        plan = session.execute(
            select(Entitlement).where(
                Entitlement.kind == "plan", Entitlement.stripe_ref == sub
            ).with_for_update()
        ).scalars().first()
        if plan is not None:
            if _plan_was_canceled(session, plan):
                return "stored_canceled_plan"
            plan.active = False
            auditlog.audit(session, action="plan_payment_failed", entity="entitlement", entity_id=plan.id)
            account = session.get(Account, plan.account_id)
            if mailer is not None and account is not None:
                mailer.send(
                    to=account.email,
                    subject="[RFP Shred] Payment failed",
                    body="Your RFP Shred subscription payment failed. Update your payment method to keep unlimited access.",
                )
            return "plan_marked_inactive"
        return "stored_no_matching_plan"

    if etype == "customer.subscription.deleted":
        sub = obj.get("id", "")
        plan = session.execute(
            select(Entitlement).where(
                Entitlement.kind == "plan", Entitlement.stripe_ref == sub
            ).with_for_update()
        ).scalars().first()
        if plan is not None:
            plan.active = False
            plan.period_end = now
            auditlog.audit(session, action="plan_canceled", entity="entitlement", entity_id=plan.id)
            return "plan_canceled"
        return "stored_no_matching_plan"

    if etype == "customer.subscription.updated":
        sub = obj.get("id", "")
        plan = session.execute(
            select(Entitlement).where(
                Entitlement.kind == "plan", Entitlement.stripe_ref == sub
            ).with_for_update()
        ).scalars().first()
        if plan is not None:
            if _plan_was_canceled(session, plan):
                return "stored_canceled_plan"
            status = obj.get("status", "active")
            plan.active = status in ("active", "trialing")
            period_unix = (obj.get("current_period_end") or 0)
            if period_unix:
                plan.period_end = datetime.fromtimestamp(period_unix, tz=timezone.utc)
            auditlog.audit(session, action="plan_synced", entity="entitlement", entity_id=plan.id)
            return "plan_synced"
        return "stored_no_matching_plan"

    # charge.dispute.created / charge.refunded and anything else: stored,
    # not auto-processed in beta (A-022).
    return "stored_unhandled"


def handle_webhook(
    session: Session,
    payload: bytes,
    sig_header: str,
    *,
    secret: str,
    mailer: Mailer | None = None,
) -> tuple[int, str]:
    """Full webhook entry. Returns (http_status, outcome)."""
    try:
        verify_signature(payload, sig_header, secret)
    except SignatureError:
        return 400, "unsigned_or_bad_signature"
    try:
        event = json.loads(payload)
    except json.JSONDecodeError:
        return 400, "invalid_json"
    event_id = event.get("id")
    if not event_id:
        return 400, "missing_event_id"

    existing = session.get(StripeEvent, event_id)
    if existing is not None:
        if existing.status == "processed":
            return 200, "already_processed"
        if existing.status == "dead_lettered":
            return 200, "dead_lettered"
        record = existing
    else:
        record = StripeEvent(event_id=event_id, event_type=event.get("type", ""), status="processing")
        session.add(record)
        session.flush()

    try:
        outcome = process_event(session, event, mailer)
    except Exception as exc:  # retry with backoff handled by caller replay
        record.attempts += 1
        if record.attempts >= MAX_ATTEMPTS:
            record.status = "dead_lettered"
            auditlog.audit(
                session, action="stripe_event_dead_lettered", entity="stripe_event",
                entity_id=event_id,
            )
        session.flush()
        return 500, f"processing_failed: {exc}"
    record.status = "processed"
    record.processed_at = datetime.now(timezone.utc)
    session.flush()
    return 200, outcome
