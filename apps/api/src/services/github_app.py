"""GitHub App authentication — mint short-lived installation access tokens.

A GitHub App authenticates in two steps:
  1. The App proves it is itself with a short-lived **App JWT** (RS256, signed with the App's
     private key, `iss` = App ID, max 10-minute lifetime).
  2. That JWT is exchanged for a per-**installation** access token via
     `POST /app/installations/{id}/access_tokens`. Installation tokens expire after ~1 hour.

This module owns step 1 + 2 and caches installation tokens in-process until shortly before they
expire. Tokens are never persisted. The GITHUB_APP_* settings must be configured to use it
(see `src.core.config.Settings`); otherwise `GitHubAppNotConfigured` is raised.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime

import httpx
import jwt

from src.core.config import settings

# GitHub rejects App JWTs with a lifetime over 10 minutes; stay comfortably under.
_APP_JWT_TTL_SECONDS = 540  # 9 minutes
# Backdate `iat` to tolerate small clock skew between us and GitHub.
_APP_JWT_BACKDATE_SECONDS = 60
# Refresh an installation token slightly before GitHub's stated expiry to avoid edge races.
_TOKEN_EXPIRY_MARGIN_SECONDS = 60


class GitHubAppNotConfigured(RuntimeError):
    """Raised when the GitHub App settings (id + private key) are not configured."""


@dataclass
class _CachedToken:
    token: str
    expires_at: float  # epoch seconds (GitHub's stated expiry)


_cache: dict[int, _CachedToken] = {}
_lock = threading.Lock()


def _require_config() -> tuple[str, str]:
    app_id = settings.github_app_id
    private_key = settings.github_app_private_key
    if not app_id or not private_key:
        raise GitHubAppNotConfigured(
            "GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY must be set to use GitHub App auth"
        )
    return app_id, private_key.get_secret_value()


def generate_app_jwt(*, now: int | None = None) -> str:
    """Return an RS256 JWT identifying the App itself (used to request installation tokens)."""
    app_id, private_key = _require_config()
    issued = int(now if now is not None else time.time())
    payload = {
        "iat": issued - _APP_JWT_BACKDATE_SECONDS,
        "exp": issued + _APP_JWT_TTL_SECONDS,
        "iss": app_id,
    }
    return jwt.encode(payload, private_key, algorithm="RS256")


def _request_installation_token(installation_id: int) -> _CachedToken:
    app_jwt = generate_app_jwt()
    url = f"{settings.github_api_base}/app/installations/{installation_id}/access_tokens"
    headers = {
        "Authorization": f"Bearer {app_jwt}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    with httpx.Client(timeout=20) as client:
        resp = client.post(url, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    # GitHub returns ISO-8601 with a trailing "Z"; normalise for fromisoformat.
    expires_at = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
    return _CachedToken(token=data["token"], expires_at=expires_at.timestamp())


def get_installation(installation_id: int) -> dict:
    """Look up an installation by ID using the App's own JWT (not an installation
    token) — used to verify a client-supplied installation_id is real and confirm
    which account it actually belongs to, before trusting it. Raises
    httpx.HTTPStatusError (404 if the installation doesn't exist) or
    GitHubAppNotConfigured."""
    app_jwt = generate_app_jwt()
    url = f"{settings.github_api_base}/app/installations/{installation_id}"
    headers = {
        "Authorization": f"Bearer {app_jwt}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    with httpx.Client(timeout=20) as client:
        resp = client.get(url, headers=headers)
    resp.raise_for_status()
    return resp.json()


def delete_installation(installation_id: int) -> None:
    """Uninstall the App from an account/org via GitHub's App API (DELETE /app/installations/{id}),
    using the App's own JWT -- this is a real revocation, not just removing our own DB row, so a
    user who disconnects in Clevis's UI actually stops granting Clevis repo access, the same as if
    they'd uninstalled it from github.com/settings/installations themselves. A 404 (already
    uninstalled -- e.g. the user beat us to it on GitHub's side) is treated as success, not an
    error, since the end state either way is "no installation." Raises httpx.HTTPStatusError for
    any other GitHub error or GitHubAppNotConfigured; callers must not delete the local DB row on
    those, so a failed uninstall doesn't leave Clevis's own record silently out of sync with a
    GitHub-side installation that's still actually there."""
    app_jwt = generate_app_jwt()
    url = f"{settings.github_api_base}/app/installations/{installation_id}"
    headers = {
        "Authorization": f"Bearer {app_jwt}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    with httpx.Client(timeout=20) as client:
        resp = client.delete(url, headers=headers)
    if resp.status_code == 404:
        return
    resp.raise_for_status()


def get_installation_token(installation_id: int) -> str:
    """Return a valid installation access token, minting and caching it as needed."""
    with _lock:
        cached = _cache.get(installation_id)
        if cached and cached.expires_at - _TOKEN_EXPIRY_MARGIN_SECONDS > time.time():
            return cached.token
        fresh = _request_installation_token(installation_id)
        _cache[installation_id] = fresh
        return fresh.token


def get_org_membership_role(installation_token: str, org_login: str, username: str) -> str | None:
    """Ask GitHub, authenticated as the App installation (not a user token), what role
    `username` currently holds in `org_login` -- "admin" | "member" -- or None if they
    have no active membership visible to the installation (404, or a non-"active" state
    like a pending invite). Requires the App's installation to have at least read access
    to the org's "Members" permission (already part of the documented required grants,
    see docs/self-hosting.md). Used to bootstrap a brand-new Org/OrgMembership at
    install-sync time without needing the caller's (never-persisted) OAuth user token."""
    url = f"{settings.github_api_base}/orgs/{org_login}/memberships/{username}"
    headers = {
        "Authorization": f"Bearer {installation_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    with httpx.Client(timeout=20) as client:
        resp = client.get(url, headers=headers)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json()
    if data.get("state") != "active":
        return None
    return data.get("role")


def clear_cache() -> None:
    """Drop all cached installation tokens (used by tests and on config change)."""
    with _lock:
        _cache.clear()
