"""FastAPI application factory (S1–S11)."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import __version__
from app.core.config import Settings
from app.core.db import create_schema, make_engine, make_session_factory
from app.core.mailer import Mailer
from app.core.ratelimit import RateLimiter
from app.web.upload_guard import (
    DEFAULT_CONCURRENT_UPLOAD_BODIES,
    MULTIPART_OVERHEAD_BYTES,
    UploadBodyAdmissionMiddleware,
)

WEB_DIR = Path(__file__).parent


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    create_schema(engine)
    settings.files_path.mkdir(parents=True, exist_ok=True)

    app = FastAPI(
        title="RFP Shred",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.rate_limiter = RateLimiter()
    app.state.mailer = Mailer(settings)
    app.state.templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))

    from app.ops.routes import router as ops_router
    from app.web import deps
    from app.web.routes_account import router as account_router
    from app.web.routes_api import router as api_router
    from app.web.routes_auth import router as auth_router
    from app.web.routes_billing import router as billing_router
    from app.web.routes_health import router as health_router
    from app.web.routes_pages import router as pages_router

    @app.middleware("http")
    async def pending_session_cookie_middleware(request, call_next):
        response = await call_next(request)
        deps.set_pending_cookie(request, response, settings)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=()",
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
            "form-action 'self'; img-src 'self' data:; connect-src 'self'; "
            "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'",
        )
        if request.url.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "public, max-age=3600")
        else:
            response.headers.setdefault("Cache-Control", "private, no-store")
        if settings.public_url.startswith("https://"):
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response

    # Added after the function middleware so Starlette places this pure ASGI
    # guard at the outside of the stack.  It therefore runs before CSRF's
    # multipart parsing and before a request-scoped database session is made.
    app.add_middleware(
        UploadBodyAdmissionMiddleware,
        max_request_bytes=settings.max_upload_bytes + MULTIPART_OVERHEAD_BYTES,
        max_concurrent=DEFAULT_CONCURRENT_UPLOAD_BODIES,
    )

    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(pages_router)
    app.include_router(billing_router)
    app.include_router(account_router)
    app.include_router(api_router)
    app.include_router(ops_router)

    static_dir = WEB_DIR / "static"
    static_dir.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    return app
