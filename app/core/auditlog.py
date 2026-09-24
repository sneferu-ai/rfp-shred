"""Append-only audit trail writer (FR-026).

The append-only property is enforced at the database layer by the
``audit_events_no_update_delete`` trigger (initial migration); this module is
the single code path that appends rows.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.core.models import AuditEvent


def audit(
    session: Session,
    *,
    action: str,
    entity: str = "",
    entity_id: str | uuid.UUID = "",
    actor_id: uuid.UUID | None = None,
) -> AuditEvent:
    event = AuditEvent(
        actor_id=actor_id,
        action=action,
        entity=entity,
        entity_id=str(entity_id),
    )
    session.add(event)
    return event
