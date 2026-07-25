"""Tests for the GitHub OAuth router + find-or-create (DB-backed)."""

from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from src.core.config import settings
from src.core.db import User, get_db
from src.core.rate_limit import _buckets as _rate_limit_buckets
from src.routers.github_auth import EmailAlreadyRegistered, find_or_create_user
from src.routers.github_auth import router as gh_router
from src.services import github_oauth
from src.services.github_oauth import GitHubIdentity


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """/auth/github/callback is rate-limited per client IP via a module-level in-memory
    bucket that persists across tests in the same process -- reset it so one test's request
    volume can't tip a later, unrelated test over the threshold (same pattern as test_auth.py)."""
    _rate_limit_buckets.clear()
    yield
    _rate_limit_buckets.clear()


@pytest.fixture()
def gh_app(db):
    a = FastAPI()
    a.include_router(gh_router, prefix="/auth/github")
    a.dependency_overrides[get_db] = lambda: db
    return a


@pytest.fixture()
def gh_client(gh_app):
    # https base_url so Secure-flagged cookies (session + OAuth state, both set with
    # secure=settings.session_cookie_secure which defaults True) actually round-trip
    # through the client's cookie jar across requests, matching real browser behavior.
    return TestClient(gh_app, base_url="https://testserver")


@pytest.fixture()
def oauth_configured(monkeypatch):
    monkeypatch.setattr(settings, "github_app_client_id", "Iv1.abc")
    monkeypatch.setattr(settings, "github_app_client_secret", SecretStr("secret"))


def _identity(**kw) -> GitHubIdentity:
    base = dict(github_user_id=1001, login="octocat", name="Octo", email="octo@example.com", avatar_url=None)
    base.update(kw)
    return GitHubIdentity(**base)


# ── find_or_create_user ─────────────────────────────────────────────────────────

def test_first_user_becomes_owner(db):
    user = find_or_create_user(db, _identity())
    assert user.is_workspace_admin is True
    assert user.github_user_id == 1001
    assert user.password_hash is None


def test_second_user_is_member(db):
    find_or_create_user(db, _identity())
    second = find_or_create_user(db, _identity(github_user_id=2002, login="hubot", email="hubot@example.com"))
    assert second.is_workspace_admin is False


def test_refuses_to_auto_link_an_existing_email_registered_account(db):
    # Regression test for the account-takeover fix: self-registration has no email
    # verification anywhere in this app, so silently linking a GitHub identity onto an
    # existing account by email match alone would let an attacker who pre-registered a
    # victim's email inherit the victim's real GitHub identity. Must raise, not link.
    existing = User(email="owner@example.com", name="Owner", password_hash="x", is_workspace_admin=True)
    db.add(existing)
    db.commit()
    db.refresh(existing)

    with pytest.raises(EmailAlreadyRegistered):
        find_or_create_user(db, _identity(github_user_id=555, email="owner@example.com"))

    db.refresh(existing)
    assert existing.github_user_id is None
    assert db.query(User).count() == 1


def test_refuses_to_auto_link_when_existing_email_differs_only_by_case(db):
    # Regression test for issue #268: the email-collision check must be case-insensitive,
    # same as the users.email uniqueness constraint itself.
    existing = User(email="owner@example.com", name="Owner", password_hash="x", is_workspace_admin=True)
    db.add(existing)
    db.commit()

    with pytest.raises(EmailAlreadyRegistered):
        find_or_create_user(db, _identity(github_user_id=555, email="Owner@Example.com"))


def test_unrelated_email_still_creates_a_new_user(db):
    existing = User(email="owner@example.com", name="Owner", password_hash="x", is_workspace_admin=True)
    db.add(existing)
    db.commit()

    created = find_or_create_user(db, _identity(github_user_id=555, email="someone-else@example.com"))
    assert created.id != existing.id
    assert created.github_user_id == 555
    assert db.query(User).count() == 2


def test_idempotent_by_github_id(db):
    first = find_or_create_user(db, _identity())
    again = find_or_create_user(db, _identity(name="Octo Updated"))
    assert again.id == first.id
    assert db.query(User).count() == 1


def test_new_github_user_is_created_already_verified(db):
    # Regression test for issue #217: GitHub already vouches for the identity's email
    # (fetch_identity only ever returns a GitHub-verified address), so a GitHub-created
    # account shouldn't need to click an emailed verification link too.
    user = find_or_create_user(db, _identity())
    assert user.email_verified is True


# ── endpoints ─────────────────────────────────────────────────────────────────

def test_login_redirects_to_github(gh_client, oauth_configured):
    resp = gh_client.get("/auth/github/login", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"].startswith("https://github.com/login/oauth/authorize")
    assert "clevis_oauth_state" in resp.headers.get("set-cookie", "")


def test_login_unconfigured_redirects_to_ui_with_error(gh_client, monkeypatch):
    # Force unconfigured regardless of the ambient .env (a real App may be configured locally).
    monkeypatch.setattr(settings, "github_app_client_id", None)
    monkeypatch.setattr(settings, "github_app_client_secret", None)
    resp = gh_client.get("/auth/github/login", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].endswith("/login?error=github_not_configured")


def test_callback_creates_user_and_sets_cookie(gh_client, db):
    with (
        patch("src.routers.github_auth.github_oauth.verify_state", return_value=True),
        patch("src.routers.github_auth.github_oauth.exchange_code_for_token", return_value="gho_x"),
        patch("src.routers.github_auth.github_oauth.fetch_identity", return_value=_identity()),
        patch("src.routers.github_auth.org_provisioning.sync_org_admin_memberships", return_value=None),
    ):
        resp = gh_client.get("/auth/github/callback?code=c&state=s", follow_redirects=False)
    assert resp.status_code == 303
    assert "clevis_session" in resp.headers.get("set-cookie", "")
    assert db.query(User).filter(User.github_user_id == 1001).first() is not None


def test_callback_rejects_bad_state(gh_client):
    with patch("src.routers.github_auth.github_oauth.verify_state", return_value=False):
        resp = gh_client.get("/auth/github/callback?code=c&state=bad", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].endswith("/login?error=github_invalid_state")


def test_callback_redirects_with_error_when_email_already_registered(gh_client, db):
    existing = User(email="octo@example.com", name="Owner", password_hash="x", is_workspace_admin=True)
    db.add(existing)
    db.commit()

    with (
        patch("src.routers.github_auth.github_oauth.verify_state", return_value=True),
        patch("src.routers.github_auth.github_oauth.exchange_code_for_token", return_value="gho_x"),
        patch("src.routers.github_auth.github_oauth.fetch_identity", return_value=_identity()),
    ):
        resp = gh_client.get("/auth/github/callback?code=c&state=s", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].endswith("/login?error=github_email_registered")
    db.refresh(existing)
    assert existing.github_user_id is None


def test_callback_redirects_to_ui_when_oauth_becomes_unconfigured_mid_flow(gh_client):
    # e.g. an admin cleared GITHUB_APP_CLIENT_ID/SECRET between /login and /callback.
    with (
        patch("src.routers.github_auth.github_oauth.verify_state", return_value=True),
        patch(
            "src.routers.github_auth.github_oauth.exchange_code_for_token",
            side_effect=github_oauth.GitHubOAuthNotConfigured("not configured"),
        ),
    ):
        resp = gh_client.get("/auth/github/callback?code=c&state=s", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].endswith("/login?error=github_not_configured")


def test_callback_redirects_to_ui_on_oauth_error(gh_client):
    from src.services.github_oauth import GitHubOAuthError

    with (
        patch("src.routers.github_auth.github_oauth.verify_state", return_value=True),
        patch("src.routers.github_auth.github_oauth.exchange_code_for_token", side_effect=GitHubOAuthError("bad_verification_code")),
    ):
        resp = gh_client.get("/auth/github/callback?code=c&state=s", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].endswith("/login?error=github_oauth_failed")


# ── state cookie binding (regression tests for the OAuth login-CSRF fix) ────────

def _extract_state(login_resp) -> str:
    return parse_qs(urlparse(login_resp.headers["location"]).query)["state"][0]


def test_full_flow_succeeds_when_the_state_cookie_matches(gh_client, db, oauth_configured):
    login_resp = gh_client.get("/auth/github/login", follow_redirects=False)
    state = _extract_state(login_resp)

    with (
        patch("src.routers.github_auth.github_oauth.exchange_code_for_token", return_value="gho_x"),
        patch("src.routers.github_auth.github_oauth.fetch_identity", return_value=_identity()),
        patch("src.routers.github_auth.org_provisioning.sync_org_admin_memberships", return_value=None),
    ):
        # Same client/cookie jar as /login -- the browser that started the flow.
        resp = gh_client.get(f"/auth/github/callback?code=c&state={state}", follow_redirects=False)
    assert resp.status_code == 303
    assert "clevis_session" in resp.headers.get("set-cookie", "")


def test_replaying_a_captured_state_from_a_different_browser_is_rejected(gh_client, db, oauth_configured):
    # Simulates the attack this fix closes: an attacker captures a valid (code, state)
    # pair from their own OAuth flow and gets a victim to open the callback URL. The
    # victim's browser never had the matching clevis_oauth_state cookie set.
    login_resp = gh_client.get("/auth/github/login", follow_redirects=False)
    state = _extract_state(login_resp)
    gh_client.cookies.delete("clevis_oauth_state")

    resp = gh_client.get(f"/auth/github/callback?code=c&state={state}", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].endswith("/login?error=github_invalid_state")
    assert db.query(User).count() == 0


# ── next-path threading (invite-link OAuth fix) ─────────────────────────────────

def test_login_embeds_next_into_state(gh_client, oauth_configured):
    login_resp = gh_client.get("/auth/github/login?next=/invite/abc123", follow_redirects=False)
    state = _extract_state(login_resp)
    assert github_oauth.decode_state_next(state) == "/invite/abc123"


def test_login_drops_unsafe_next(gh_client, oauth_configured):
    login_resp = gh_client.get("/auth/github/login?next=//evil.com", follow_redirects=False)
    state = _extract_state(login_resp)
    assert github_oauth.decode_state_next(state) is None


def test_callback_redirects_to_next_path_from_invite_link(gh_client, db, oauth_configured):
    login_resp = gh_client.get("/auth/github/login?next=/invite/abc123", follow_redirects=False)
    state = _extract_state(login_resp)

    with (
        patch("src.routers.github_auth.github_oauth.exchange_code_for_token", return_value="gho_x"),
        patch("src.routers.github_auth.github_oauth.fetch_identity", return_value=_identity()),
        patch("src.routers.github_auth.org_provisioning.sync_org_admin_memberships", return_value=None),
    ):
        resp = gh_client.get(f"/auth/github/callback?code=c&state={state}", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "http://localhost:3000/invite/abc123"


def test_callback_redirects_to_root_when_no_next_was_requested(gh_client, db, oauth_configured):
    login_resp = gh_client.get("/auth/github/login", follow_redirects=False)
    state = _extract_state(login_resp)

    with (
        patch("src.routers.github_auth.github_oauth.exchange_code_for_token", return_value="gho_x"),
        patch("src.routers.github_auth.github_oauth.fetch_identity", return_value=_identity()),
        patch("src.routers.github_auth.org_provisioning.sync_org_admin_memberships", return_value=None),
    ):
        resp = gh_client.get(f"/auth/github/callback?code=c&state={state}", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "http://localhost:3000"


def test_callback_ignores_unsafe_next_and_redirects_to_root(gh_client, db, oauth_configured):
    login_resp = gh_client.get("/auth/github/login?next=//evil.com", follow_redirects=False)
    state = _extract_state(login_resp)

    with (
        patch("src.routers.github_auth.github_oauth.exchange_code_for_token", return_value="gho_x"),
        patch("src.routers.github_auth.github_oauth.fetch_identity", return_value=_identity()),
        patch("src.routers.github_auth.org_provisioning.sync_org_admin_memberships", return_value=None),
    ):
        resp = gh_client.get(f"/auth/github/callback?code=c&state={state}", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "http://localhost:3000"


def test_callback_clears_the_state_cookie_on_success(gh_client, db, oauth_configured):
    login_resp = gh_client.get("/auth/github/login", follow_redirects=False)
    state = _extract_state(login_resp)

    with (
        patch("src.routers.github_auth.github_oauth.exchange_code_for_token", return_value="gho_x"),
        patch("src.routers.github_auth.github_oauth.fetch_identity", return_value=_identity()),
        patch("src.routers.github_auth.org_provisioning.sync_org_admin_memberships", return_value=None),
    ):
        resp = gh_client.get(f"/auth/github/callback?code=c&state={state}", follow_redirects=False)
    # There are two Set-Cookie headers on this response (session + state-cookie-clear) --
    # use get_list so the second one isn't dropped by a naive single-value lookup.
    set_cookies = resp.headers.get_list("set-cookie")
    state_cookie_headers = [c for c in set_cookies if c.startswith("clevis_oauth_state=")]
    assert len(state_cookie_headers) == 1
    assert "Max-Age=0" in state_cookie_headers[0]
