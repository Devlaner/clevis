"""Tests for the file-as-GitHub-issue route (issue #286).

Personal-scoped (`/me/repos/{owner}/{repo}/issues`), admin-gated when `owner` is a
connected Clevis org, and the first Clevis endpoint that writes to GitHub -- so it needs
a token with `Issues: write`, surfaced as a 400 when GitHub 403s.
"""

from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.auth import UserOut, require_auth
from src.core.db import AuditLog, User, get_db
from src.repositories import org_membership_repo, org_repo
from src.routers.issues import router


def _make_user(db, email: str) -> UserOut:
    user = User(email=email, name=None, password_hash=None, is_workspace_admin=False)
    db.add(user)
    db.commit()
    db.refresh(user)
    return UserOut(id=user.id, email=user.email, name=user.name, is_workspace_admin=False)


@pytest.fixture()
def user(db) -> UserOut:
    return _make_user(db, "file-issue@example.com")


@pytest.fixture()
def client(db, user) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def test_requires_auth(db):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    resp = TestClient(app).post("/me/repos/acme/api/issues", json={"title": "x"})
    assert resp.status_code == 401


def test_no_token_available_returns_400(client):
    # No membership for "acme", no personal install, no client token -> nothing to use.
    resp = client.post("/me/repos/acme/api/issues", json={"title": "MFA not enforced"})
    assert resp.status_code == 400


def test_member_of_connected_org_is_forbidden(client, db, user):
    org = org_repo.get_or_create(db, github_login="acme")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=user.id, role="member")
    db.commit()

    resp = client.post(
        "/me/repos/acme/api/issues", json={"title": "Fix it", "token": "ghp_member"}
    )
    assert resp.status_code == 403


def test_admin_creates_issue_and_writes_audit(client, db, user):
    org = org_repo.get_or_create(db, github_login="acme")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=user.id, role="admin")
    db.commit()

    with patch("src.routers.issues.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = {
            "number": 42,
            "html_url": "https://github.com/acme/api/issues/42",
        }
        resp = client.post(
            "/me/repos/acme/api/issues",
            json={"title": "Enforce org MFA", "body": "remediation steps", "token": "ghp_admin"},
        )

    assert resp.status_code == 201
    assert resp.json() == {"number": 42, "html_url": "https://github.com/acme/api/issues/42"}

    _method, path = mock_client.return_value.request.call_args[0][:2]
    assert path == "/repos/acme/api/issues"

    log = db.query(AuditLog).filter(AuditLog.action == "issues.create").one()
    assert log.target == "acme/api" and log.actor == user.email


def test_github_permission_error_maps_to_400(client, db, user):
    org = org_repo.get_or_create(db, github_login="acme")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=user.id, role="admin")
    db.commit()

    err = httpx.HTTPStatusError(
        "403", request=httpx.Request("POST", "https://api.github.com"),
        response=httpx.Response(403),
    )
    with patch("src.routers.issues.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = err
        resp = client.post(
            "/me/repos/acme/api/issues", json={"title": "x", "token": "ghp_noscope"}
        )
    assert resp.status_code == 400
    assert "403" in resp.json()["detail"]

    # The attempt is still audited even though GitHub rejected it.
    assert db.query(AuditLog).filter(AuditLog.action == "issues.create").count() == 1
