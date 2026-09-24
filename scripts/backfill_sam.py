"""Backfill SAM.gov postings: ``python -m scripts.backfill_sam --days N``.

Runs in 24-hour chunks up to 7 days; beyond 7 days it proceeds with a loud
warning (FR-021).
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone

from app.core.config import Settings
from app.core.db import make_engine, make_session_factory
from app.sweep.runner import run_sweep_once


def backfill(settings: Settings, days: int) -> list[dict]:
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    results: list[dict] = []
    today = datetime.now(timezone.utc).date()
    for back in range(days, 0, -1):
        day = today - timedelta(days=back)
        posted = day.strftime("%m/%d/%Y")
        with factory() as session:
            sweep = run_sweep_once(session, settings, posted_from=posted, posted_to=posted)
            session.commit()
            results.append({"date": posted, "found": sweep.found, "matched": sweep.matched, "errors": len(sweep.errors)})
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill SAM.gov postings in 24-hour chunks")
    parser.add_argument("--days", type=int, required=True)
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("--days must be >= 1")
    if args.days > 7:
        print(f"warning: backfilling {args.days} days exceeds the 7-day in-app window", flush=True)
    results = backfill(Settings.from_env(strict=False), args.days)
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
