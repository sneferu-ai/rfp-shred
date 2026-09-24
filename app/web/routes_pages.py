"""HTML surfaces S1, S3, S4, S5, S6 + workbench actions
(restore, reprocess, annotate)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.billing.entitlement import is_entitled, visible_rows
from app.core import provider_health, usage as usage_mod
from app.core.config import Settings
from app.core.models import (
    Account,
    AuditDrop,
    CorpusAnnotation,
    Correction,
    ExtractionRun,
    FilteredLine,
    Requirement,
    Rfp,
    SweepRun,
)
from app.core.queue import enqueue
from app.core.ratelimit import LIMITS
from app.pipeline import pagemap as pagemap_mod
from app.pipeline.audit import audit_items
from app.pipeline.intake_files import IntakeRejection
from app.pipeline.mine import MinedItem
from app.pipeline.runner import _prepare_processing_pdf
from app.web import deps
from app.web.intake_service import process_upload

router = APIRouter()


def _get_rfp(db: OrmSession, rfp_id: str, account: Account) -> Rfp:
    try:
        rid = uuid.UUID(rfp_id)
    except ValueError:
        raise deps.not_found()
    rfp = db.get(Rfp, rid)
    if rfp is None or rfp.account_id != account.id:
        raise deps.not_found()
    return rfp


# ---------------------------------------------------------------------------
# S1 — Home
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
def home(request: Request, ctx: deps.WebContext = Depends(deps.web_context)):
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "home.html", {"ctx": ctx})


# ---------------------------------------------------------------------------
# S3 — Library
# ---------------------------------------------------------------------------

@router.get("/app", response_class=HTMLResponse)
def library(request: Request, ctx: deps.WebContext = Depends(deps.require_account)):
    db = ctx.db
    rfps = db.execute(
        select(Rfp).where(Rfp.account_id == ctx.account.id).order_by(Rfp.created_at.desc())
    ).scalars().all()
    latest_sweep = db.execute(
        select(SweepRun).where(SweepRun.matched > 0).order_by(SweepRun.started_at.desc())
    ).scalars().first()
    sweep_matches: list[Rfp] = []
    if latest_sweep is not None:
        started = latest_sweep.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        sweep_matches = [
            r for r in rfps
            if r.origin == "sweep"
            and (r.created_at.replace(tzinfo=timezone.utc) if r.created_at.tzinfo is None else r.created_at) >= started
        ]
    parent_titles = {
        r.id: r for r in rfps
    }
    settings: Settings = request.app.state.settings
    over_cap = usage_mod.is_over_cap(db, ctx.account.id, settings.monthly_inference_cap_usd)
    degraded = provider_health.is_degraded(settings)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "library.html",
        {
            "ctx": ctx,
            "rfps": rfps,
            "sweep_matches": sweep_matches,
            "parent_map": parent_titles,
            "over_cap": over_cap,
            "degraded": degraded,
        },
    )


# ---------------------------------------------------------------------------
# S4 — Intake
# ---------------------------------------------------------------------------

@router.get("/app/new", response_class=HTMLResponse)
def intake_form(request: Request, ctx: deps.WebContext = Depends(deps.require_account)):
    db = ctx.db
    priors = db.execute(
        select(Rfp).where(Rfp.account_id == ctx.account.id, Rfp.state.in_(["ready", "unlocked", "cleared", "exported"]))
        .order_by(Rfp.created_at.desc())
    ).scalars().all()
    settings: Settings = request.app.state.settings
    over_cap = usage_mod.is_over_cap(db, ctx.account.id, settings.monthly_inference_cap_usd)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "intake.html",
        {"ctx": ctx, "priors": priors, "error": None, "over_cap": over_cap,
         "degraded": provider_health.is_degraded(settings)},
    )


@router.post("/app/new", dependencies=[Depends(deps.csrf_protect)])
async def intake_upload(
    request: Request,
    ctx: deps.WebContext = Depends(deps.require_account),
    settings: Settings = Depends(deps.get_settings),
):
    limit, window = LIMITS["upload"]
    allowed, retry_after = request.app.state.rate_limiter.hit(
        "upload", str(ctx.account.id), limit, window
    )
    if not allowed:
        raise HTTPException(status_code=429, detail="Too many uploads — try again later", headers={"Retry-After": str(retry_after)})

    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        return _intake_error(request, ctx, "Choose a file to upload", 422)
    data = await upload.read()
    filename = getattr(upload, "filename", "upload") or "upload"
    retain = str(form.get("keep_source", "")).lower() in ("on", "true", "1")
    amends_raw = str(form.get("amends_rfp_id", "") or "")
    amends_id: uuid.UUID | None = None
    if amends_raw:
        try:
            candidate = uuid.UUID(amends_raw)
            parent = ctx.db.get(Rfp, candidate)
            if parent is not None and parent.account_id == ctx.account.id:
                amends_id = candidate
        except ValueError:
            pass
    try:
        rfp = process_upload(
            ctx.db, settings,
            account=ctx.account, filename=filename, data=data,
            retain_source=retain, amends_rfp_id=amends_id,
        )
    except IntakeRejection as exc:
        return _intake_error(request, ctx, exc.detail, exc.status)
    return RedirectResponse(f"/app/jobs/{rfp.id}", status_code=303)


def _intake_error(request: Request, ctx: deps.WebContext, message: str, status_code: int):
    db = ctx.db
    priors = db.execute(
        select(Rfp).where(Rfp.account_id == ctx.account.id).order_by(Rfp.created_at.desc())
    ).scalars().all()
    settings: Settings = request.app.state.settings
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "intake.html",
        {"ctx": ctx, "priors": priors, "error": message,
         "over_cap": usage_mod.is_over_cap(db, ctx.account.id, settings.monthly_inference_cap_usd),
         "degraded": provider_health.is_degraded(settings)},
        status_code=status_code,
    )


# ---------------------------------------------------------------------------
# S5 — Progress
# ---------------------------------------------------------------------------

@router.get("/app/jobs/{rfp_id}", response_class=HTMLResponse)
def progress(request: Request, rfp_id: str, ctx: deps.WebContext = Depends(deps.require_account)):
    rfp = _get_rfp(ctx.db, rfp_id, ctx.account)
    run = _latest_run(ctx.db, rfp.id)
    settings: Settings = request.app.state.settings
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "progress.html",
        {"ctx": ctx, "rfp": rfp, "run": run, "degraded": provider_health.is_degraded(settings)},
    )


def _latest_run(db: OrmSession, rfp_id: uuid.UUID) -> ExtractionRun | None:
    return db.execute(
        select(ExtractionRun).where(ExtractionRun.rfp_id == rfp_id).order_by(ExtractionRun.created_at.desc())
    ).scalars().first()


# ---------------------------------------------------------------------------
# S6 — Workbench
# ---------------------------------------------------------------------------

@router.get("/app/matrices/{rfp_id}", response_class=HTMLResponse)
def workbench(request: Request, rfp_id: str, ctx: deps.WebContext = Depends(deps.require_account)):
    db = ctx.db
    rfp = _get_rfp(db, rfp_id, ctx.account)
    rows, locked_count, entitled = visible_rows(db, ctx.account.id, rfp)
    filtered = db.execute(
        select(FilteredLine).where(FilteredLine.rfp_id == rfp.id, FilteredLine.re_included.is_(False)).order_by(FilteredLine.seq)
    ).scalars().all()
    drops = db.execute(
        select(AuditDrop).where(AuditDrop.rfp_id == rfp.id, AuditDrop.re_included.is_(False)).order_by(AuditDrop.seq)
    ).scalars().all()
    total = len(rows) + locked_count
    checked = sum(1 for r in rows if r.verified_at is not None)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "workbench.html",
        {
            "ctx": ctx,
            "rfp": rfp,
            "rows": rows,
            "locked_count": locked_count,
            "entitled": entitled,
            "total_rows": total,
            "checked": checked,
            "all_checked": checked == total and total > 0,
            "filtered": filtered,
            "drops": drops,
            "is_staff": ctx.account.is_staff,
        },
    )


# ---------------------------------------------------------------------------
# FR-034 / FR-040 — restore panels
# ---------------------------------------------------------------------------

@router.post("/app/matrices/{rfp_id}/filtered/{line_id}/restore", dependencies=[Depends(deps.csrf_protect)])
def restore_filtered(
    request: Request,
    rfp_id: str,
    line_id: str,
    ctx: deps.WebContext = Depends(deps.require_account),
):
    db = ctx.db
    rfp = _get_rfp(db, rfp_id, ctx.account)
    line = db.get(FilteredLine, _uuid_or_404(line_id))
    if line is None or line.rfp_id != rfp.id:
        raise deps.not_found()
    if not line.re_included:
        next_seq = _next_seq(db, rfp.id)
        db.add(
            Requirement(
                rfp_id=rfp.id, seq=next_seq, clause_id=f"restored.{line.seq}",
                section="(restored)", page=line.page, body=line.full_text,
                excerpt=line.full_text[:200], tags=[],
            )
        )
        line.re_included = True
        line.re_included_at = datetime.now(timezone.utc)
        db.add(
            Correction(
                account_id=ctx.account.id, rfp_id=rfp.id, requirement_id=None,
                correction_type="reinclude_filtered", old_value=None, new_value=line.full_text[:500],
            )
        )
        db.flush()
    return RedirectResponse(f"/app/matrices/{rfp.id}", status_code=303)


@router.post("/app/matrices/{rfp_id}/audit_drops/{line_id}/restore", dependencies=[Depends(deps.csrf_protect)])
def restore_audit_drop(
    request: Request,
    rfp_id: str,
    line_id: str,
    ctx: deps.WebContext = Depends(deps.require_account),
    settings: Settings = Depends(deps.get_settings),
):
    db = ctx.db
    rfp = _get_rfp(db, rfp_id, ctx.account)
    drop = db.get(AuditDrop, _uuid_or_404(line_id))
    if drop is None or drop.rfp_id != rfp.id:
        raise deps.not_found()
    if drop.re_included:
        return RedirectResponse(f"/app/matrices/{rfp.id}", status_code=303)

    pages = _load_pages(db, settings, rfp)
    restored = False
    if pages is not None:
        for candidate_excerpt in (drop.excerpt, drop.text[:200]):
            item = MinedItem(
                clause_id=f"restored.{drop.seq}", section="(restored)", page=drop.page,
                text=drop.text, excerpt=candidate_excerpt, is_requirement=True,
                chunk_index=-1, doc_order=0,
            )
            outcome = audit_items([item], pages)
            if outcome.passed:
                db.add(
                    Requirement(
                        rfp_id=rfp.id, seq=_next_seq(db, rfp.id), clause_id=item.clause_id,
                        section=item.section, page=drop.page, body=drop.text,
                        excerpt=candidate_excerpt, tags=[],
                    )
                )
                restored = True
                break
    drop.re_included = True
    drop.re_included_at = datetime.now(timezone.utc)
    if not restored:
        drop.audit_failure_reason = (
            "re-audit on re-include failed"
            if pages is not None
            else "re-audit unavailable: source file no longer retained"
        )
    db.add(
        Correction(
            account_id=ctx.account.id, rfp_id=rfp.id, requirement_id=None,
            correction_type="reinclude_audit", old_value=None, new_value=drop.text[:500],
        )
    )
    db.flush()
    return RedirectResponse(f"/app/matrices/{rfp.id}", status_code=303)


def _load_pages(db: OrmSession, settings: Settings, rfp: Rfp) -> dict[int, str] | None:
    if not rfp.stored_path or not Path(rfp.stored_path).exists():
        return None
    run = _latest_run(db, rfp.id)
    if run is None:
        return None
    import tempfile

    try:
        with tempfile.TemporaryDirectory(dir=settings.files_dir) as tmp:
            pdf = _prepare_processing_pdf(db, settings, rfp, run, Path(tmp))
            page_map = pagemap_mod.build_page_map(pdf, ocr_enabled=False)
            return page_map.pages
    except Exception:
        return None


def _next_seq(db: OrmSession, rfp_id: uuid.UUID) -> int:
    from sqlalchemy import func

    current = db.execute(
        select(func.coalesce(func.max(Requirement.seq), 0)).where(Requirement.rfp_id == rfp_id)
    ).scalar_one()
    return int(current) + 1


# ---------------------------------------------------------------------------
# FR-045 — source re-trigger
# ---------------------------------------------------------------------------

@router.post("/app/matrices/{rfp_id}/reprocess", dependencies=[Depends(deps.csrf_protect)])
def reprocess(
    request: Request,
    rfp_id: str,
    ctx: deps.WebContext = Depends(deps.require_account),
    settings: Settings = Depends(deps.get_settings),
):
    db = ctx.db
    rfp = _get_rfp(db, rfp_id, ctx.account)
    if not rfp.stored_path or not Path(rfp.stored_path).exists():
        raise HTTPException(status_code=410, detail="Source file no longer available — please re-upload")
    prior = _latest_run(db, rfp.id)
    if prior is None or prior.stage not in ("ready", "failed"):
        return RedirectResponse(f"/app/jobs/{rfp.id}", status_code=303)
    run = ExtractionRun(rfp_id=rfp.id, stage="reading", attempt=prior.attempt + 1)
    db.add(run)
    rfp.state = "received"
    db.flush()
    priority = usage_mod.priority_for_new_job(db, ctx.account.id, rfp.origin, settings.monthly_inference_cap_usd)
    enqueue(db, rfp.id, priority)
    db.flush()
    return RedirectResponse(f"/app/jobs/{rfp.id}", status_code=303)


# ---------------------------------------------------------------------------
# FR-038 — staff annotation mode
# ---------------------------------------------------------------------------

@router.post("/app/matrices/{rfp_id}/annotate", dependencies=[Depends(deps.csrf_protect)])
async def annotate(
    request: Request,
    rfp_id: str,
    ctx: deps.WebContext = Depends(deps.require_staff),
):
    db = ctx.db
    rfp = _get_rfp(db, rfp_id, ctx.account)
    form = await request.form()
    action = str(form.get("action", ""))
    requirement_id = str(form.get("requirement_id", "") or "")
    requirement = None
    if requirement_id:
        requirement = db.get(Requirement, _uuid_or_404(requirement_id))
        if requirement is None or requirement.rfp_id != rfp.id:
            raise deps.not_found()
    if action == "correct_binding" and requirement is not None:
        entry = CorpusAnnotation(
            rfp_id=rfp.id, requirement_id=requirement.id, clause_id=requirement.clause_id,
            section=requirement.section, page=requirement.page, text=requirement.body,
            is_binding=True, created_by=ctx.account.id,
        )
    elif action == "not_requirement" and requirement is not None:
        entry = CorpusAnnotation(
            rfp_id=rfp.id, requirement_id=requirement.id, clause_id=requirement.clause_id,
            section=requirement.section, page=requirement.page, text=requirement.body,
            is_binding=False, created_by=ctx.account.id,
        )
    elif action == "add_missing":
        text = str(form.get("text", "")).strip()
        if not text:
            raise HTTPException(status_code=422, detail="Missing requirement text")
        entry = CorpusAnnotation(
            rfp_id=rfp.id, requirement_id=None,
            clause_id=str(form.get("clause_id", "")).strip(),
            section=str(form.get("section", "")).strip(),
            page=int(str(form.get("page", "1")) or 1),
            text=text, is_binding=True, created_by=ctx.account.id,
        )
    else:
        raise HTTPException(status_code=422, detail="Unknown annotation action")
    db.add(entry)
    from app.core import auditlog

    auditlog.audit(db, action="corpus_annotation", entity="rfp", entity_id=rfp.id, actor_id=ctx.account.id)
    db.flush()
    return RedirectResponse(f"/app/matrices/{rfp.id}?annotate=1", status_code=303)


def _uuid_or_404(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise deps.not_found()
