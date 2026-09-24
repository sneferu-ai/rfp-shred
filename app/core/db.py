"""Database engine/session factory.

PostgreSQL 16 is the production database (DIS-3). SQLite is supported for the
local test-suite; JSONB columns use a with_variant JSON type, and PG-only
features (advisory locks, GIN indexes, the append-only trigger) are applied
by migrations guarded on the dialect name.
"""

from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.models import Base


def make_engine(database_url: str, **kwargs) -> Engine:
    connect_args = {}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    engine = create_engine(database_url, connect_args=connect_args, future=True, **kwargs)
    if database_url.startswith("sqlite"):
        _install_sqlite_pragmas(engine)
    return engine


def _install_sqlite_pragmas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _record):  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False, future=True)


def create_schema(engine: Engine) -> None:
    """Create all tables (used by tests and the initial migration)."""
    Base.metadata.create_all(engine)
