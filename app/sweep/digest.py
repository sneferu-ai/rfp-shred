"""Daily watchlist digest email (FR-044).

Sent after all matches for a run are processed (or the per-run cap is
reached); zero matches -> no email. Uses the shared SMTP/outbox mailer.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.mailer import Mailer


@dataclass
class DigestEntry:
    solicitation_no: str
    title: str
    due_date: str
    matrix_url: str


def build_digest(entries: list[DigestEntry]) -> tuple[str, str]:
    subject = f"[RFP Shred] {len(entries)} new watchlist match(es)"
    lines = ["New RFPs matching your watchlist:", ""]
    for e in entries:
        lines.append(f"- {e.solicitation_no} — {e.title}")
        lines.append(f"  Due: {e.due_date}")
        lines.append(f"  Open: {e.matrix_url}")
        lines.append("")
    return subject, "\n".join(lines)


def send_digest(mailer: Mailer, *, to: str, entries: list[DigestEntry]) -> bool:
    """Returns True when an email was sent (>=1 match), False otherwise."""
    if not entries:
        return False
    subject, body = build_digest(entries)
    mailer.send(to=to, subject=subject, body=body)
    return True
