"""Export corrections (FR-037):
``python -m scripts.export_corrections --from <date> --to <date>``
Outputs a JSON array to stdout.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.config import Settings
from app.core.db import make_engine, make_session_factory
from app.core.models import Correction


def export_corrections(database_url: str, date_from: str, date_to: str) -> list[dict]:
    start = datetime.fromisoformat(date_from).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(date_to).replace(tzinfo=timezone.utc)
    engine = make_engine(database_url)
    factory = make_session_factory(engine)
    with factory() as session:
        rows = session.execute(
            select(Correction)
            .where(Correction.created_at >= start, Correction.created_at <= end)
            .order_by(Correction.created_at)
        ).scalars().all()
        return [
            {
                "id": str(r.id),
                "account_id": str(r.account_id) if r.account_id else None,
                "rfp_id": str(r.rfp_id),
                "requirement_id": str(r.requirement_id) if r.requirement_id else None,
                "correction_type": r.correction_type,
                "old_value": r.old_value,
                "new_value": r.new_value,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export user corrections as JSON")
    parser.add_argument("--from", dest="date_from", required=True, help="ISO date, e.g. 2026-01-01")
    parser.add_argument("--to", dest="date_to", required=True, help="ISO date, e.g. 2026-02-01")
    args = parser.parse_args(argv)
    rows = export_corrections(Settings.from_env(strict=False).database_url, args.date_from, args.date_to)
    print(json.dumps(rows, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
