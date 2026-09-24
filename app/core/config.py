"""Configuration loading and validation (FR section 7).

`.env.example` documents every variable; this module loads and validates at
boot, failing fast on ABSENT names when ``strict=True`` (per `.env.example`:
"refuses a strict boot on missing names"). Development may use the documented
stub values, but strict production refuses every stub that would silently turn
off a paid, model, or mail integration. APP_SECRET — the session/HMAC signing
secret — must additionally be at least 32 bytes and must not be the shipped
development secret (section 5 security contract). ``strict=False`` (dev)
fills the same placeholder defaults and never refuses to boot.

**Auto-detection:** when ``strict`` is ``None`` (the default), strictness is
determined from ``APP_ENV``: ``prod`` → strict (refuses on missing/empty/short
secrets, per the §8 deployment contract "refuses boot on missing names");
``dev``/``test``/absent → non-strict (never refuses, for dev/test stacks with
placeholder values). Callers that need a specific mode pass ``strict=True``
or ``strict=False`` explicitly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

# Names that must be present (at least as placeholders) for a strict boot.
REQUIRED_NAMES: tuple[str, ...] = (
    # Core
    "APP_SECRET",
    "DATABASE_URL",
    "FILES_DIR",
    "PUBLIC_URL",
    # Model
    "MODEL_PROVIDER",
    "MODEL_NAME",
    # Money
    "STRIPE_SECRET",
    "STRIPE_WEBHOOK_SECRET",
    "PRICE_SINGLE",
    "PRICE_PLAN",
    # Registry
    "SAM_API_KEY",
    # Mail
    "SMTP_URL",
    "MAIL_FROM",
)

# Names whose present-but-empty value is a documented *development* stub
# (`.env.example`: dev checkout, optional SAM sweeps, and outbox mailer).
# Strict validation applies the narrower production rules below; this tuple is
# retained as the public inventory of values accepted by the non-strict stack.
DEV_STUB_EMPTY_NAMES: tuple[str, ...] = (
    "STRIPE_SECRET",
    "STRIPE_WEBHOOK_SECRET",
    "SAM_API_KEY",
    "SMTP_URL",
)

# Section 5 security contract: signing secrets are at least 32 bytes.
MIN_SECRET_BYTES = 32

# This value is intentionally public and convenient for local development. It
# must never become a production signing key merely because it is long enough.
SHIPPED_DEV_APP_SECRET = "dev-placeholder-secret-change-me-32bytes!"

# The shipped production stack always starts the sweep scheduler, and its first
# action is a SAM.gov request even when no watchlist exists. Until production
# has an explicit no-sweep mode, SAM is therefore a required integration too.
PRODUCTION_REQUIRED_NONEMPTY: tuple[str, ...] = (
    "STRIPE_SECRET",
    "STRIPE_WEBHOOK_SECRET",
    "SAM_API_KEY",
    "SMTP_URL",
)


def _is_https_public_url(value: str) -> bool:
    """Return whether ``value`` is an absolute HTTPS public origin."""

    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return False
    return parsed.scheme == "https" and bool(parsed.hostname)


def _is_placeholder_price_id(value: str) -> bool:
    normalized = value.strip().lower()
    return not normalized or "placeholder" in normalized


def _float(env: dict[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _int(env: dict[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _cron(env: dict[str, str], name: str, default: str) -> str:
    return env.get(name) or default


@dataclass
class Settings:
    """Runtime settings. All values come from the environment; nothing is
    baked into images (section 8)."""

    # Core
    app_secret: str = SHIPPED_DEV_APP_SECRET
    # PostgreSQL 16 is the stack (DIS-3); compose always injects DATABASE_URL.
    # The bare fallback points at a local Postgres so a missing variable fails
    # LOUDLY at connect time instead of silently booting against SQLite.
    database_url: str = "postgresql://rfp:rfp@localhost:5432/rfp"
    files_dir: str = "./files"
    public_url: str = "http://localhost:8080"
    port: int = 8080
    worker_concurrency: int = 1
    # Model
    model_provider: str = "mock"  # mock | openai | anthropic
    model_name: str = "mock-extractor-1"
    model_max_tokens: int = 8192
    model_input_price_per_m: float = 3.0
    model_output_price_per_m: float = 15.0
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    provider_health_cron: str = "*/30 * * * *"
    # Money
    stripe_secret: str = ""
    stripe_webhook_secret: str = ""
    price_single: str = "price_single_placeholder"
    price_plan: str = "price_plan_placeholder"
    monthly_inference_cap_usd: float = 100.0
    # Registry
    sam_api_key: str = ""
    sam_max_rps: float = 2.0
    sweep_cron: str = "0 6 * * *"
    max_sweep_matches_per_run: int = 50
    # Mail
    smtp_url: str = ""  # empty -> outbox mailer (dev/test)
    mail_from: str = "rfp-shred@example.com"
    # Limits
    max_upload_mb: int = 60
    max_zip_entries: int = 500
    quality_cron: str = "0 3 * * 0"
    cleanup_cron: str = "0 3 * * *"
    # Behavior
    environment: str = "dev"  # dev | prod | test

    missing: list[str] = field(default_factory=list)

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def files_path(self) -> Path:
        return Path(self.files_dir)

    @classmethod
    def from_env(cls, strict: bool | None = None) -> Settings:
        env = dict(os.environ)
        environment = env.get("APP_ENV", "dev").strip().lower()
        # Auto-detect strictness from APP_ENV when not explicitly specified.
        # §8 deployment contract: "app/core/config.py refuses boot on missing
        # names." Production (APP_ENV=prod) gets strict validation; dev/test
        # boot leniently with placeholder values. An explicit strict=True or
        # strict=False always wins.
        if strict is None:
            strict = environment == "prod"
        # Strict boot refuses on ABSENT names. Development stub names are
        # excluded from this generic empty check so production validation can
        # report the narrower, actionable integration errors below.
        missing = [n for n in REQUIRED_NAMES if n not in env]
        empty = [
            n
            for n in REQUIRED_NAMES
            if n in env and not env[n].strip() and n not in DEV_STUB_EMPTY_NAMES
        ]
        # Section 5: signing secrets are at least 32 bytes. APP_SECRET signs
        # sessions and password-reset tokens (core/security.py), so a short or
        # empty value is a production security defect, not a dev stub.
        app_secret = env.get("APP_SECRET", "")
        short_secret = (
            "APP_SECRET" in env
            and app_secret.strip() != ""
            and len(app_secret.encode("utf-8")) < MIN_SECRET_BYTES
        )
        s = cls(
            app_secret=env.get("APP_SECRET", cls.app_secret),
            database_url=env.get("DATABASE_URL", cls.database_url),
            files_dir=env.get("FILES_DIR", cls.files_dir),
            public_url=env.get("PUBLIC_URL", cls.public_url),
            port=_int(env, "PORT", 8080),
            worker_concurrency=_int(env, "WORKER_CONCURRENCY", 1),
            model_provider=env.get("MODEL_PROVIDER", "mock"),
            model_name=env.get("MODEL_NAME", "mock-extractor-1"),
            model_max_tokens=_int(env, "MODEL_MAX_TOKENS", 8192),
            model_input_price_per_m=_float(env, "MODEL_INPUT_PRICE_PER_M", 3.0),
            model_output_price_per_m=_float(env, "MODEL_OUTPUT_PRICE_PER_M", 15.0),
            anthropic_api_key=env.get("ANTHROPIC_API_KEY", ""),
            openai_api_key=env.get("OPENAI_API_KEY", ""),
            provider_health_cron=_cron(env, "PROVIDER_HEALTH_CRON", "*/30 * * * *"),
            stripe_secret=env.get("STRIPE_SECRET", ""),
            stripe_webhook_secret=env.get("STRIPE_WEBHOOK_SECRET", ""),
            price_single=env.get("PRICE_SINGLE", cls.price_single),
            price_plan=env.get("PRICE_PLAN", cls.price_plan),
            monthly_inference_cap_usd=_float(env, "MONTHLY_INFERENCE_CAP_USD", 100.0),
            sam_api_key=env.get("SAM_API_KEY", ""),
            sam_max_rps=_float(env, "SAM_MAX_RPS", 2.0),
            sweep_cron=_cron(env, "SWEEP_CRON", "0 6 * * *"),
            max_sweep_matches_per_run=_int(env, "MAX_SWEEP_MATCHES_PER_RUN", 50),
            smtp_url=env.get("SMTP_URL", ""),
            mail_from=env.get("MAIL_FROM", cls.mail_from),
            max_upload_mb=_int(env, "MAX_UPLOAD_MB", 60),
            max_zip_entries=_int(env, "MAX_ZIP_ENTRIES", 500),
            quality_cron=_cron(env, "QUALITY_CRON", "0 3 * * 0"),
            cleanup_cron=_cron(env, "CLEANUP_CRON", "0 3 * * *"),
            environment=environment,
            missing=missing,
        )
        if strict:
            problems: list[str] = []
            if missing:
                problems.append(
                    "missing required environment variables: " + ", ".join(missing)
                )
            if empty:
                problems.append(
                    "empty values (only dev-stub names may be empty): "
                    + ", ".join(empty)
                )
            if short_secret:
                problems.append(
                    f"APP_SECRET must be at least {MIN_SECRET_BYTES} bytes "
                    "(section 5 signing-secret contract)"
                )
            if app_secret == SHIPPED_DEV_APP_SECRET:
                problems.append(
                    "APP_SECRET must not use the shipped development secret"
                )

            public_url = env.get("PUBLIC_URL", "")
            if public_url and not _is_https_public_url(public_url):
                problems.append(
                    "PUBLIC_URL must be an absolute HTTPS URL in production"
                )

            provider = env.get("MODEL_PROVIDER", "").strip().lower()
            model_name = env.get("MODEL_NAME", "").strip().lower()
            if provider and provider not in {"openai", "anthropic"}:
                problems.append(
                    "MODEL_PROVIDER must be openai or anthropic in production "
                    "(the mock provider is development-only)"
                )
            if model_name.startswith("mock"):
                problems.append(
                    "MODEL_NAME must not select a mock model in production"
                )
            if provider == "openai" and not env.get("OPENAI_API_KEY", "").strip():
                problems.append(
                    "OPENAI_API_KEY must be non-empty for MODEL_PROVIDER=openai"
                )
            if (
                provider == "anthropic"
                and not env.get("ANTHROPIC_API_KEY", "").strip()
            ):
                problems.append(
                    "ANTHROPIC_API_KEY must be non-empty for "
                    "MODEL_PROVIDER=anthropic"
                )

            for name in PRODUCTION_REQUIRED_NONEMPTY:
                if name in env and not env[name].strip():
                    problems.append(f"{name} must be non-empty in production")

            for name in ("PRICE_SINGLE", "PRICE_PLAN"):
                value = env.get(name, "")
                if value and _is_placeholder_price_id(value):
                    problems.append(
                        f"{name} must not use a placeholder price ID in production"
                    )
            if problems:
                raise ConfigError("Strict boot refused: " + "; ".join(problems))
        return s


class ConfigError(RuntimeError):
    """Raised when strict boot validation fails (section 7/8)."""
