"""Outbound email.

SMTP is optional (see src.core.config.Settings). If unconfigured, send_email raises
EmailNotConfigured and callers are expected to degrade gracefully -- neither account
creation (issue #217) nor the leadership digest (issue #292) may fail because email
sending isn't set up.
"""

import smtplib
from email.message import EmailMessage

from src.core.config import settings


class EmailNotConfigured(RuntimeError):
    """Raised when SMTP isn't configured. Mirrors GitHubOAuthNotConfigured
    (src.services.github_oauth) -- callers decide how to degrade."""


def is_configured() -> bool:
    return bool(settings.smtp_host and settings.smtp_from)


def send_email(to_email: str, subject: str, text_body: str, html_body: str | None = None) -> None:
    """Send one plain-text (optionally multipart-with-HTML) message. Raises
    EmailNotConfigured if SMTP isn't set up."""
    if not is_configured():
        raise EmailNotConfigured("SMTP_HOST and SMTP_FROM must be set to send email")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.smtp_from
    message["To"] = to_email
    message.set_content(text_body)
    if html_body is not None:
        message.add_alternative(html_body, subtype="html")

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
        smtp.starttls()
        if settings.smtp_user and settings.smtp_password:
            smtp.login(settings.smtp_user, settings.smtp_password.get_secret_value())
        smtp.send_message(message)


def send_verification_email(to_email: str, verify_url: str) -> None:
    send_email(
        to_email,
        "Verify your Clevis email address",
        (
            "Welcome to Clevis!\n\n"
            "Verify your email address to accept organization invitations:\n"
            f"{verify_url}\n\n"
            "If you didn't create this account, you can ignore this email."
        ),
    )
