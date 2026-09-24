"""FR-036 staff bootstrap: ``python -m scripts.create_staff --email <email>``.

Creates an accounts row with is_staff=true and a random 16-character
password printed to stdout (changeable via FR-028). Idempotent: re-running
updates is_staff on an existing account. Requires DATABASE_URL and
APP_SECRET.
"""

from __future__ import annotations

import argparse
import secrets
import string

from sqlalchemy import select

from app.core.config import Settings
from app.core.db import make_engine, make_session_factory
from app.core.models import Account
from app.core.security import hash_password

_ALPHABET = string.ascii_letters + string.digits


def generate_password(length: int = 16) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def create_staff(database_url: str, email: str) -> tuple[str, bool]:
    """Returns (password, created). Password is a fresh random one only when
    the account is created; on re-run it is empty (no rotation)."""
    engine = make_engine(database_url)
    factory = make_session_factory(engine)
    email = email.strip().lower()
    with factory() as session:
        existing = session.execute(select(Account).where(Account.email == email)).scalars().first()
        if existing is not None:
            existing.is_staff = True
            session.commit()
            return "", False
        password = generate_password()
        session.add(Account(email=email, pass_hash=hash_password(password), is_staff=True))
        session.commit()
        return password, True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create or promote a staff account")
    parser.add_argument("--email", required=True)
    args = parser.parse_args(argv)
    settings = Settings.from_env(strict=False)
    password, created = create_staff(settings.database_url, args.email)
    if created:
        print(f"staff account created for {args.email}")
        print(f"password: {password}")
    else:
        print(f"existing account {args.email} promoted to staff (password unchanged)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
