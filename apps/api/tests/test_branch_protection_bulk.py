"""Tests for POST /orgs/{org_login}/branch-protection/bulk (issue #288).

Org-admin only. dry_run returns a per-repo diff and writes nothing; apply PUTs the
merged body per repo, capturing per-repo failures. The merged body preserves every
existing rule the preset doesn't touch (via check_remediation._preserving_put_body),
and a branch whose protection restricts *who* can push is reported as an error and
left alone. Faked GitHub via ``patch("src.routers.branch_protection.GitHubClient")``.
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

# GitHub GET .../protection shape: booleans as {"enabled": bool}, nested review/check
# objects, restrictions null unless a push allowlist is configured.
def _protection(**overrides):
    base = {
        "required_status_checks": None,
        "enforce_admins": {"enabled": False},
        "required_pull_request_reviews": {
            "required_approving_review_count": 1,
            "dismiss_stale_reviews": False,
            "require_code_owner_reviews": False,
        },
        "restrictions": None,
        "allow_force_pushes": {"enabled": False},
        "allow_deletions": {"enabled": False},
        "required_linear_history": {"enabled": False},
        "block_creations": {"enabled": False},
        "required_conversation_resolution": {"enabled": False},
    }
    base.update(overrides)
    return base


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
    """protection: the GET .../protection body per repo (default: 404 -> unprotected)."""
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


def _post(client, body):
    return client.post("/orgs/acme/branch-protection/bulk", json={"token": "ghp_admin", **body})


# --- preset validation ---------------------------------------------------


def test_out_of_range_approval_count_is_a_422(db, acme):
    client = _client(db, acme["admin"].id)
    resp = _post(
        client,
        {
            "repos": ["api"],
            "dry_run": True,
            "preset": {"required_pull_request_reviews": {"required_approving_review_count": 99}},
        },
    )
    assert resp.status_code == 422


def test_string_boolean_knob_is_a_422(db, acme):
    client = _client(db, acme["admin"].id)
    resp = _post(client, {"repos": ["api"], "dry_run": True, "preset": {"enforce_admins": "true"}})
    assert resp.status_code == 422


# --- dry run --------------------------------------------------------------


def test_unprotected_repo_shows_the_conservative_default_as_a_change(db, acme):
    client = _client(db, acme["admin"].id)
    with patch("src.routers.branch_protection.GitHubClient") as mock:
        _fake_github(mock, protection_404=True)
        resp = _post(client, {"repos": ["api"], "dry_run": True})
    diff = resp.json()["diffs"][0]
    assert diff["currently_protected"] is False and diff["would_change"] is True
    assert set(diff["changes"]) == {
        "enforce_admins",
        "required_pull_request_reviews",
        "allow_force_pushes",
        "allow_deletions",
    }


def test_already_matching_repo_reports_no_change(db, acme):
    client = _client(db, acme["admin"].id)
    with patch("src.routers.branch_protection.GitHubClient") as mock:
        _fake_github(mock, protection=_protection())
        resp = _post(client, {"repos": ["api"], "dry_run": True})
    diff = resp.json()["diffs"][0]
    assert diff["currently_protected"] is True and diff["would_change"] is False
    assert diff["changes"] == {}


def test_preset_touching_only_approvals_preserves_other_review_rules(db, acme):
    # The repo requires code-owner review + stale-dismissal. The admin bumps the approval
    # count to 2. The diff must show *only* the count change, and the apply must keep the
    # other two rules — not reset them the way a bare PUT would.
    client = _client(db, acme["admin"].id)
    protection = _protection(
        required_pull_request_reviews={
            "required_approving_review_count": 1,
            "dismiss_stale_reviews": True,
            "require_code_owner_reviews": True,
        },
        required_status_checks={"strict": True, "contexts": ["ci"]},
    )
    with patch("src.routers.branch_protection.GitHubClient") as mock:
        inst = _fake_github(mock, protection=protection)
        resp = _post(
            client,
            {
                "repos": ["api"],
                "dry_run": False,
                "preset": {"required_pull_request_reviews": {"required_approving_review_count": 2}},
            },
        )
    assert resp.json()["results"][0]["applied"] is True
    put = next(c for c in inst.request.call_args_list if c[0][0] == "PUT").kwargs["json"]
    assert put["required_pull_request_reviews"] == {
        "required_approving_review_count": 2,
        "dismiss_stale_reviews": True,
        "require_code_owner_reviews": True,
    }
    assert put["required_status_checks"] == {"strict": True, "contexts": ["ci"]}


@pytest.mark.parametrize("dry_run", [True, False])
def test_a_branch_with_push_restrictions_is_reported_and_left_alone(db, acme, dry_run):
    client = _client(db, acme["admin"].id)
    protection = _protection(restrictions={"users": [{"login": "release-bot"}], "teams": [], "apps": []})
    with patch("src.routers.branch_protection.GitHubClient") as mock:
        inst = _fake_github(mock, protection=protection)
        resp = _post(client, {"repos": ["api"], "dry_run": dry_run})
    row = (resp.json().get("diffs") or resp.json().get("results"))[0]
    assert "push" in row["error"].lower()
    assert not [c for c in inst.request.call_args_list if c[0][0] == "PUT"]


@pytest.mark.parametrize("dry_run", [True, False])
def test_an_invalid_repo_name_is_rejected_per_repo(db, acme, dry_run):
    client = _client(db, acme["admin"].id)
    with patch("src.routers.branch_protection.GitHubClient") as mock:
        inst = _fake_github(mock, protection_404=True)
        resp = _post(client, {"repos": ["../../other-org/secret", "api"], "dry_run": dry_run})
    rows = {r["repo"]: r for r in (resp.json().get("diffs") or resp.json().get("results"))}
    assert rows["../../other-org/secret"]["error"] == "invalid repository name"
    assert not any("other-org" in str(c) for c in inst.request.call_args_list)


def test_enforce_admins_knob_change_shows_in_the_diff(db, acme):
    client = _client(db, acme["admin"].id)
    with patch("src.routers.branch_protection.GitHubClient") as mock:
        _fake_github(mock, protection=_protection())
        resp = _post(
            client, {"repos": ["api"], "dry_run": True, "preset": {"enforce_admins": True}}
        )
    assert resp.json()["diffs"][0]["changes"]["enforce_admins"] == {"from": False, "to": True}


def test_dry_run_diff_keeps_configured_status_checks_and_null_reviews(db, acme):
    client = _client(db, acme["admin"].id)
    protection = _protection(
        required_status_checks={"strict": True, "contexts": ["build"]},
        required_pull_request_reviews=None,
    )
    with patch("src.routers.branch_protection.GitHubClient") as mock:
        _fake_github(mock, protection=protection)
        resp = _post(
            client,
            {
                "repos": ["api"],
                "dry_run": True,
                "preset": {"required_pull_request_reviews": {"required_approving_review_count": 2}},
            },
        )
    changes = resp.json()["diffs"][0]["changes"]
    # status checks preserved -> not a change; reviews go from none -> the new count
    assert "required_status_checks" not in changes
    assert changes["required_pull_request_reviews"]["from"] is None
    assert changes["required_pull_request_reviews"]["to"]["required_approving_review_count"] == 2


# --- apply ---------------------------------------------------------------


def test_apply_puts_the_merged_body_per_repo(db, acme):
    client = _client(db, acme["admin"].id)
    with patch("src.routers.branch_protection.GitHubClient") as mock:
        inst = _fake_github(mock, protection_404=True)
        resp = _post(client, {"repos": ["api", "web"], "dry_run": False})
    assert [r["applied"] for r in resp.json()["results"]] == [True, True]
    puts = [c for c in inst.request.call_args_list if c[0][0] == "PUT"]
    assert len(puts) == 2 and puts[0].kwargs["json"]["allow_force_pushes"] is False


def test_apply_reports_one_repo_403_while_the_others_succeed(db, acme):
    client = _client(db, acme["admin"].id)

    def request(method, path, params=None, json=None):
        if method == "GET" and path.count("/") == 3:
            return {"default_branch": "main"}
        if method == "PUT" and "/bad/" in path:
            raise httpx.HTTPStatusError(
                "403", request=httpx.Request("PUT", "https://api.github.com"), response=httpx.Response(403)
            )
        if method == "GET" and path.endswith("/protection"):
            raise httpx.HTTPStatusError(
                "404", request=httpx.Request("GET", "https://api.github.com"), response=httpx.Response(404)
            )
        return {}

    with patch("src.routers.branch_protection.GitHubClient") as mock:
        mock.return_value.request.side_effect = request
        resp = _post(client, {"repos": ["good", "bad"], "dry_run": False})
    results = {r["repo"]: r for r in resp.json()["results"]}
    assert results["good"]["applied"] is True
    assert results["bad"]["applied"] is False and "403" in results["bad"]["error"]


def test_every_repo_403_becomes_a_400_with_the_permission_hint(db, acme):
    client = _client(db, acme["admin"].id)
    with patch("src.routers.branch_protection.GitHubClient") as mock:
        _fake_github(mock, protection_404=True, put_status=403)
        resp = _post(client, {"repos": ["api", "web"], "dry_run": False})
    assert resp.status_code == 400 and "Administration" in resp.json()["detail"]


def test_dry_run_all_repos_403_becomes_a_400(db, acme):
    client = _client(db, acme["admin"].id)

    def request(method, path, params=None, json=None):
        raise httpx.HTTPStatusError(
            "403", request=httpx.Request("GET", "https://api.github.com"), response=httpx.Response(403)
        )

    with patch("src.routers.branch_protection.GitHubClient") as mock:
        mock.return_value.request.side_effect = request
        resp = _post(client, {"repos": ["api", "web"], "dry_run": True})
    assert resp.status_code == 400 and "Administration" in resp.json()["detail"]


def test_member_is_forbidden(db, acme):
    client = _client(db, acme["member"].id, email="member@e.com")
    resp = _post(client, {"repos": ["api"], "dry_run": True})
    assert resp.status_code == 403


def test_no_token_available_returns_400(db, acme):
    client = _client(db, acme["admin"].id)
    resp = client.post("/orgs/acme/branch-protection/bulk", json={"repos": ["api"], "dry_run": True})
    assert resp.status_code == 400


def test_save_preset_persists_per_repo_under_the_org_tenant(db, acme):
    client = _client(db, acme["admin"].id)
    with patch("src.routers.branch_protection.GitHubClient") as mock:
        _fake_github(mock, protection_404=True)
        resp = _post(
            client,
            {
                "repos": ["api"],
                "dry_run": False,
                "save_preset": True,
                "preset": {"required_pull_request_reviews": {"required_approving_review_count": 2}},
            },
        )
    assert resp.status_code == 200
    tenant_id = acme["org"].tenant_id
    row = automation_settings_repo.get(db, tenant_id, "acme/api", "branch_protection")
    assert row is not None and row.enabled is True
    assert row.extra["required_approving_review_count"] == 2
    assert [r.repo for r in automation_settings_repo.list_for_feature(db, tenant_id, "branch_protection")] == [
        "acme/api"
    ]


# --- error surfacing ----------------------------------------------------


def test_dry_run_surfaces_a_network_error_per_repo(db, acme):
    client = _client(db, acme["admin"].id)

    def request(method, path, params=None, json=None):
        if method == "GET" and path.count("/") == 3:
            return {"default_branch": "main"}
        raise httpx.ConnectError("boom", request=httpx.Request("GET", "https://api.github.com"))

    with patch("src.routers.branch_protection.GitHubClient") as mock:
        mock.return_value.request.side_effect = request
        resp = _post(client, {"repos": ["api"], "dry_run": True})
    assert resp.json()["diffs"][0]["error"] == "GitHub API unreachable"


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
        resp = _post(client, {"repos": ["api"], "dry_run": True})
    assert resp.json()["diffs"][0]["error"] == "GitHub API error: 500"


def test_apply_surfaces_a_network_error_per_repo(db, acme):
    client = _client(db, acme["admin"].id)

    def request(method, path, params=None, json=None):
        if method == "GET" and path.count("/") == 3:
            return {"default_branch": "main"}
        if path.endswith("/protection") and method == "GET":
            raise httpx.HTTPStatusError(
                "404", request=httpx.Request("GET", "https://api.github.com"), response=httpx.Response(404)
            )
        raise httpx.ConnectError("boom", request=httpx.Request("PUT", "https://api.github.com"))

    with patch("src.routers.branch_protection.GitHubClient") as mock:
        mock.return_value.request.side_effect = request
        resp = _post(client, {"repos": ["api"], "dry_run": False})
    assert resp.json()["results"][0]["error"] == "GitHub API unreachable"


def test_apply_captures_a_non_403_github_error_per_repo(db, acme):
    client = _client(db, acme["admin"].id)
    with patch("src.routers.branch_protection.GitHubClient") as mock:
        _fake_github(mock, protection_404=True, put_status=500)
        resp = _post(client, {"repos": ["api"], "dry_run": False})
    assert resp.status_code == 200
    assert resp.json()["results"][0]["error"] == "GitHub API error: 500"


def test_dry_run_writes_an_audit_row(db, acme):
    client = _client(db, acme["admin"].id)
    with patch("src.routers.branch_protection.GitHubClient") as mock:
        _fake_github(mock, protection_404=True)
        _post(client, {"repos": ["api"], "dry_run": True})
    assert db.query(AuditLog).filter(AuditLog.action == "branch_protection.bulk_dryrun").count() == 1
