"""Sweep orchestration (FR-021/FR-022/FR-044) — one full sweep cycle.

The sweep container NEVER runs the extraction pipeline synchronously: per
match it enqueues a ``jobs`` row (priority=1) for the worker. Sweep jobs are
exempt from the per-account inference cap (FR-039). No vendor registry, no
packets, no outreach (section 1 scope decision).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.mailer import Mailer
from app.core.models import Account, ExtractionRun, Rfp, SamNotice, SweepRun
from app.core.queue import PRIORITY_SWEEP, enqueue
from app.sweep.client import AttachmentRejected, SamGovClient
from app.sweep.digest import DigestEntry, send_digest
from app.sweep.matcher import matches_watchlist


def _already_matched(session: Session, account_id: uuid.UUID, solicitation_no: str) -> bool:
    row = session.execute(
        select(Rfp.id).where(
            Rfp.account_id == account_id,
            Rfp.origin == "sweep",
            Rfp.solicitation_no == solicitation_no,
        )
    ).first()
    return row is not None


def run_sweep_once(
    session: Session,
    settings: Settings,
    *,
    client: SamGovClient | None = None,
    mailer: Mailer | None = None,
    posted_from: str | None = None,
    posted_to: str | None = None,
) -> SweepRun:
    now = datetime.now(timezone.utc)
    posted_to = posted_to or now.strftime("%m/%d/%Y")
    posted_from = posted_from or (now - timedelta(hours=24)).strftime("%m/%d/%Y")
    client = client or SamGovClient(settings.sam_api_key, max_rps=settings.sam_max_rps)
    mailer = mailer or Mailer(settings)

    sweep = SweepRun(started_at=now, found=0, matched=0, errors=[])
    session.add(sweep)
    session.flush()

    result = client.fetch_postings(posted_from, posted_to)
    errors: list[dict] = list(result.errors)
    sweep.found = len(result.notices)

    # Upsert into sam_notices (dedup via notice_id PK).
    for notice in result.notices:
        existing = session.get(SamNotice, notice.notice_id)
        if existing is None:
            session.add(
                SamNotice(
                    notice_id=notice.notice_id,
                    title=notice.title,
                    naics=notice.naics,
                    set_aside=notice.set_aside,
                    posted_at=notice.posted_at,
                    due_at=notice.due_at,
                    payload=notice.payload,
                )
            )
    session.flush()

    staff = session.execute(
        select(Account).where(Account.is_staff.is_(True))
    ).scalars().all()

    digest_sent = False
    for founder in staff:
        if not founder.watch_naics:
            continue
        fresh = [
            n
            for n in result.notices
            if matches_watchlist(n, [str(x) for x in founder.watch_naics], [str(x) for x in founder.watch_set_asides])
        ]
        entries: list[DigestEntry] = []
        processed = 0
        skipped_excess = 0
        for notice in fresh:
            sol_no = (notice.payload.get("solicitationNumber") or notice.notice_id)
            if _already_matched(session, founder.id, sol_no):
                continue
            if processed >= settings.max_sweep_matches_per_run:
                skipped_excess += 1
                continue
            processed += 1
            entry = _materialize_match(session, settings, client, founder, notice, sol_no, errors)
            if entry is not None:
                entries.append(entry)
                sweep.matched += 1
        if skipped_excess:
            errors.append(
                {"kind": "excess_matches_skipped", "count": skipped_excess, "account": founder.email}
            )
        if send_digest(mailer, to=founder.email, entries=entries):
            digest_sent = True

    if digest_sent:
        from app.core import auditlog

        auditlog.audit(session, action="sweep_digest_sent", entity="sweep_run", entity_id=sweep.id)

    sweep.errors = errors
    sweep.finished_at = datetime.now(timezone.utc)
    session.flush()
    return sweep


def _materialize_match(
    session: Session,
    settings: Settings,
    client: SamGovClient,
    founder: Account,
    notice,
    sol_no: str,
    errors: list[dict],
) -> DigestEntry | None:
    """Fetch + validate the attachment, create the rfp/run/job rows. Invalid
    attachments are logged and skipped without touching siblings (AC-053)."""
    if not notice.attachment_url:
        errors.append({"kind": "no_attachment_url", "notice_id": notice.notice_id})
        return None
    try:
        data = client.fetch_attachment(notice.attachment_url, max_bytes=settings.max_upload_bytes)
    except AttachmentRejected as exc:
        errors.append({"kind": exc.kind, "notice_id": notice.notice_id})
        return None
    except Exception as exc:
        errors.append({"kind": "attachment_fetch_failed", "notice_id": notice.notice_id, "error": str(exc)[:200]})
        return None

    incoming = Path(settings.files_dir) / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    from app.pipeline.intake_files import content_sha256, sniff_type

    kind = sniff_type(data) or "pdf"
    stored = incoming / f"{uuid.uuid4()}.{kind}"
    stored.write_bytes(data)

    rfp = Rfp(
        account_id=founder.id,
        orig_name=f"{sol_no}.{kind}",
        stored_path=str(stored),
        content_hash=content_sha256(data),
        solicitation_no=sol_no,
        due_at=notice.due_at,
        state="received",
        retain_source=False,
        origin="sweep",
    )
    session.add(rfp)
    session.flush()
    session.add(ExtractionRun(rfp_id=rfp.id, stage="reading"))
    enqueue(session, rfp.id, PRIORITY_SWEEP)
    session.flush()
    return DigestEntry(
        solicitation_no=sol_no,
        title=notice.title,
        due_date=notice.due_at.date().isoformat() if notice.due_at else "(no due date)",
        matrix_url=f"{settings.public_url}/app/matrices/{rfp.id}",
    )
