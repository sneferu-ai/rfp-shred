"""Scheduler (sweep container entrypoint, section 6).

One cron loop drives:
- FR-021 nightly sweep (SWEEP_CRON, default 06:00 UTC)
- FR-020 source cleanup (CLEANUP_CRON, default 03:00 UTC)
- FR-035 weekly quality monitoring (QUALITY_CRON, default Sunday 03:00 UTC)
- FR-043 provider health probe (PROVIDER_HEALTH_CRON, default every 30 min)

The cron parser supports the field shapes the defaults use: exact numbers,
``*``, and ``*/n`` (minute/hour/dom/month/dow). Anything else falls back to
"run daily at 03:00 UTC" with a loud log line rather than crashing the loop.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy.orm import sessionmaker

from app.core import maintenance, provider_health
from app.core.config import Settings
from app.core.mailer import Mailer
from app.core.models import QualityRun
from app.sweep.runner import run_sweep_once

log = logging.getLogger(__name__)

OMISSION_THRESHOLD = 0.10
FP_THRESHOLD = 0.20
PAGE_ATTR_THRESHOLD = 0.05


@dataclass
class CronSpec:
    minute: str
    hour: str
    dom: str
    month: str
    dow: str

    @classmethod
    def parse(cls, text: str) -> "CronSpec":
        parts = (text or "").split()
        if len(parts) != 5:
            log.warning("unparseable cron spec %r — falling back to daily 03:00", text)
            return cls("0", "3", "*", "*", "*")
        return cls(*parts)


def _field_matches(field: str, value: int) -> bool:
    if field == "*":
        return True
    if field.startswith("*/"):
        try:
            return value % int(field[2:]) == 0
        except ValueError:
            return False
    try:
        return value == int(field)
    except ValueError:
        return False


def is_due(spec: CronSpec, now: datetime, last_run_minute: datetime | None) -> bool:
    """Due when the current minute matches and we have not already run in
    this minute."""
    if not (
        _field_matches(spec.minute, now.minute)
        and _field_matches(spec.hour, now.hour)
        and _field_matches(spec.dom, now.day)
        and _field_matches(spec.month, now.month)
        and _field_matches(spec.dow, (now.weekday() + 1) % 7)  # cron: 0=Sunday
    ):
        return False
    if last_run_minute is not None and last_run_minute.replace(second=0, microsecond=0) == now.replace(second=0, microsecond=0):
        return False
    return True


def run_quality_check(session, settings: Settings, corpus_dir: str) -> QualityRun | None:
    """FR-035: re-run the FR-025 harness on a rotating 3-RFP subset; write a
    quality_runs row; threshold breaches surface an audit_events warning."""
    from scripts.evaluate_extraction import evaluate_corpus, load_manifest

    manifest = load_manifest(corpus_dir)
    subset = sorted(manifest)[:3]
    if not subset:
        log.warning("quality check: empty corpus at %s", corpus_dir)
        return None
    metrics = evaluate_corpus(corpus_dir, subset=subset, settings=settings)
    if metrics is None:
        return None
    passed = (
        metrics["omission_rate"] <= OMISSION_THRESHOLD
        and metrics["fp_rate"] <= FP_THRESHOLD
        and metrics["page_attr_error"] <= PAGE_ATTR_THRESHOLD
    )
    row = QualityRun(
        corpus_subset=subset,
        omission_rate=metrics["omission_rate"],
        fp_rate=metrics["fp_rate"],
        page_attr_error=metrics["page_attr_error"],
        per_format=metrics.get("per_format", {}),
        passed=passed,
    )
    session.add(row)
    if not passed:
        from app.core import auditlog

        auditlog.audit(
            session, action="quality_threshold_breach", entity="quality_run", entity_id=row.id
        )
    session.flush()
    return row


def run_scheduler(
    session_factory: sessionmaker,
    settings: Settings,
    *,
    corpus_dir: str = "tests/fixtures/corpus",
    poll_s: float = 30.0,
    sleep_fn: Callable[[float], None] = time.sleep,
    max_cycles: int | None = None,
) -> None:
    last_runs: dict[str, datetime] = {}
    cycles = 0
    while True:
        now = datetime.now(timezone.utc)
        due_jobs = [
            ("sweep", settings.sweep_cron),
            ("cleanup", settings.cleanup_cron),
            ("quality", settings.quality_cron),
            ("provider_health", settings.provider_health_cron),
        ]
        for name, cron_text in due_jobs:
            spec = CronSpec.parse(cron_text)
            if not is_due(spec, now, last_runs.get(name)):
                continue
            last_runs[name] = now
            log.info("scheduler: running %s", name)
            try:
                with session_factory() as session:
                    if name == "sweep":
                        run_sweep_once(session, settings, mailer=Mailer(settings))
                    elif name == "cleanup":
                        maintenance.run_daily_maintenance(session, settings.monthly_inference_cap_usd)
                    elif name == "quality":
                        run_quality_check(session, settings, corpus_dir)
                    elif name == "provider_health":
                        provider_health.probe_once(settings)
                    session.commit()
            except Exception:
                log.exception("scheduler: %s failed", name)
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            return
        sleep_fn(poll_s)


def main() -> None:  # pragma: no cover - container entrypoint
    from app.core.db import make_engine, make_session_factory

    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env()
    engine = make_engine(settings.database_url)
    run_scheduler(make_session_factory(engine), settings)


if __name__ == "__main__":  # pragma: no cover
    main()
