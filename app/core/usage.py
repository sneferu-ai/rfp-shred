"""Per-account inference usage + monthly cap (FR-039).

The cap is a hard guard, not an alert: when an account's cumulative monthly
estimated inference cost exceeds ``MONTHLY_INFERENCE_CAP_USD``, new uploads
are queued at priority 0 (not processed) and S3/S4 show the cap banner.
Sweep-generated jobs are exempt. The cap resets on the first of each month
at 00:00 UTC. The month boundary is computed in Python so the query is
portable across Postgres and SQLite.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.models import InferenceLog
from app.core.queue import PRIORITY_CAPPED, PRIORITY_SWEEP, PRIORITY_USER


def month_start_utc(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return datetime(now.year, now.month, 1, tzinfo=timezone.utc)


def monthly_usage_usd(session: Session, account_id: uuid.UUID, now: datetime | None = None) -> float:
    start = month_start_utc(now)
    total = session.execute(
        select(func.coalesce(func.sum(InferenceLog.estimated_cost_usd), 0)).where(
            InferenceLog.account_id == account_id,
            InferenceLog.created_at >= start,
        )
    ).scalar_one()
    return float(total)


def is_over_cap(session: Session, account_id: uuid.UUID, cap_usd: float, now: datetime | None = None) -> bool:
    return monthly_usage_usd(session, account_id, now) > cap_usd


def priority_for_new_job(
    session: Session, account_id: uuid.UUID, origin: str, cap_usd: float
) -> int:
    """FR-039: sweep jobs exempt (priority 1); over-cap user uploads park at
    priority 0; otherwise user priority 10."""
    if origin == "sweep":
        return PRIORITY_SWEEP
    if is_over_cap(session, account_id, cap_usd):
        return PRIORITY_CAPPED
    return PRIORITY_USER
