"""Tests for org-scoped and personal installation endpoints."""

import json
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from src.core.auth import UserOut, require_auth
from src.core.db import AuditLog, User, get_db
from src.repositories import installation_repo, org_membership_repo, org_repo
from src.routers.installations import router as inst_router
from src.services import github_app

_OUTSIDER = UserOut(id=99999, email="outsider@e.com", name=None, is_workspace_admin=False)


def _mock_installation(account_login: str, account_type: str):
    return patch(
        "src.routers.installations.github_app.get_installation",
        return_value={"account": {"login": account_login, "type": account_type}},
    )


def _make_user(db, email: str, github_login: str | None = None) -> UserOut:
    user = User(email=email, name=None, password_hash=None, is_workspace_admin=False, github_login=github_login)
    db.add(user)
    db.commit()
    db.refresh(user)
    return UserOut(id=user.id, email=user.email, name=user.name, is_workspace_admin=False)


def _client(db, user):
    app = FastAPI()
    app.include_router(inst_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_auth] = lambda: user
    # Issue #330: overriding require_auth for tests bypasses its real body, including the
    # SET app.user_id side effect (src.core.db.set_session_user) RLS's self-access clauses
    # (migration 0031) depend on -- set it here directly so tests exercise the same session
    # context a real authenticated request would have.
    db.execute(text(f"SET app.user_id = {user.id}"))
    return TestClient(app)


@pytest.fixture()
def acme_org(db):
    admin = _make_user(db, "admin@e.com")
    member = _make_user(db, "member@e.com")
    org = org_repo.get_or_create(db, github_login="acme")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=admin.id, role="admin")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=member.id, role="member")
    return {"org": org, "admin": admin, "member": member}


def test_list_org_installations_empty(db, acme_org):
    resp = _client(db, acme_org["admin"]).get("/orgs/acme/installations")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_org_installations_returns_rows(db, acme_org):
    installation_repo.create(
        db,
        account_login="acme",
        account_type="Organization",
        auth_mode="app",
        installation_id=42,
        org_id=acme_org["org"].id,
    )
    resp = _client(db, acme_org["member"]).get("/orgs/acme/installations")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["account_login"] == "acme"


def test_list_org_installations_outsider_forbidden(db, acme_org):
    resp = _client(db, _OUTSIDER).get("/orgs/acme/installations")
    assert resp.status_code == 403


def test_list_org_installations_requires_auth(db, acme_org):
    app = FastAPI()
    app.include_router(inst_router)
    app.dependency_overrides[get_db] = lambda: db
    resp = TestClient(app).get("/orgs/acme/installations")
    assert resp.status_code == 401


def test_sync_org_installation_requires_admin(db, acme_org):
    resp = _client(db, acme_org["member"]).post(
        "/orgs/acme/installations/sync",
        json={"account_login": "acme", "account_type": "Organization", "installation_id": 7},
    )
    assert resp.status_code == 403


def test_sync_org_installation_admin_ok(db, acme_org):
    with _mock_installation("acme", "Organization"):
        resp = _client(db, acme_org["admin"]).post(
            "/orgs/acme/installations/sync",
            json={"account_login": "acme", "account_type": "Organization", "installation_id": 7},
        )
    assert resp.status_code == 200
    assert resp.json()["synced"] is True


def test_sync_org_installation_writes_audit_log(db, acme_org):
    # Regression test: connecting a GitHub App installation used to write no audit
    # entry at all, so the Audit page stayed empty even for a workspace admin who'd
    # genuinely connected an org.
    with _mock_installation("acme", "Organization"):
        _client(db, acme_org["admin"]).post(
            "/orgs/acme/installations/sync",
            json={"account_login": "acme", "account_type": "Organization", "installation_id": 7},
        )
    logs = db.query(AuditLog).filter(AuditLog.action == "installation.connected").all()
    assert len(logs) == 1
    assert logs[0].target == "acme"
    assert logs[0].actor == acme_org["admin"].email
    payload = json.loads(logs[0].payload)
    assert payload == {"account_type": "Organization", "installation_id": 7}


def test_personal_installations_scoped_to_self(db):
    me = _make_user(db, "shabnam@e.com")
    someone_else = _make_user(db, "someoneelse@e.com")
    installation_repo.create(
        db, account_login="shabnam", account_type="User", auth_mode="app", installation_id=1, owner_user_id=me.id
    )
    installation_repo.create(
        db,
        account_login="someoneelse",
        account_type="User",
        auth_mode="app",
        installation_id=2,
        owner_user_id=someone_else.id,
    )
    resp = _client(db, me).get("/me/installations")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["account_login"] == "shabnam"


def test_sync_personal_installation(db):
    me = _make_user(db, "shabnam@e.com", github_login="shabnam")
    with _mock_installation("shabnam", "User"):
        resp = _client(db, me).post(
            "/me/installations/sync",
            json={"account_login": "shabnam", "account_type": "User", "installation_id": 3},
        )
    assert resp.status_code == 200
    assert resp.json()["synced"] is True


def test_sync_personal_installation_writes_audit_log(db):
    me = _make_user(db, "shabnam@e.com", github_login="shabnam")
    with _mock_installation("shabnam", "User"):
        _client(db, me).post(
            "/me/installations/sync",
            json={"account_login": "shabnam", "account_type": "User", "installation_id": 3},
        )
    logs = db.query(AuditLog).filter(AuditLog.action == "installation.connected.personal").all()
    assert len(logs) == 1
    assert logs[0].target == "shabnam"
    assert logs[0].actor == me.email
    payload = json.loads(logs[0].payload)
    assert payload == {"account_type": "User", "installation_id": 3}


def test_sync_personal_installation_requires_linked_github_account(db):
    me = _make_user(db, "unlinked@e.com")
    resp = _client(db, me).post(
        "/me/installations/sync",
        json={"account_login": "someone-else", "account_type": "User", "installation_id": 3},
    )
    assert resp.status_code == 403


def test_sync_personal_installation_login_mismatch_forbidden(db):
    me = _make_user(db, "shabnam@e.com", github_login="shabnam")
    resp = _client(db, me).post(
        "/me/installations/sync",
        json={"account_login": "someone-else", "account_type": "User", "installation_id": 3},
    )
    assert resp.status_code == 403


def test_sync_org_installation_upserts_existing_row(db, acme_org):
    client = _client(db, acme_org["admin"])
    payload = {"account_login": "acme", "account_type": "Organization", "installation_id": 7}
    with _mock_installation("acme", "Organization"):
        assert client.post("/orgs/acme/installations/sync", json=payload).status_code == 200
        payload["installation_id"] = 8
        assert client.post("/orgs/acme/installations/sync", json=payload).status_code == 200
    rows = client.get("/orgs/acme/installations").json()
    assert len(rows) == 1
    assert rows[0]["installation_id"] == 8


def test_sync_org_installation_rejects_installation_id_owned_by_a_different_account(db, acme_org):
    with _mock_installation("someone-else", "Organization"):
        resp = _client(db, acme_org["admin"]).post(
            "/orgs/acme/installations/sync",
            json={"account_login": "acme", "account_type": "Organization", "installation_id": 7},
        )
    assert resp.status_code == 422
    assert installation_repo.list_for_org(db, org_id=acme_org["org"].id) == []


def test_sync_org_installation_rejects_nonexistent_installation_id(db, acme_org):
    response = httpx.Response(404, request=httpx.Request("GET", "https://api.github.com/app/installations/7"))
    with patch(
        "src.routers.installations.github_app.get_installation",
        side_effect=httpx.HTTPStatusError("not found", request=response.request, response=response),
    ):
        resp = _client(db, acme_org["admin"]).post(
            "/orgs/acme/installations/sync",
            json={"account_login": "acme", "account_type": "Organization", "installation_id": 7},
        )
    assert resp.status_code == 422
    assert installation_repo.list_for_org(db, org_id=acme_org["org"].id) == []


def test_sync_org_installation_returns_503_when_app_not_configured(db, acme_org):
    with patch(
        "src.routers.installations.github_app.get_installation",
        side_effect=github_app.GitHubAppNotConfigured("not configured"),
    ):
        resp = _client(db, acme_org["admin"]).post(
            "/orgs/acme/installations/sync",
            json={"account_login": "acme", "account_type": "Organization", "installation_id": 7},
        )
    assert resp.status_code == 503
    assert installation_repo.list_for_org(db, org_id=acme_org["org"].id) == []


def test_sync_org_installation_returns_400_on_other_github_api_error(db, acme_org):
    response = httpx.Response(500, request=httpx.Request("GET", "https://api.github.com/app/installations/7"))
    with patch(
        "src.routers.installations.github_app.get_installation",
        side_effect=httpx.HTTPStatusError("server error", request=response.request, response=response),
    ):
        resp = _client(db, acme_org["admin"]).post(
            "/orgs/acme/installations/sync",
            json={"account_login": "acme", "account_type": "Organization", "installation_id": 7},
        )
    assert resp.status_code == 400
    assert installation_repo.list_for_org(db, org_id=acme_org["org"].id) == []


def test_sync_org_installation_returns_503_on_github_network_error(db, acme_org):
    with patch(
        "src.routers.installations.github_app.get_installation",
        side_effect=httpx.ConnectError("connection refused"),
    ):
        resp = _client(db, acme_org["admin"]).post(
            "/orgs/acme/installations/sync",
            json={"account_login": "acme", "account_type": "Organization", "installation_id": 7},
        )
    assert resp.status_code == 503
    assert installation_repo.list_for_org(db, org_id=acme_org["org"].id) == []


def test_sync_org_installation_bootstraps_new_org_for_live_github_admin(db):
    """No Clevis Org/OrgMembership exists yet for 'acme' -- this is the org's first-ever
    connection. The caller has a linked GitHub account and the App's installation
    reports them as a live GitHub org admin, so the sync should create the Org +
    admin OrgMembership itself instead of 404ing."""
    me = _make_user(db, "founder@e.com", github_login="founder")
    with (
        _mock_installation("acme", "Organization"),
        patch("src.routers.installations.github_app.get_installation_token", return_value="itok"),
        patch("src.routers.installations.github_app.get_org_membership_role", return_value="admin") as mock_role,
    ):
        resp = _client(db, me).post(
            "/orgs/acme/installations/sync",
            json={"account_login": "acme", "account_type": "Organization", "installation_id": 7},
        )
    assert resp.status_code == 200
    assert resp.json()["synced"] is True
    mock_role.assert_called_once_with("itok", "acme", "founder")
    org = org_repo.get_by_login(db, "acme")
    assert org is not None
    membership = org_membership_repo.get(db, org.id, me.id)
    assert membership is not None
    assert membership.role == "admin"


def test_bootstrap_org_admin_advisory_lock_serializes_concurrent_holders(_engine):
    # Regression test for #249: two concurrent installation syncs for the same org_login
    # (different installation_id's) both live-verify the caller as admin before either
    # commits its Org/OrgMembership rows -- only a lock closes that window, same pattern
    # as auth.py's /auth/setup lock (test_setup_advisory_lock_serializes_concurrent_holders).
    # Verifies the lock primitive itself: a second connection can't acquire the same
    # hashtext(org_login) key while the first transaction holds it, and can once released.
    from sqlalchemy import text

    with _engine.connect() as conn1, _engine.connect() as conn2:
        conn1.begin()
        conn2.begin()
        try:
            got1 = conn1.execute(
                text("SELECT pg_try_advisory_xact_lock(hashtext(:org_login))"), {"org_login": "acme"}
            ).scalar()
            got2 = conn2.execute(
                text("SELECT pg_try_advisory_xact_lock(hashtext(:org_login))"), {"org_login": "acme"}
            ).scalar()
            assert got1 is True
            assert got2 is False

            conn1.commit()  # releases conn1's advisory lock

            got2_retry = conn2.execute(
                text("SELECT pg_try_advisory_xact_lock(hashtext(:org_login))"), {"org_login": "acme"}
            ).scalar()
            assert got2_retry is True
        finally:
            conn2.rollback()


def test_sync_org_installation_rejects_live_non_admin(db):
    """The org already exists in Clevis (someone else connected it), the caller has no
    local membership, and the live GitHub check says they're a "member" not "admin" --
    must not bootstrap an admin membership for them."""
    org_repo.get_or_create(db, github_login="acme")
    me = _make_user(db, "regular@e.com", github_login="regular")
    with (
        _mock_installation("acme", "Organization"),
        patch("src.routers.installations.github_app.get_installation_token", return_value="itok"),
        patch("src.routers.installations.github_app.get_org_membership_role", return_value="member"),
    ):
        resp = _client(db, me).post(
            "/orgs/acme/installations/sync",
            json={"account_login": "acme", "account_type": "Organization", "installation_id": 7},
        )
    assert resp.status_code == 403
    org = org_repo.get_by_login(db, "acme")
    assert org_membership_repo.get(db, org.id, me.id) is None


def test_sync_org_installation_requires_installation_id_to_bootstrap(db):
    me = _make_user(db, "founder@e.com", github_login="founder")
    resp = _client(db, me).post(
        "/orgs/acme/installations/sync",
        json={"account_login": "acme", "account_type": "Organization"},
    )
    assert resp.status_code == 404
    assert org_repo.get_by_login(db, "acme") is None


def test_sync_org_installation_rejects_account_login_org_login_mismatch(db, acme_org):
    resp = _client(db, acme_org["admin"]).post(
        "/orgs/acme/installations/sync",
        json={"account_login": "widgets-inc", "account_type": "Organization", "installation_id": 7},
    )
    assert resp.status_code == 403


def test_sync_org_installation_bootstrap_returns_503_when_app_not_configured(db):
    me = _make_user(db, "founder@e.com", github_login="founder")
    with (
        _mock_installation("acme", "Organization"),
        patch(
            "src.routers.installations.github_app.get_installation_token",
            side_effect=github_app.GitHubAppNotConfigured("not configured"),
        ),
    ):
        resp = _client(db, me).post(
            "/orgs/acme/installations/sync",
            json={"account_login": "acme", "account_type": "Organization", "installation_id": 7},
        )
    assert resp.status_code == 503
    assert org_repo.get_by_login(db, "acme") is None


def test_sync_org_installation_bootstrap_returns_400_on_other_github_api_error(db):
    me = _make_user(db, "founder@e.com", github_login="founder")
    response = httpx.Response(500, request=httpx.Request("GET", "https://api.github.com/orgs/acme/memberships/founder"))
    with (
        _mock_installation("acme", "Organization"),
        patch(
            "src.routers.installations.github_app.get_installation_token",
            side_effect=httpx.HTTPStatusError("server error", request=response.request, response=response),
        ),
    ):
        resp = _client(db, me).post(
            "/orgs/acme/installations/sync",
            json={"account_login": "acme", "account_type": "Organization", "installation_id": 7},
        )
    assert resp.status_code == 400
    assert org_repo.get_by_login(db, "acme") is None


def test_sync_org_installation_bootstrap_returns_503_on_github_network_error(db):
    me = _make_user(db, "founder@e.com", github_login="founder")
    with (
        _mock_installation("acme", "Organization"),
        patch(
            "src.routers.installations.github_app.get_installation_token",
            side_effect=httpx.ConnectError("connection refused"),
        ),
    ):
        resp = _client(db, me).post(
            "/orgs/acme/installations/sync",
            json={"account_login": "acme", "account_type": "Organization", "installation_id": 7},
        )
    assert resp.status_code == 503
    assert org_repo.get_by_login(db, "acme") is None


def test_sync_org_installation_requires_admin_unlinked_github_account_no_network_call(db, acme_org):
    """A non-admin member with no linked GitHub account can't be live-verified at all --
    must fail fast with 403 and never attempt a GitHub call."""
    with patch("src.routers.installations.github_app.get_installation_token") as mock_token:
        resp = _client(db, acme_org["member"]).post(
            "/orgs/acme/installations/sync",
            json={"account_login": "acme", "account_type": "Organization", "installation_id": 7},
        )
    assert resp.status_code == 403
    mock_token.assert_not_called()


def test_sync_org_installation_skips_verification_when_installation_id_omitted(db, acme_org):
    with patch("src.routers.installations.github_app.get_installation") as mock_get:
        resp = _client(db, acme_org["admin"]).post(
            "/orgs/acme/installations/sync",
            json={"account_login": "acme", "account_type": "Organization"},
        )
    assert resp.status_code == 200
    mock_get.assert_not_called()


def test_sync_personal_installation_rejects_installation_id_owned_by_a_different_account(db):
    me = _make_user(db, "shabnam@e.com", github_login="shabnam")
    with _mock_installation("someone-else", "User"):
        resp = _client(db, me).post(
            "/me/installations/sync",
            json={"account_login": "shabnam", "account_type": "User", "installation_id": 3},
        )
    assert resp.status_code == 422
    assert installation_repo.list_for_user(db, owner_user_id=me.id) == []


def test_sync_personal_installation_rejects_nonexistent_installation_id(db):
    me = _make_user(db, "shabnam@e.com", github_login="shabnam")
    response = httpx.Response(404, request=httpx.Request("GET", "https://api.github.com/app/installations/3"))
    with patch(
        "src.routers.installations.github_app.get_installation",
        side_effect=httpx.HTTPStatusError("not found", request=response.request, response=response),
    ):
        resp = _client(db, me).post(
            "/me/installations/sync",
            json={"account_login": "shabnam", "account_type": "User", "installation_id": 3},
        )
    assert resp.status_code == 422
    assert installation_repo.list_for_user(db, owner_user_id=me.id) == []


def test_sync_personal_installation_returns_503_when_app_not_configured(db):
    me = _make_user(db, "shabnam@e.com", github_login="shabnam")
    with patch(
        "src.routers.installations.github_app.get_installation",
        side_effect=github_app.GitHubAppNotConfigured("not configured"),
    ):
        resp = _client(db, me).post(
            "/me/installations/sync",
            json={"account_login": "shabnam", "account_type": "User", "installation_id": 3},
        )
    assert resp.status_code == 503
    assert installation_repo.list_for_user(db, owner_user_id=me.id) == []


def test_sync_personal_installation_skips_verification_when_installation_id_omitted(db):
    me = _make_user(db, "shabnam@e.com", github_login="shabnam")
    with patch("src.routers.installations.github_app.get_installation") as mock_get:
        resp = _client(db, me).post(
            "/me/installations/sync",
            json={"account_login": "shabnam", "account_type": "User"},
        )
    assert resp.status_code == 200
    mock_get.assert_not_called()


def test_lookup_installation_returns_account(db):
    me = _make_user(db, "shabnam@e.com", github_login="shabnam")
    with _mock_installation("shabnam", "User"):
        resp = _client(db, me).get("/me/installations/lookup/3")
    assert resp.status_code == 200
    assert resp.json() == {"account_login": "shabnam", "account_type": "User"}


def test_lookup_installation_org_account(db):
    me = _make_user(db, "shabnam@e.com", github_login="shabnam")
    with _mock_installation("acme", "Organization"):
        resp = _client(db, me).get("/me/installations/lookup/7")
    assert resp.status_code == 200
    assert resp.json() == {"account_login": "acme", "account_type": "Organization"}


def test_lookup_installation_requires_auth(db):
    app = FastAPI()
    app.include_router(inst_router)
    app.dependency_overrides[get_db] = lambda: db
    resp = TestClient(app).get("/me/installations/lookup/3")
    assert resp.status_code == 401


def test_lookup_installation_rejects_nonexistent_installation_id(db):
    me = _make_user(db, "shabnam@e.com", github_login="shabnam")
    response = httpx.Response(404, request=httpx.Request("GET", "https://api.github.com/app/installations/3"))
    with patch(
        "src.routers.installations.github_app.get_installation",
        side_effect=httpx.HTTPStatusError("not found", request=response.request, response=response),
    ):
        resp = _client(db, me).get("/me/installations/lookup/3")
    assert resp.status_code == 422


def test_lookup_installation_returns_503_when_app_not_configured(db):
    me = _make_user(db, "shabnam@e.com", github_login="shabnam")
    with patch(
        "src.routers.installations.github_app.get_installation",
        side_effect=github_app.GitHubAppNotConfigured("not configured"),
    ):
        resp = _client(db, me).get("/me/installations/lookup/3")
    assert resp.status_code == 503
