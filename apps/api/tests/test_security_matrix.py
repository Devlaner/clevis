"""Tests for the security compliance matrix and secret-scanning routes (docs/plan.md Phase 16)."""

import json
from datetime import datetime, timezone
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from src.core.auth import UserOut, require_auth
from src.core.db import User, get_db
from src.repositories import installation_repo, org_membership_repo, org_repo
from src.routers.security import router

_USER = UserOut(id=1, email="u@example.com", name=None, is_workspace_admin=False)


def _make_user(db, email: str) -> UserOut:
    user = User(email=email, name=None, password_hash=None, is_workspace_admin=False)
    db.add(user)
    db.commit()
    db.refresh(user)
    return UserOut(id=user.id, email=user.email, name=user.name, is_workspace_admin=False)


@pytest.fixture()
def mock_user(db):
    return _make_user(db, "security-matrix@example.com")


@pytest.fixture()
def acme_org_with_installation(db, mock_user):
    """Same connected-tenant fixture pattern as test_analytics_cockpit.py's -- a
    personal endpoint (require_auth only), gated by the same org-membership +
    installation_id-presence check _security_connected_tenant mirrors from
    analytics.py's _cockpit_connected_tenant."""
    org = org_repo.get_or_create(db, github_login="acme")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=mock_user.id, role="member")
    installation_repo.create(
        db, account_login="acme", account_type="Organization", auth_mode="app", installation_id=7, org_id=org.id
    )
    return org


def _insert_security_alert(db, tenant_id, *, repo, kind, number, state, severity, details):
    db.execute(text(f"SET app.tenant_id = {int(tenant_id)}"))
    db.execute(
        text(
            "INSERT INTO security_alerts (tenant_id, repo, kind, number, state, severity, details, created_at, updated_at) "
            "VALUES (:tenant_id, :repo, :kind, :number, :state, :severity, :details, :now, :now)"
        ),
        {
            "tenant_id": tenant_id,
            "repo": repo,
            "kind": kind,
            "number": number,
            "state": state,
            "severity": severity,
            "details": json.dumps(details),
            "now": datetime.now(timezone.utc),
        },
    )
    db.commit()


@pytest.fixture()
def client(db):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: _USER
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


@pytest.fixture()
def connected_client(db, mock_user):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: mock_user
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def test_security_matrix_requires_auth(db):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    resp = TestClient(app).get("/me/analytics/security-matrix/acme")
    assert resp.status_code == 401


def test_security_matrix_no_token_returns_400(client):
    resp = client.get("/me/analytics/security-matrix/acme")
    assert resp.status_code == 400


def test_security_matrix_computes_rows_and_summary(client):
    def _request_side_effect(method, path, params=None):
        if path.endswith("/branches/main"):
            return {"protected": True, "protection": {"allow_force_pushes": {"enabled": False}}}
        if path.endswith("/dependabot/alerts"):
            return [{"security_advisory": {"severity": "critical"}}]
        if path.endswith("/code-scanning/alerts"):
            return []
        return {}

    with patch("src.routers.security.GitHubClient") as mock_client:
        mock_client.return_value.request_paginated.return_value = [
            {"name": "api", "default_branch": "main", "security_and_analysis": {"secret_scanning": {"status": "enabled"}}},
        ]
        mock_client.return_value.request.side_effect = _request_side_effect
        resp = client.get("/me/analytics/security-matrix/acme", headers={"X-GitHub-Token": "ghp_test"})

    assert resp.status_code == 200
    body = resp.json()
    row = body["repos"][0]
    assert row["repo"] == "api"
    assert row["branch_protection"] is True
    assert row["secret_scanning"] is True
    assert row["dependabot_critical_count"] == 1
    assert row["code_scanning"] is True
    assert row["force_push_allowed"] is False
    assert row["score"] == 80  # 4 of 5 dimensions pass (dependabot has a critical alert)
    assert row["unknown_dimensions"] == []
    assert body["summary"]["critical_risk_count"] == 1
    assert body["summary"]["vuln_by_severity"]["critical"] == 1
    assert body["summary"]["fully_compliant_count"] == 0


def test_security_matrix_excludes_unknown_dimensions_from_score(client):
    """A 403/network error must not be scored as if the dimension were compliant --
    see the DependabotAlertsCheck false-pass fix (packages/checks, 3184c76) this
    mirrors. Every GitHub call fails here, so only secret_scanning (read from the
    already-fetched repo list, no extra call) is evaluable."""
    with patch("src.routers.security.GitHubClient") as mock_client:
        mock_client.return_value.request_paginated.return_value = [
            {"name": "api", "default_branch": "main", "security_and_analysis": {}},
        ]
        mock_client.return_value.request.side_effect = httpx.RequestError("boom")
        resp = client.get("/me/analytics/security-matrix/acme", headers={"X-GitHub-Token": "ghp_test"})

    assert resp.status_code == 200
    row = resp.json()["repos"][0]
    assert row["branch_protection"] is False
    assert row["dependabot_enabled"] is False
    assert sorted(row["unknown_dimensions"]) == ["branch_protection", "code_scanning", "dependabot", "force_push"]
    assert row["score"] == 0  # secret_scanning is the only evaluable dimension, and it's False


def test_security_matrix_403_on_dependabot_is_unknown_not_clean(client):
    """A 403 (missing security-events scope) must not read as 'no critical/high
    alerts' -- that's the exact bug fixed for DependabotAlertsCheck in 3184c76."""
    forbidden = httpx.HTTPStatusError(
        "boom", request=httpx.Request("GET", "https://api.github.com/x"),
        response=httpx.Response(403, request=httpx.Request("GET", "https://api.github.com/x")),
    )

    def _request_side_effect(method, path, params=None):
        if path.endswith("/dependabot/alerts"):
            raise forbidden
        if path.endswith("/branches/main"):
            return {"protected": True, "protection": {"allow_force_pushes": {"enabled": False}}}
        if path.endswith("/code-scanning/alerts"):
            return []
        return {}

    with patch("src.routers.security.GitHubClient") as mock_client:
        mock_client.return_value.request_paginated.return_value = [
            {"name": "api", "default_branch": "main", "security_and_analysis": {"secret_scanning": {"status": "enabled"}}},
        ]
        mock_client.return_value.request.side_effect = _request_side_effect
        resp = client.get("/me/analytics/security-matrix/acme", headers={"X-GitHub-Token": "ghp_test"})

    row = resp.json()["repos"][0]
    assert row["unknown_dimensions"] == ["dependabot"]
    assert row["score"] == 100  # remaining 4 evaluable dimensions all pass
    assert row["dependabot_critical_count"] == 0  # not silently zero-and-clean -- flagged unknown instead


def test_security_matrix_404_on_dependabot_is_genuinely_disabled(client):
    """Unlike a 403, a 404 is a real 'Dependabot is off for this repo' answer and
    should count as a real (non-unknown) 'no alerts' pass."""
    not_found = httpx.HTTPStatusError(
        "boom", request=httpx.Request("GET", "https://api.github.com/x"),
        response=httpx.Response(404, request=httpx.Request("GET", "https://api.github.com/x")),
    )

    def _request_side_effect(method, path, params=None):
        if path.endswith("/dependabot/alerts"):
            raise not_found
        if path.endswith("/branches/main"):
            return {"protected": True, "protection": {"allow_force_pushes": {"enabled": False}}}
        if path.endswith("/code-scanning/alerts"):
            return []
        return {}

    with patch("src.routers.security.GitHubClient") as mock_client:
        mock_client.return_value.request_paginated.return_value = [
            {"name": "api", "default_branch": "main", "security_and_analysis": {"secret_scanning": {"status": "enabled"}}},
        ]
        mock_client.return_value.request.side_effect = _request_side_effect
        resp = client.get("/me/analytics/security-matrix/acme", headers={"X-GitHub-Token": "ghp_test"})

    row = resp.json()["repos"][0]
    assert row["unknown_dimensions"] == []
    assert row["score"] == 100


def test_secret_scanning_no_token_returns_400(client):
    resp = client.get("/me/repos/acme/demo/secret-scanning")
    assert resp.status_code == 400


def test_secret_scanning_never_includes_secret_value(client):
    raw_alert = {
        "number": 1,
        "state": "open",
        "secret_type": "github_personal_access_token",
        "secret_type_display_name": "GitHub Personal Access Token",
        "created_at": "2026-07-01T00:00:00Z",
        "resolved_at": None,
        "resolution": None,
        "html_url": "https://github.com/acme/demo/security/secret-scanning/1",
        "secret": "ghp_thisShouldNeverAppear1234567890",
    }
    with patch("src.routers.security.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = [raw_alert]
        resp = client.get("/me/repos/acme/demo/secret-scanning", headers={"X-GitHub-Token": "ghp_test"})

    assert resp.status_code == 200
    body_text = resp.text
    assert "ghp_thisShouldNeverAppear1234567890" not in body_text
    alert = resp.json()["alerts"][0]
    assert alert["secret_type"] == "github_personal_access_token"
    assert alert["secret_type_display"] == "GitHub Personal Access Token"
    assert "secret" not in alert


def test_secret_scanning_skips_malformed_entries(client):
    with patch("src.routers.security.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = [{"state": "open"}]  # missing number/created_at
        resp = client.get("/me/repos/acme/demo/secret-scanning", headers={"X-GitHub-Token": "ghp_test"})

    assert resp.status_code == 200
    assert resp.json()["alerts"] == []


def test_secret_scanning_github_error_maps_to_400(client):
    error = httpx.HTTPStatusError(
        "boom", request=httpx.Request("GET", "https://api.github.com/x"),
        response=httpx.Response(404, request=httpx.Request("GET", "https://api.github.com/x")),
    )
    with patch("src.routers.security.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = error
        resp = client.get("/me/repos/acme/demo/secret-scanning", headers={"X-GitHub-Token": "ghp_test"})

    assert resp.status_code == 400


def test_security_matrix_uses_aggregate_when_installation_connected(connected_client, db, acme_org_with_installation):
    _insert_security_alert(
        db, acme_org_with_installation.tenant_id, repo="acme/api", kind="dependabot", number=1,
        state="open", severity="critical", details={"dependency": {}},
    )
    _insert_security_alert(
        db, acme_org_with_installation.tenant_id, repo="acme/api", kind="code_scanning", number=2,
        state="open", severity="error", details={"rule": {}},
    )
    # A dismissed alert must not count toward critical_count/code_scanning -- only 'open'
    # rows are live findings, mirroring the live path's own state=open GitHub query param.
    _insert_security_alert(
        db, acme_org_with_installation.tenant_id, repo="acme/api", kind="dependabot", number=3,
        state="dismissed", severity="critical", details={"dependency": {}},
    )

    def _request_side_effect(method, path, params=None):
        if path.endswith("/branches/main"):
            return {"protected": True, "protection": {"allow_force_pushes": {"enabled": False}}}
        raise AssertionError(f"unexpected live GitHub call for a connected org: {path}")

    with patch("src.routers.security.GitHubClient") as mock_client:
        mock_client.return_value.request_paginated.return_value = [
            {"name": "api", "default_branch": "main", "security_and_analysis": {"secret_scanning": {"status": "enabled"}}},
        ]
        mock_client.return_value.request.side_effect = _request_side_effect
        resp = connected_client.get("/me/analytics/security-matrix/acme", headers={"X-GitHub-Token": "ghp_test"})

    assert resp.status_code == 200
    row = resp.json()["repos"][0]
    assert row["alerts_source"] == "aggregate"
    assert row["dependabot_enabled"] is True
    assert row["dependabot_critical_count"] == 1  # the dismissed one doesn't count
    assert row["dependabot_high_count"] == 0
    assert row["code_scanning"] is False  # an open code_scanning alert exists
    assert row["unknown_dimensions"] == []


def test_security_matrix_aggregate_dependabot_enabled_with_only_dismissed_alerts(connected_client, db, acme_org_with_installation):
    """CodeRabbit finding on PR #352: dependabot_enabled must consider every state, not
    just 'open' -- a repo whose only Dependabot alert has been dismissed still has
    Dependabot enabled, and must not read identically to one with it disabled."""
    _insert_security_alert(
        db, acme_org_with_installation.tenant_id, repo="acme/api", kind="dependabot", number=1,
        state="dismissed", severity="critical", details={"dependency": {}},
    )

    with patch("src.routers.security.GitHubClient") as mock_client:
        mock_client.return_value.request_paginated.return_value = [
            {"name": "api", "default_branch": "main", "security_and_analysis": {"secret_scanning": {"status": "enabled"}}},
        ]
        mock_client.return_value.request.return_value = {"protected": True, "protection": {"allow_force_pushes": {"enabled": False}}}
        resp = connected_client.get("/me/analytics/security-matrix/acme", headers={"X-GitHub-Token": "ghp_test"})

    row = resp.json()["repos"][0]
    assert row["dependabot_enabled"] is True
    assert row["dependabot_critical_count"] == 0  # dismissed, not a live finding
    assert row["score"] == 100


def test_security_matrix_falls_back_to_live_for_a_repo_with_no_ingested_alert_rows(connected_client, db, acme_org_with_installation):
    """No security_alerts rows for this repo at all -- ambiguous (genuinely clean vs. not-yet-
    ingested, security_alerts has no completeness cursor) so this repo must fall back to the
    live GitHub path rather than trusting an empty aggregate result as authoritative
    (CodeRabbit finding on PR #356)."""
    def _request_side_effect(method, path, params=None):
        if path.endswith("/branches/main"):
            return {"protected": True, "protection": {"allow_force_pushes": {"enabled": False}}}
        if path.endswith("/dependabot/alerts"):
            return []
        if path.endswith("/code-scanning/alerts"):
            return []
        raise AssertionError(f"unexpected call: {path}")

    with patch("src.routers.security.GitHubClient") as mock_client:
        mock_client.return_value.request_paginated.return_value = [
            {"name": "clean-repo", "default_branch": "main", "security_and_analysis": {"secret_scanning": {"status": "enabled"}}},
        ]
        mock_client.return_value.request.side_effect = _request_side_effect
        resp = connected_client.get("/me/analytics/security-matrix/acme", headers={"X-GitHub-Token": "ghp_test"})

    row = resp.json()["repos"][0]
    assert row["alerts_source"] == "github"
    assert row["dependabot_enabled"] is True  # 200 with an empty list, not a 404 -- genuinely enabled
    assert row["code_scanning"] is True


def test_security_matrix_uses_aggregate_only_for_repos_with_ingested_rows(connected_client, db, acme_org_with_installation):
    """A connected tenant can have one repo with real ingested alert rows and another (e.g.
    added to the org after this tenant connected) with none yet -- each repo's alerts_source
    is decided independently, not once for the whole tenant."""
    _insert_security_alert(
        db, acme_org_with_installation.tenant_id, repo="acme/api", kind="dependabot", number=1,
        state="open", severity="critical", details={"dependency": {}},
    )

    def _request_side_effect(method, path, params=None):
        if path.endswith("/branches/main"):
            return {"protected": True, "protection": {"allow_force_pushes": {"enabled": False}}}
        if path.endswith("/dependabot/alerts"):
            return []
        if path.endswith("/code-scanning/alerts"):
            return []
        raise AssertionError(f"unexpected call: {path}")

    with patch("src.routers.security.GitHubClient") as mock_client:
        mock_client.return_value.request_paginated.return_value = [
            {"name": "api", "default_branch": "main", "security_and_analysis": {"secret_scanning": {"status": "enabled"}}},
            {"name": "new-repo", "default_branch": "main", "security_and_analysis": {"secret_scanning": {"status": "enabled"}}},
        ]
        mock_client.return_value.request.side_effect = _request_side_effect
        resp = connected_client.get("/me/analytics/security-matrix/acme", headers={"X-GitHub-Token": "ghp_test"})

    rows = {r["repo"]: r for r in resp.json()["repos"]}
    assert rows["api"]["alerts_source"] == "aggregate"
    assert rows["api"]["dependabot_critical_count"] == 1
    assert rows["api"]["code_scanning"] is True  # no ingested code_scanning rows -- live path
    assert rows["new-repo"]["alerts_source"] == "github"


def test_security_matrix_gates_dependabot_and_code_scanning_independently(connected_client, db, acme_org_with_installation):
    """Regression test for CodeRabbit's round-2 finding on PR #356: a repo can have ingested
    dependabot_alert webhooks but never a code_scanning_alert webhook (or vice versa).
    Trusting the aggregate for one kind must not silently mark the *other* kind clean -- here
    the repo has only a dependabot row ingested, and live GitHub has a real open code-scanning
    alert that was never ingested. The old (buggy) per-repo-only gate would have reported
    code_scanning=True (clear) purely because repo_alert_rows was non-empty; the fix must
    still hit live GitHub for code_scanning and report the real open alert."""
    _insert_security_alert(
        db, acme_org_with_installation.tenant_id, repo="acme/api", kind="dependabot", number=1,
        state="open", severity="high", details={"dependency": {}},
    )

    def _request_side_effect(method, path, params=None):
        if path.endswith("/branches/main"):
            return {"protected": True, "protection": {"allow_force_pushes": {"enabled": False}}}
        if path.endswith("/dependabot/alerts"):
            raise AssertionError("dependabot dimension should come from the aggregate, not a live call")
        if path.endswith("/code-scanning/alerts"):
            return [{"number": 9}]  # a real open alert, never ingested
        raise AssertionError(f"unexpected call: {path}")

    with patch("src.routers.security.GitHubClient") as mock_client:
        mock_client.return_value.request_paginated.return_value = [
            {"name": "api", "default_branch": "main", "security_and_analysis": {"secret_scanning": {"status": "enabled"}}},
        ]
        mock_client.return_value.request.side_effect = _request_side_effect
        resp = connected_client.get("/me/analytics/security-matrix/acme", headers={"X-GitHub-Token": "ghp_test"})

    row = resp.json()["repos"][0]
    assert row["alerts_source"] == "aggregate"  # dependabot was aggregate-sourced
    assert row["dependabot_high_count"] == 1
    assert row["code_scanning"] is False  # must reflect the real live open alert, not a false "clear"


def test_security_matrix_connected_org_with_no_repos_is_empty(connected_client, db, acme_org_with_installation):
    """No repos returned from the org repo listing -- _open_alerts_by_repo's
    empty-input short-circuit must not error out on an empty IN (...) query."""
    with patch("src.routers.security.GitHubClient") as mock_client:
        mock_client.return_value.request_paginated.return_value = []
        resp = connected_client.get("/me/analytics/security-matrix/acme", headers={"X-GitHub-Token": "ghp_test"})

    assert resp.status_code == 200
    assert resp.json()["repos"] == []


def test_security_matrix_falls_back_to_github_when_caller_lacks_membership(client, db):
    """Org exists and has an installation, but the caller (_USER) has no OrgMembership
    row -- _security_connected_tenant must return None (not silently trust `owner`
    naming a real org) so the matrix falls back to the live path."""
    org = org_repo.get_or_create(db, github_login="acme")
    installation_repo.create(
        db, account_login="acme", account_type="Organization", auth_mode="app", installation_id=7, org_id=org.id
    )

    def _request_side_effect(method, path, params=None):
        if path.endswith("/branches/main"):
            return {"protected": True, "protection": {"allow_force_pushes": {"enabled": False}}}
        if path.endswith("/dependabot/alerts") or path.endswith("/code-scanning/alerts"):
            return []
        return {}

    with patch("src.routers.security.GitHubClient") as mock_client:
        mock_client.return_value.request_paginated.return_value = [
            {"name": "api", "default_branch": "main", "security_and_analysis": {"secret_scanning": {"status": "enabled"}}},
        ]
        mock_client.return_value.request.side_effect = _request_side_effect
        resp = client.get("/me/analytics/security-matrix/acme", headers={"X-GitHub-Token": "ghp_test"})

    assert resp.json()["repos"][0]["alerts_source"] == "github"


def test_security_matrix_falls_back_to_github_when_org_has_no_installation(connected_client, db, mock_user):
    """Org exists and the caller is a member, but no GitHub App installation is
    connected -- _security_connected_tenant must return None."""
    org = org_repo.get_or_create(db, github_login="acme")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=mock_user.id, role="member")

    def _request_side_effect(method, path, params=None):
        if path.endswith("/branches/main"):
            return {"protected": True, "protection": {"allow_force_pushes": {"enabled": False}}}
        if path.endswith("/dependabot/alerts") or path.endswith("/code-scanning/alerts"):
            return []
        return {}

    with patch("src.routers.security.GitHubClient") as mock_client:
        mock_client.return_value.request_paginated.return_value = [
            {"name": "api", "default_branch": "main", "security_and_analysis": {"secret_scanning": {"status": "enabled"}}},
        ]
        mock_client.return_value.request.side_effect = _request_side_effect
        resp = connected_client.get("/me/analytics/security-matrix/acme", headers={"X-GitHub-Token": "ghp_test"})

    assert resp.json()["repos"][0]["alerts_source"] == "github"


def test_security_matrix_unconnected_org_still_uses_live_github(client, db):
    """No installation for this owner -- _security_connected_tenant returns None, so the
    matrix must fall back to the pre-existing live-GitHub path unchanged."""
    def _request_side_effect(method, path, params=None):
        if path.endswith("/branches/main"):
            return {"protected": True, "protection": {"allow_force_pushes": {"enabled": False}}}
        if path.endswith("/dependabot/alerts"):
            return []
        if path.endswith("/code-scanning/alerts"):
            return []
        return {}

    with patch("src.routers.security.GitHubClient") as mock_client:
        mock_client.return_value.request_paginated.return_value = [
            {"name": "api", "default_branch": "main", "security_and_analysis": {"secret_scanning": {"status": "enabled"}}},
        ]
        mock_client.return_value.request.side_effect = _request_side_effect
        resp = client.get("/me/analytics/security-matrix/acme", headers={"X-GitHub-Token": "ghp_test"})

    row = resp.json()["repos"][0]
    assert row["alerts_source"] == "github"


def test_secret_scanning_uses_aggregate_when_installation_connected(connected_client, db, acme_org_with_installation):
    _insert_security_alert(
        db, acme_org_with_installation.tenant_id, repo="acme/demo", kind="secret_scanning", number=1,
        state="open", severity=None,
        details={"secret_type": "github_personal_access_token", "secret_type_display_name": "GitHub Personal Access Token", "resolution": None},
    )
    _insert_security_alert(
        db, acme_org_with_installation.tenant_id, repo="acme/demo", kind="secret_scanning", number=2,
        state="resolved", severity=None,
        details={"secret_type": "aws_access_key_id", "secret_type_display_name": "AWS Access Key", "resolution": "revoked"},
    )

    with patch("src.routers.security.GitHubClient") as mock_client:
        resp = connected_client.get("/me/repos/acme/demo/secret-scanning", headers={"X-GitHub-Token": "ghp_test"})
        mock_client.return_value.request.assert_not_called()

    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "aggregate"
    alerts = {a["number"]: a for a in body["alerts"]}
    assert alerts[1]["state"] == "open"
    assert alerts[1]["resolved_at"] is None
    assert alerts[2]["state"] == "resolved"
    assert alerts[2]["resolved_reason"] == "revoked"
    assert alerts[2]["resolved_at"] is not None
    assert "secret" not in alerts[1]
    # CodeRabbit finding on PR #352: url must be None (not a fake ""), since
    # security_alerts doesn't store GitHub's html_url.
    assert alerts[1]["url"] is None


def test_secret_scanning_falls_back_to_live_when_aggregate_has_no_rows(connected_client, db, acme_org_with_installation):
    # security_alerts has no backfill/sync-cursor -- only webhook events populate it, so an
    # empty aggregate result is ambiguous (genuinely no alerts vs. not-yet-ingested) and must
    # not be trusted as authoritative (CodeRabbit finding on PR #356).
    with patch("src.routers.security.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = [
            {"number": 7, "state": "open", "secret_type": "aws_access_key_id", "created_at": "2026-08-01T00:00:00Z"},
        ]
        resp = connected_client.get("/me/repos/acme/demo/secret-scanning", headers={"X-GitHub-Token": "ghp_test"})
        mock_client.return_value.request.assert_called_once()

    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "github"
    assert [a["number"] for a in body["alerts"]] == [7]
