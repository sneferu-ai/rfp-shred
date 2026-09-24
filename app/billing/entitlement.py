"""Entitlement resolution (FR-015).

A matrix is open iff:
- rfps.unlocked_at IS NOT NULL (never reverts once set), OR
- the account holds a `single` entitlement for that matrix, OR
- the account holds an active plan (active=true AND period_end in the
  future), OR
- the matrix's total requirement rows <= 10 (FR-012 small-matrix clause).
Evaluation happens per request, server-side.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.models import Entitlement, Requirement, Rfp

FREE_ROW_LIMIT = 10


def requirement_count(session: Session, rfp_id: uuid.UUID) -> int:
    return int(
        session.execute(
            select(func.count(Requirement.id)).where(Requirement.rfp_id == rfp_id)
        ).scalar_one()
    )


def is_entitled(session: Session, account_id: uuid.UUID, rfp: Rfp) -> bool:
    if rfp.unlocked_at is not None:
        return True
    if rfp.account_id == account_id:
        single = session.execute(
            select(Entitlement.id).where(
                Entitlement.account_id == account_id,
                Entitlement.kind == "single",
                Entitlement.rfp_id == rfp.id,
                Entitlement.active.is_(True),
            )
        ).first()
        if single is not None:
            return True
    now = datetime.now(timezone.utc)
    plan = session.execute(
        select(Entitlement.id).where(
            Entitlement.account_id == account_id,
            Entitlement.kind == "plan",
            Entitlement.active.is_(True),
            (Entitlement.period_end.is_(None)) | (Entitlement.period_end > now),
        )
    ).first()
    if plan is not None:
        return True
    return requirement_count(session, rfp.id) <= FREE_ROW_LIMIT


def visible_rows(session: Session, account_id: uuid.UUID, rfp: Rfp) -> tuple[list, int, bool]:
    """FR-012 preview gate. Returns (rows, locked_count, entitled).

    Without entitlement: exactly the first ten rows by seq plus the locked
    count; rows beyond the preview window are NEVER returned (they must not
    reach a template context or JSON payload in any form).
    """
    entitled = is_entitled(session, account_id, rfp)
    total = requirement_count(session, rfp.id)
    if entitled:
        rows = session.execute(
            select(Requirement).where(Requirement.rfp_id == rfp.id).order_by(Requirement.seq.asc())
        ).scalars().all()
        return list(rows), 0, True
    rows = session.execute(
        select(Requirement).where(Requirement.rfp_id == rfp.id).order_by(Requirement.seq.asc()).limit(FREE_ROW_LIMIT)
    ).scalars().all()
    return list(rows), max(0, total - FREE_ROW_LIMIT), False
