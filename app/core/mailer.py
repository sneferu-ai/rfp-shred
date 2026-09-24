"""Mail sending via SMTP (FR-028 dunning/digest share this).

When ``SMTP_URL`` is empty (dev/test), mail is written to
``FILES_DIR/outbox/*.eml`` instead — the outbox mailer is the seam the
test-suite and the dev stack use to observe sent mail.
"""

from __future__ import annotations

import smtplib
import time
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlparse

from app.core.config import Settings


@dataclass
class SentMail:
    to: str
    subject: str
    body: str


class Mailer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def send(self, *, to: str, subject: str, body: str) -> SentMail:
        if not self.settings.smtp_url:
            self._write_outbox(to=to, subject=subject, body=body)
        else:
            self._send_smtp(to=to, subject=subject, body=body)
        return SentMail(to=to, subject=subject, body=body)

    def _write_outbox(self, *, to: str, subject: str, body: str) -> None:
        outbox = Path(self.settings.files_dir) / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        msg = EmailMessage()
        msg["From"] = self.settings.mail_from
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        name = f"{int(time.time() * 1000)}-{abs(hash((to, subject))) % 99999}.eml"
        (outbox / name).write_text(msg.as_string())

    def _send_smtp(self, *, to: str, subject: str, body: str) -> None:
        url = urlparse(self.settings.smtp_url)
        host = url.hostname or "localhost"
        port = url.port or (465 if url.scheme == "smtps" else 25)
        username = url.username
        password = url.password
        msg = EmailMessage()
        msg["From"] = self.settings.mail_from
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        cls = smtplib.SMTP_SSL if url.scheme == "smtps" else smtplib.SMTP
        with cls(host, port, timeout=30) as smtp:
            if username:
                smtp.login(username, password or "")
            smtp.send_message(msg)
