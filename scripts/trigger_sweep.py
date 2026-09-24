"""Run one sweep cycle immediately: ``python -m scripts.trigger_sweep``.

Exit 0 on success; prints {"sweep_run_id": ..., "found": N, "matched": N,
"errors": [...]}.
"""

from __future__ import annotations

import argparse
import json

from app.core.config import Settings
from app.core.db import make_engine, make_session_factory
from app.sweep.runner import run_sweep_once


def trigger(settings: Settings) -> dict:
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    with factory() as session:
        sweep = run_sweep_once(session, settings)
        session.commit()
        return {
            "sweep_run_id": sweep.id,
            "found": sweep.found,
            "matched": sweep.matched,
            "errors": sweep.errors,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one SAM.gov watchlist sweep cycle")
    parser.parse_args(argv)
    try:
        result = trigger(Settings.from_env(strict=False))
    except Exception as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
