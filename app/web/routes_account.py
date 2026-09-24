"""S9 — Account: plan status, unlock history, watchlist editor, usage
widget, amendment links, delete matrix, delete account (FR-027)."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.core import usage as usage_mod
from app.core.config import Settings
from app.core.deferred_cleanup import defer_unlink_until_commit
from app.core.models import Entitlement, Rfp
from app.web import deps
from app.web.routes_pages import _get_rfp

router = APIRouter()

SET_ASIDE_CHOICES = ["SBC", "SDVOSB", "VOSB", "8A", "HUBZone", "WOSB", "EDWOSB"]


@router.get("/app/account", response_class=HTMLResponse)
def account_page(request: Request, ctx: deps.WebContext = Depends(deps.require_account)):
    db = ctx.db
    settings: Settings = request.app.state.settings
    entitlements = db.execute(
        select(Entitlement).where(Entitlement.account_id == ctx.account.id).order_by(Entitlement.created_at.desc())
    ).scalars().all()
    plan = next((e for e in entitlements if e.kind == "plan"), None)
    from app.core.models import as_aware

    now = datetime.now(timezone.utc)
    plan_active = bool(
        plan and plan.active and (plan.period_end is None or as_aware(plan.period_end) > now)
    )
    singles = [e for e in entitlements if e.kind == "single"]
    amendments = db.execute(
        select(Rfp).where(Rfp.account_id == ctx.account.id, Rfp.amends_rfp_id.is_not(None))
    ).scalars().all()
    parent_map = {
        a.amends_rfp_id: db.get(Rfp, a.amends_rfp_id) for a in amendments
    }
    month_used = usage_mod.monthly_usage_usd(db, ctx.account.id)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "account.html",
        {
            "ctx": ctx,
            "plan": plan,
            "plan_active": plan_active,
            "singles": singles,
            "amendments": amendments,
            "parent_map": parent_map,
            "month_used": month_used,
            "cap": settings.monthly_inference_cap_usd,
            "set_aside_choices": SET_ASIDE_CHOICES,
            "now": datetime.now(timezone.utc),
        },
    )


@router.post("/app/account/watchlist", dependencies=[Depends(deps.csrf_protect)])
async def save_watchlist(request: Request, ctx: deps.WebContext = Depends(deps.require_account)):
    form = await request.form()
    naics_raw = str(form.get("watch_naics", ""))
    naics = [n.strip() for n in naics_raw.replace(";", ",").split(",") if n.strip()]
    set_asides = [str(v) for v in form.getlist("watch_set_asides")]
    ctx.account.watch_naics = naics[:100]
    ctx.account.watch_set_asides = [s for s in set_asides if s in SET_ASIDE_CHOICES]
    ctx.db.flush()
    return RedirectResponse("/app/account", status_code=303)


@router.post("/app/account/matrices/{rfp_id}/delete", dependencies=[Depends(deps.csrf_protect)])
def delete_matrix(request: Request, rfp_id: str, ctx: deps.WebContext = Depends(deps.require_account)):
    db = ctx.db
    rfp = _get_rfp(db, rfp_id, ctx.account)
    _defer_source_file_destroy(db, rfp)
    db.delete(rfp)  # FK CASCADE removes runs/jobs/rows/filtered/drops/outputs/annotations
    db.flush()
    return RedirectResponse("/app", status_code=303)


@router.post("/app/account/delete", dependencies=[Depends(deps.csrf_protect)])
def delete_account(request: Request, ctx: deps.WebContext = Depends(deps.require_account)):
    db = ctx.db
    account = ctx.account
    rfps = db.execute(select(Rfp).where(Rfp.account_id == account.id)).scalars().all()
    for rfp in rfps:
        _defer_source_file_destroy(db, rfp)
    # FK actions do the rest: sessions/reset_tokens/rfps(+children)/entitlements
    # CASCADE; audit_events.actor_id, inference_log.*, corrections.account_id,
    # corpus_annotations.created_by SET NULL. Email is reusable afterwards.
    db.delete(account)
    db.flush()
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(deps.COOKIE_NAME)
    return response


def _defer_source_file_destroy(db: OrmSession, rfp: Rfp) -> None:
    if rfp.stored_path:
        defer_unlink_until_commit(db, rfp.stored_path)
        rfp.stored_path = None
