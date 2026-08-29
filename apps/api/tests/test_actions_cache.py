"""Tests for actions-cache GitHub error mapping."""

from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.auth import UserOut, require_auth
from src.core.db import User, get_db
from src.repositories import installation_repo, org_membership_repo, org_repo
from src.routers.actions_cache import router as cache_router

_USER = UserOut(id=1, email="u@example.com", name=None, is_workspace_admin=False)


@pytest.fixture()
def cache_client(db):
    app = FastAPI()
    app.include_router(cache_router)
    app.dependency_overrides[require_auth] = lambda: _USER
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def test_list_caches_maps_github_status_error_to_400(cache_client):
    response = httpx.Response(404, request=httpx.Request("GET", "https://api.github.com/x"))
    error = httpx.HTTPStatusError("missing", request=response.request, response=response)
    with patch("src.routers.actions_cache.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = error
        resp = cache_client.post(
            "/me/repos/acme/demo/actions-caches",
            json={"token": "ghp_testtoken123456789012345678901234"},
        )
    assert resp.status_code == 400
    assert "404" in resp.json()["detail"]


# ── GitHub App installation-token fallback ─────────────────────────────────────

def test_list_caches_uses_installation_token_when_no_client_token(cache_client, db, monkeypatch):
    from pydantic import SecretStr

    from src.core.config import settings

    monkeypatch.setattr(settings, "github_app_id", "123")
    monkeypatch.setattr(settings, "github_app_private_key", SecretStr("dummy-pem"))
    db.add(User(id=_USER.id, email=_USER.email, name=None, password_hash=None, is_workspace_admin=False))
    db.commit()
    installation_repo.create(
        db, account_login="acme", account_type="User", auth_mode="app", installation_id=42, owner_user_id=_USER.id
    )
    with (
        patch("src.routers.actions_cache.GitHubClient") as mock_client,
        patch("src.services.token_resolution.github_app.get_installation_token", return_value="minted-token"),
    ):
        mock_client.return_value.request.return_value = {"total_count": 0, "actions_caches": []}
        resp = cache_client.post("/me/repos/acme/demo/actions-caches", json={})
    assert resp.status_code == 200
    mock_client.assert_called_once_with("minted-token")


def test_list_caches_no_installation_and_no_token_returns_400(cache_client):
    resp = cache_client.post("/me/repos/acme/demo/actions-caches", json={})
    assert resp.status_code == 400


def test_clear_caches_dry_run_does_not_require_a_token(cache_client, db):
    db.add(User(id=_USER.id, email=_USER.email, name=None, password_hash=None, is_workspace_admin=False))
    db.commit()
    resp = cache_client.post(
        "/me/repos/acme/demo/actions-caches/clear",
        json={"dry_run": True},
    )
    assert resp.status_code == 200
    assert resp.json()["dry_run"] is True


def test_personal_clear_caches_non_dry_run_uses_client_token(cache_client, db):
    db.add(User(id=_USER.id, email=_USER.email, name=None, password_hash=None, is_workspace_admin=False))
    db.commit()
    resp = cache_client.post(
        "/me/repos/acme/demo/actions-caches/clear",
        json={"dry_run": False, "token": "ghp_testtoken123456789012345678901234"},
    )
    assert resp.status_code == 200
    assert resp.json()["queued"] is True


def test_clear_caches_rejects_oversized_key_and_ref(cache_client):
    # Regression test for issue #224 item 3: CacheClearInput.key/.ref had no max_length,
    # letting a caller bloat the jobs/audit_logs payload columns with an arbitrarily large
    # value via a legitimate authenticated endpoint.
    resp = cache_client.post(
        "/me/repos/acme/demo/actions-caches/clear",
        json={"dry_run": True, "key": "x" * 600},
    )
    assert resp.status_code == 422

    resp = cache_client.post(
        "/me/repos/acme/demo/actions-caches/clear",
        json={"dry_run": True, "ref": "x" * 300},
    )
    assert resp.status_code == 422


def test_personal_clear_caches_non_dry_run_no_token_returns_400(cache_client):
    resp = cache_client.post(
        "/me/repos/acme/demo/actions-caches/clear",
        json={"dry_run": False},
    )
    assert resp.status_code == 400


def test_personal_clear_caches_rejects_org_member_supplying_own_token(cache_client, db):
    # Regression test (CodeRabbit finding on PR #264): a plain "member" of a connected
    # org must not be able to trigger its cache clear through the personal endpoint by
    # supplying their own PAT -- that would bypass the admin-only gate this endpoint is
    # supposed to enforce via resolve_owner_token(min_role="admin").
    db.add(User(id=_USER.id, email=_USER.email, name=None, password_hash=None, is_workspace_admin=False))
    db.commit()
    org = org_repo.get_or_create(db, github_login="acme")
    org_membership_repo.get_or_create(db, org.id, _USER.id, role="member")
    installation_repo.create(
        db, account_login="acme", account_type="Organization", auth_mode="app", installation_id=42, org_id=org.id
    )
    resp = cache_client.post(
        "/me/repos/acme/demo/actions-caches/clear",
        json={"dry_run": False, "token": "ghp_testtoken123456789012345678901234"},
    )
    assert resp.status_code == 403
