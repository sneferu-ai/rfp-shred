"""S11 health endpoint (FR-024). Checks are dynamic — a hardcoded 200 is a
violation."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session as OrmSession

from app import __version__
from app.core import provider_health
from app.core.config import Settings
from app.web import deps

router = APIRouter()


@router.get("/healthz")
def healthz(
    request: Request,
    db: OrmSession = Depends(deps.get_db),
    settings: Settings = Depends(deps.get_settings),
):
    checks: dict[str, str] = {}
    try:
        db.execute(text("SELECT 1"))
        checks["db"] = "ok"
    except Exception:
        checks["db"] = "error"
    try:
        db.execute(text("SELECT count(*) FROM jobs WHERE status='queued'"))
        checks["jobs"] = "ok"
    except Exception:
        checks["jobs"] = "error"
    degraded = provider_health.is_degraded(settings)
    checks["model"] = "degraded" if degraded else "ok"
    ok = checks["db"] == "ok" and checks["jobs"] == "ok"
    payload = {"status": "ok" if ok else "degraded", "version": __version__, **checks}
    return JSONResponse(payload, status_code=200 if ok else 503)
