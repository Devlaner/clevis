"""Unit tests for the GitHub App installation-token service (no DB, httpx mocked)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr

from src.core.config import settings
from src.services import github_app

_APP_ID = "123456"


def _make_keypair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private_pem, public_pem


@pytest.fixture
def app_configured(monkeypatch):
    """Configure the GitHub App settings with a throwaway keypair; clear the token cache."""
    private_pem, public_pem = _make_keypair()
    monkeypatch.setattr(settings, "github_app_id", _APP_ID)
    monkeypatch.setattr(settings, "github_app_private_key", SecretStr(private_pem))
    github_app.clear_cache()
    yield public_pem
    github_app.clear_cache()


def test_generate_app_jwt_claims_and_signature(app_configured):
    public_pem = app_configured
    token = github_app.generate_app_jwt(now=1_000_000)

    decoded = jwt.decode(token, public_pem, algorithms=["RS256"], options={"verify_exp": False})
    assert decoded["iss"] == _APP_ID
    assert decoded["iat"] == 1_000_000 - github_app._APP_JWT_BACKDATE_SECONDS
    assert decoded["exp"] == 1_000_000 + github_app._APP_JWT_TTL_SECONDS


def test_get_installation_token_caches(app_configured):
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    with patch("src.services.github_app.httpx.Client") as mock_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_resp = MagicMock()
        mock_resp.json = MagicMock(return_value={"token": "ghs_abc", "expires_at": future})
        mock_client.post = MagicMock(return_value=mock_resp)
        mock_cls.return_value = mock_client

        first = github_app.get_installation_token(99)
        second = github_app.get_installation_token(99)

    assert first == second == "ghs_abc"
    # Second call must be served from cache — only one HTTP request total.
    mock_client.post.assert_called_once()
    # Token request hits the installation access-tokens endpoint.
    url = mock_client.post.call_args.args[0]
    assert url.endswith("/app/installations/99/access_tokens")


def test_expired_cache_refetches(app_configured):
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    with patch("src.services.github_app.httpx.Client") as mock_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        resp_expired = MagicMock(json=MagicMock(return_value={"token": "ghs_old", "expires_at": past}))
        resp_fresh = MagicMock(json=MagicMock(return_value={"token": "ghs_new", "expires_at": future}))
        mock_client.post = MagicMock(side_effect=[resp_expired, resp_fresh])
        mock_cls.return_value = mock_client

        first = github_app.get_installation_token(7)   # already-expired token, not cached
        second = github_app.get_installation_token(7)   # must refetch

    assert first == "ghs_old"
    assert second == "ghs_new"
    assert mock_client.post.call_count == 2


def test_get_installation_token_rechecks_cache_after_acquiring_mint_lock(app_configured, monkeypatch):
    # Simulates another thread minting the token in the window between the lock-free
    # cache check and this call acquiring the per-installation mint lock: the second
    # check must short-circuit and no HTTP mint should happen.
    calls: list[int] = []

    def fake_cached(installation_id: int):
        calls.append(installation_id)
        return None if len(calls) == 1 else "ghs_raced"

    minted = MagicMock()
    monkeypatch.setattr(github_app, "_cached_token", fake_cached)
    monkeypatch.setattr(github_app, "_request_installation_token", minted)

    assert github_app.get_installation_token(42) == "ghs_raced"
    minted.assert_not_called()


def test_not_configured_raises(monkeypatch):
    monkeypatch.setattr(settings, "github_app_id", None)
    monkeypatch.setattr(settings, "github_app_private_key", None)
    with pytest.raises(github_app.GitHubAppNotConfigured):
        github_app.generate_app_jwt()


def test_get_installation_uses_app_jwt_not_installation_token(app_configured):
    with patch("src.services.github_app.httpx.Client") as mock_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_resp = MagicMock()
        mock_resp.json = MagicMock(return_value={"id": 99, "account": {"login": "acme", "type": "Organization"}})
        mock_client.get = MagicMock(return_value=mock_resp)
        mock_cls.return_value = mock_client

        result = github_app.get_installation(99)

    assert result["account"]["login"] == "acme"
    url = mock_client.get.call_args.args[0]
    assert url.endswith("/app/installations/99")
    auth_header = mock_client.get.call_args.kwargs["headers"]["Authorization"]
    # Distinct from an installation access token (which get_installation_token would mint) —
    # this must be the App's own JWT, decodable with the App's public key.
    token = auth_header.removeprefix("Bearer ")
    decoded = jwt.decode(token, app_configured, algorithms=["RS256"], options={"verify_exp": False})
    assert decoded["iss"] == _APP_ID


def test_get_installation_raises_on_404(app_configured):
    with patch("src.services.github_app.httpx.Client") as mock_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "not found",
                request=httpx.Request("GET", "https://api.github.com/app/installations/404"),
                response=httpx.Response(404),
            )
        )
        mock_client.get = MagicMock(return_value=mock_resp)
        mock_cls.return_value = mock_client

        with pytest.raises(httpx.HTTPStatusError):
            github_app.get_installation(404)


def _mock_get_client(mock_resp):
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get = MagicMock(return_value=mock_resp)
    return mock_client


def _mock_delete_client(mock_resp: MagicMock) -> MagicMock:
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.delete = MagicMock(return_value=mock_resp)
    return mock_client


def test_delete_installation_calls_the_uninstall_endpoint(app_configured):
    mock_resp = MagicMock(status_code=204)
    with patch("src.services.github_app.httpx.Client") as mock_cls:
        mock_cls.return_value = _mock_delete_client(mock_resp)
        github_app.delete_installation(42)

    mock_resp.raise_for_status.assert_called_once()
    url = mock_cls.return_value.delete.call_args.args[0]
    assert url.endswith("/app/installations/42")
    headers = mock_cls.return_value.delete.call_args.kwargs["headers"]
    assert headers["Authorization"].startswith("Bearer ")


def test_delete_installation_treats_404_as_success(app_configured):
    """Already uninstalled on GitHub's side (e.g. the user beat us to it) -- same end
    state as a successful delete, not an error."""
    mock_resp = MagicMock(status_code=404)
    with patch("src.services.github_app.httpx.Client") as mock_cls:
        mock_cls.return_value = _mock_delete_client(mock_resp)
        github_app.delete_installation(42)  # must not raise

    mock_resp.raise_for_status.assert_not_called()


def test_delete_installation_propagates_other_http_errors(app_configured):
    mock_resp = MagicMock(status_code=500)
    mock_resp.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError(
            "server error",
            request=httpx.Request("DELETE", "https://api.github.com/app/installations/42"),
            response=httpx.Response(500),
        )
    )
    with patch("src.services.github_app.httpx.Client") as mock_cls:
        mock_cls.return_value = _mock_delete_client(mock_resp)
        with pytest.raises(httpx.HTTPStatusError):
            github_app.delete_installation(42)


def test_get_org_membership_role_returns_role_for_active_membership():
    mock_resp = MagicMock(status_code=200)
    mock_resp.json = MagicMock(return_value={"state": "active", "role": "admin"})
    with patch("src.services.github_app.httpx.Client") as mock_cls:
        mock_cls.return_value = _mock_get_client(mock_resp)
        role = github_app.get_org_membership_role("itok", "acme", "founder")

    assert role == "admin"
    mock_resp.raise_for_status.assert_called_once()
    url = mock_cls.return_value.get.call_args.args[0]
    assert url.endswith("/orgs/acme/memberships/founder")
    headers = mock_cls.return_value.get.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer itok"


def test_get_org_membership_role_returns_none_on_404():
    mock_resp = MagicMock(status_code=404)
    with patch("src.services.github_app.httpx.Client") as mock_cls:
        mock_cls.return_value = _mock_get_client(mock_resp)
        role = github_app.get_org_membership_role("itok", "acme", "outsider")

    assert role is None
    mock_resp.raise_for_status.assert_not_called()


def test_get_org_membership_role_returns_none_for_pending_invite():
    mock_resp = MagicMock(status_code=200)
    mock_resp.json = MagicMock(return_value={"state": "pending", "role": "admin"})
    with patch("src.services.github_app.httpx.Client") as mock_cls:
        mock_cls.return_value = _mock_get_client(mock_resp)
        role = github_app.get_org_membership_role("itok", "acme", "invitee")

    assert role is None


def test_get_org_membership_role_propagates_other_http_errors():
    mock_resp = MagicMock(status_code=500)
    mock_resp.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError(
            "server error",
            request=httpx.Request("GET", "https://api.github.com/orgs/acme/memberships/founder"),
            response=httpx.Response(500),
        )
    )
    with patch("src.services.github_app.httpx.Client") as mock_cls:
        mock_cls.return_value = _mock_get_client(mock_resp)
        with pytest.raises(httpx.HTTPStatusError):
            github_app.get_org_membership_role("itok", "acme", "founder")
