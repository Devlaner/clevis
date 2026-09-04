"""Tests for the "Fix this" auto-remediation route (issue #287)."""
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.auth import UserOut, require_auth
from src.core.db import AuditLog, User, get_db
from src.repositories import org_membership_repo, org_repo
from src.services import check_remediation
from src.routers.remediation import router

BP = "repository_default_branch_protection_enabled"
SS = "repository_secret_scanning_enabled"


def _make_user(db, email: str) -> UserOut:
    user = User(email=email, name=None, password_hash=None, is_workspace_admin=False)
    db.add(user)
    db.commit()
    db.refresh(user)
    return UserOut(id=user.id, email=user.email, name=user.name, is_workspace_admin=False)


@pytest.fixture()
def user(db) -> UserOut:
    return _make_user(db, "remediate@example.com")


@pytest.fixture()
def client(db, user) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def _admin_org(db, user, login="acme"):
    org = org_repo.get_or_create(db, github_login=login)
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=user.id, role="admin")
    db.commit()
    return org


def _url(check_id, owner="acme", repo="api"):
    return f"/me/repos/{owner}/{repo}/security/checks/{check_id}/remediate"


def test_requires_auth(db):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    assert TestClient(app).post(_url(SS), json={}).status_code == 401


def test_unknown_check_id_is_404(client):
    assert client.post(_url("not_a_real_check"), json={"token": "ghp_x"}).status_code == 404


def test_no_token_available_returns_400(client):
    assert client.post(_url(SS), json={}).status_code == 400


def test_member_of_connected_org_is_forbidden(client, db, user):
    org = org_repo.get_or_create(db, github_login="acme")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=user.id, role="member")
    db.commit()
    assert client.post(_url(SS), json={"token": "ghp_member"}).status_code == 403


def test_admin_enables_secret_scanning_and_audits(client, db, user):
    _admin_org(db, user)
    with patch("src.routers.remediation.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = {}
        resp = client.post(_url(SS), json={"token": "ghp_admin"})

    assert resp.status_code == 200
    assert resp.json()["check_id"] == SS
    method, path = mock_client.return_value.request.call_args[0][:2]
    kwargs = mock_client.return_value.request.call_args.kwargs
    assert (method, path) == ("PATCH", "/repos/acme/api")
    assert kwargs["json"]["security_and_analysis"]["secret_scanning"]["status"] == "enabled"

    log = db.query(AuditLog).filter(AuditLog.action == "security.remediate").one()
    assert log.target == "acme/api"


def test_bring_your_own_pat_against_an_unconnected_owner_audits_under_the_personal_tenant(client, db, user):
    # "someone" isn't a connected Clevis org -> BYO-PAT path; the audit row must be
    # scoped to the caller's personal tenant, never NULL (audit_logs RLS, issue #330).
    from src.repositories import tenant_repo

    with patch("src.routers.remediation.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = {}
        resp = client.post(_url(SS, owner="someone"), json={"token": "ghp_byo"})
    assert resp.status_code == 200
    log = db.query(AuditLog).filter(AuditLog.action == "security.remediate").one()
    assert log.tenant_id == tenant_repo.ensure_personal_tenant(db, user.id).id


def _not_found() -> httpx.HTTPStatusError:
    return httpx.HTTPStatusError(
        "404", request=httpx.Request("GET", "https://api.github.com"), response=httpx.Response(404)
    )


def test_admin_applies_default_branch_protection_when_the_branch_has_none(client, db, user):
    _admin_org(db, user)
    with patch("src.routers.remediation.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = [
            {"default_branch": "trunk"},  # GET /repos/acme/api
            _not_found(),                  # GET protection -> branch is unprotected
            {},                            # PUT protection
        ]
        resp = client.post(_url(BP), json={"token": "ghp_admin"})

    assert resp.status_code == 200
    calls = mock_client.return_value.request.call_args_list
    assert calls[0][0][:2] == ("GET", "/repos/acme/api")
    assert calls[1][0][:2] == ("GET", "/repos/acme/api/branches/trunk/protection")
    assert calls[2][0][:2] == ("PUT", "/repos/acme/api/branches/trunk/protection")
    assert calls[2].kwargs["json"]["allow_force_pushes"] is False


def test_branch_with_a_slash_is_encoded_as_one_path_segment(client, db, user):
    _admin_org(db, user)
    with patch("src.routers.remediation.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = [
            {"default_branch": "release/1.x"},
            _not_found(),
            {},
        ]
        resp = client.post(_url(BP), json={"token": "ghp_admin"})

    assert resp.status_code == 200
    calls = mock_client.return_value.request.call_args_list
    assert calls[2][0][1] == "/repos/acme/api/branches/release%2F1.x/protection"


def test_existing_branch_protection_is_preserved_and_only_force_pushes_flipped(client, db, user):
    _admin_org(db, user)
    existing = {
        "required_status_checks": {"strict": True, "contexts": ["ci/build"]},
        "enforce_admins": {"enabled": True},
        "required_pull_request_reviews": {"required_approving_review_count": 2},
        "required_linear_history": {"enabled": True},
        "allow_force_pushes": {"enabled": True},
        "allow_deletions": {"enabled": False},
    }
    with patch("src.routers.remediation.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = [
            {"default_branch": "main"},
            existing,
            {},
        ]
        resp = client.post(_url("repository_default_branch_no_force_push"), json={"token": "ghp_admin"})

    assert resp.status_code == 200
    put_body = mock_client.return_value.request.call_args_list[2].kwargs["json"]
    assert put_body["allow_force_pushes"] is False
    assert put_body["required_status_checks"] == {"strict": True, "contexts": ["ci/build"]}
    assert put_body["required_pull_request_reviews"]["required_approving_review_count"] == 2
    assert put_body["enforce_admins"] is True
    assert put_body["required_linear_history"] is True


def test_branch_protection_with_push_restrictions_is_rejected_with_409(client, db, user):
    _admin_org(db, user)
    existing = {
        "allow_force_pushes": {"enabled": True},
        "restrictions": {"users": [{"login": "octocat"}], "teams": [], "apps": []},
    }
    with patch("src.routers.remediation.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = [
            {"default_branch": "main"},
            existing,
        ]
        resp = client.post(_url("repository_default_branch_no_force_push"), json={"token": "ghp_admin"})

    assert resp.status_code == 409
    assert "restrict" in resp.json()["detail"].lower()
    # The write was refused before any PUT went out.
    assert [c[0][0] for c in mock_client.return_value.request.call_args_list] == ["GET", "GET"]


def test_github_403_maps_to_400_with_permission_hint_and_still_audits(client, db, user):
    _admin_org(db, user)
    err = httpx.HTTPStatusError(
        "403", request=httpx.Request("PATCH", "https://api.github.com"), response=httpx.Response(403)
    )
    with patch("src.routers.remediation.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = err
        resp = client.post(_url(SS), json={"token": "ghp_noscope"})

    assert resp.status_code == 400
    assert "permission" in resp.json()["detail"].lower()
    assert db.query(AuditLog).filter(AuditLog.action == "security.remediate").count() == 1


def test_github_unreachable_maps_to_503(client, db, user):
    _admin_org(db, user)
    with patch("src.routers.remediation.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = httpx.RequestError("boom", request=MagicMock())
        resp = client.post(_url(SS), json={"token": "ghp_admin"})
    assert resp.status_code == 503


def test_remediate_helper_rejects_an_unsupported_check():
    with pytest.raises(check_remediation.RemediationNotSupported):
        check_remediation.remediate(MagicMock(), "organization_members_mfa_required", "acme", "api")
