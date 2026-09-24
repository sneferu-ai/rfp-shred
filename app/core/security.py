"""Password hashing, session tokens, CSRF tokens, reset tokens (FR-001/002/028/029).

Password hashing uses argon2id when ``argon2-cffi`` is installed (production
image). When it is not importable (bare dev/test environments) a PBKDF2-HMAC-
SHA256 fallback is used; the stored hash is self-describing and verification
dispatches on the prefix, so hashes migrate transparently when argon2 appears.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

try:  # pragma: no cover - depends on environment
    from argon2 import PasswordHasher
    from argon2.exceptions import VerificationError, VerifyMismatchError

    _HASHER = PasswordHasher()
except Exception:  # pragma: no cover
    _HASHER = None
    VerificationError = VerifyMismatchError = Exception  # type: ignore[assignment]

_PBKDF2_ITERATIONS = 260_000
SESSION_TTL_DAYS = 7
RESET_TOKEN_TTL_HOURS = 1


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    if _HASHER is not None:
        return _HASHER.hash(password)
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return "pbkdf2${}${}${}".format(
        _PBKDF2_ITERATIONS,
        base64.b64encode(salt).decode(),
        base64.b64encode(digest).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    if stored.startswith("pbkdf2$"):
        try:
            _, iterations, salt_b64, digest_b64 = stored.split("$")
            digest = hashlib.pbkdf2_hmac(
                "sha256",
                password.encode(),
                base64.b64decode(salt_b64),
                int(iterations),
            )
            return hmac.compare_digest(digest, base64.b64decode(digest_b64))
        except (ValueError, TypeError):
            return False
    if _HASHER is not None:
        try:
            return _HASHER.verify(stored, password)
        except (VerifyMismatchError, VerificationError):
            return False
        except Exception:
            return False
    return False


def check_password_strength(password: str) -> bool:
    """FR-001: >= 12 chars."""
    return len(password) >= 12


# ---------------------------------------------------------------------------
# Sessions (FR-031)
# ---------------------------------------------------------------------------

def new_session_token() -> tuple[str, str]:
    """Return (plain token, sha256 hash) — only the hash is stored."""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def session_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=SESSION_TTL_DAYS)


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_matches(expected: str, presented: str | None) -> bool:
    """Constant-time comparison (FR-029)."""
    if not presented:
        return False
    return hmac.compare_digest(expected, presented)


# ---------------------------------------------------------------------------
# Password reset tokens (FR-028)
# ---------------------------------------------------------------------------

def make_reset_token(account_id: str, app_secret: str) -> tuple[str, str]:
    """Return (plain token, token_hash).

    Token shape: base64url(account_id).base64url(32 random bytes).base64url(hmac)
    The HMAC binds the account id and the random part with APP_SECRET (>=32
    bytes), so a tampered token fails signature verification -> 410 (AC-050).
    """
    random_part = secrets.token_bytes(32)
    id_b64 = base64.urlsafe_b64encode(account_id.encode()).decode().rstrip("=")
    rand_b64 = base64.urlsafe_b64encode(random_part).decode().rstrip("=")
    sig = _reset_signature(id_b64, rand_b64, app_secret)
    token = f"{id_b64}.{rand_b64}.{sig}"
    return token, hash_token(token)


def parse_reset_token(token: str, app_secret: str) -> tuple[str, str] | None:
    """Verify structure + HMAC. Returns (account_id, token_hash) or None.

    A None return maps to 410 with no side effects — a forged (tampered-HMAC)
    token must never touch the account's sessions (AC-050a), and a correctly
    signed token that matches no stored row also returns 410 (AC-050b).
    """
    try:
        id_b64, rand_b64, sig = token.split(".")
    except ValueError:
        return None
    expected = _reset_signature(id_b64, rand_b64, app_secret)
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        account_id = base64.urlsafe_b64decode(id_b64 + "=" * (-len(id_b64) % 4)).decode()
    except Exception:
        return None
    return account_id, hash_token(token)


def _reset_signature(id_b64: str, rand_b64: str, app_secret: str) -> str:
    mac = hmac.new(app_secret.encode(), f"{id_b64}.{rand_b64}".encode(), hashlib.sha256)
    return base64.urlsafe_b64encode(mac.digest()).decode().rstrip("=")


def reset_token_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=RESET_TOKEN_TTL_HOURS)
