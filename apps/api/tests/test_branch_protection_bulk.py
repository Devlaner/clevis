"""Tests for POST /orgs/{org_login}/branch-protection/bulk (issue #288).

Org-admin only. dry_run returns a per-repo diff and writes nothing; apply PUTs the
preset per repo, capturing per-repo failures. A whole-batch 403 becomes a 400 with
the "grant Administration: write" hint. Faked GitHub via
``patch("src.routers.branch_protection.GitHubClient")``.
"""

from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.auth import UserOut, require_auth
from src.core.db import AuditLog, User, get_db
from src.repositories import automation_settings_repo, org_membership_repo, org_repo
from src.routers.branch_protection import router

_MATCHING_PROTECTION = {
    "required_status_checks": None,
    "enforce_admins": {"enabled": False},
    "required_pull_request_reviews": {"required_approving_review_count": 1},
    "restrictions": None,
    "allow_force_pushes": {"enabled": False},
    "allow_deletions": {"enabled": False},
}


@pytest.fixture()
def acme(db):
    admin = User(email="admin@e.com", name=None, password_hash=None, is_workspace_admin=False)
    member = User(email="member@e.com", name=None, password_hash=None, is_workspace_admin=False)
    db.add_all([admin, member])
    db.commit()
    db.refresh(admin)
    db.refresh(member)
    org = org_repo.get_or_create(db, github_login="acme")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=admin.id, role="admin")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=member.id, role="member")
    return {"org": org, "admin": admin, "member": member}


def _client(db, user_id, email="admin@e.com"):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: UserOut(
        id=user_id, email=email, name=None, is_workspace_admin=False
    )
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def _fake_github(mock, *, protection=None, protection_404=False, put_status=None):
    """protection: the GET .../protection body per repo (default: 404 -> unprotected).
    put_status: if set, PUT .../protection raises HTTPStatusError with that code."""
    inst = mock.return_value

    def request(method, path, params=None, json=None):
        if method == "GET" and path.count("/") == 3:  # /repos/{o}/{r}
            return {"default_branch": "main"}
        if path.endswith("/protection"):
            if method == "PUT":
                if put_status is not None:
                    raise httpx.HTTPStatusError(
                        str(put_status),
                        request=httpx.Request("PUT", "https://api.github.com"),
                        response=httpx.Response(put_status),
                    )
                return {}
            if protection_404 or protection is None:
                raise httpx.HTTPStatusError(
                    "404",
                    request=httpx.Request("GET", "https://api.github.com"),
                    response=httpx.Response(404),
                )
            return protection
        return {}

    inst.request.side_effect = request
    return inst


def test_dry_run_unprotected_repo_lists_every_preset_key_as_a_change(db, acme):
    client = _client(db, acme["admin"].id)
    with patch("src.routers.branch_protection.GitHubClient") as mock:
        _fake_github(mock, protection_404=True)
        resp = client.post(
            "/orgs/acme/branch-protection/bulk",
            json={"repos": ["api"], "dry_run": True, "token": "ghp_admin"},
        )
    assert resp.status_code == 200
    diff = resp.json()["diffs"][0]
    assert diff["currently_protected"] is False
    assert diff["would_change"] is True
    # required_status_checks / restrictions already match the preset default (None); the
    # rest of the conservative default is a change on a repo with no protection.
    assert set(diff["changes"]) == {
        "required_pull_request_reviews",
        "enforce_admins",
        "allow_force_pushes",
        "allow_deletions",
    }
    assert diff["changes"]["required_pull_request_reviews"]["to"] == {
        "required_approving_review_count": 1
    }


def test_dry_run_already_matching_repo_reports_no_change(db, acme):
    client = _client(db, acme["admin"].id)
    with patch("src.routers.branch_protection.GitHubClient") as mock:
        _fake_github(mock, protection=_MATCHING_PROTECTION)
        resp = client.post(
            "/orgs/acme/branch-protection/bulk",
            json={"repos": ["api"], "dry_run": True, "token": "ghp_admin"},
        )
    assert resp.status_code == 200
    diff = resp.json()["diffs"][0]
    assert diff["currently_protected"] is True
    assert diff["would_change"] is False
    assert diff["changes"] == {}


def test_apply_puts_the_preset_per_repo(db, acme):
    client = _client(db, acme["admin"].id)
    with patch("src.routers.branch_protection.GitHubClient") as mock:
        inst = _fake_github(mock, protection_404=True)
        resp = client.post(
            "/orgs/acme/branch-protection/bulk",
            json={"repos": ["api", "web"], "dry_run": False, "token": "ghp_admin"},
        )
    assert resp.status_code == 200
    assert [r["applied"] for r in resp.json()["results"]] == [True, True]
    puts = [c for c in inst.request.call_args_list if c[0][0] == "PUT"]
    assert len(puts) == 2
    assert puts[0].kwargs["json"]["allow_force_pushes"] is False


def test_apply_reports_one_repo_403_while_the_others_succeed(db, acme):
    client = _client(db, acme["admin"].id)

    def request(method, path, params=None, json=None):
        if method == "GET" and path.count("/") == 3:
            return {"default_branch": "main"}
        if method == "PUT" and "/bad/" in path:
            raise httpx.HTTPStatusError(
                "403",
                request=httpx.Request("PUT", "https://api.github.com"),
                response=httpx.Response(403),
            )
        return {}

    with patch("src.routers.branch_protection.GitHubClient") as mock:
        mock.return_value.request.side_effect = request
        resp = client.post(
            "/orgs/acme/branch-protection/bulk",
            json={"repos": ["good", "bad"], "dry_run": False, "token": "ghp_admin"},
        )
    assert resp.status_code == 200
    results = {r["repo"]: r for r in resp.json()["results"]}
    assert results["good"]["applied"] is True
    assert results["bad"]["applied"] is False and "403" in results["bad"]["error"]


def test_every_repo_403_becomes_a_400_with_the_permission_hint(db, acme):
    client = _client(db, acme["admin"].id)
    with patch("src.routers.branch_protection.GitHubClient") as mock:
        _fake_github(mock, protection_404=True, put_status=403)
        resp = client.post(
            "/orgs/acme/branch-protection/bulk",
            json={"repos": ["api", "web"], "dry_run": False, "token": "ghp_x"},
        )
    assert resp.status_code == 400
    assert "Administration" in resp.json()["detail"]


def test_dry_run_all_repos_403_becomes_a_400(db, acme):
    client = _client(db, acme["admin"].id)

    def request(method, path, params=None, json=None):
        raise httpx.HTTPStatusError(
            "403", request=httpx.Request("GET", "https://api.github.com"), response=httpx.Response(403)
        )

    with patch("src.routers.branch_protection.GitHubClient") as mock:
        mock.return_value.request.side_effect = request
        resp = client.post(
            "/orgs/acme/branch-protection/bulk",
            json={"repos": ["api", "web"], "dry_run": True, "token": "ghp_x"},
        )
    assert resp.status_code == 400
    assert "Administration" in resp.json()["detail"]


def test_member_is_forbidden(db, acme):
    client = _client(db, acme["member"].id, email="member@e.com")
    resp = client.post(
        "/orgs/acme/branch-protection/bulk",
        json={"repos": ["api"], "dry_run": True, "token": "ghp_member"},
    )
    assert resp.status_code == 403


def test_no_token_available_returns_400(db, acme):
    client = _client(db, acme["admin"].id)
    resp = client.post("/orgs/acme/branch-protection/bulk", json={"repos": ["api"], "dry_run": True})
    assert resp.status_code == 400


def test_save_preset_persists_per_repo_under_the_org_tenant(db, acme):
    client = _client(db, acme["admin"].id)
    with patch("src.routers.branch_protection.GitHubClient") as mock:
        _fake_github(mock, protection_404=True)
        resp = client.post(
            "/orgs/acme/branch-protection/bulk",
            json={
                "repos": ["api"],
                "dry_run": False,
                "save_preset": True,
                "preset": {"required_pull_request_reviews": {"required_approving_review_count": 2}},
                "token": "ghp_admin",
            },
        )
    assert resp.status_code == 200
    tenant_id = acme["org"].tenant_id
    row = automation_settings_repo.get(db, tenant_id, "acme/api", "branch_protection")
    assert row is not None and row.enabled is True
    assert row.extra["required_pull_request_reviews"]["required_approving_review_count"] == 2
    listed = automation_settings_repo.list_for_feature(db, tenant_id, "branch_protection")
    assert [r.repo for r in listed] == ["acme/api"]


def test_dry_run_normalizes_existing_status_checks_and_restrictions_for_the_diff(db, acme):
    client = _client(db, acme["admin"].id)
    protection = {
        **_MATCHING_PROTECTION,
        "required_status_checks": {"strict": True, "contexts": ["ci"], "checks": []},
        "enforce_admins": {"enabled": True},
        "required_pull_request_reviews": None,  # -> current value None, preset wants a dict
    }
    with patch("src.routers.branch_protection.GitHubClient") as mock:
        _fake_github(mock, protection=protection)
        resp = client.post(
            "/orgs/acme/branch-protection/bulk",
            json={"repos": ["api"], "dry_run": True, "token": "ghp_admin"},
        )
    changes = resp.json()["diffs"][0]["changes"]
    # status checks currently configured, preset wants none -> a change; enforce_admins flips
    assert changes["required_status_checks"]["from"] == {"strict": True, "contexts": ["ci"]}
    assert changes["enforce_admins"] == {"from": True, "to": False}
    assert changes["required_pull_request_reviews"]["from"] is None


def test_dry_run_surfaces_a_network_error_per_repo(db, acme):
    client = _client(db, acme["admin"].id)

    def request(method, path, params=None, json=None):
        if method == "GET" and path.count("/") == 3:
            return {"default_branch": "main"}
        raise httpx.ConnectError("boom", request=httpx.Request("GET", "https://api.github.com"))

    with patch("src.routers.branch_protection.GitHubClient") as mock:
        mock.return_value.request.side_effect = request
        resp = client.post(
            "/orgs/acme/branch-protection/bulk",
            json={"repos": ["api"], "dry_run": True, "token": "ghp_admin"},
        )
    assert resp.json()["diffs"][0]["error"] == "GitHub API unreachable"


def test_dry_run_captures_a_per_repo_github_error(db, acme):
    client = _client(db, acme["admin"].id)

    def request(method, path, params=None, json=None):
        raise httpx.HTTPStatusError(
            "500", request=httpx.Request("GET", "https://api.github.com"), response=httpx.Response(500)
        )

    with patch("src.routers.branch_protection.GitHubClient") as mock:
        mock.return_value.request.side_effect = request
        resp = client.post(
            "/orgs/acme/branch-protection/bulk",
            json={"repos": ["api"], "dry_run": True, "token": "ghp_admin"},
        )
    assert resp.status_code == 200
    assert resp.json()["diffs"][0]["error"] == "GitHub API error: 500"


def test_dry_run_captures_a_non_404_protection_error(db, acme):
    client = _client(db, acme["admin"].id)

    def request(method, path, params=None, json=None):
        if method == "GET" and path.count("/") == 3:
            return {"default_branch": "main"}
        raise httpx.HTTPStatusError(
            "500", request=httpx.Request("GET", "https://api.github.com"), response=httpx.Response(500)
        )

    with patch("src.routers.branch_protection.GitHubClient") as mock:
        mock.return_value.request.side_effect = request
        resp = client.post(
            "/orgs/acme/branch-protection/bulk",
            json={"repos": ["api"], "dry_run": True, "token": "ghp_admin"},
        )
    assert resp.json()["diffs"][0]["error"] == "GitHub API error: 500"


def test_apply_surfaces_a_network_error_per_repo(db, acme):
    client = _client(db, acme["admin"].id)

    def request(method, path, params=None, json=None):
        if method == "GET" and path.count("/") == 3:
            return {"default_branch": "main"}
        raise httpx.ConnectError("boom", request=httpx.Request("PUT", "https://api.github.com"))

    with patch("src.routers.branch_protection.GitHubClient") as mock:
        mock.return_value.request.side_effect = request
        resp = client.post(
            "/orgs/acme/branch-protection/bulk",
            json={"repos": ["api"], "dry_run": False, "token": "ghp_admin"},
        )
    assert resp.status_code == 200
    assert resp.json()["results"][0]["error"] == "GitHub API unreachable"


def test_dry_run_writes_an_audit_row(db, acme):
    client = _client(db, acme["admin"].id)
    with patch("src.routers.branch_protection.GitHubClient") as mock:
        _fake_github(mock, protection_404=True)
        client.post(
            "/orgs/acme/branch-protection/bulk",
            json={"repos": ["api"], "dry_run": True, "token": "ghp_admin"},
        )
    assert (
        db.query(AuditLog).filter(AuditLog.action == "branch_protection.bulk_dryrun").count() == 1
    )
