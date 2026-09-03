"""Tests for workflow-policy lint + auto-fix PR (issue #291).

`lint()` is a pure function tested directly; the routes are tested with a faked
GitHubClient (`patch("src.routers.workflow_lint.GitHubClient")`) that serves the
`contents/.github/workflows` listing, per-file blobs, and the branch/commit/PR calls.
"""

import base64
from unittest.mock import patch

import httpx
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.auth import UserOut, require_auth
from src.core.db import AuditLog, User, get_db
from src.repositories import org_membership_repo, org_repo, tenant_repo
from src.routers.workflow_lint import router
from src.services import workflow_lint
from src.services.workflow_lint import WorkflowFile, lint

_BAD_PRT = """
name: bad
on: pull_request_target
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.head.sha }}
      - run: npm test
"""

_BAD_PRT_WITH_SECRETS = _BAD_PRT.replace("npm test", "deploy --token ${{ secrets.DEPLOY }}")

_INJECTION = """
name: inj
on: issue_comment
jobs:
  echo:
    runs-on: ubuntu-latest
    steps:
      - run: echo "${{ github.event.comment.body }}"
"""

_CLEAN = """
name: ok
on: pull_request
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: npm test
"""


# ── lint() unit tests ─────────────────────────────────────────────────────


def test_flags_pull_request_target_checking_out_pr_head_and_offers_a_fix():
    result = lint(WorkflowFile("w.yml", _BAD_PRT, "sha1"))
    assert [f.rule for f in result.findings] == ["pull_request_target_checks_out_pr_code"]
    assert result.fixable
    assert "on: pull_request\n" in result.fixes["w.yml"]
    assert "pull_request_target" not in result.fixes["w.yml"]


def test_does_not_auto_fix_when_the_workflow_uses_secrets():
    result = lint(WorkflowFile("w.yml", _BAD_PRT_WITH_SECRETS, "sha1"))
    assert result.findings and not result.fixable


def test_does_not_auto_fix_when_secrets_use_the_bracket_index_form():
    text = _BAD_PRT.replace("npm test", "deploy ${{ secrets['DEPLOY_KEY'] }}")
    result = lint(WorkflowFile("w.yml", text, "sha1"))
    assert result.findings and not result.fixable


def test_does_not_auto_fix_when_pull_request_is_already_a_trigger():
    text = _BAD_PRT.replace("on: pull_request_target", "on: [pull_request, pull_request_target]")
    result = lint(WorkflowFile("w.yml", text, "sha1"))
    assert result.findings and not result.fixable  # a blind replace would duplicate the trigger


def test_does_not_auto_fix_when_pull_request_target_appears_more_than_once():
    text = _BAD_PRT + "\n# note: pull_request_target is dangerous\n"
    result = lint(WorkflowFile("w.yml", text, "sha1"))
    assert result.findings and not result.fixable  # the 2nd occurrence is in a comment


def test_flags_untrusted_input_interpolated_into_run():
    result = lint(WorkflowFile("w.yml", _INJECTION, "sha1"))
    assert [f.rule for f in result.findings] == ["untrusted_input_in_run"]
    assert not result.fixable


def test_clean_workflow_has_no_findings():
    assert lint(WorkflowFile("w.yml", _CLEAN, "sha1")).findings == []


def test_unparseable_yaml_yields_a_warning_not_an_exception():
    result = lint(WorkflowFile("w.yml", "on: [\n  bad", "sha1"))
    assert [f.rule for f in result.findings] == ["unparseable"]


def test_non_mapping_yaml_is_ignored():
    assert lint(WorkflowFile("w.yml", "- just\n- a\n- list", "sha1")).findings == []


def test_trigger_as_a_list_and_merge_commit_sha_checkout_are_handled():
    text = """
name: x
on: [pull_request_target, push]
jobs:
  a:
    steps:
      - not-a-mapping
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.merge_commit_sha }}
"""
    result = lint(WorkflowFile("w.yml", text, "sha1"))
    assert any(f.rule == "pull_request_target_checks_out_pr_code" for f in result.findings)


def test_trigger_as_a_bare_string_without_checkout_is_clean():
    assert lint(WorkflowFile("w.yml", "on: pull_request_target\njobs:\n  a:\n    steps: []", "sha1")).findings == []


# ── route tests ──────────────────────────────────────────────────────────


def _make_user(db, email):
    user = User(email=email, name=None, password_hash=None, is_workspace_admin=False)
    db.add(user)
    db.commit()
    db.refresh(user)
    return UserOut(id=user.id, email=user.email, name=user.name, is_workspace_admin=False)


@pytest.fixture()
def user(db):
    return _make_user(db, "wf@example.com")


@pytest.fixture()
def client(db, user):
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


def _blob(text):
    return {"content": base64.b64encode(text.encode()).decode(), "sha": "blobsha"}


def _github(mock, *, workflows: dict[str, str], existing_pr_url: str | None = None):
    """workflows: {filename: yaml text}. existing_pr_url: if set, GET .../pulls returns
    an open fix PR (the idempotent path)."""
    inst = mock.return_value
    calls = {"refs": [], "puts": [], "pulls": []}

    def request(method, path, params=None, json=None):
        if path.endswith("/contents/.github/workflows"):
            return [
                {"name": n, "path": f".github/workflows/{n}", "url": f"blob:{n}"}
                for n in workflows
            ]
        if path.startswith("blob:"):
            return _blob(workflows[path[len("blob:") :]])
        if path == "/repos/acme/api" or path.endswith("/repos/acme/api"):
            return {"default_branch": "main"}
        if "/git/ref/heads/" in path:
            return {"object": {"sha": "basesha"}}
        if method == "GET" and path.endswith("/pulls"):
            return [{"html_url": existing_pr_url}] if existing_pr_url else []
        if method == "POST" and path.endswith("/git/refs"):
            calls["refs"].append(json)
            return {}
        if "/contents/.github/workflows/" in path:
            if method == "PUT":
                calls["puts"].append(json)
                return {}
            return {"sha": "existingsha"}
        if method == "POST" and path.endswith("/pulls"):
            calls["pulls"].append(json)
            return {"html_url": "https://github.com/acme/api/pull/99"}
        return {}

    inst.request.side_effect = request
    return inst, calls


def test_org_scan_returns_findings(client, db, user):
    _admin_org(db, user)
    with patch("src.routers.workflow_lint.GitHubClient") as mock:
        _github(mock, workflows={"bad.yml": _BAD_PRT})
        resp = client.post(
            "/orgs/acme/repos/acme/api/workflow-lint", json={"token": "ghp_admin"}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["fixable"] is True
    assert body["findings"][0]["rule"] == "pull_request_target_checks_out_pr_code"
    assert body["pr_url"] is None
    assert db.query(AuditLog).filter(AuditLog.action == "workflow_lint.scan").count() == 1


def test_open_pr_creates_branch_commits_and_pr(client, db, user):
    _admin_org(db, user)
    with patch("src.routers.workflow_lint.GitHubClient") as mock:
        _inst, calls = _github(mock, workflows={"bad.yml": _BAD_PRT})
        resp = client.post(
            "/orgs/acme/repos/acme/api/workflow-lint",
            json={"token": "ghp_admin", "open_pr": True},
        )
    assert resp.status_code == 200
    assert resp.json()["pr_url"] == "https://github.com/acme/api/pull/99"
    assert calls["refs"] and calls["refs"][0]["ref"] == "refs/heads/clevis/workflow-lint-fix"
    assert calls["puts"] and "pull_request" in base64.b64decode(calls["puts"][0]["content"]).decode()
    assert calls["pulls"]
    assert db.query(AuditLog).filter(AuditLog.action == "workflow_lint.autofix_pr").count() == 1


def test_open_pr_with_no_fixable_finding_opens_no_pr(client, db, user):
    _admin_org(db, user)
    with patch("src.routers.workflow_lint.GitHubClient") as mock:
        _inst, calls = _github(mock, workflows={"inj.yml": _INJECTION})
        resp = client.post(
            "/orgs/acme/repos/acme/api/workflow-lint",
            json={"token": "ghp_admin", "open_pr": True},
        )
    assert resp.status_code == 200
    assert resp.json()["pr_url"] is None
    assert not calls["refs"] and not calls["pulls"]


def test_github_403_becomes_400_with_the_scope_hint(client, db, user):
    _admin_org(db, user)
    err = httpx.HTTPStatusError(
        "403", request=httpx.Request("GET", "https://api.github.com"), response=httpx.Response(403)
    )
    with patch("src.routers.workflow_lint.GitHubClient") as mock:
        mock.return_value.request.side_effect = err
        resp = client.post(
            "/orgs/acme/repos/acme/api/workflow-lint", json={"token": "ghp_noscope"}
        )
    assert resp.status_code == 400
    assert "Workflows" in resp.json()["detail"]


def test_repo_without_workflows_dir_returns_empty_findings(client, db, user):
    _admin_org(db, user)
    err = httpx.HTTPStatusError(
        "404", request=httpx.Request("GET", "https://api.github.com"), response=httpx.Response(404)
    )
    with patch("src.routers.workflow_lint.GitHubClient") as mock:
        mock.return_value.request.side_effect = err
        resp = client.post(
            "/orgs/acme/repos/acme/api/workflow-lint", json={"token": "ghp_admin"}
        )
    assert resp.status_code == 200
    assert resp.json() == {"findings": [], "fixable": False, "pr_url": None}


def test_personal_route_scan_needs_only_membership_and_audits_under_personal_tenant(client, db, user):
    # owner isn't a connected Clevis org -> bring-your-own-PAT scan, audit lands on the
    # caller's personal tenant (never NULL -- audit_logs RLS).
    with patch("src.routers.workflow_lint.GitHubClient") as mock:
        _github(mock, workflows={"ok.yml": _CLEAN})
        resp = client.post(
            "/me/repos/someone/api/workflow-lint", json={"token": "ghp_byo"}
        )
    assert resp.status_code == 200
    row = db.query(AuditLog).filter(AuditLog.action == "workflow_lint.scan").one()
    assert row.tenant_id == tenant_repo.ensure_personal_tenant(db, user.id).id


def test_personal_scan_of_a_connected_org_audits_under_the_org_tenant(client, db, user):
    org = _admin_org(db, user)
    with patch("src.routers.workflow_lint.GitHubClient") as mock:
        _github(mock, workflows={"ok.yml": _CLEAN})
        resp = client.post("/me/repos/acme/api/workflow-lint", json={"token": "ghp_admin"})
    assert resp.status_code == 200
    row = db.query(AuditLog).filter(AuditLog.action == "workflow_lint.scan").one()
    assert row.tenant_id == org.tenant_id


def test_non_403_github_error_is_surfaced(client, db, user):
    _admin_org(db, user)
    err = httpx.HTTPStatusError(
        "500", request=httpx.Request("GET", "https://api.github.com"), response=httpx.Response(500)
    )
    with patch("src.routers.workflow_lint.GitHubClient") as mock:
        mock.return_value.request.side_effect = err
        resp = client.post("/orgs/acme/repos/acme/api/workflow-lint", json={"token": "ghp_x"})
    assert resp.status_code >= 400 and "Workflows" not in resp.json()["detail"]


def test_org_route_without_a_token_returns_400(client, db, user):
    _admin_org(db, user)
    resp = client.post("/orgs/acme/repos/acme/api/workflow-lint", json={})
    assert resp.status_code == 400


def test_personal_route_without_a_token_returns_400(client, db, user):
    resp = client.post("/me/repos/someone/api/workflow-lint", json={})
    assert resp.status_code == 400


def test_github_unreachable_is_surfaced(client, db, user):
    _admin_org(db, user)
    with patch("src.routers.workflow_lint.GitHubClient") as mock:
        mock.return_value.request.side_effect = httpx.ConnectError("boom")
        resp = client.post("/orgs/acme/repos/acme/api/workflow-lint", json={"token": "ghp_x"})
    assert resp.status_code >= 500


def test_a_blob_that_is_not_valid_utf8_base64_is_skipped(client, db, user):
    _admin_org(db, user)

    def request(method, path, params=None, json=None):
        if path.endswith("/contents/.github/workflows"):
            return [{"name": "bad.yml", "path": ".github/workflows/bad.yml", "url": "blob:bad"}]
        if path == "blob:bad":
            return {"content": "!!!not base64!!!", "sha": "s"}
        return {}

    with patch("src.routers.workflow_lint.GitHubClient") as mock:
        mock.return_value.request.side_effect = request
        resp = client.post("/orgs/acme/repos/acme/api/workflow-lint", json={"token": "ghp_x"})
    assert resp.status_code == 200 and resp.json()["findings"] == []


def test_open_pr_resets_an_existing_fix_branch(client, db, user):
    _admin_org(db, user)
    seen = {"patched": False}

    def request(method, path, params=None, json=None):
        if path.endswith("/contents/.github/workflows"):
            return [{"name": "bad.yml", "path": ".github/workflows/bad.yml", "url": "blob:bad"}]
        if path == "blob:bad":
            return _blob(_BAD_PRT)
        if path.endswith("/repos/acme/api"):
            return {"default_branch": "main"}
        if "/git/ref/heads/" in path:
            return {"object": {"sha": "basesha"}}
        if method == "POST" and path.endswith("/git/refs"):
            raise httpx.HTTPStatusError(
                "422", request=httpx.Request("POST", "https://api.github.com"), response=httpx.Response(422)
            )
        if method == "PATCH" and "/git/refs/heads/" in path:
            seen["patched"] = True
            return {}
        if "/contents/.github/workflows/" in path:
            return {} if method == "PUT" else {"sha": "existing"}
        if method == "POST" and path.endswith("/pulls"):
            return {"html_url": "https://github.com/acme/api/pull/1"}
        return {}

    with patch("src.routers.workflow_lint.GitHubClient") as mock:
        mock.return_value.request.side_effect = request
        resp = client.post(
            "/orgs/acme/repos/acme/api/workflow-lint",
            json={"token": "ghp_admin", "open_pr": True},
        )
    assert resp.status_code == 200 and seen["patched"] is True


def test_open_pr_surfaces_a_non_422_branch_create_error(client, db, user):
    _admin_org(db, user)

    def request(method, path, params=None, json=None):
        if path.endswith("/contents/.github/workflows"):
            return [{"name": "bad.yml", "path": ".github/workflows/bad.yml", "url": "blob:bad"}]
        if path == "blob:bad":
            return _blob(_BAD_PRT)
        if path.endswith("/repos/acme/api"):
            return {"default_branch": "main"}
        if "/git/ref/heads/" in path:
            return {"object": {"sha": "basesha"}}
        if method == "GET" and path.endswith("/pulls"):
            return []
        if method == "POST" and path.endswith("/git/refs"):
            raise httpx.HTTPStatusError(
                "500", request=httpx.Request("POST", "https://api.github.com"), response=httpx.Response(500)
            )
        return {}

    with patch("src.routers.workflow_lint.GitHubClient") as mock:
        mock.return_value.request.side_effect = request
        resp = client.post(
            "/orgs/acme/repos/acme/api/workflow-lint",
            json={"token": "ghp_admin", "open_pr": True},
        )
    assert resp.status_code >= 400 and "Workflows" not in resp.json()["detail"]


def test_open_pr_is_idempotent_when_a_fix_pr_is_already_open(client, db, user):
    _admin_org(db, user)
    with patch("src.routers.workflow_lint.GitHubClient") as mock:
        _inst, calls = _github(
            mock,
            workflows={"bad.yml": _BAD_PRT},
            existing_pr_url="https://github.com/acme/api/pull/7",
        )
        resp = client.post(
            "/orgs/acme/repos/acme/api/workflow-lint",
            json={"token": "ghp_admin", "open_pr": True},
        )
    assert resp.status_code == 200
    assert resp.json()["pr_url"] == "https://github.com/acme/api/pull/7"
    # the branch isn't reset and no new PR is opened — the files are just refreshed
    assert not calls["refs"] and not calls["pulls"] and calls["puts"]


def test_personal_open_pr_requires_admin_of_a_connected_org(client, db, user):
    org = org_repo.get_or_create(db, github_login="acme")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=user.id, role="member")
    db.commit()
    resp = client.post(
        "/me/repos/acme/api/workflow-lint", json={"token": "ghp_x", "open_pr": True}
    )
    assert resp.status_code == 403
