"""S11 machine routes + FR-032 workbench JSON API + FR-016 row PATCH."""

from __future__ import annotations

import json as jsonlib
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from app.billing.entitlement import is_entitled, visible_rows
from app.core.models import AuditDrop, Correction, FilteredLine, Requirement, Rfp
from app.core.ratelimit import LIMITS
from app.web import deps
from app.web.routes_pages import _get_rfp, _latest_run

router = APIRouter()


def _uuid_or_404(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise deps.not_found()


@router.get("/api/csrf")
def api_csrf(ctx: deps.WebContext = Depends(deps.web_context)):
    """FR-029: token retrieval for JSON API clients (authenticated)."""
    if ctx.account is None:
        raise deps.not_found()
    return {"csrf_token": ctx.session_row.csrf_token}


@router.get("/api/jobs/{rfp_id}")
def api_job_status(rfp_id: str, ctx: deps.WebContext = Depends(deps.web_context)):
    if ctx.account is None:
        raise deps.not_found()
    rfp = _get_rfp(ctx.db, rfp_id, ctx.account)
    run = _latest_run(ctx.db, rfp.id)
    stage = run.stage if run is not None else rfp.state
    pages_total = rfp.page_count or 0
    pages_done = pages_total if stage in ("auditing", "ready") else 0
    return {
        "id": str(rfp.id),
        "stage": stage,
        "state": rfp.state,
        "pages_done": pages_done,
        "pages_total": pages_total,
        "error": run.error if run else None,
    }


@router.get("/api/matrices/{rfp_id}")
def api_matrix(rfp_id: str, ctx: deps.WebContext = Depends(deps.web_context)):
    """FR-032. Locked rows never appear in the payload (FR-012)."""
    if ctx.account is None:
        raise deps.not_found()
    db = ctx.db
    rfp = _get_rfp(db, rfp_id, ctx.account)
    rows, locked_count, entitled = visible_rows(db, ctx.account.id, rfp)
    filtered_count = db.execute(
        select(func.count(FilteredLine.id)).where(
            FilteredLine.rfp_id == rfp.id, FilteredLine.re_included.is_(False)
        )
    ).scalar_one()
    audit_drop_count = db.execute(
        select(func.count(AuditDrop.id)).where(
            AuditDrop.rfp_id == rfp.id, AuditDrop.re_included.is_(False)
        )
    ).scalar_one()
    return {
        "id": str(rfp.id),
        "solicitation_no": rfp.solicitation_no,
        "state": rfp.state,
        "unlocked": entitled,
        "locked_count": locked_count,
        "rows": [
            {
                "id": str(row.id),
                "seq": row.seq,
                "clause_id": row.clause_id,
                "section": row.section,
                "page": row.page,
                "body": row.body,
                "excerpt": row.excerpt,
                "response": row.response,
                "verified": row.verified_at is not None,
                "tags": list(row.tags or []),
            }
            for row in rows
        ],
        "filtered_count": int(filtered_count),
        "audit_drop_count": int(audit_drop_count),
    }


@router.patch("/api/rows/{row_id}", dependencies=[Depends(deps.csrf_protect)])
async def api_patch_row(
    request: Request,
    row_id: str,
    ctx: deps.WebContext = Depends(deps.web_context),
):
    """FR-016. Authorization order with identical 404 wording at every step:
    (1) row exists, (2) belongs to caller's account, (3) matrix entitled.
    Entitlement is evaluated at processing time, so a mid-session unlock
    succeeds. Only CSRF mismatch returns 403."""
    if ctx.account is None:
        raise deps.not_found()
    limit, window = LIMITS["row_patch"]
    allowed, retry_after = request.app.state.rate_limiter.hit(
        "row_patch", str(ctx.account.id), limit, window
    )
    if not allowed:
        raise HTTPException(status_code=429, detail="Too many requests", headers={"Retry-After": str(retry_after)})

    db = ctx.db
    row = db.get(Requirement, _uuid_or_404(row_id))
    if row is None:  # (1) exists
        raise deps.not_found()
    rfp = db.get(Rfp, row.rfp_id)
    if rfp is None or rfp.account_id != ctx.account.id:  # (2) ownership
        raise deps.not_found()
    if not is_entitled(db, ctx.account.id, rfp):  # (3) entitlement at processing time
        raise deps.not_found()

    payload = await _payload(request)
    now = datetime.now(timezone.utc)
    if "verified" in payload:
        want = bool(payload["verified"])
        if want and row.verified_at is None:
            row.verified_at = now
        elif not want and row.verified_at is not None:
            row.verified_at = None
            db.add(
                Correction(
                    account_id=ctx.account.id, rfp_id=rfp.id, requirement_id=row.id,
                    correction_type="uncheck", old_value=str(now), new_value=None,
                )
            )
    if "response" in payload and payload["response"] is not None:
        new_response = str(payload["response"])
        if new_response != row.response:
            db.add(
                Correction(
                    account_id=ctx.account.id, rfp_id=rfp.id, requirement_id=row.id,
                    correction_type="response_edit", old_value=row.response[:500], new_value=new_response[:500],
                )
            )
            row.response = new_response
    if "tags" in payload and payload["tags"] is not None:
        new_tags = [str(t) for t in payload["tags"]][:50]
        if new_tags != (row.tags or []):
            db.add(
                Correction(
                    account_id=ctx.account.id, rfp_id=rfp.id, requirement_id=row.id,
                    correction_type="tag_edit",
                    old_value=jsonlib.dumps(row.tags or []), new_value=jsonlib.dumps(new_tags),
                )
            )
            row.tags = new_tags
    db.flush()

    if _wants_json(request):
        return JSONResponse(_row_json(row))
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "row_fragment.html",
        {"row": row, "rfp": rfp, "entitled": True, "is_staff": ctx.account.is_staff, "ctx": ctx},
    )


async def _payload(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            body = await request.json()
            return body if isinstance(body, dict) else {}
        except Exception:
            return {}
    form = await request.form()
    payload: dict[str, Any] = {}
    if "verified" in form:
        payload["verified"] = str(form.get("verified")).lower() in ("true", "1", "on", "yes")
    if "response" in form:
        payload["response"] = str(form.get("response"))
    if "tags" in form:
        try:
            payload["tags"] = jsonlib.loads(str(form.get("tags")))
        except jsonlib.JSONDecodeError:
            pass
    return payload


def _wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "") or "application/json" in request.headers.get(
        "content-type", ""
    )


def _row_json(row: Requirement) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "seq": row.seq,
        "clause_id": row.clause_id,
        "section": row.section,
        "page": row.page,
        "body": row.body,
        "excerpt": row.excerpt,
        "response": row.response,
        "verified": row.verified_at is not None,
        "tags": list(row.tags or []),
    }
