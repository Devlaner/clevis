"""Tests for src.services.email (issue #217)."""

import ssl
from unittest.mock import MagicMock, patch

import pytest
from pydantic import SecretStr

from src.core.config import settings
from src.services.email import EmailNotConfigured, is_configured, send_email, send_verification_email


def test_is_configured_reflects_smtp_settings(monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", None)
    monkeypatch.setattr(settings, "smtp_from", "clevis@example.com")
    assert is_configured() is False
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    assert is_configured() is True


def test_send_email_attaches_html_alternative(monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_from", "clevis@example.com")
    monkeypatch.setattr(settings, "smtp_user", None)
    monkeypatch.setattr(settings, "smtp_password", None)

    mock_smtp = MagicMock()
    mock_smtp.__enter__ = MagicMock(return_value=mock_smtp)
    mock_smtp.__exit__ = MagicMock(return_value=False)
    with patch("src.services.email.smtplib.SMTP", return_value=mock_smtp):
        send_email("ceo@example.com", "Digest", "plain body", html_body="<p>rich</p>")

    sent = mock_smtp.send_message.call_args[0][0]
    assert sent["Subject"] == "Digest"
    assert sent.is_multipart()
    assert {p.get_content_type() for p in sent.iter_parts()} == {"text/plain", "text/html"}


def test_send_email_raises_when_unconfigured(monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", None)
    with pytest.raises(EmailNotConfigured):
        send_email("x@example.com", "s", "b")


def test_raises_not_configured_when_smtp_host_unset(monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", None)
    monkeypatch.setattr(settings, "smtp_from", "clevis@example.com")
    with pytest.raises(EmailNotConfigured):
        send_verification_email("user@example.com", "https://app.example.com/verify-email?token=x")


def test_raises_not_configured_when_smtp_from_unset(monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_from", None)
    with pytest.raises(EmailNotConfigured):
        send_verification_email("user@example.com", "https://app.example.com/verify-email?token=x")


def test_sends_via_smtp_with_starttls_and_login(monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_port", 587)
    monkeypatch.setattr(settings, "smtp_from", "clevis@example.com")
    monkeypatch.setattr(settings, "smtp_user", "smtp-user")
    monkeypatch.setattr(settings, "smtp_password", SecretStr("smtp-pass"))

    mock_smtp = MagicMock()
    mock_smtp.__enter__ = MagicMock(return_value=mock_smtp)
    mock_smtp.__exit__ = MagicMock(return_value=False)

    with patch("src.services.email.smtplib.SMTP", return_value=mock_smtp) as mock_cls:
        send_verification_email("user@example.com", "https://app.example.com/verify-email?token=abc")

    mock_cls.assert_called_once_with("smtp.example.com", 587, timeout=10)
    mock_smtp.starttls.assert_called_once()
    # STARTTLS must be given a verifying SSL context (cert + hostname checks).
    tls_context = mock_smtp.starttls.call_args.kwargs.get("context")
    assert isinstance(tls_context, ssl.SSLContext)
    assert tls_context.verify_mode == ssl.CERT_REQUIRED
    assert tls_context.check_hostname is True
    mock_smtp.login.assert_called_once_with("smtp-user", "smtp-pass")
    mock_smtp.send_message.assert_called_once()
    sent_message = mock_smtp.send_message.call_args[0][0]
    assert sent_message["To"] == "user@example.com"
    assert sent_message["From"] == "clevis@example.com"
    assert "verify-email?token=abc" in sent_message.get_content()


def test_sends_without_login_when_no_smtp_credentials(monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_from", "clevis@example.com")
    monkeypatch.setattr(settings, "smtp_user", None)
    monkeypatch.setattr(settings, "smtp_password", None)

    mock_smtp = MagicMock()
    mock_smtp.__enter__ = MagicMock(return_value=mock_smtp)
    mock_smtp.__exit__ = MagicMock(return_value=False)

    with patch("src.services.email.smtplib.SMTP", return_value=mock_smtp):
        send_verification_email("user@example.com", "https://app.example.com/verify-email?token=abc")

    mock_smtp.login.assert_not_called()
    mock_smtp.send_message.assert_called_once()
