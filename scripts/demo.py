"""One-command, local showcase launcher.

Runs the real FastAPI application and database-backed extraction worker with
SQLite and the deterministic mock model. This intentionally avoids Docker,
external model APIs, Stripe, SAM.gov, and email delivery so the core upload →
extract → verify → export journey is reproducible on a laptop.
"""

from __future__ import annotations

import argparse
import logging
import threading
from pathlib import Path

import uvicorn

from app.core.config import Settings
from app.web.app import create_app
from app.worker.main import run_worker


def build_demo_settings(root: Path, port: int) -> Settings:
    data_dir = root / ".demo"
    files_dir = data_dir / "files"
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    files_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    return Settings(
        app_secret="local-showcase-only-secret-32bytes-minimum",
        database_url=f"sqlite:///{data_dir / 'rfp-shred.sqlite3'}",
        files_dir=str(files_dir),
        public_url=f"http://127.0.0.1:{port}",
        port=port,
        model_provider="mock",
        model_name="mock-extractor-1",
        environment="test",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local RFP Shred showcase")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8180, type=int)
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    settings = build_demo_settings(root, args.port)
    application = create_app(settings)
    worker = threading.Thread(
        target=run_worker,
        args=(application.state.session_factory, settings),
        kwargs={"worker_id": "showcase-worker", "poll_s": 0.25},
        daemon=True,
        name="rfp-shred-showcase-worker",
    )
    worker.start()

    logging.getLogger(__name__).info("Showcase data: %s", root / ".demo")
    print(f"\nRFP Shred showcase: http://{args.host}:{args.port}")
    print("Create any 12+ character demo password, then upload the committed sample:")
    print(f"  {root / 'tests/fixtures/corpus/smoke_fixture.pdf'}\n")
    uvicorn.run(application, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
