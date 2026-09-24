"""Checkout (FR-013) + Stripe client seam.

The real adapter talks to Stripe via the ``stripe`` package (production
image). When ``STRIPE_SECRET`` is empty (dev/test) a stub adapter returns a
local URL; a dev-only endpoint can then simulate completion, exercising the
identical webhook path. Checkout guards (FR-013/AC-026): 409 on a
zero-requirement matrix, 409 when total rows <= 10 (already free), 409 when
already entitled.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.orm import Session

from app.billing.entitlement import is_entitled, requirement_count
from app.core.config import Settings
from app.core.models import Account, Rfp


class CheckoutRejected(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass
class CheckoutSession:
    id: str
    url: str


class StripeClient(Protocol):
    def create_checkout_session(
        self, *, kind: str, price_id: str, success_url: str, cancel_url: str, metadata: dict[str, str]
    ) -> CheckoutSession: ...


class RealStripeClient:
    def __init__(self, secret: str) -> None:
        self.secret = secret

    def create_checkout_session(self, *, kind, price_id, success_url, cancel_url, metadata) -> CheckoutSession:
        import stripe

        stripe.api_key = self.secret
        mode = "subscription" if kind == "plan" else "payment"
        session = stripe.checkout.Session.create(
            mode=mode,
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            metadata=metadata,
        )
        return CheckoutSession(id=session["id"], url=session["url"])


class StubStripeClient:
    """Dev/test checkout: no network; completion is simulated through the
    dev stub endpoint, which feeds the identical webhook code path."""

    def create_checkout_session(self, *, kind, price_id, success_url, cancel_url, metadata) -> CheckoutSession:
        sid = f"stub-{uuid.uuid4()}"
        sep = "&" if "?" in success_url else "?"
        return CheckoutSession(id=sid, url=f"{success_url}{sep}session_id={sid}")


def get_stripe_client(settings: Settings) -> StripeClient:
    if settings.stripe_secret:
        return RealStripeClient(settings.stripe_secret)
    return StubStripeClient()


def create_checkout(
    session: Session,
    settings: Settings,
    *,
    account: Account,
    rfp: Rfp,
    kind: str,
    client: StripeClient | None = None,
) -> CheckoutSession:
    if kind not in ("single", "plan"):
        raise CheckoutRejected(422, "kind must be 'single' or 'plan'")
    total = requirement_count(session, rfp.id)
    if total == 0:
        raise CheckoutRejected(409, "This matrix has no requirements — nothing to unlock")
    if total <= 10:
        raise CheckoutRejected(409, "All rows are already visible — this matrix is free")
    if is_entitled(session, account.id, rfp):
        raise CheckoutRejected(409, "This matrix is already unlocked")
    price_id = settings.price_single if kind == "single" else settings.price_plan
    sc = client or get_stripe_client(settings)
    return sc.create_checkout_session(
        kind=kind,
        price_id=price_id,
        success_url=f"{settings.public_url}/billing/return?m={rfp.id}",
        cancel_url=f"{settings.public_url}/app/matrices/{rfp.id}",
        metadata={"rfp_id": str(rfp.id), "account_id": str(account.id), "kind": kind},
    )
