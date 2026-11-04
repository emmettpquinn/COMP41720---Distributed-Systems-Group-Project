from __future__ import annotations

from email.message import EmailMessage
from typing import Protocol

import aiosmtplib


class MailDeliveryError(RuntimeError):
    pass


class Mailer(Protocol):
    async def send(self, recipient: str, subject: str, body: str, message_key: str) -> None: ...


class SmtpMailer:
    """Sends through SMTP. MailHog in development and in the demo."""

    def __init__(self, host: str, port: int, sender: str, timeout_s: float = 5.0) -> None:
        self.host = host
        self.port = port
        self.sender = sender
        self.timeout_s = timeout_s

    async def send(self, recipient: str, subject: str, body: str, message_key: str) -> None:
        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = recipient
        msg["Subject"] = subject
        # Lets anyone inspecting the inbox see which notification produced each email.
        msg["X-Notification-Key"] = message_key
        msg.set_content(body)
        try:
            await aiosmtplib.send(msg, hostname=self.host, port=self.port, timeout=self.timeout_s)
        except (aiosmtplib.SMTPException, OSError, TimeoutError) as exc:
            raise MailDeliveryError(f"SMTP {self.host}:{self.port} failed: {exc}") from exc
