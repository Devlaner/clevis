"""Tests for the read-only GitHub org roster router (Phase 11 — Collaborators)."""

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
from src.routers.collab import router as collab_router

_ADMIN = UserOut(id=1, email="admin@example.com", name=None, is_workspace_admin=False)

_HTTP_ERROR = httpx.HTTPStatusError(
    "boom",
    request=httpx.Request("GET", "https://api.github.com/x"),
    response=httpx.Response(404, request=httpx.Request("GET", "https://api.github.com/x")),
)
_FORBIDDEN_ERROR = httpx.HTTPStatusError(
    "forbidden",
    request=httpx.Request("GET", "https://api.github.com/x"),
    response=httpx.Response(403, request=httpx.Request("GET", "https://api.github.com/x")),
)
_NOT_FOUND_ERROR = httpx.HTTPStatusError(
    "missing",
    request=httpx.Request("GET", "https://api.github.com/x"),
    response=httpx.Response(404, request=httpx.Request("GET", "https://api.github.com/x")),
)
_SERVER_ERROR = httpx.HTTPStatusError(
    "boom",
    request=httpx.Request("GET", "https://api.github.com/x"),
    response=httpx.Response(500, request=httpx.Request("GET", "https://api.github.com/x")),
)


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
def collab_client(db, acme_org):
    app = FastAPI()
    app.include_router(collab_router)
    app.dependency_overrides[require_auth] = lambda: _ADMIN
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def _insert_org_member(db, tenant_id, *, login, avatar_url="", role="member", two_factor_enabled=None):
    db.execute(text(f"SET app.tenant_id = {int(tenant_id)}"))
    db.execute(
        text(
            "INSERT INTO org_members (tenant_id, login, avatar_url, role, two_factor_enabled, added_at) "
            "VALUES (:tenant_id, :login, :avatar_url, :role, :two_factor_enabled, :added_at)"
        ),
        {
            "tenant_id": tenant_id,
            "login": login,
            "avatar_url": avatar_url,
            "role": role,
            "two_factor_enabled": two_factor_enabled,
            "added_at": datetime.now(timezone.utc),
        },
    )
    db.commit()


def _unknown_org_client(db):
    app = FastAPI()
    app.include_router(collab_router)
    app.dependency_overrides[require_auth] = lambda: _ADMIN
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def _non_member_client(db):
    org_repo.get_or_create(db, github_login="acme")
    app = FastAPI()
    app.include_router(collab_router)
    app.dependency_overrides[require_auth] = lambda: UserOut(
        id=99, email="outsider@example.com", name=None, is_workspace_admin=False
    )
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


_ALICE = {"login": "alice", "avatar_url": "https://avatars/alice.png", "site_admin": False}
_BOB = {"login": "bob", "avatar_url": "https://avatars/bob.png", "site_admin": False}
_CAROL = {"login": "carol", "avatar_url": "https://avatars/carol.png", "site_admin": False}


# ---------------------------------------------------------------------------
# members
# ---------------------------------------------------------------------------


def test_members_returns_role_annotated_list(collab_client):
    with patch("src.routers.collab.resolve_org_token", return_value="ghp_test"), patch(
        "src.routers.collab.GitHubClient"
    ) as mock_client:
        mock_client.return_value.request_paginated.side_effect = [
            [_ALICE],  # role=admin
            [_ALICE, _BOB],  # role=all
            [_BOB],  # 2fa_disabled
        ]
        resp = collab_client.get("/github/orgs/acme/members")
    assert resp.status_code == 200
    body = resp.json()
    members = {m["login"]: m for m in body["members"]}
    assert members["alice"]["role"] == "admin"
    assert members["bob"]["role"] == "member"
    assert members["alice"]["two_factor_enabled"] is True
    assert members["bob"]["two_factor_enabled"] is False
    assert body["two_factor_overlay_available"] is True


def test_members_role_filter_member_only_still_fetches_admin_set_for_annotation(collab_client):
    with patch("src.routers.collab.resolve_org_token", return_value="ghp_test"), patch(
        "src.routers.collab.GitHubClient"
    ) as mock_client:
        mock_client.return_value.request_paginated.side_effect = [
            [_ALICE],  # role=admin
            [_BOB],  # role=member
            [],  # 2fa_disabled
        ]
        resp = collab_client.get("/github/orgs/acme/members?role=member")
    assert resp.status_code == 200
    calls = mock_client.return_value.request_paginated.call_args_list
    assert calls[0].kwargs["params"] == {"role": "admin"}
    assert calls[1].kwargs["params"] == {"role": "member"}
    assert resp.json()["members"][0]["login"] == "bob"
    assert resp.json()["members"][0]["role"] == "member"


def test_members_role_filter_admin_reuses_admin_set_without_a_third_call(collab_client):
    with patch("src.routers.collab.resolve_org_token", return_value="ghp_test"), patch(
        "src.routers.collab.GitHubClient"
    ) as mock_client:
        mock_client.return_value.request_paginated.side_effect = [
            [_ALICE],  # role=admin (reused as target set too)
            [],  # 2fa_disabled
        ]
        resp = collab_client.get("/github/orgs/acme/members?role=admin")
    assert resp.status_code == 200
    assert mock_client.return_value.request_paginated.call_count == 2
    assert resp.json()["members"][0]["login"] == "alice"


def test_members_2fa_overlay_degrades_gracefully_on_403(collab_client):
    with patch("src.routers.collab.resolve_org_token", return_value="ghp_test"), patch(
        "src.routers.collab.GitHubClient"
    ) as mock_client:
        mock_client.return_value.request_paginated.side_effect = [
            [_ALICE],
            [_ALICE, _BOB],
            _FORBIDDEN_ERROR,
        ]
        resp = collab_client.get("/github/orgs/acme/members")
    assert resp.status_code == 200
    body = resp.json()
    assert body["two_factor_overlay_available"] is False
    assert all(m["two_factor_enabled"] is None for m in body["members"])


def test_members_2fa_overlay_degrades_gracefully_on_404(collab_client):
    with patch("src.routers.collab.resolve_org_token", return_value="ghp_test"), patch(
        "src.routers.collab.GitHubClient"
    ) as mock_client:
        mock_client.return_value.request_paginated.side_effect = [
            [_ALICE],
            [_ALICE],
            _NOT_FOUND_ERROR,
        ]
        resp = collab_client.get("/github/orgs/acme/members")
    assert resp.status_code == 200
    assert resp.json()["two_factor_overlay_available"] is False


def test_members_2fa_overlay_degrades_gracefully_on_other_http_errors(collab_client):
    # The overlay is optional context on top of member data that already succeeded --
    # any error fetching it (not just a 403/404 scope issue) should degrade rather
    # than fail the whole response.
    with patch("src.routers.collab.resolve_org_token", return_value="ghp_test"), patch(
        "src.routers.collab.GitHubClient"
    ) as mock_client:
        mock_client.return_value.request_paginated.side_effect = [
            [_ALICE],
            [_ALICE],
            _SERVER_ERROR,
        ]
        resp = collab_client.get("/github/orgs/acme/members")
    assert resp.status_code == 200
    body = resp.json()
    assert body["two_factor_overlay_available"] is False
    assert body["members"][0]["two_factor_enabled"] is None


def test_members_2fa_overlay_degrades_gracefully_on_network_error(collab_client):
    with patch("src.routers.collab.resolve_org_token", return_value="ghp_test"), patch(
        "src.routers.collab.GitHubClient"
    ) as mock_client:
        mock_client.return_value.request_paginated.side_effect = [
            [_ALICE],
            [_ALICE],
            httpx.RequestError("boom"),
        ]
        resp = collab_client.get("/github/orgs/acme/members")
    assert resp.status_code == 200
    assert resp.json()["two_factor_overlay_available"] is False


def test_members_reads_from_ingested_org_members_when_connected(db, acme_org_with_installation):
    _insert_org_member(
        db, acme_org_with_installation.tenant_id, login="alice", role="admin", two_factor_enabled=True
    )
    _insert_org_member(
        db, acme_org_with_installation.tenant_id, login="bob", role="member", two_factor_enabled=False
    )
    app = FastAPI()
    app.include_router(collab_router)
    app.dependency_overrides[require_auth] = lambda: _ADMIN
    app.dependency_overrides[get_db] = lambda: db

    with patch("src.routers.collab.GitHubClient") as mock_client:
        resp = TestClient(app).get("/github/orgs/acme/members")

    assert resp.status_code == 200
    mock_client.assert_not_called()
    body = resp.json()
    members = {m["login"]: m for m in body["members"]}
    assert members["alice"]["role"] == "admin"
    assert members["alice"]["two_factor_enabled"] is True
    assert members["bob"]["role"] == "member"
    assert members["bob"]["two_factor_enabled"] is False
    assert body["two_factor_overlay_available"] is True


def test_members_from_ingested_db_respects_role_filter(db, acme_org_with_installation):
    _insert_org_member(db, acme_org_with_installation.tenant_id, login="alice", role="admin")
    _insert_org_member(db, acme_org_with_installation.tenant_id, login="bob", role="member")
    app = FastAPI()
    app.include_router(collab_router)
    app.dependency_overrides[require_auth] = lambda: _ADMIN
    app.dependency_overrides[get_db] = lambda: db

    resp = TestClient(app).get("/github/orgs/acme/members?role=admin")

    assert resp.status_code == 200
    logins = [m["login"] for m in resp.json()["members"]]
    assert logins == ["alice"]


def test_members_from_ingested_db_overlay_unavailable_when_never_polled(db, acme_org_with_installation):
    # two_factor_enabled is NULL on every row until the reconciliation poll's first tick --
    # a brand-new installation whose only org_members rows came from the webhook path.
    _insert_org_member(db, acme_org_with_installation.tenant_id, login="alice", role="member", two_factor_enabled=None)
    app = FastAPI()
    app.include_router(collab_router)
    app.dependency_overrides[require_auth] = lambda: _ADMIN
    app.dependency_overrides[get_db] = lambda: db

    resp = TestClient(app).get("/github/orgs/acme/members")

    assert resp.status_code == 200
    body = resp.json()
    assert body["two_factor_overlay_available"] is False
    assert body["members"][0]["two_factor_enabled"] is None


def test_members_falls_back_to_client_supplied_token_header_when_no_installation(collab_client):
    with patch("src.routers.collab.GitHubClient") as mock_client:
        mock_client.return_value.request_paginated.side_effect = [[_ALICE], [_ALICE], []]
        resp = collab_client.get(
            "/github/orgs/acme/members", headers={"X-GitHub-Token": "ghp_client_supplied"}
        )
    assert resp.status_code == 200
    mock_client.assert_called_once_with("ghp_client_supplied")


def test_members_maps_github_request_error_to_503(collab_client):
    with patch("src.routers.collab.resolve_org_token", return_value="ghp_test"), patch(
        "src.routers.collab.GitHubClient"
    ) as mock_client:
        mock_client.return_value.request_paginated.side_effect = httpx.RequestError("boom")
        resp = collab_client.get("/github/orgs/acme/members")
    assert resp.status_code == 503


def test_members_no_installation_and_no_token_returns_400(collab_client):
    resp = collab_client.get("/github/orgs/acme/members")
    assert resp.status_code == 400


def test_members_unknown_org_returns_404(db):
    client = _unknown_org_client(db)
    resp = client.get("/github/orgs/does-not-exist/members")
    assert resp.status_code == 404


def test_members_non_member_forbidden(db):
    client = _non_member_client(db)
    resp = client.get("/github/orgs/acme/members")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# outside collaborators
# ---------------------------------------------------------------------------


def test_outside_collaborators_maps_repos_per_login(collab_client):
    # The per-repo collaborator fan-out runs concurrently (ThreadPoolExecutor), so
    # results must be keyed by request path rather than assumed call order.
    def fake_request_paginated(path, params=None):
        if path == "/orgs/acme/outside_collaborators":
            return [_CAROL]
        if path == "/orgs/acme/repos":
            return [{"name": "api"}, {"name": "worker"}]
        if path == "/repos/acme/api/collaborators":
            return [{"login": "carol", "avatar_url": "https://avatars/carol.png"}]
        if path == "/repos/acme/worker/collaborators":
            return []
        raise AssertionError(f"unexpected path {path}")

    with patch("src.routers.collab.resolve_org_token", return_value="ghp_test"), patch(
        "src.routers.collab.GitHubClient"
    ) as mock_client:
        mock_client.return_value.request_paginated.side_effect = fake_request_paginated
        resp = collab_client.get("/github/orgs/acme/outside_collaborators")
    assert resp.status_code == 200
    body = resp.json()
    assert body["collaborators"][0]["login"] == "carol"
    assert body["collaborators"][0]["repos"] == ["acme/api"]
    assert body["repos_scanned"] == 2
    assert body["repos_total"] == 2


def test_outside_collaborators_caps_repo_scan_and_reports_totals(collab_client):
    many_repos = [{"name": f"repo-{i}"} for i in range(60)]

    def fake_request_paginated(path, params=None):
        if path == "/orgs/acme/outside_collaborators":
            return [_CAROL]
        if path == "/orgs/acme/repos":
            return many_repos
        return []

    with patch("src.routers.collab.resolve_org_token", return_value="ghp_test"), patch(
        "src.routers.collab.GitHubClient"
    ) as mock_client:
        mock_client.return_value.request_paginated.side_effect = fake_request_paginated
        resp = collab_client.get("/github/orgs/acme/outside_collaborators")
    assert resp.status_code == 200
    body = resp.json()
    assert body["repos_scanned"] == 50
    assert body["repos_total"] == 60


def test_outside_collaborators_maps_github_error(collab_client):
    with patch("src.routers.collab.resolve_org_token", return_value="ghp_test"), patch(
        "src.routers.collab.GitHubClient"
    ) as mock_client:
        mock_client.return_value.request_paginated.side_effect = _HTTP_ERROR
        resp = collab_client.get("/github/orgs/acme/outside_collaborators")
    assert resp.status_code == 400


def test_outside_collaborators_maps_request_error(collab_client):
    with patch("src.routers.collab.resolve_org_token", return_value="ghp_test"), patch(
        "src.routers.collab.GitHubClient"
    ) as mock_client:
        mock_client.return_value.request_paginated.side_effect = httpx.RequestError("boom")
        resp = collab_client.get("/github/orgs/acme/outside_collaborators")
    assert resp.status_code == 503


def test_outside_collaborators_no_token_400(collab_client):
    resp = collab_client.get("/github/orgs/acme/outside_collaborators")
    assert resp.status_code == 400


def test_outside_collaborators_non_member_forbidden(db):
    client = _non_member_client(db)
    resp = client.get("/github/orgs/acme/outside_collaborators")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# invitations
# ---------------------------------------------------------------------------


def test_invitations_maps_fields_including_email_only_invites(collab_client):
    raw = [
        {
            "login": "dave",
            "email": None,
            "role": "direct_member",
            "created_at": "2026-07-10T00:00:00Z",
            "inviter": {"login": "alice"},
        },
        {
            "login": None,
            "email": "eve@example.com",
            "role": "direct_member",
            "created_at": "2026-07-11T00:00:00Z",
            "inviter": {"login": "alice"},
        },
    ]
    with patch("src.routers.collab.resolve_org_token", return_value="ghp_test"), patch(
        "src.routers.collab.GitHubClient"
    ) as mock_client:
        mock_client.return_value.request_paginated.return_value = raw
        resp = collab_client.get("/github/orgs/acme/invitations")
    assert resp.status_code == 200
    invitations = resp.json()["invitations"]
    assert invitations[0]["login"] == "dave"
    assert invitations[0]["inviter"] == "alice"
    assert invitations[1]["login"] is None
    assert invitations[1]["email"] == "eve@example.com"


def test_invitations_skips_malformed_entries_missing_created_at(collab_client):
    raw = [
        {"login": "dave", "email": None, "role": "direct_member", "created_at": "2026-07-10T00:00:00Z", "inviter": None},
        {"login": "malformed", "email": None, "role": "direct_member", "inviter": None},
    ]
    with patch("src.routers.collab.resolve_org_token", return_value="ghp_test"), patch(
        "src.routers.collab.GitHubClient"
    ) as mock_client:
        mock_client.return_value.request_paginated.return_value = raw
        resp = collab_client.get("/github/orgs/acme/invitations")
    assert resp.status_code == 200
    invitations = resp.json()["invitations"]
    assert len(invitations) == 1
    assert invitations[0]["login"] == "dave"


def test_invitations_maps_github_error(collab_client):
    with patch("src.routers.collab.resolve_org_token", return_value="ghp_test"), patch(
        "src.routers.collab.GitHubClient"
    ) as mock_client:
        mock_client.return_value.request_paginated.side_effect = _HTTP_ERROR
        resp = collab_client.get("/github/orgs/acme/invitations")
    assert resp.status_code == 400


def test_invitations_maps_request_error(collab_client):
    with patch("src.routers.collab.resolve_org_token", return_value="ghp_test"), patch(
        "src.routers.collab.GitHubClient"
    ) as mock_client:
        mock_client.return_value.request_paginated.side_effect = httpx.RequestError("boom")
        resp = collab_client.get("/github/orgs/acme/invitations")
    assert resp.status_code == 503


def test_invitations_no_token_400(collab_client):
    resp = collab_client.get("/github/orgs/acme/invitations")
    assert resp.status_code == 400


def test_invitations_non_member_forbidden(db):
    client = _non_member_client(db)
    resp = client.get("/github/orgs/acme/invitations")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# membership
# ---------------------------------------------------------------------------


def test_membership_returns_state_and_role(collab_client):
    with patch("src.routers.collab.resolve_org_token", return_value="ghp_test"), patch(
        "src.routers.collab.GitHubClient"
    ) as mock_client:
        mock_client.return_value.request.return_value = {"state": "active", "role": "member"}
        resp = collab_client.get("/github/orgs/acme/members/alice/membership")
    assert resp.status_code == 200
    assert resp.json() == {"state": "active", "role": "member"}
    mock_client.return_value.request.assert_called_once_with("GET", "/orgs/acme/members/alice/membership")


def test_membership_github_404_maps_to_400(collab_client):
    with patch("src.routers.collab.resolve_org_token", return_value="ghp_test"), patch(
        "src.routers.collab.GitHubClient"
    ) as mock_client:
        mock_client.return_value.request.side_effect = _NOT_FOUND_ERROR
        resp = collab_client.get("/github/orgs/acme/members/ghost/membership")
    assert resp.status_code == 400


def test_membership_request_error_maps_to_503(collab_client):
    with patch("src.routers.collab.resolve_org_token", return_value="ghp_test"), patch(
        "src.routers.collab.GitHubClient"
    ) as mock_client:
        mock_client.return_value.request.side_effect = httpx.RequestError("boom")
        resp = collab_client.get("/github/orgs/acme/members/alice/membership")
    assert resp.status_code == 503


def test_membership_no_token_400(collab_client):
    resp = collab_client.get("/github/orgs/acme/members/alice/membership")
    assert resp.status_code == 400


def test_membership_non_member_forbidden(db):
    client = _non_member_client(db)
    resp = client.get("/github/orgs/acme/members/alice/membership")
    assert resp.status_code == 403
