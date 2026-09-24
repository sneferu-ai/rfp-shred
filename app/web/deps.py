"""Request-scoped dependencies: db, sessions, CSRF (FR-029/031).

Session model: a cookie carries an opaque token; only its SHA-256 hash is
stored. Anonymous visitors get a pre-auth session row (account_id NULL) so a
CSRF token exists before login; the token ROTATES on login/signup. Sessions
expire after 7 days of inactivity with sliding refresh on every request.
404 wording is identical for "foreign" and "nonexistent" everywhere.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Generator

from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session as OrmSession

from app.core.config import Settings
from app.core.deferred_cleanup import discard_deferred_unlinks, run_deferred_unlinks
from app.core.models import Account, SessionRow
from app.core.security import (
    csrf_matches,
    hash_token,
    new_csrf_token,
    new_session_token,
    session_expiry,
)

COOKIE_NAME = "rs_session"
NOT_FOUND_DETAIL = "Not found"


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Generator[OrmSession, None, None]:
    factory = request.app.state.session_factory
    session = factory()
    try:
        yield session
        session.commit()
        run_deferred_unlinks(session)
    except Exception:
        session.rollback()
        discard_deferred_unlinks(session)
        raise
    finally:
        session.close()


@dataclass
class WebContext:
    db: OrmSession
    session_row: SessionRow
    account: Account | None


def _queue_session_cookie(request: Request, token: str) -> None:
    """Mark a session cookie for the app-level middleware to set on the
    outgoing response. (Cookies set on the injected Response parameter are
    NOT merged when a route returns a Response directly — the middleware
    guarantees delivery on every response type.)"""
    request.state.pending_session_cookie = token


def set_pending_cookie(request: Request, response: Response, settings: Settings) -> None:
    token = getattr(request.state, "pending_session_cookie", None)
    if token:
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=7 * 24 * 3600,
            httponly=True,
            secure=settings.public_url.startswith("https://"),
            samesite="lax",
        )


def _load_session_row(request: Request, db: OrmSession) -> SessionRow | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    row = db.query(SessionRow).filter(SessionRow.token_hash == hash_token(token)).first()
    if row is None:
        return None
    now = datetime.now(timezone.utc)
    expires = row.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < now:
        db.delete(row)
        db.flush()
        return None
    # sliding expiration (FR-031)
    row.expires_at = session_expiry()
    db.flush()
    return row


def web_context(
    request: Request,
    response: Response,
    db: OrmSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> WebContext:
    row = _load_session_row(request, db)
    if row is None:
        token, token_hash = new_session_token()
        row = SessionRow(
            account_id=None,
            token_hash=token_hash,
            csrf_token=new_csrf_token(),
            expires_at=session_expiry(),
        )
        db.add(row)
        db.flush()
        _queue_session_cookie(request, token)
    account = db.get(Account, row.account_id) if row.account_id else None
    return WebContext(db=db, session_row=row, account=account)


def require_account(ctx: WebContext = Depends(web_context)) -> WebContext:
    if ctx.account is None:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )
    return ctx


def require_staff(ctx: WebContext = Depends(web_context)) -> WebContext:
    """Founder console: 404 for non-staff (FR-023) — no existence leak."""
    if ctx.account is None or not ctx.account.is_staff:
        raise HTTPException(status_code=404, detail=NOT_FOUND_DETAIL)
    return ctx


async def csrf_protect(
    request: Request,
    ctx: WebContext = Depends(web_context),
) -> WebContext:
    """Synchronizer-token check on all state-changing routes (FR-029).

    Token arrives as the ``X-CSRF-Token`` header (htmx / JSON clients) or the
    ``csrf_token`` form field. Missing or mismatched -> 403, constant-time
    comparison.
    """
    token = request.headers.get("X-CSRF-Token")
    if token is None:
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            try:
                body = await request.json()
                token = body.get("csrf_token") if isinstance(body, dict) else None
            except Exception:
                token = None
        elif "form" in content_type or "multipart" in content_type:
            form = await request.form()
            value = form.get("csrf_token")
            token = str(value) if value is not None else None
    if not csrf_matches(ctx.session_row.csrf_token, token):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")
    return ctx


def rotate_session(
    ctx: WebContext,
    request: Request,
    response: Response,
    account: Account,
) -> SessionRow:
    """On login/signup: bind a fresh session to the account with a rotated
    CSRF token, replacing the anonymous one."""
    token, token_hash = new_session_token()
    row = SessionRow(
        account_id=account.id,
        token_hash=token_hash,
        csrf_token=new_csrf_token(),
        expires_at=session_expiry(),
    )
    ctx.db.add(row)
    # the anonymous pre-auth row is discarded
    if ctx.session_row.account_id is None:
        ctx.db.delete(ctx.session_row)
    ctx.db.flush()
    _queue_session_cookie(request, token)
    return row


def not_found() -> HTTPException:
    return HTTPException(status_code=404, detail=NOT_FOUND_DETAIL)
