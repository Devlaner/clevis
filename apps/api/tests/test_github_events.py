"""Tests for the org events feed router (Phase 9 groundwork)."""

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
from src.routers.github import router as github_router
from src.routers.github import _events_cache

_ADMIN = UserOut(id=1, email="admin@example.com", name=None, is_workspace_admin=False)


@pytest.fixture(autouse=True)
def _clear_events_cache():
    _events_cache.clear()
    yield
    _events_cache.clear()


@pytest.fixture()
def acme_org(db):
    user = User(id=_ADMIN.id, email=_ADMIN.email, name=None, password_hash=None, is_workspace_admin=False)
    db.add(user)
    db.commit()
    org = org_repo.get_or_create(db, github_login="acme")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=user.id, role="member")
    return org


@pytest.fixture()
def events_client(db, acme_org):
    app = FastAPI()
    app.include_router(github_router)
    app.dependency_overrides[require_auth] = lambda: _ADMIN
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


_PUSH_EVENT = {
    "id": "1",
    "type": "PushEvent",
    "actor": {"login": "alice", "avatar_url": "https://avatars/alice.png"},
    "repo": {"name": "acme/api"},
    "payload": {"ref": "refs/heads/main", "commits": [{"sha": "a"}, {"sha": "b"}, {"sha": "c"}]},
    "created_at": "2026-07-17T10:00:00Z",
}

_LARGE_PUSH_EVENT = {
    "id": "1b",
    "type": "PushEvent",
    "actor": {"login": "alice", "avatar_url": "https://avatars/alice.png"},
    "repo": {"name": "acme/api"},
    # GitHub truncates the embedded commits array at 20 even when more were pushed;
    # `size` carries the true total.
    "payload": {"ref": "refs/heads/main", "size": 50, "commits": [{"sha": str(i)} for i in range(20)]},
    "created_at": "2026-07-17T10:05:00Z",
}

_PR_OPENED_EVENT = {
    "id": "2",
    "type": "PullRequestEvent",
    "actor": {"login": "bob", "avatar_url": "https://avatars/bob.png"},
    "repo": {"name": "acme/worker"},
    "payload": {"action": "opened", "number": 42, "pull_request": {"title": "Fix cache timeout", "merged": False}},
    "created_at": "2026-07-17T09:00:00Z",
}

_PR_MERGED_EVENT = {
    "id": "3",
    "type": "PullRequestEvent",
    "actor": {"login": "bob", "avatar_url": "https://avatars/bob.png"},
    "repo": {"name": "acme/worker"},
    "payload": {"action": "closed", "number": 42, "pull_request": {"title": "Fix cache timeout", "merged": True}},
    "created_at": "2026-07-17T09:30:00Z",
}

_ISSUE_EVENT = {
    "id": "4",
    "type": "IssuesEvent",
    "actor": {"login": "alice", "avatar_url": "https://avatars/alice.png"},
    "repo": {"name": "acme/api"},
    "payload": {"action": "closed", "issue": {"number": 17, "title": "Timeout on scan"}},
    "created_at": "2026-07-17T08:00:00Z",
}

_RELEASE_EVENT = {
    "id": "5",
    "type": "ReleaseEvent",
    "actor": {"login": "alice", "avatar_url": "https://avatars/alice.png"},
    "repo": {"name": "acme/api"},
    "payload": {"release": {"tag_name": "v0.4.1"}},
    "created_at": "2026-07-15T00:00:00Z",
}

_CREATE_EVENT = {
    "id": "6",
    "type": "CreateEvent",
    "actor": {"login": "alice", "avatar_url": "https://avatars/alice.png"},
    "repo": {"name": "acme/api"},
    "payload": {"ref_type": "branch", "ref": "feature-x"},
    "created_at": "2026-07-14T00:00:00Z",
}

_UNKNOWN_EVENT = {
    "id": "7",
    "type": "WatchEvent",
    "actor": {"login": "alice", "avatar_url": "https://avatars/alice.png"},
    "repo": {"name": "acme/api"},
    "payload": {},
    "created_at": "2026-07-13T00:00:00Z",
}

_BOT_EVENT = {
    "id": "8",
    "type": "PushEvent",
    "actor": {"login": "dependabot[bot]", "avatar_url": "https://avatars/dependabot.png"},
    "repo": {"name": "acme/api"},
    "payload": {"ref": "refs/heads/main", "commits": [{"sha": "a"}]},
    "created_at": "2026-07-12T00:00:00Z",
}


def test_events_summarizes_each_event_type(events_client):
    with patch("src.routers.github.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = [
            _PUSH_EVENT, _PR_OPENED_EVENT, _PR_MERGED_EVENT, _ISSUE_EVENT, _RELEASE_EVENT, _CREATE_EVENT, _UNKNOWN_EVENT,
        ]
        resp = events_client.post("/github/orgs/acme/events", json={"token": "ghp_testtoken123456789012345678901234"})
    assert resp.status_code == 200
    events = resp.json()["events"]
    summaries = {e["id"]: e["summary"] for e in events}
    assert summaries["1"] == "pushed 3 commits to main"
    assert summaries["2"] == "opened PR #42: Fix cache timeout"
    assert summaries["3"] == "merged PR #42: Fix cache timeout"
    assert summaries["4"] == "closed issue #17: Timeout on scan"
    assert summaries["5"] == "created release v0.4.1"
    assert summaries["6"] == "created branch feature-x"
    assert summaries["7"] == "WatchEvent"


def test_events_push_summary_uses_true_total_not_truncated_commit_list(events_client):
    with patch("src.routers.github.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = [_LARGE_PUSH_EVENT]
        resp = events_client.post(
            "/github/orgs/acme/events", json={"token": "ghp_testtoken123456789012345678901234"}
        )
    assert resp.status_code == 200
    assert resp.json()["events"][0]["summary"] == "pushed 50 commits to main"


def test_events_excludes_bot_actors(events_client):
    with patch("src.routers.github.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = [_PUSH_EVENT, _BOT_EVENT]
        resp = events_client.post("/github/orgs/acme/events", json={"token": "ghp_testtoken123456789012345678901234"})
    assert resp.status_code == 200
    events = resp.json()["events"]
    assert len(events) == 1
    assert events[0]["actor"] == "alice"


def test_events_passes_per_page_through(events_client):
    with patch("src.routers.github.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = []
        events_client.post(
            "/github/orgs/acme/events", json={"token": "ghp_testtoken123456789012345678901234", "per_page": 10}
        )
    mock_client.return_value.request.assert_called_once_with(
        "GET", "/orgs/acme/events", params={"per_page": 10}
    )


def test_events_second_call_is_served_from_cache(events_client):
    with patch("src.routers.github.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = [_PUSH_EVENT]
        first = events_client.post(
            "/github/orgs/acme/events", json={"token": "ghp_testtoken123456789012345678901234"}
        )
        second = events_client.post(
            "/github/orgs/acme/events", json={"token": "ghp_testtoken123456789012345678901234"}
        )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    mock_client.return_value.request.assert_called_once()


def test_events_different_tokens_are_not_served_from_the_same_cache_entry(events_client):
    with patch("src.routers.github.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = [_PUSH_EVENT]
        events_client.post("/github/orgs/acme/events", json={"token": "ghp_token_one_1234567890123456789"})
        events_client.post("/github/orgs/acme/events", json={"token": "ghp_token_two_1234567890123456789"})
    assert mock_client.return_value.request.call_count == 2


def test_events_evicts_expired_entries_from_the_cache(events_client):
    # Same reasoning as test_repos.py's equivalent test: token_hash rotates hourly with
    # each fresh installation token, so stale entries would otherwise never be pruned (#247).
    import time as time_module

    stale_key = ("other-org", "deadbeef", 30)
    _events_cache[stale_key] = (time_module.monotonic() - 10_000, {"org": "other-org", "events": []})

    with patch("src.routers.github.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = [_PUSH_EVENT]
        resp = events_client.post(
            "/github/orgs/acme/events", json={"token": "ghp_testtoken123456789012345678901234"}
        )

    assert resp.status_code == 200
    assert stale_key not in _events_cache


def test_events_non_list_response_returns_502(events_client):
    # GitHubClient.request falls back to `{}` on an empty response body -- a non-list
    # response must surface as an error, not silently render as "no events".
    with patch("src.routers.github.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = {}
        resp = events_client.post(
            "/github/orgs/acme/events", json={"token": "ghp_testtoken123456789012345678901234"}
        )
    assert resp.status_code == 502


def test_events_no_installation_and_no_token_returns_400(events_client):
    resp = events_client.post("/github/orgs/acme/events", json={})
    assert resp.status_code == 400


def test_events_maps_github_status_error_to_400(events_client):
    response = httpx.Response(404, request=httpx.Request("GET", "https://api.github.com/x"))
    error = httpx.HTTPStatusError("missing", request=response.request, response=response)
    with patch("src.routers.github.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = error
        resp = events_client.post("/github/orgs/acme/events", json={"token": "ghp_testtoken123456789012345678901234"})
    assert resp.status_code == 400
    assert "404" in resp.json()["detail"]


def test_events_maps_github_request_error_to_503(events_client):
    with patch("src.routers.github.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = httpx.RequestError("boom")
        resp = events_client.post("/github/orgs/acme/events", json={"token": "ghp_testtoken123456789012345678901234"})
    assert resp.status_code == 503


def test_events_unknown_org_returns_404(db):
    app = FastAPI()
    app.include_router(github_router)
    app.dependency_overrides[require_auth] = lambda: _ADMIN
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    resp = client.post("/github/orgs/does-not-exist/events", json={})
    assert resp.status_code == 404


def test_events_non_member_forbidden(db):
    org_repo.get_or_create(db, github_login="acme")
    app = FastAPI()
    app.include_router(github_router)
    app.dependency_overrides[require_auth] = lambda: UserOut(
        id=99, email="outsider@example.com", name=None, is_workspace_admin=False
    )
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    resp = client.post("/github/orgs/acme/events", json={})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# S6: repo_events-backed path for orgs with a connected GitHub App installation
# (issue #191/#192's ingestion pipeline -- S3 webhooks + S4 normalization + S5
# backfill/gap-healing -- all require one, so this is the signal used to decide
# which path serves a given org; see org_events's own comment).
# ---------------------------------------------------------------------------


def _insert_repo_event(db, tenant_id, *, delivery_id, event_type, actor, repo, summary, occurred_at):
    db.execute(text(f"SET app.tenant_id = {int(tenant_id)}"))
    db.execute(
        text(
            "INSERT INTO repo_events (tenant_id, delivery_id, event_type, actor, actor_avatar, repo, summary, occurred_at) "
            "VALUES (:tenant_id, :delivery_id, :event_type, :actor, '', :repo, :summary, :occurred_at)"
        ),
        {
            "tenant_id": tenant_id,
            "delivery_id": delivery_id,
            "event_type": event_type,
            "actor": actor,
            "repo": repo,
            "summary": summary,
            "occurred_at": occurred_at,
        },
    )
    db.commit()


@pytest.fixture()
def acme_org_with_installation(db, acme_org):
    installation_repo.create(db, account_login="acme", account_type="Organization", auth_mode="app", installation_id=7, org_id=acme_org.id)
    return acme_org


def test_events_served_from_repo_events_when_an_installation_is_connected(events_client, db, acme_org_with_installation):
    now = datetime.now(timezone.utc)
    _insert_repo_event(
        db, acme_org_with_installation.tenant_id,
        delivery_id="d1", event_type="push", actor="alice", repo="acme/api", summary="pushed 2 commits to main", occurred_at=now,
    )

    with patch("src.routers.github.GitHubClient") as mock_client:
        resp = events_client.post("/github/orgs/acme/events", json={})

    assert resp.status_code == 200
    mock_client.assert_not_called()
    events = resp.json()["events"]
    assert len(events) == 1
    assert events[0]["type"] == "PushEvent"  # mapped back from repo_events' lowercase "push"
    assert events[0]["actor"] == "alice"
    assert events[0]["summary"] == "pushed 2 commits to main"


def test_events_from_repo_events_orders_newest_first_and_respects_per_page(events_client, db, acme_org_with_installation):
    now = datetime.now(timezone.utc)
    for i in range(3):
        _insert_repo_event(
            db, acme_org_with_installation.tenant_id,
            delivery_id=f"d{i}", event_type="push", actor="alice", repo="acme/api", summary=f"push {i}",
            occurred_at=now - timedelta(minutes=3 - i),
        )

    resp = events_client.post("/github/orgs/acme/events", json={"per_page": 2})

    assert resp.status_code == 200
    summaries = [e["summary"] for e in resp.json()["events"]]
    assert summaries == ["push 2", "push 1"]  # newest first, capped at per_page


def test_events_from_repo_events_excludes_bot_actors(events_client, db, acme_org_with_installation):
    now = datetime.now(timezone.utc)
    _insert_repo_event(
        db, acme_org_with_installation.tenant_id,
        delivery_id="d1", event_type="push", actor="alice", repo="acme/api", summary="pushed", occurred_at=now,
    )
    _insert_repo_event(
        db, acme_org_with_installation.tenant_id,
        delivery_id="d2", event_type="push", actor="dependabot[bot]", repo="acme/api", summary="pushed", occurred_at=now,
    )

    resp = events_client.post("/github/orgs/acme/events", json={})

    events = resp.json()["events"]
    assert len(events) == 1
    assert events[0]["actor"] == "alice"


def test_events_from_repo_events_maps_all_five_tracked_types_back_to_github_style(
    events_client, db, acme_org_with_installation
):
    now = datetime.now(timezone.utc)
    for i, event_type in enumerate(["push", "pull_request", "issues", "release", "create"]):
        _insert_repo_event(
            db, acme_org_with_installation.tenant_id,
            delivery_id=f"t{i}", event_type=event_type, actor="alice", repo="acme/api", summary=event_type,
            occurred_at=now - timedelta(minutes=i),
        )

    resp = events_client.post("/github/orgs/acme/events", json={})

    types_by_summary = {e["summary"]: e["type"] for e in resp.json()["events"]}
    assert types_by_summary == {
        "push": "PushEvent",
        "pull_request": "PullRequestEvent",
        "issues": "IssuesEvent",
        "release": "ReleaseEvent",
        "create": "CreateEvent",
    }


def test_events_falls_back_to_live_github_when_the_installation_has_no_installation_id(events_client, db, acme_org):
    # sync_org_installation's known-admin path lets a caller re-sync org metadata without a
    # real installation_id -- that row exists (get_for_org finds it) but there's no actual
    # App installation behind it, so no webhook/backfill pipeline ever populated
    # repo_events for this tenant. Must not be treated as "connected" (issue found on this
    # PR's own review).
    installation_repo.create(db, account_login="acme", account_type="Organization", auth_mode="app", installation_id=None, org_id=acme_org.id)

    with patch("src.routers.github.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = [_PUSH_EVENT]
        resp = events_client.post("/github/orgs/acme/events", json={"token": "ghp_testtoken123456789012345678901234"})

    assert resp.status_code == 200
    mock_client.assert_called_once()
    assert resp.json()["events"][0]["actor"] == "alice"


def test_events_fall_back_to_live_github_when_installation_connected_but_repo_events_empty(
    events_client, db, acme_org_with_installation
):
    # An App-connected org whose repo_events table is still empty (webhooks not yet
    # subscribed, or the install-time backfill aged out). Instead of a permanently blank
    # feed, org_events must fall through to the live-GitHub read.
    with patch("src.routers.github.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = [_PUSH_EVENT]
        resp = events_client.post(
            "/github/orgs/acme/events", json={"token": "ghp_testtoken123456789012345678901234"}
        )

    assert resp.status_code == 200
    mock_client.assert_called_once()
    assert resp.json()["events"][0]["actor"] == "alice"


def test_events_falls_back_to_live_github_when_no_installation_is_connected(events_client, acme_org):
    # acme_org (no installation fixture) -- confirms the hybrid still uses the unchanged
    # live-GitHub path for a legacy PAT-only org, not an empty feed.
    with patch("src.routers.github.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = [_PUSH_EVENT]
        resp = events_client.post("/github/orgs/acme/events", json={"token": "ghp_testtoken123456789012345678901234"})

    assert resp.status_code == 200
    mock_client.assert_called_once()
    assert resp.json()["events"][0]["actor"] == "alice"
