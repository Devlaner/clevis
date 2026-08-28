"""Tests for permission-audit and inactive-members routes (docs/plan.md Phase 18)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from src.core.auth import UserOut, require_auth
from src.core.db import User, get_db
from src.repositories import installation_repo, org_membership_repo, org_repo
from src.routers.collab import router as collab_router

_ADMIN = UserOut(id=1, email="admin@example.com", name=None, is_workspace_admin=False)


@pytest.fixture()
def acme_org(db):
    user = User(id=_ADMIN.id, email=_ADMIN.email, name=None, password_hash=None, is_workspace_admin=False)
    db.add(user)
    db.commit()
    org = org_repo.get_or_create(db, github_login="acme")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=user.id, role="member")
    return org


@pytest.fixture()
def acme_org_with_installation(db, acme_org):
    installation_repo.create(
        db, account_login="acme", account_type="Organization", auth_mode="app", installation_id=7, org_id=acme_org.id
    )
    return acme_org


@pytest.fixture()
def client(db, acme_org):
    app = FastAPI()
    app.include_router(collab_router)
    app.dependency_overrides[require_auth] = lambda: _ADMIN
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def _insert_org_member(db, tenant_id, *, login, avatar_url="", role="member"):
    db.execute(text(f"SET app.tenant_id = {int(tenant_id)}"))
    db.execute(
        text(
            "INSERT INTO org_members (tenant_id, login, avatar_url, role, added_at) "
            "VALUES (:tenant_id, :login, :avatar_url, :role, :added_at)"
        ),
        {"tenant_id": tenant_id, "login": login, "avatar_url": avatar_url, "role": role, "added_at": datetime.now(timezone.utc)},
    )
    db.commit()


def _seed_membership_cursor(db, tenant_id, org_login="acme"):
    db.execute(text(f"SET app.tenant_id = {int(tenant_id)}"))
    db.execute(
        text(
            "INSERT INTO org_membership_sync_cursors (tenant_id, org_login, last_synced_at) "
            "VALUES (:tenant_id, :org_login, :last_synced_at)"
        ),
        {"tenant_id": tenant_id, "org_login": org_login, "last_synced_at": datetime.now(timezone.utc)},
    )
    db.commit()


def _seed_activity_cursor(db, tenant_id, account_login="acme", account_type="Organization"):
    db.execute(text(f"SET app.tenant_id = {int(tenant_id)}"))
    db.execute(
        text(
            "INSERT INTO activity_sync_cursors (tenant_id, account_login, account_type, last_synced_at) "
            "VALUES (:tenant_id, :account_login, :account_type, :last_synced_at)"
        ),
        {"tenant_id": tenant_id, "account_login": account_login, "account_type": account_type, "last_synced_at": datetime.now(timezone.utc)},
    )
    db.commit()


def _seed_both_cursors(db, tenant_id):
    _seed_membership_cursor(db, tenant_id)
    _seed_activity_cursor(db, tenant_id)


def _insert_push_event(db, tenant_id, *, repo, actor, occurred_at, delivery_id):
    db.execute(text(f"SET app.tenant_id = {int(tenant_id)}"))
    db.execute(
        text(
            "INSERT INTO repo_events (tenant_id, delivery_id, event_type, actor, actor_avatar, repo, summary, occurred_at) "
            "VALUES (:tenant_id, :delivery_id, 'push', :actor, '', :repo, 'pushed 1 commit', :occurred_at)"
        ),
        {"tenant_id": tenant_id, "delivery_id": delivery_id, "actor": actor, "repo": repo, "occurred_at": occurred_at},
    )
    db.commit()


def test_permission_audit_no_token_returns_400(client):
    resp = client.get("/github/orgs/acme/permission-audit")
    assert resp.status_code == 400


def test_permission_audit_flags_outside_collaborator_with_write_access(client):
    def _paginated_side_effect(path, params=None):
        if path == "/orgs/acme/members":
            return [{"login": "alice"}]
        if path == "/orgs/acme/outside_collaborators":
            return [{"login": "bob"}]
        if path == "/orgs/acme/repos":
            return [{"name": "api"}]
        if path == "/repos/acme/api/collaborators":
            return [
                {"login": "alice", "avatar_url": "", "permissions": {"pull": True, "push": True, "admin": True}},
                {"login": "bob", "avatar_url": "", "permissions": {"pull": True, "push": True, "admin": False}},
            ]
        return []

    with patch("src.routers.collab.GitHubClient") as mock_client:
        mock_client.return_value.request_paginated.side_effect = _paginated_side_effect
        resp = client.get("/github/orgs/acme/permission-audit", headers={"X-GitHub-Token": "ghp_test"})

    assert resp.status_code == 200
    body = resp.json()
    row = body["repos"][0]["collaborators"]
    alice = next(c for c in row if c["login"] == "alice")
    bob = next(c for c in row if c["login"] == "bob")
    assert alice["permission"] == "admin"
    assert alice["is_outside_collaborator"] is False
    assert bob["permission"] == "write"
    assert bob["is_outside_collaborator"] is True
    assert body["risk_summary"]["outside_with_write_or_admin"] == 1
    assert body["risk_summary"]["members_with_admin"] == 1
    assert body["risk_summary"]["total_outside_collaborators"] == 1


def test_permission_audit_one_bad_repo_does_not_blank_others(client):
    def _paginated_side_effect(path, params=None):
        if path == "/orgs/acme/members":
            return [{"login": "alice"}]
        if path == "/orgs/acme/outside_collaborators":
            return []
        if path == "/orgs/acme/repos":
            return [{"name": "repo-bad"}, {"name": "repo-good"}]
        if path == "/repos/acme/repo-bad/collaborators":
            raise httpx.RequestError("boom")
        if path == "/repos/acme/repo-good/collaborators":
            return [{"login": "alice", "avatar_url": "", "permissions": {"pull": True}}]
        return []

    with patch("src.routers.collab.GitHubClient") as mock_client:
        mock_client.return_value.request_paginated.side_effect = _paginated_side_effect
        resp = client.get("/github/orgs/acme/permission-audit", headers={"X-GitHub-Token": "ghp_test"})

    assert resp.status_code == 200
    repos_by_name = {r["repo"]: r for r in resp.json()["repos"]}
    assert repos_by_name["repo-bad"]["collaborators"] == []
    assert len(repos_by_name["repo-good"]["collaborators"]) == 1


def test_permission_audit_skips_collaborator_entries_missing_login(client):
    def _paginated_side_effect(path, params=None):
        if path == "/orgs/acme/members":
            return [{"login": "alice"}]
        if path == "/orgs/acme/outside_collaborators":
            return []
        if path == "/orgs/acme/repos":
            return [{"name": "api"}]
        if path == "/repos/acme/api/collaborators":
            return [
                {"permissions": {"pull": True}},  # malformed: no "login"
                {"login": "alice", "avatar_url": "", "permissions": {"pull": True}},
            ]
        return []

    with patch("src.routers.collab.GitHubClient") as mock_client:
        mock_client.return_value.request_paginated.side_effect = _paginated_side_effect
        resp = client.get("/github/orgs/acme/permission-audit", headers={"X-GitHub-Token": "ghp_test"})

    assert resp.status_code == 200
    collaborators = resp.json()["repos"][0]["collaborators"]
    assert len(collaborators) == 1
    assert collaborators[0]["login"] == "alice"


def test_permission_audit_outsider_forbidden(db):
    org_repo.get_or_create(db, github_login="acme")
    app = FastAPI()
    app.include_router(collab_router)
    app.dependency_overrides[require_auth] = lambda: UserOut(id=999, email="outsider@example.com", name=None, is_workspace_admin=False)
    app.dependency_overrides[get_db] = lambda: db
    resp = TestClient(app).get("/github/orgs/acme/permission-audit")
    assert resp.status_code == 403


def test_inactive_members_from_ingested_db_flags_member_with_no_push_events(db, acme_org_with_installation):
    _seed_both_cursors(db, acme_org_with_installation.tenant_id)
    _insert_org_member(db, acme_org_with_installation.tenant_id, login="alice", role="member")
    app = FastAPI()
    app.include_router(collab_router)
    app.dependency_overrides[require_auth] = lambda: _ADMIN
    app.dependency_overrides[get_db] = lambda: db

    with patch("src.routers.collab.GitHubClient") as mock_client:
        resp = TestClient(app).get("/github/orgs/acme/inactive-members?days=30")

    assert resp.status_code == 200
    mock_client.assert_not_called()
    body = resp.json()
    assert len(body["members"]) == 1
    assert body["members"][0]["login"] == "alice"
    assert body["members"][0]["last_commit_days_ago"] is None
    assert body["members"][0]["last_commit_repo"] is None


def test_inactive_members_from_ingested_db_excludes_recently_active_member(db, acme_org_with_installation):
    _seed_both_cursors(db, acme_org_with_installation.tenant_id)
    _insert_org_member(db, acme_org_with_installation.tenant_id, login="alice", role="member")
    _insert_push_event(
        db,
        acme_org_with_installation.tenant_id,
        repo="acme/api",
        actor="alice",
        occurred_at=datetime.now(timezone.utc) - timedelta(days=1),
        delivery_id="d1",
    )
    app = FastAPI()
    app.include_router(collab_router)
    app.dependency_overrides[require_auth] = lambda: _ADMIN
    app.dependency_overrides[get_db] = lambda: db

    resp = TestClient(app).get("/github/orgs/acme/inactive-members?days=30")

    assert resp.status_code == 200
    assert resp.json()["members"] == []


def test_inactive_members_from_ingested_db_flags_member_with_only_stale_push(db, acme_org_with_installation):
    _seed_both_cursors(db, acme_org_with_installation.tenant_id)
    _insert_org_member(db, acme_org_with_installation.tenant_id, login="alice", role="member")
    _insert_push_event(
        db,
        acme_org_with_installation.tenant_id,
        repo="acme/api",
        actor="alice",
        occurred_at=datetime.now(timezone.utc) - timedelta(days=60),
        delivery_id="d2",
    )
    app = FastAPI()
    app.include_router(collab_router)
    app.dependency_overrides[require_auth] = lambda: _ADMIN
    app.dependency_overrides[get_db] = lambda: db

    resp = TestClient(app).get("/github/orgs/acme/inactive-members?days=30")

    assert resp.status_code == 200
    body = resp.json()
    assert body["members"][0]["login"] == "alice"
    assert body["members"][0]["last_commit_repo"] == "acme/api"
    assert body["members"][0]["last_commit_days_ago"] >= 59
    assert body["sampled_repos"] == ["acme/api"]


def test_inactive_members_from_ingested_db_uses_most_recent_push_across_all_repos(db, acme_org_with_installation):
    # Unlike the live path's bounded _MAX_REPOS_SAMPLED_FOR_ACTIVITY sample, the ingested path
    # considers every repo with a push event -- a recent push to a second repo must still
    # exclude the member even though their first/older repo alone would flag them.
    tenant_id = acme_org_with_installation.tenant_id
    _seed_both_cursors(db, tenant_id)
    _insert_org_member(db, tenant_id, login="alice", role="member")
    _insert_push_event(
        db, tenant_id, repo="acme/old-repo", actor="alice", occurred_at=datetime.now(timezone.utc) - timedelta(days=90), delivery_id="d3"
    )
    _insert_push_event(
        db, tenant_id, repo="acme/new-repo", actor="alice", occurred_at=datetime.now(timezone.utc) - timedelta(days=1), delivery_id="d4"
    )
    app = FastAPI()
    app.include_router(collab_router)
    app.dependency_overrides[require_auth] = lambda: _ADMIN
    app.dependency_overrides[get_db] = lambda: db

    resp = TestClient(app).get("/github/orgs/acme/inactive-members?days=30")

    assert resp.status_code == 200
    assert resp.json()["members"] == []


def test_inactive_members_falls_back_to_live_when_only_one_cursor_has_synced(db, acme_org_with_installation):
    # CodeRabbit finding on PR #355: this endpoint needs BOTH org_members (membership cursor)
    # and repo_events (activity cursor) to be trustworthy -- seeding only one must still fall
    # back to the live path rather than serve a half-ingested answer.
    _seed_membership_cursor(db, acme_org_with_installation.tenant_id)
    _insert_org_member(db, acme_org_with_installation.tenant_id, login="alice", role="member")

    def _paginated_side_effect(path, params=None):
        if path == "/orgs/acme/members" and params == {"role": "admin"}:
            return []
        if path == "/orgs/acme/members":
            return [{"login": "alice", "avatar_url": ""}]
        if path == "/orgs/acme/repos":
            return [{"name": "api"}]
        return []

    app = FastAPI()
    app.include_router(collab_router)
    app.dependency_overrides[require_auth] = lambda: _ADMIN
    app.dependency_overrides[get_db] = lambda: db

    with patch("src.routers.collab.resolve_org_token", return_value="ghp_test"), patch(
        "src.routers.collab.GitHubClient"
    ) as mock_client:
        mock_client.return_value.request_paginated.side_effect = _paginated_side_effect
        mock_client.return_value.request.return_value = []
        resp = TestClient(app).get("/github/orgs/acme/inactive-members?days=30")

    assert resp.status_code == 200
    mock_client.return_value.request_paginated.assert_any_call("/orgs/acme/members", params={"role": "admin"})


def test_inactive_members_no_token_returns_400(client):
    resp = client.get("/github/orgs/acme/inactive-members")
    assert resp.status_code == 400


def test_inactive_members_flags_member_with_no_recent_commits(client):
    def _paginated_side_effect(path, params=None):
        if path == "/orgs/acme/members" and params == {"role": "admin"}:
            return []
        if path == "/orgs/acme/members":
            return [{"login": "alice", "avatar_url": ""}]
        if path == "/orgs/acme/repos":
            return [{"name": "api"}]
        return []

    with patch("src.routers.collab.GitHubClient") as mock_client:
        mock_client.return_value.request_paginated.side_effect = _paginated_side_effect
        mock_client.return_value.request.return_value = []  # no commits found by alice
        resp = client.get("/github/orgs/acme/inactive-members?days=30", headers={"X-GitHub-Token": "ghp_test"})

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["members"]) == 1
    assert body["members"][0]["login"] == "alice"
    assert body["members"][0]["last_commit_days_ago"] is None
    assert body["sampled_repos"] == ["acme/api"]


def test_inactive_members_excludes_recently_active_member(client):
    from datetime import datetime, timedelta, timezone

    recent = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _paginated_side_effect(path, params=None):
        if path == "/orgs/acme/members" and params == {"role": "admin"}:
            return []
        if path == "/orgs/acme/members":
            return [{"login": "alice", "avatar_url": ""}]
        if path == "/orgs/acme/repos":
            return [{"name": "api"}]
        return []

    with patch("src.routers.collab.GitHubClient") as mock_client:
        mock_client.return_value.request_paginated.side_effect = _paginated_side_effect
        mock_client.return_value.request.return_value = [{"commit": {"author": {"date": recent}}}]
        resp = client.get("/github/orgs/acme/inactive-members?days=30", headers={"X-GitHub-Token": "ghp_test"})

    assert resp.status_code == 200
    assert resp.json()["members"] == []


def test_inactive_members_does_not_flag_a_member_whose_activity_could_not_be_verified(client):
    """A transient API failure on every sampled repo must not be conflated with a
    genuine 'zero commits found' answer -- an unverifiable member is excluded
    entirely rather than wrongly flagged inactive (a false access-risk signal)."""
    def _paginated_side_effect(path, params=None):
        if path == "/orgs/acme/members" and params == {"role": "admin"}:
            return []
        if path == "/orgs/acme/members":
            return [{"login": "alice", "avatar_url": ""}]
        if path == "/orgs/acme/repos":
            return [{"name": "api"}]
        return []

    with patch("src.routers.collab.GitHubClient") as mock_client:
        mock_client.return_value.request_paginated.side_effect = _paginated_side_effect
        mock_client.return_value.request.side_effect = httpx.RequestError("boom")
        resp = client.get("/github/orgs/acme/inactive-members?days=30", headers={"X-GitHub-Token": "ghp_test"})

    assert resp.status_code == 200
    assert resp.json()["members"] == []
