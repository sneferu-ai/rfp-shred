"""S2 auth surfaces: signup / login / logout / forgot / reset
(FR-001, FR-002, FR-028, FR-031).

Browser form POSTs validate the anonymous pre-auth session's synchronizer
token. JSON login/signup remains the explicit API bootstrap; Origin and Fetch
Metadata are validated whenever present. Every authenticated state-changing
route also enforces CSRF."""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.core import auditlog
from app.core.config import Settings
from app.core.models import Account, ResetToken, SessionRow
from app.core.ratelimit import LIMITS, RateLimiter
from app.core.security import (
    check_password_strength,
    csrf_matches,
    hash_password,
    hash_token,
    make_reset_token,
    parse_reset_token,
    reset_token_expiry,
    verify_password,
)
from app.web import deps

router = APIRouter()

_DUMMY_HASH = hash_password("constant-time-dummy-password")


def _limiter(request: Request) -> RateLimiter:
    return request.app.state.rate_limiter


def _rate_check(request: Request, name: str, key: str) -> None:
    limit, window = LIMITS[name]
    allowed, retry_after = _limiter(request).hit(name, key, limit, window)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Too many requests — try again later",
            headers={"Retry-After": str(retry_after)},
        )


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "") or "application/json" in request.headers.get(
        "content-type", ""
    )


def _same_origin(request: Request, settings: Settings) -> None:
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        raise HTTPException(status_code=403, detail="Cross-site request refused")
    origin = request.headers.get("origin")
    if not origin:
        return
    request_origin = f"{request.url.scheme}://{request.headers.get('host', '')}".rstrip("/")
    public = urlsplit(settings.public_url)
    configured_origin = f"{public.scheme}://{public.netloc}".rstrip("/")
    if origin.rstrip("/") not in {request_origin, configured_origin}:
        raise HTTPException(status_code=403, detail="Cross-site request refused")


async def _auth_payload(request: Request, ctx: deps.WebContext, settings: Settings):
    _same_origin(request, settings)
    content_type = request.headers.get("content-type", "")
    if "form" in content_type or "multipart" in content_type:
        form = await request.form()
        value = form.get("csrf_token")
        token = str(value) if value is not None else None
        if not csrf_matches(ctx.session_row.csrf_token, token):
            raise HTTPException(status_code=403, detail="CSRF token missing or invalid")
        return form
    return await request.json()


@router.get("/signup", response_class=HTMLResponse)
def signup_form(request: Request, ctx: deps.WebContext = Depends(deps.web_context)):
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request, "signup.html", {"ctx": ctx, "error": None}
    )


@router.post("/signup")
async def signup(
    request: Request,
    response: Response,
    ctx: deps.WebContext = Depends(deps.web_context),
    settings: Settings = Depends(deps.get_settings),
):
    _rate_check(request, "signup", _client_ip(request))
    form = await _auth_payload(request, ctx, settings)
    email = str(form.get("email", "")).strip().lower()
    password = str(form.get("password", ""))
    templates = request.app.state.templates
    if not email or "@" not in email:
        return templates.TemplateResponse(
            request, "signup.html", {"ctx": ctx, "error": "Enter a valid email address"}, status_code=422
        )
    if not check_password_strength(password):
        return templates.TemplateResponse(
            request, "signup.html", {"ctx": ctx, "error": "Password must be at least 12 characters"}, status_code=422
        )
    existing = ctx.db.query(Account).filter(Account.email == email).first()
    if existing is not None:
        return templates.TemplateResponse(
            request, "signup.html", {"ctx": ctx, "error": "An account with this email already exists"}, status_code=409
        )
    account = Account(email=email, pass_hash=hash_password(password), is_staff=False)
    ctx.db.add(account)
    ctx.db.flush()
    row = deps.rotate_session(ctx, request, response, account)
    if _wants_json(request):
        return JSONResponse({"account_id": str(account.id), "csrf_token": row.csrf_token}, status_code=201)
    return RedirectResponse("/app", status_code=303)


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, ctx: deps.WebContext = Depends(deps.web_context)):
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "login.html", {"ctx": ctx, "error": None})


@router.post("/login")
async def login(
    request: Request,
    response: Response,
    ctx: deps.WebContext = Depends(deps.web_context),
    settings: Settings = Depends(deps.get_settings),
):
    ip = _client_ip(request)
    # lockout check first (FR-002): while throttled, even a correct password
    # gets 429; only FAILED attempts increment the bucket.
    limit, window = LIMITS["login"]
    allowed, retry_after = _limiter(request).peek("login", ip, limit, window)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Too many requests — try again later",
            headers={"Retry-After": str(retry_after)},
        )
    form = await _auth_payload(request, ctx, settings)
    email = str(form.get("email", "")).strip().lower()
    password = str(form.get("password", ""))
    account = ctx.db.query(Account).filter(Account.email == email).first()
    # constant-time comparison + uniform failure wording (FR-002)
    ok = verify_password(password, account.pass_hash if account else _DUMMY_HASH) and account is not None
    templates = request.app.state.templates
    if not ok:
        _limiter(request).hit("login", ip, limit, window)
        if _wants_json(request):
            return JSONResponse({"detail": "invalid credentials"}, status_code=401)
        return templates.TemplateResponse(
            request, "login.html", {"ctx": ctx, "error": "invalid credentials"}, status_code=401
        )
    row = deps.rotate_session(ctx, request, response, account)
    if _wants_json(request):
        return JSONResponse({"account_id": str(account.id), "csrf_token": row.csrf_token})
    return RedirectResponse("/app", status_code=303)


@router.post("/logout", dependencies=[Depends(deps.csrf_protect)])
def logout(
    request: Request,
    response: Response,
    ctx: deps.WebContext = Depends(deps.web_context),
):
    ctx.db.delete(ctx.session_row)
    ctx.db.flush()
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(deps.COOKIE_NAME)
    return response


@router.get("/forgot-password", response_class=HTMLResponse)
def forgot_form(request: Request, ctx: deps.WebContext = Depends(deps.web_context)):
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "forgot.html", {"ctx": ctx, "sent": False})


@router.post("/forgot-password")
async def forgot_password(
    request: Request,
    ctx: deps.WebContext = Depends(deps.web_context),
    settings: Settings = Depends(deps.get_settings),
):
    form = await _auth_payload(request, ctx, settings)
    email = str(form.get("email", "")).strip().lower()
    _rate_check(request, "password_reset", email or _client_ip(request))
    account = ctx.db.query(Account).filter(Account.email == email).first()
    if account is not None:
        token, token_hash = make_reset_token(str(account.id), settings.app_secret)
        ctx.db.add(
            ResetToken(account_id=account.id, token_hash=token_hash, expires_at=reset_token_expiry())
        )
        ctx.db.flush()
        request.app.state.mailer.send(
            to=account.email,
            subject="[RFP Shred] Password reset",
            body=f"Reset your password (1 hour): {settings.public_url}/reset-password?token={token}",
        )
    # always 200 — no account enumeration (FR-028)
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "forgot.html", {"ctx": ctx, "sent": True})


@router.get("/reset-password", response_class=HTMLResponse)
def reset_form(request: Request, token: str = "", ctx: deps.WebContext = Depends(deps.web_context)):
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "reset.html", {"ctx": ctx, "token": token, "error": None})


@router.post("/reset-password")
async def reset_password(
    request: Request,
    ctx: deps.WebContext = Depends(deps.web_context),
    settings: Settings = Depends(deps.get_settings),
):
    form = await _auth_payload(request, ctx, settings)
    token = str(form.get("token", ""))
    password = str(form.get("password", ""))
    templates = request.app.state.templates
    parsed = parse_reset_token(token, settings.app_secret)
    row: ResetToken | None = None
    if parsed is not None:
        _account_id, token_hash = parsed
        row = ctx.db.query(ResetToken).filter(ResetToken.token_hash == token_hash).first()
    now = datetime.now(timezone.utc)
    if (
        row is None
        or row.used_at is not None
        or (row.expires_at.replace(tzinfo=timezone.utc) if row.expires_at.tzinfo is None else row.expires_at) < now
    ):
        # forged / unknown / used / expired -> 410 with no side effects (AC-050)
        return templates.TemplateResponse(
            request, "reset.html",
            {"ctx": ctx, "token": token, "error": "This reset link is invalid or has expired"},
            status_code=410,
        )
    if not check_password_strength(password):
        return templates.TemplateResponse(
            request, "reset.html",
            {"ctx": ctx, "token": token, "error": "Password must be at least 12 characters"},
            status_code=422,
        )
    account = ctx.db.get(Account, row.account_id)
    if account is None:
        return templates.TemplateResponse(
            request, "reset.html", {"ctx": ctx, "token": token, "error": "This reset link is invalid or has expired"}, status_code=410
        )
    account.pass_hash = hash_password(password)
    row.used_at = now
    # revoke all sessions for the account (FR-028)
    ctx.db.query(SessionRow).filter(SessionRow.account_id == account.id).delete()
    auditlog.audit(ctx.db, action="password_reset", entity="account", entity_id=account.id, actor_id=account.id)
    ctx.db.flush()
    return RedirectResponse("/login", status_code=303)
