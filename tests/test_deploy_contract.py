"""Production Compose, proxy, migration, and build-context contracts."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASE_COMPOSE = ROOT / "deploy" / "compose.yml"
PROD_COMPOSE = ROOT / "deploy" / "compose.prod.yml"


def _merged_production_config() -> dict:
    docker = _compose_available()

    env = os.environ.copy()
    env["PUBLIC_HOST"] = "rfp.example.com"
    rendered = subprocess.run(
        [
            docker,
            "compose",
            "-f",
            str(BASE_COMPOSE),
            "-f",
            str(PROD_COMPOSE),
            "config",
            "--no-env-resolution",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert rendered.returncode == 0, rendered.stderr
    return json.loads(rendered.stdout)


def _compose_available() -> str:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker CLI is unavailable")
    available = subprocess.run(
        [docker, "compose", "version"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if available.returncode != 0:
        pytest.skip("Docker Compose plugin is unavailable")
    return docker


def test_production_overlay_resets_api_host_port_and_requires_public_host():
    overlay = PROD_COMPOSE.read_text(encoding="utf-8")
    caddyfile = (ROOT / "deploy" / "Caddyfile").read_text(encoding="utf-8")

    assert "ports: !reset []" in overlay
    assert "${PUBLIC_HOST:?" in overlay
    assert "{$PUBLIC_HOST}" in caddyfile
    assert "{$PUBLIC_HOST:" not in caddyfile


def test_production_compose_refuses_a_missing_public_host():
    docker = _compose_available()
    env = os.environ.copy()
    # An explicit empty shell value outranks any developer `.env` file and is
    # rejected by Compose's `${name:?message}` interpolation contract.
    env["PUBLIC_HOST"] = ""
    rendered = subprocess.run(
        [
            docker,
            "compose",
            "-f",
            str(BASE_COMPOSE),
            "-f",
            str(PROD_COMPOSE),
            "config",
            "--no-env-resolution",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert rendered.returncode != 0
    assert "PUBLIC_HOST must be set" in rendered.stderr


def test_merged_production_compose_has_only_caddy_host_ingress():
    services = _merged_production_config()["services"]

    assert services["api"].get("ports", []) == []
    assert "--proxy-headers" in services["api"]["command"]
    assert "--forwarded-allow-ips=*" in services["api"]["command"]
    assert services["postgres"].get("ports", []) == []
    assert {port["published"] for port in services["caddy"]["ports"]} == {
        "80",
        "443",
    }
    assert services["caddy"]["environment"]["PUBLIC_HOST"] == "rfp.example.com"


def test_migration_is_one_shot_and_gates_every_database_consumer():
    services = _merged_production_config()["services"]
    migrate = services["migrate"]

    assert migrate["command"] == ["alembic", "upgrade", "head"]
    assert migrate["restart"] == "no"
    assert migrate["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert migrate["environment"]["APP_ENV"] == "prod"
    for service_name in ("api", "worker", "sweep"):
        assert (
            services[service_name]["depends_on"]["migrate"]["condition"]
            == "service_completed_successfully"
        )


def test_docker_context_is_a_source_allowlist_that_cannot_include_dotenv():
    rules = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    active = [line.strip() for line in rules if line.strip() and not line.startswith("#")]

    assert active[0] == "**"
    assert "!.env" not in active
    assert "!.env.example" not in active
    assert {
        "!app/**",
        "!alembic/**",
        "!scripts/**",
        "!deploy/Dockerfile",
        "!alembic.ini",
        "!pyproject.toml",
        "!uv.lock",
    }.issubset(active)
