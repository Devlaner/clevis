"""Tests for GET /orgs/{org}/usage/actions (issue #294).

Scaffold endpoint: admin-only, reads GitHub's Actions billing API, which needs an App
permission Clevis doesn't request by default -- a 403 from GitHub is turned into a 400
with a clear hint so the UI can hide the card instead of erroring the page.
"""

from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.auth import UserOut, require_auth
from src.core.db import User, get_db
from src.repositories import org_membership_repo, org_repo
from src.routers.analytics import router


def _make_user(db, email: str) -> UserOut:
    user = User(email=email, name=None, password_hash=None, is_workspace_admin=False)
    db.add(user)
    db.commit()
    db.refresh(user)
    return UserOut(id=user.id, email=user.email, name=user.name, is_workspace_admin=False)


@pytest.fixture()
def user(db) -> UserOut:
    return _make_user(db, "usage@example.com")


@pytest.fixture()
def http(db, user) -> TestClient:
    app = FastAPI()
    app.dependency_overrides[require_auth] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    app.include_router(router)
    return TestClient(app)


def _admin_org(db, user):
    org = org_repo.get_or_create(db, github_login="acme")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=user.id, role="admin")
    db.commit()
    return org


def test_outsider_forbidden(http, db):
    org_repo.get_or_create(db, github_login="acme")
    assert http.get("/orgs/acme/usage/actions").status_code == 403


def test_member_forbidden(http, db, user):
    org = org_repo.get_or_create(db, github_login="acme")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=user.id, role="member")
    db.commit()
    assert http.get("/orgs/acme/usage/actions").status_code == 403


def test_admin_gets_shaped_usage(http, db, user):
    _admin_org(db, user)
    payload = {
        "total_minutes_used": 1250,
        "total_paid_minutes_used": 250,
        "included_minutes": 3000,
        "minutes_used_breakdown": {"UBUNTU": 1000, "MACOS": 250, "total": 1250.0},
    }
    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = payload
        resp = http.get("/orgs/acme/usage/actions", headers={"X-GitHub-Token": "ghp_admin"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_minutes_used"] == 1250
    assert body["included_minutes"] == 3000
    assert body["minutes_used_breakdown"]["UBUNTU"] == 1000


def test_github_403_becomes_400_with_permission_hint(http, db, user):
    _admin_org(db, user)
    err = httpx.HTTPStatusError(
        "403", request=httpx.Request("GET", "https://api.github.com"), response=httpx.Response(403)
    )
    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = err
        resp = http.get("/orgs/acme/usage/actions", headers={"X-GitHub-Token": "ghp_noscope"})

    assert resp.status_code == 400
    assert "Administration" in resp.json()["detail"]


def test_no_token_available_returns_400(http, db, user):
    _admin_org(db, user)  # admin, but no installation and no client token
    resp = http.get("/orgs/acme/usage/actions")
    assert resp.status_code == 400
