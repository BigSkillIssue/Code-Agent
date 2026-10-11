"""Sending mail when SMTP is configured: verification and reset links, and the apps' relay."""

import asyncio
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from forge_web.settings import SmtpSettings


def mail_enabled(smtp: SmtpSettings) -> bool:
    """SMTP is set up."""
    return bool(smtp.host and smtp.from_address)


def _send(
    smtp: SmtpSettings, to: str, subject: str, body: str, from_name: str, reply_to: str
) -> None:
    message = EmailMessage()
    message["From"] = formataddr((from_name, smtp.from_address)) if from_name else smtp.from_address
    message["To"], message["Subject"] = to, subject
    if reply_to:
        message["Reply-To"] = reply_to
    message.set_content(body)
    with smtplib.SMTP(smtp.host, smtp.port, timeout=30) as client:
        if smtp.starttls:
            client.starttls(context=ssl.create_default_context())
        if smtp.username:
            client.login(smtp.username, os.environ.get(smtp.password_env, ""))
        client.send_message(message)


async def send_mail(
    smtp: SmtpSettings, to: str, subject: str, body: str, *, from_name: str = "", reply_to: str = ""
) -> None:
    """Send one plain-text mail (in a thread: smtplib blocks); always from the server's address,
    under another name if given (the mail relay sends in each app's name)."""
    await asyncio.to_thread(_send, smtp, to, subject, body, from_name, reply_to)
