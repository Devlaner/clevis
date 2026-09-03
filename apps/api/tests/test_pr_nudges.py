"""Tests for the stale-PR nudge routes (issue #289).

Personal + org scoped, admin-gated, first write to GitHub's Pull requests API from
Clevis -- so it needs `pull_requests: write`, surfaced as a 400 when GitHub 403s.
Faked GitHub via `patch(".GitHubClient")`: list reads go through
``request_paginated`` (PR list, issue comments), writes through ``request``.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.app_config import set_config
from src.core.auth import UserOut, require_auth
from src.core.db import AuditLog, User, get_db
from src.repositories import org_membership_repo, org_repo
from src.routers.pr_nudges import router


def _make_user(db, email: str) -> UserOut:
    user = User(email=email, name=None, password_hash=None, is_workspace_admin=False)
    db.add(user)
    db.commit()
    db.refresh(user)
    return UserOut(id=user.id, email=user.email, name=user.name, is_workspace_admin=False)


@pytest.fixture()
def user(db) -> UserOut:
    return _make_user(db, "nudge@example.com")


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


def _pr(number, created_days_ago, updated_days_ago=None, draft=False):
    now = datetime.now(timezone.utc)
    updated = now - timedelta(days=updated_days_ago if updated_days_ago is not None else created_days_ago)
    return {
        "number": number,
        "title": f"PR {number}",
        "draft": draft,
        "created_at": (now - timedelta(days=created_days_ago)).isoformat(),
        "updated_at": updated.isoformat(),
    }


def _wire(mock, *, prs, comments=None):
    """Point the faked GitHubClient at canned data. ``comments`` is the marker-comment
    list returned for every PR (default: none, i.e. not previously nudged)."""
    inst = mock.return_value

    def paginated(path, params=None):
        if path.endswith("/pulls"):
            return prs
        if path.endswith("/comments"):
            return comments or []
        return []

    inst.request_paginated.side_effect = paginated
    inst.request.return_value = {"id": 1}
    return inst


def test_requires_admin_of_connected_org(client, db, user):
    org = org_repo.get_or_create(db, github_login="acme")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=user.id, role="member")
    db.commit()
    resp = client.post("/me/repos/acme/api/pr-nudges", json={"token": "ghp_member"})
    assert resp.status_code == 403


def test_no_token_available_returns_400(client):
    resp = client.post("/me/repos/acme/api/pr-nudges", json={})
    assert resp.status_code == 400


def test_comment_mode_nudges_only_stale_non_draft_prs_and_audits(client, db, user):
    _admin_org(db, user)
    set_config("pr_nudge_stale_days", "3")
    set_config("pr_nudge_mode", "comment")

    prs = [_pr(1, 10), _pr(2, 1), _pr(3, 10, draft=True), _pr(4, 10, updated_days_ago=0)]
    with patch("src.routers.pr_nudges.GitHubClient") as mock:
        _wire(mock, prs=prs)
        resp = client.post("/me/repos/acme/api/pr-nudges", json={"token": "ghp_admin"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "comment"
    actions = {r["number"]: r["action"] for r in body["results"]}
    assert actions[1] == "commented"
    assert actions[2] == "skipped-not-stale"       # too new
    assert actions[3] == "skipped-not-stale"       # draft
    assert actions[4] == "skipped-not-stale"       # updated recently
    assert db.query(AuditLog).filter(AuditLog.action == "pr_nudge.sweep").count() == 1


def test_comment_mode_skips_a_pr_already_nudged(client, db, user):
    _admin_org(db, user)
    set_config("pr_nudge_stale_days", "3")
    set_config("pr_nudge_mode", "comment")
    with patch("src.routers.pr_nudges.GitHubClient") as mock:
        inst = _wire(mock, prs=[_pr(1, 10)], comments=[{"body": "ping <!-- clevis:pr-nudge -->"}])
        resp = client.post("/me/repos/acme/api/pr-nudges", json={"token": "ghp_admin"})
    assert [r["action"] for r in resp.json()["results"]] == ["skipped-recent-nudge"]
    # no comment was posted
    assert not [c for c in inst.request.call_args_list if c[0][0] == "POST"]


def test_off_mode_makes_no_calls(client, db, user):
    _admin_org(db, user)
    set_config("pr_nudge_mode", "off")
    with patch("src.routers.pr_nudges.GitHubClient") as mock:
        inst = _wire(mock, prs=[_pr(1, 30)])
        resp = client.post("/me/repos/acme/api/pr-nudges", json={"token": "ghp_admin"})
    assert resp.status_code == 200
    assert resp.json()["results"] == []
    inst.request_paginated.assert_not_called()
    inst.request.assert_not_called()


def test_label_mode_adds_the_needs_review_label(client, db, user):
    _admin_org(db, user)
    set_config("pr_nudge_stale_days", "3")
    set_config("pr_nudge_mode", "label")
    with patch("src.routers.pr_nudges.GitHubClient") as mock:
        inst = _wire(mock, prs=[_pr(1, 10)])
        resp = client.post("/me/repos/acme/api/pr-nudges", json={"token": "ghp_admin"})
    assert [r["action"] for r in resp.json()["results"]] == ["labeled"]
    labels_call = [c for c in inst.request.call_args_list if "/labels" in c[0][1]]
    assert labels_call and labels_call[0].kwargs["json"] == {"labels": ["needs-review"]}


def test_github_403_becomes_400_with_permission_hint(client, db, user):
    _admin_org(db, user)
    set_config("pr_nudge_stale_days", "3")
    set_config("pr_nudge_mode", "comment")
    err = httpx.HTTPStatusError(
        "403", request=httpx.Request("GET", "https://api.github.com"), response=httpx.Response(403)
    )
    with patch("src.routers.pr_nudges.GitHubClient") as mock:
        mock.return_value.request_paginated.side_effect = err
        resp = client.post("/me/repos/acme/api/pr-nudges", json={"token": "ghp_noscope"})
    assert resp.status_code == 400
    assert "Pull requests" in resp.json()["detail"]


def test_org_route_requires_owner_to_match_org(client, db, user):
    _admin_org(db, user, login="acme")
    resp = client.post("/orgs/acme/repos/other/api/pr-nudges", json={"token": "ghp_admin"})
    assert resp.status_code in (400, 403)
