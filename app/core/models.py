"""PostgreSQL 16 data model (section 5).

Rules honored here:
- State-like columns are VARCHAR + CHECK constraints, never PG enum types.
- Every FK specifies its ON DELETE action explicitly.
- jsonb columns use JSONB with a plain-JSON variant for SQLite (tests).
- PG-only artifacts (GIN index on sam_notices.naics, the append-only trigger
  on audit_events) live in the initial Alembic migration, dialect-guarded.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JSONBCompat = JSONB().with_variant(JSON(), "sqlite")

RFP_STATES = (
    "received", "reading", "extracting", "auditing", "ready",
    "unlocked", "cleared", "exported", "failed", "expired",
)
RFP_ORIGINS = ("user", "sweep")
RUN_STAGES = ("reading", "extracting", "auditing", "ready", "failed")
JOB_STATUSES = ("queued", "processing", "completed", "failed")
ENTITLEMENT_KINDS = ("single", "plan")
STRIPE_EVENT_STATUSES = ("processing", "processed", "dead_lettered")
CORRECTION_TYPES = (
    "uncheck", "reinclude_filtered", "reinclude_audit", "response_edit", "tag_edit",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_aware(dt: datetime | None) -> datetime | None:
    """Normalize to tz-aware UTC. Postgres returns aware datetimes; SQLite
    (tests) returns naive ones — comparisons must not crash on the seam."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _ck(name: str, column: str, values: tuple[str, ...]) -> CheckConstraint:
    allowed = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{column} IN ({allowed})", name=name)


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    pass_hash: Mapped[str] = mapped_column(Text, nullable=False)
    is_staff: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    watch_naics: Mapped[list] = mapped_column(JSONBCompat, default=list, nullable=False)
    watch_set_asides: Mapped[list] = mapped_column(JSONBCompat, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class SessionRow(Base):
    """Server-side session (FR-031). account_id is nullable so a pre-auth
    anonymous session can carry the CSRF token that rotates on login (FR-029)."""

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=True
    )
    token_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    csrf_token: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ResetToken(Base):
    __tablename__ = "reset_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Rfp(Base):
    __tablename__ = "rfps"
    __table_args__ = (
        _ck("ck_rfps_state", "state", RFP_STATES),
        _ck("ck_rfps_origin", "origin", RFP_ORIGINS),
        Index("ix_rfps_content_hash", "content_hash"),
        Index("ix_rfps_account", "account_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    orig_name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    stored_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    solicitation_no: Mapped[str | None] = mapped_column(Text, nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="received")
    retain_source: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    unlocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    origin: Mapped[str] = mapped_column(Text, nullable=False, default="user")
    amends_rfp_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("rfps.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ExtractionRun(Base):
    __tablename__ = "extraction_runs"
    __table_args__ = (
        _ck("ck_extraction_runs_stage", "stage", RUN_STAGES),
        Index("ix_extraction_runs_rfp", "rfp_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    rfp_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("rfps.id", ondelete="CASCADE"), nullable=False
    )
    stage: Mapped[str] = mapped_column(Text, nullable=False, default="reading")
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        _ck("ck_jobs_status", "status", JOB_STATUSES),
        Index("ix_jobs_claim", "status", "priority", "enqueued_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    rfp_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("rfps.id", ondelete="CASCADE"), nullable=True
    )
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(Text, default="queued", nullable=False)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    enqueued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RawModelOutput(Base):
    __tablename__ = "raw_model_outputs"
    __table_args__ = (Index("ix_raw_model_outputs_run", "extraction_run_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    extraction_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("extraction_runs.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_json: Mapped[dict] = mapped_column(JSONBCompat, nullable=False)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    finish_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_version: Mapped[str] = mapped_column(Text, default="v1", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Requirement(Base):
    __tablename__ = "requirements"
    __table_args__ = (Index("ix_requirements_rfp_seq", "rfp_id", "seq"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    rfp_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("rfps.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    clause_id: Mapped[str] = mapped_column(Text, nullable=False, default="")
    section: Mapped[str] = mapped_column(Text, nullable=False, default="")
    page: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    excerpt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    response: Mapped[str] = mapped_column(Text, nullable=False, default="")
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tags: Mapped[list] = mapped_column(JSONBCompat, default=list, nullable=False)
    model_output_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("raw_model_outputs.id", ondelete="SET NULL"), nullable=True
    )


class FilteredLine(Base):
    __tablename__ = "filtered_lines"
    __table_args__ = (Index("ix_filtered_lines_rfp", "rfp_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    rfp_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("rfps.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    page: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    text_preview: Mapped[str] = mapped_column(Text, nullable=False, default="")
    full_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    filter_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    re_included: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    re_included_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditDrop(Base):
    __tablename__ = "audit_drops"
    __table_args__ = (Index("ix_audit_drops_rfp", "rfp_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    rfp_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("rfps.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    page: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    excerpt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    audit_failure_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    re_included: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    re_included_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Entitlement(Base):
    __tablename__ = "entitlements"
    __table_args__ = (
        _ck("ck_entitlements_kind", "kind", ENTITLEMENT_KINDS),
        Index("ix_entitlements_account", "account_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    rfp_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("rfps.id", ondelete="CASCADE"), nullable=True
    )
    stripe_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class StripeEvent(Base):
    __tablename__ = "stripe_events"
    __table_args__ = (_ck("ck_stripe_events_status", "status", STRIPE_EVENT_STATUSES),)

    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(Text, default="processing", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class SamNotice(Base):
    __tablename__ = "sam_notices"

    notice_id: Mapped[str] = mapped_column(Text, primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    naics: Mapped[list] = mapped_column(JSONBCompat, default=list, nullable=False)
    set_aside: Mapped[str | None] = mapped_column(Text, nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payload: Mapped[dict] = mapped_column(JSONBCompat, default=dict, nullable=False)


class SweepRun(Base):
    __tablename__ = "sweep_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    matched: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    errors: Mapped[list] = mapped_column(JSONBCompat, default=list, nullable=False)


class QualityRun(Base):
    __tablename__ = "quality_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    corpus_subset: Mapped[list] = mapped_column(JSONBCompat, default=list, nullable=False)
    omission_rate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    fp_rate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    page_attr_error: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    per_format: Mapped[dict] = mapped_column(JSONBCompat, default=dict, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class RetentionLog(Base):
    __tablename__ = "retention_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rfp_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("rfps.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AuditEvent(Base):
    """Append-only (FR-026); the PG trigger in migration 0001 blocks
    UPDATE/DELETE at the database layer."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    entity: Mapped[str] = mapped_column(Text, nullable=False, default="")
    entity_id: Mapped[str] = mapped_column(Text, nullable=False, default="")
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Correction(Base):
    __tablename__ = "corrections"
    __table_args__ = (
        _ck("ck_corrections_type", "correction_type", CORRECTION_TYPES),
        Index("ix_corrections_rfp", "rfp_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    rfp_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("rfps.id", ondelete="CASCADE"), nullable=False
    )
    requirement_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("requirements.id", ondelete="SET NULL"), nullable=True
    )
    correction_type: Mapped[str] = mapped_column(Text, nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class InferenceLog(Base):
    __tablename__ = "inference_log"
    __table_args__ = (Index("ix_inference_log_account_month", "account_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    rfp_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("rfps.id", ondelete="SET NULL"), nullable=True
    )
    tokens_in: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    estimated_cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class CorpusAnnotation(Base):
    __tablename__ = "corpus_annotations"
    __table_args__ = (Index("ix_corpus_annotations_rfp", "rfp_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    rfp_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("rfps.id", ondelete="CASCADE"), nullable=False
    )
    requirement_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("requirements.id", ondelete="SET NULL"), nullable=True
    )
    clause_id: Mapped[str] = mapped_column(Text, nullable=False, default="")
    section: Mapped[str] = mapped_column(Text, nullable=False, default="")
    page: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_binding: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


ALL_TABLES = (
    "accounts", "sessions", "reset_tokens", "rfps", "extraction_runs", "jobs",
    "raw_model_outputs", "requirements", "filtered_lines", "audit_drops",
    "entitlements", "stripe_events", "sam_notices", "sweep_runs",
    "quality_runs", "retention_log", "audit_events", "corrections",
    "inference_log", "corpus_annotations",
)
