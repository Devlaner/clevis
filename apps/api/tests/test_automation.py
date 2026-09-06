"""Tests for the Automation router (docs/plan.md Phase 13)."""

from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.auth import UserOut, require_auth
from src.core.db import User, get_db
from src.core.db import AuditLog
from src.repositories import installation_repo, org_membership_repo, org_repo
from src.routers.automation import router as automation_router

_USER = UserOut(id=1, email="u@example.com", name=None, is_workspace_admin=False)


@pytest.fixture()
def automation_client(db):
    app = FastAPI()
    app.include_router(automation_router)
    app.dependency_overrides[require_auth] = lambda: _USER
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def test_personal_list_workflows_no_token_returns_400(automation_client):
    resp = automation_client.get("/me/repos/acme/demo/workflows")
    assert resp.status_code == 400


def test_personal_list_workflows_ok(automation_client):
    with patch("src.routers.automation.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = [
            {"total_count": 1, "workflows": [{"id": 1, "name": "CI", "path": ".github/workflows/ci.yml", "state": "active"}]},
            {"workflow_runs": [{"workflow_id": 1, "status": "completed", "conclusion": "success", "created_at": "2026-01-01T00:00:00Z"}]},
        ]
        resp = automation_client.get(
            "/me/repos/acme/demo/workflows", headers={"X-GitHub-Token": "ghp_testtoken123456789012345678901234"}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["workflows"][0]["name"] == "CI"
    assert body["workflows"][0]["last_run_status"] == "completed"


def test_personal_list_workflows_overlay_failure_degrades_gracefully(automation_client):
    error = httpx.HTTPStatusError(
        "boom", request=httpx.Request("GET", "https://api.github.com/x"),
        response=httpx.Response(403, request=httpx.Request("GET", "https://api.github.com/x")),
    )
    with patch("src.routers.automation.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = [
            {"total_count": 1, "workflows": [{"id": 1, "name": "CI", "path": ".github/workflows/ci.yml", "state": "active"}]},
            error,
        ]
        resp = automation_client.get(
            "/me/repos/acme/demo/workflows", headers={"X-GitHub-Token": "ghp_testtoken123456789012345678901234"}
        )
    assert resp.status_code == 200
    assert resp.json()["workflows"][0]["last_run_status"] is None


def test_personal_list_runs_computes_duration(automation_client):
    with patch("src.routers.automation.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = {
            "workflow_runs": [
                {
                    "id": 10,
                    "name": "CI",
                    "status": "completed",
                    "conclusion": "success",
                    "head_branch": "main",
                    "created_at": "2026-01-01T00:00:00Z",
                    "run_started_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-01T00:02:00Z",
                }
            ]
        }
        resp = automation_client.get(
            "/me/repos/acme/demo/actions/runs", headers={"X-GitHub-Token": "ghp_testtoken123456789012345678901234"}
        )
    assert resp.status_code == 200
    assert resp.json()["runs"][0]["duration_ms"] == 120_000


def test_personal_list_workflows_github_error_propagates(automation_client):
    error = httpx.HTTPStatusError(
        "boom", request=httpx.Request("GET", "https://api.github.com/x"),
        response=httpx.Response(404, request=httpx.Request("GET", "https://api.github.com/x")),
    )
    with patch("src.routers.automation.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = error
        resp = automation_client.get(
            "/me/repos/acme/demo/workflows", headers={"X-GitHub-Token": "ghp_testtoken123456789012345678901234"}
        )
    assert resp.status_code == 400


def test_personal_list_runs_github_error_propagates(automation_client):
    error = httpx.HTTPStatusError(
        "boom", request=httpx.Request("GET", "https://api.github.com/x"),
        response=httpx.Response(503, request=httpx.Request("GET", "https://api.github.com/x")),
    )
    with patch("src.routers.automation.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = error
        resp = automation_client.get(
            "/me/repos/acme/demo/actions/runs", headers={"X-GitHub-Token": "ghp_testtoken123456789012345678901234"}
        )
    assert resp.status_code == 400


def test_personal_list_runs_no_duration_when_not_completed_or_missing_timestamps(automation_client):
    with patch("src.routers.automation.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = {
            "workflow_runs": [
                {
                    "id": 11,
                    "name": "CI",
                    "status": "in_progress",
                    "conclusion": None,
                    "head_branch": "main",
                    "created_at": "2026-01-01T00:00:00Z",
                    "run_started_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-01T00:02:00Z",
                },
                {
                    "id": 12,
                    "name": "CI",
                    "status": "completed",
                    "conclusion": "success",
                    "head_branch": "main",
                    "created_at": "2026-01-01T00:00:00Z",
                    "run_started_at": "not-a-real-timestamp",
                    "updated_at": "2026-01-01T00:02:00Z",
                },
            ]
        }
        resp = automation_client.get(
            "/me/repos/acme/demo/actions/runs", headers={"X-GitHub-Token": "ghp_testtoken123456789012345678901234"}
        )
    assert resp.status_code == 200
    runs = resp.json()["runs"]
    assert runs[0]["duration_ms"] is None
    assert runs[1]["duration_ms"] is None


def test_personal_list_runs_no_token_returns_400(automation_client):
    resp = automation_client.get("/me/repos/acme/demo/actions/runs")
    assert resp.status_code == 400


def test_personal_list_runs_rejects_out_of_range_per_page(automation_client):
    # Regression test for issue #224 item 2: per_page had no Query(le=...) bound, so an
    # arbitrary/negative value produced an opaque upstream GitHub 400 instead of a clean
    # 422 at the boundary, unlike audit.py's equivalent limit param.
    resp = automation_client.get(
        "/me/repos/acme/demo/actions/runs",
        params={"per_page": 1000},
        headers={"X-GitHub-Token": "ghp_testtoken123456789012345678901234"},
    )
    assert resp.status_code == 422

    resp = automation_client.get(
        "/me/repos/acme/demo/actions/runs",
        params={"per_page": 0},
        headers={"X-GitHub-Token": "ghp_testtoken123456789012345678901234"},
    )
    assert resp.status_code == 422


def test_personal_dispatch_rejects_oversized_ref(automation_client):
    # Regression test for issue #224 item 3: DispatchInput.ref/inputs had no max_length,
    # letting a caller bloat the jobs/audit_logs payload columns with an arbitrarily large
    # value via a legitimate authenticated endpoint.
    resp = automation_client.post(
        "/me/repos/acme/demo/workflows/1/dispatch",
        json={"token": "ghp_testtoken123456789012345678901234", "ref": "x" * 300},
    )
    assert resp.status_code == 422


def test_personal_dispatch_rejects_too_many_inputs(automation_client):
    resp = automation_client.post(
        "/me/repos/acme/demo/workflows/1/dispatch",
        json={
            "token": "ghp_testtoken123456789012345678901234",
            "ref": "main",
            "inputs": {f"key{i}": "v" for i in range(11)},
        },
    )
    assert resp.status_code == 422


def test_personal_dispatch_writes_audit_log_before_github_call(automation_client, db):
    db.add(User(id=_USER.id, email=_USER.email, name=None, password_hash=None, is_workspace_admin=False))
    db.commit()
    with patch("src.routers.automation.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = {}
        resp = automation_client.post(
            "/me/repos/acme/demo/workflows/1/dispatch",
            json={"token": "ghp_testtoken123456789012345678901234", "ref": "main"},
        )
    assert resp.status_code == 200
    assert resp.json()["dispatched"] is True
    logs = db.query(AuditLog).filter(AuditLog.action == "automation.workflow.dispatch").all()
    assert len(logs) == 1
    assert logs[0].actor == _USER.email
    assert logs[0].target == "acme/demo#1"


def test_personal_dispatch_no_token_returns_400_and_still_no_github_call(automation_client):
    with patch("src.routers.automation.GitHubClient") as mock_client:
        resp = automation_client.post("/me/repos/acme/demo/workflows/1/dispatch", json={"ref": "main"})
    assert resp.status_code == 400
    mock_client.return_value.request.assert_not_called()


def test_personal_dispatch_rejects_org_member_supplying_own_token(automation_client, db):
    # Regression test (CodeRabbit finding on PR #264): a plain "member" of a connected
    # org must not be able to dispatch its workflows through the personal endpoint by
    # supplying their own PAT -- that would bypass the admin-only gate this endpoint is
    # supposed to enforce via resolve_owner_token(min_role="admin").
    db.add(User(id=_USER.id, email=_USER.email, name=None, password_hash=None, is_workspace_admin=False))
    db.commit()
    org = org_repo.get_or_create(db, github_login="acme")
    org_membership_repo.get_or_create(db, org.id, _USER.id, role="member")
    installation_repo.create(
        db, account_login="acme", account_type="Organization", auth_mode="app", installation_id=42, org_id=org.id
    )
    with patch("src.routers.automation.GitHubClient") as mock_client:
        resp = automation_client.post(
            "/me/repos/acme/demo/workflows/1/dispatch",
            json={"token": "ghp_testtoken123456789012345678901234", "ref": "main"},
        )
    assert resp.status_code == 403
    mock_client.return_value.request.assert_not_called()


def test_personal_dispatch_github_error_still_leaves_audit_log(automation_client, db):
    db.add(User(id=_USER.id, email=_USER.email, name=None, password_hash=None, is_workspace_admin=False))
    db.commit()
    error = httpx.HTTPStatusError(
        "boom", request=httpx.Request("POST", "https://api.github.com/x"),
        response=httpx.Response(422, request=httpx.Request("POST", "https://api.github.com/x")),
    )
    with patch("src.routers.automation.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = error
        resp = automation_client.post(
            "/me/repos/acme/demo/workflows/1/dispatch",
            json={"token": "ghp_testtoken123456789012345678901234", "ref": "main"},
        )
    assert resp.status_code == 400
    logs = db.query(AuditLog).filter(AuditLog.action == "automation.workflow.dispatch").all()
    assert len(logs) == 1


# ── org-scoped ────────────────────────────────────────────────────────────────

@pytest.fixture()
def acme_org(db):
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


def _org_client(db, user_id, email="u@example.com"):
    app = FastAPI()
    app.include_router(automation_router)
    app.dependency_overrides[require_auth] = lambda: UserOut(id=user_id, email=email, name=None, is_workspace_admin=False)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def test_org_list_workflows_member_ok(db, acme_org):
    client = _org_client(db, acme_org["member"].id)
    with patch("src.routers.automation.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = [
            {"total_count": 0, "workflows": []},
            {"workflow_runs": []},
        ]
        resp = client.get(
            "/orgs/acme/repos/acme/demo/workflows", headers={"X-GitHub-Token": "ghp_testtoken123456789012345678901234"}
        )
    assert resp.status_code == 200


def test_org_dispatch_requires_admin(db, acme_org):
    client = _org_client(db, acme_org["member"].id, email=acme_org["member"].email)
    resp = client.post(
        "/orgs/acme/repos/acme/demo/workflows/1/dispatch",
        json={"token": "ghp_testtoken123456789012345678901234", "ref": "main"},
    )
    assert resp.status_code == 403


def test_org_dispatch_admin_ok(db, acme_org):
    client = _org_client(db, acme_org["admin"].id, email=acme_org["admin"].email)
    with patch("src.routers.automation.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = {}
        resp = client.post(
            "/orgs/acme/repos/acme/demo/workflows/1/dispatch",
            json={"token": "ghp_testtoken123456789012345678901234", "ref": "main"},
        )
    assert resp.status_code == 200
    logs = db.query(AuditLog).filter(AuditLog.action == "automation.workflow.dispatch").all()
    assert logs[0].actor == acme_org["admin"].email


def test_org_list_workflows_outsider_forbidden(db, acme_org):
    client = _org_client(db, 999999)
    resp = client.get(
        "/orgs/acme/repos/acme/demo/workflows", headers={"X-GitHub-Token": "ghp_testtoken123456789012345678901234"}
    )
    assert resp.status_code == 403


def test_org_dispatch_owner_mismatch_forbidden(db, acme_org):
    client = _org_client(db, acme_org["admin"].id, email=acme_org["admin"].email)
    resp = client.post(
        "/orgs/acme/repos/other-owner/demo/workflows/1/dispatch",
        json={"token": "ghp_testtoken123456789012345678901234", "ref": "main"},
    )
    assert resp.status_code == 403


def test_org_list_workflows_no_token_returns_400(db, acme_org):
    client = _org_client(db, acme_org["member"].id, email=acme_org["member"].email)
    resp = client.get("/orgs/acme/repos/acme/demo/workflows")
    assert resp.status_code == 400


def test_org_list_runs_member_ok(db, acme_org):
    client = _org_client(db, acme_org["member"].id, email=acme_org["member"].email)
    with patch("src.routers.automation.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = {"workflow_runs": []}
        resp = client.get(
            "/orgs/acme/repos/acme/demo/actions/runs", headers={"X-GitHub-Token": "ghp_testtoken123456789012345678901234"}
        )
    assert resp.status_code == 200
    assert resp.json()["runs"] == []


def test_org_list_runs_no_token_returns_400(db, acme_org):
    client = _org_client(db, acme_org["member"].id, email=acme_org["member"].email)
    resp = client.get("/orgs/acme/repos/acme/demo/actions/runs")
    assert resp.status_code == 400


def test_org_dispatch_no_token_returns_400(db, acme_org):
    client = _org_client(db, acme_org["admin"].id, email=acme_org["admin"].email)
    resp = client.post("/orgs/acme/repos/acme/demo/workflows/1/dispatch", json={"ref": "main"})
    assert resp.status_code == 400


# ── bulk dispatch-all ─────────────────────────────────────────────────────────

_WORKFLOWS_3 = {
    "workflows": [
        {"id": 1, "name": "CI", "path": ".github/workflows/ci.yml", "state": "active"},
        {"id": 2, "name": "Release", "path": ".github/workflows/release.yml", "state": "active"},
        {"id": 3, "name": "Old", "path": ".github/workflows/old.yml", "state": "disabled_manually"},
    ]
}


def _no_trigger_422():
    return httpx.HTTPStatusError(
        "boom",
        request=httpx.Request("POST", "https://api.github.com/x"),
        response=httpx.Response(422, json={"message": "Workflow does not have 'workflow_dispatch' trigger."}),
    )


def test_personal_dispatch_all_mixed_results(automation_client, db):
    db.add(User(id=_USER.id, email=_USER.email, name=None, password_hash=None, is_workspace_admin=False))
    db.commit()
    with patch("src.routers.automation.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = [
            _WORKFLOWS_3,      # GET workflows
            {},               # workflow 1 dispatched
            _no_trigger_422(),  # workflow 2 skipped
        ]
        resp = automation_client.post(
            "/me/repos/acme/demo/workflows/dispatch-all",
            json={"token": "ghp_testtoken123456789012345678901234", "ref": "main"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert (body["dispatched_count"], body["skipped_count"], body["failed_count"]) == (1, 1, 0)
    # Only the two *active* workflows are attempted -> two audit rows.
    logs = db.query(AuditLog).filter(AuditLog.action == "automation.workflow.dispatch").all()
    assert {log.target for log in logs} == {"acme/demo#1", "acme/demo#2"}


def test_personal_dispatch_all_reports_failure_row(automation_client, db):
    db.add(User(id=_USER.id, email=_USER.email, name=None, password_hash=None, is_workspace_admin=False))
    db.commit()
    forbidden = httpx.HTTPStatusError(
        "boom",
        request=httpx.Request("POST", "https://api.github.com/x"),
        response=httpx.Response(403, json={"message": "Resource not accessible by integration"}),
    )
    with patch("src.routers.automation.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = [
            {"workflows": [{"id": 1, "name": "CI", "path": "p", "state": "active"}]},
            forbidden,
        ]
        resp = automation_client.post(
            "/me/repos/acme/demo/workflows/dispatch-all",
            json={"token": "ghp_testtoken123456789012345678901234", "ref": "main"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["failed_count"] == 1
    assert body["results"][0]["message"] == "Resource not accessible by integration"


def test_personal_dispatch_all_no_token_returns_400(automation_client):
    with patch("src.routers.automation.GitHubClient") as mock_client:
        resp = automation_client.post("/me/repos/acme/demo/workflows/dispatch-all", json={"ref": "main"})
    assert resp.status_code == 400
    mock_client.return_value.request.assert_not_called()


def test_personal_dispatch_all_over_cap_returns_422(automation_client, db):
    db.add(User(id=_USER.id, email=_USER.email, name=None, password_hash=None, is_workspace_admin=False))
    db.commit()
    many = {"workflows": [{"id": i, "name": f"w{i}", "path": "p", "state": "active"} for i in range(41)]}
    with patch("src.routers.automation.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = [many]
        resp = automation_client.post(
            "/me/repos/acme/demo/workflows/dispatch-all",
            json={"token": "ghp_testtoken123456789012345678901234", "ref": "main"},
        )
    assert resp.status_code == 422


def test_org_dispatch_all_requires_admin(db, acme_org):
    client = _org_client(db, acme_org["member"].id, email=acme_org["member"].email)
    resp = client.post(
        "/orgs/acme/repos/acme/demo/workflows/dispatch-all",
        json={"token": "ghp_testtoken123456789012345678901234", "ref": "main"},
    )
    assert resp.status_code == 403


def test_org_dispatch_all_admin_ok(db, acme_org):
    client = _org_client(db, acme_org["admin"].id, email=acme_org["admin"].email)
    with patch("src.routers.automation.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = [
            {"workflows": [{"id": 7, "name": "CI", "path": "p", "state": "active"}]},
            {},
        ]
        resp = client.post(
            "/orgs/acme/repos/acme/demo/workflows/dispatch-all",
            json={"token": "ghp_testtoken123456789012345678901234", "ref": "main"},
        )
    assert resp.status_code == 200
    assert resp.json()["dispatched_count"] == 1
    logs = db.query(AuditLog).filter(AuditLog.action == "automation.workflow.dispatch").all()
    assert logs[0].target == "acme/demo#7"


def test_org_dispatch_all_owner_mismatch_forbidden(db, acme_org):
    client = _org_client(db, acme_org["admin"].id, email=acme_org["admin"].email)
    resp = client.post(
        "/orgs/acme/repos/other-owner/demo/workflows/dispatch-all",
        json={"token": "ghp_testtoken123456789012345678901234", "ref": "main"},
    )
    assert resp.status_code == 403
