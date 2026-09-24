"""Export workbench annotations as FR-025 ground truth:
``python -m scripts.export_annotations --rfp-id <uuid> --output <path>``
"""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path

from sqlalchemy import select

from app.core.config import Settings
from app.core.db import make_engine, make_session_factory
from app.core.models import CorpusAnnotation


def export_annotations(database_url: str, rfp_id: str, output: str) -> dict:
    engine = make_engine(database_url)
    factory = make_session_factory(engine)
    with factory() as session:
        rows = session.execute(
            select(CorpusAnnotation)
            .where(CorpusAnnotation.rfp_id == uuid.UUID(rfp_id))
            .order_by(CorpusAnnotation.created_at)
        ).scalars().all()
        payload = {
            "requirements": [
                {
                    "clause_id": r.clause_id,
                    "section": r.section,
                    "page": r.page,
                    "text": r.text,
                    "is_binding": r.is_binding,
                }
                for r in rows
            ]
        }
    Path(output).write_text(json.dumps(payload, indent=2))
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export corpus annotations in FR-025 ground-truth format")
    parser.add_argument("--rfp-id", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    payload = export_annotations(Settings.from_env(strict=False).database_url, args.rfp_id, args.output)
    print(f"wrote {len(payload['requirements'])} annotations to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
