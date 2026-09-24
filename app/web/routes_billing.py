"""S7 checkout handoff, webhook (S11), and S8 export endpoints."""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select

from app.billing import webhook as webhook_mod
from app.billing.checkout import CheckoutRejected, create_checkout
from app.billing.entitlement import is_entitled
from app.core.config import Settings
from app.core.models import Requirement
from app.core.ratelimit import LIMITS
from app.web import deps
from app.web.exports import build_docx, build_xlsx
from app.web.routes_pages import _get_rfp

router = APIRouter()


@router.post("/app/matrices/{rfp_id}/unlock", dependencies=[Depends(deps.csrf_protect)])
async def unlock(
    request: Request,
    rfp_id: str,
    ctx: deps.WebContext = Depends(deps.require_account),
    settings: Settings = Depends(deps.get_settings),
):
    limit, window = LIMITS["checkout"]
    allowed, retry_after = request.app.state.rate_limiter.hit(
        "checkout", str(ctx.account.id), limit, window
    )
    if not allowed:
        raise HTTPException(status_code=429, detail="Too many checkout attempts", headers={"Retry-After": str(retry_after)})
    rfp = _get_rfp(ctx.db, rfp_id, ctx.account)
    form = await request.form()
    kind = str(form.get("kind", "single"))
    try:
        session = create_checkout(ctx.db, settings, account=ctx.account, rfp=rfp, kind=kind)
    except CheckoutRejected as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail)
    return RedirectResponse(session.url, status_code=303)


@router.get("/billing/return", response_class=HTMLResponse)
def billing_return(request: Request, m: str = "", ctx: deps.WebContext = Depends(deps.web_context)):
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "billing_return.html", {"ctx": ctx, "matrix_id": m})


@router.post("/stripe/webhook")
async def stripe_webhook(
    request: Request,
    settings: Settings = Depends(deps.get_settings),
    db=Depends(deps.get_db),
):
    """FR-014. CSRF-exempt: authenticity comes from the Stripe signature."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    status_code, outcome = webhook_mod.handle_webhook(
        db, payload, sig_header,
        secret=settings.stripe_webhook_secret or "dev-webhook-secret",
        mailer=request.app.state.mailer,
    )
    return Response(content=json.dumps({"outcome": outcome}), status_code=status_code, media_type="application/json")


@router.post("/dev/stub-checkout-complete", dependencies=[Depends(deps.csrf_protect)])
async def stub_checkout_complete(
    request: Request,
    ctx: deps.WebContext = Depends(deps.require_account),
    settings: Settings = Depends(deps.get_settings),
):
    """Dev/test only: simulate a completed checkout through the identical
    webhook processing path. Refused when a real Stripe secret is set."""
    if settings.stripe_secret or settings.environment == "prod":
        raise deps.not_found()
    form = await request.form()
    rfp_id = str(form.get("rfp_id", ""))
    kind = str(form.get("kind", "single"))
    event = {
        "id": f"evt-{uuid.uuid4()}",
        "type": "checkout.session.completed",
        "data": {"object": {"id": f"stub-{uuid.uuid4()}", "metadata": {
            "rfp_id": rfp_id, "account_id": str(ctx.account.id), "kind": kind,
        }}},
    }
    webhook_mod.process_event(ctx.db, event, request.app.state.mailer)
    return RedirectResponse(f"/app/matrices/{rfp_id}", status_code=303)


# ---------------------------------------------------------------------------
# S8 — exports
# ---------------------------------------------------------------------------

def _export_guard(request: Request, rfp_id: str, ctx: deps.WebContext):
    rfp = _get_rfp(ctx.db, rfp_id, ctx.account)
    if not is_entitled(ctx.db, ctx.account.id, rfp):
        raise HTTPException(status_code=403, detail="This matrix is locked")
    rows = ctx.db.execute(
        select(Requirement).where(Requirement.rfp_id == rfp.id).order_by(Requirement.seq.asc())
    ).scalars().all()
    unchecked = [r for r in rows if r.verified_at is None]
    if unchecked:
        raise HTTPException(status_code=409, detail=f"{len(unchecked)} rows unchecked")
    return rfp, rows


def _export_name(rfp) -> str:
    base = (rfp.solicitation_no or rfp.orig_name or str(rfp.id)).replace("/", "-")
    return f"matrix-{base}"


@router.get("/app/matrices/{rfp_id}/export.xlsx")
def export_xlsx(request: Request, rfp_id: str, ctx: deps.WebContext = Depends(deps.require_account)):
    from app.core import auditlog

    rfp, rows = _export_guard(request, rfp_id, ctx)
    auditlog.audit(ctx.db, action="export_delivered", entity="rfp", entity_id=rfp.id, actor_id=ctx.account.id)
    ctx.db.flush()
    return Response(
        content=build_xlsx(rows),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{_export_name(rfp)}.xlsx"'},
    )


@router.get("/app/matrices/{rfp_id}/export.docx")
def export_docx(request: Request, rfp_id: str, ctx: deps.WebContext = Depends(deps.require_account)):
    from app.core import auditlog

    rfp, rows = _export_guard(request, rfp_id, ctx)
    auditlog.audit(ctx.db, action="export_delivered", entity="rfp", entity_id=rfp.id, actor_id=ctx.account.id)
    ctx.db.flush()
    return Response(
        content=build_docx(rows),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{_export_name(rfp)}.docx"'},
    )
