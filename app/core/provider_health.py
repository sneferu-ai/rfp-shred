"""Provider health probe + queued-degradation state (FR-043).

When both model providers are unreachable the pipeline enters a queued-
degradation state: uploads are still accepted and enqueued at priority 10
but the worker does not process them; S5/S3/S4 explain the pause and
``/healthz`` reports ``model: "degraded"``. A health-probe cron (default
every 30 minutes) pings both providers and auto-resumes in priority order
when either returns. State is recorded under FILES_DIR so every container
observes the same truth; outages are also recorded in sweep_runs.errors.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import Settings

STATE_FILE = "provider_state.json"


def _state_path(settings: Settings) -> Path:
    return Path(settings.files_dir) / STATE_FILE


def current_state(settings: Settings) -> dict:
    try:
        data = json.loads(_state_path(settings).read_text())
        return {
            "openai": data.get("openai", "unknown"),
            "anthropic": data.get("anthropic", "unknown"),
            "checked_at": data.get("checked_at"),
        }
    except (OSError, json.JSONDecodeError):
        return {"openai": "unknown", "anthropic": "unknown", "checked_at": None}


def is_degraded(settings: Settings) -> bool:
    """True only when BOTH providers are down (FR-043 trigger condition)."""
    if (settings.model_provider or "mock").lower() == "mock":
        return False
    state = current_state(settings)
    return state["openai"] == "down" and state["anthropic"] == "down"


def _probe_openai(settings: Settings) -> str:
    if not settings.openai_api_key:
        return "unknown"
    import httpx

    try:
        resp = httpx.get("https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {settings.openai_api_key}"}, timeout=10.0)
        # Any 4xx/5xx (bad key 401/403 included) means extraction calls would
        # fail too — only <400 counts as reachable for FR-043.
        return "ok" if resp.status_code < 400 else "down"
    except Exception:
        return "down"


def _probe_anthropic(settings: Settings) -> str:
    if not settings.anthropic_api_key:
        return "unknown"
    import httpx

    try:
        resp = httpx.get("https://api.anthropic.com/v1/models", headers={"x-api-key": settings.anthropic_api_key, "anthropic-version": "2023-06-01"}, timeout=10.0)
        # Any 4xx/5xx (bad key 401/403 included) means extraction calls would
        # fail too — only <400 counts as reachable for FR-043.
        return "ok" if resp.status_code < 400 else "down"
    except Exception:
        return "down"


def probe_once(settings: Settings, *, probe_openai=None, probe_anthropic=None) -> dict:
    """Ping both providers and persist the state. Probes are injectable for
    tests."""
    state = {
        "openai": (probe_openai or _probe_openai)(settings),
        "anthropic": (probe_anthropic or _probe_anthropic)(settings),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    path = _state_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state))
    return state
