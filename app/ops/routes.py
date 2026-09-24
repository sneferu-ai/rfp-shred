"""S10 — Founder console: sweep history, quality monitoring, audit log
(FR-023). Staff only; everyone else gets 404."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from app.core.models import AuditEvent, QualityRun, SweepRun
from app.web import deps

router = APIRouter(prefix="/ops")

PAGE_SIZE = 50


@router.get("/sweeps", response_class=HTMLResponse)
def ops_sweeps(request: Request, ctx: deps.WebContext = Depends(deps.require_staff)):
    runs = ctx.db.execute(
        select(SweepRun).order_by(SweepRun.started_at.desc()).limit(200)
    ).scalars().all()
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "ops_sweeps.html", {"ctx": ctx, "runs": runs})


@router.get("/quality", response_class=HTMLResponse)
def ops_quality(request: Request, ctx: deps.WebContext = Depends(deps.require_staff)):
    runs = ctx.db.execute(
        select(QualityRun).order_by(QualityRun.run_at.desc()).limit(200)
    ).scalars().all()
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "ops_quality.html", {"ctx": ctx, "runs": runs})


@router.get("/audit", response_class=HTMLResponse)
def ops_audit(
    request: Request,
    page: int = Query(default=1, ge=1),
    action: str = Query(default=""),
    ctx: deps.WebContext = Depends(deps.require_staff),
):
    stmt = select(AuditEvent).order_by(AuditEvent.at.desc())
    if action:
        stmt = stmt.where(AuditEvent.action == action)
    events = ctx.db.execute(stmt.limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE)).scalars().all()
    actions = [
        row[0]
        for row in ctx.db.execute(
            select(AuditEvent.action).distinct().order_by(AuditEvent.action)
        ).all()
    ]
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "ops_audit.html",
        {"ctx": ctx, "events": events, "page": page, "action": action, "actions": actions},
    )
