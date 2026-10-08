"""Sending verification and reset links by mail, when SMTP is configured."""

import asyncio
import os
import smtplib
import ssl
from email.message import EmailMessage

from forge_web.settings import SmtpSettings


def mail_enabled(smtp: SmtpSettings) -> bool:
    """SMTP is set up."""
    return bool(smtp.host and smtp.from_address)


def _send(smtp: SmtpSettings, to: str, subject: str, body: str) -> None:
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = smtp.from_address, to, subject
    message.set_content(body)
    with smtplib.SMTP(smtp.host, smtp.port, timeout=30) as client:
        if smtp.starttls:
            client.starttls(context=ssl.create_default_context())
        if smtp.username:
            client.login(smtp.username, os.environ.get(smtp.password_env, ""))
        client.send_message(message)


async def send_mail(smtp: SmtpSettings, to: str, subject: str, body: str) -> None:
    """Send one plain-text mail (in a thread: smtplib blocks)."""
    await asyncio.to_thread(_send, smtp, to, subject, body)
