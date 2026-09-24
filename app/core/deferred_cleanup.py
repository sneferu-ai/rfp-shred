"""Filesystem cleanup that is allowed to run only after a DB commit.

Request handlers stage unlink intents in ``Session.info``.  The request
dependency executes them after its commit succeeds and discards them on
rollback, keeping database truth and irreversible source-file deletion in
the right order.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy.orm import Session

_DEFERRED_UNLINKS = "rfp_shred.deferred_unlinks"
logger = logging.getLogger(__name__)


def defer_unlink_until_commit(db: Session, path: str | Path) -> None:
    pending = db.info.setdefault(_DEFERRED_UNLINKS, set())
    pending.add(str(path))


def run_deferred_unlinks(db: Session) -> None:
    """Run and clear staged unlinks after a successful commit.

    A failed unlink cannot roll the committed database transaction back.
    Leave the orphan for operator reconciliation and log the exact path rather
    than turning a completed database delete into a misleading HTTP failure.
    """

    pending = db.info.pop(_DEFERRED_UNLINKS, set())
    for raw_path in pending:
        path = Path(raw_path)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.exception("Post-commit source-file cleanup failed: %s", path)


def discard_deferred_unlinks(db: Session) -> None:
    """Forget staged unlinks when the surrounding transaction rolls back."""

    db.info.pop(_DEFERRED_UNLINKS, None)
