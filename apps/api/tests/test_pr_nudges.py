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
from src.services import pr_nudge


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


# --- config fallback / clamp ------------------------------------------------


def test_non_int_stale_days_config_falls_back_to_default(client, db, user):
    _admin_org(db, user)
    set_config("pr_nudge_stale_days", "not-a-number")
    set_config("pr_nudge_mode", "comment")
    with patch("src.routers.pr_nudges.GitHubClient") as mock:
        _wire(mock, prs=[_pr(1, 10)])
        resp = client.post("/me/repos/acme/api/pr-nudges", json={"token": "ghp_admin"})
    assert resp.status_code == 200
    assert resp.json()["stale_days"] == pr_nudge.DEFAULT_STALE_DAYS


def test_unknown_mode_config_falls_back_to_default(client, db, user):
    _admin_org(db, user)
    set_config("pr_nudge_mode", "bogus")
    with patch("src.routers.pr_nudges.GitHubClient") as mock:
        _wire(mock, prs=[_pr(1, 10)])
        resp = client.post("/me/repos/acme/api/pr-nudges", json={"token": "ghp_admin"})
    assert resp.status_code == 200
    assert resp.json()["mode"] == pr_nudge.DEFAULT_MODE


def test_absurd_stale_days_is_clamped_to_an_upper_bound(client, db, user):
    _admin_org(db, user)
    set_config("pr_nudge_stale_days", "100000")
    set_config("pr_nudge_mode", "comment")
    with patch("src.routers.pr_nudges.GitHubClient") as mock:
        _wire(mock, prs=[_pr(1, 10)])
        resp = client.post("/me/repos/acme/api/pr-nudges", json={"token": "ghp_admin"})
    assert resp.status_code == 200
    assert resp.json()["stale_days"] == 365


# --- GitHub error mapping (non-403) ---------------------------------------


def test_non_403_github_status_error_becomes_400(client, db, user):
    _admin_org(db, user)
    set_config("pr_nudge_mode", "comment")
    err = httpx.HTTPStatusError(
        "500", request=httpx.Request("GET", "https://api.github.com"), response=httpx.Response(500)
    )
    with patch("src.routers.pr_nudges.GitHubClient") as mock:
        mock.return_value.request_paginated.side_effect = err
        resp = client.post("/me/repos/acme/api/pr-nudges", json={"token": "ghp_admin"})
    assert resp.status_code == 400
    assert "500" in resp.json()["detail"]


def test_github_unreachable_becomes_503(client, db, user):
    _admin_org(db, user)
    set_config("pr_nudge_mode", "comment")
    err = httpx.ConnectError("boom", request=httpx.Request("GET", "https://api.github.com"))
    with patch("src.routers.pr_nudges.GitHubClient") as mock:
        mock.return_value.request_paginated.side_effect = err
        resp = client.post("/me/repos/acme/api/pr-nudges", json={"token": "ghp_admin"})
    assert resp.status_code == 503


# --- _connected_tenant: audit tenant scoping ------------------------------


def test_unconnected_owner_still_runs_and_audits_without_a_tenant(client, db, user):
    set_config("pr_nudge_mode", "comment")
    with patch("src.routers.pr_nudges.GitHubClient") as mock:
        _wire(mock, prs=[_pr(1, 10)])
        resp = client.post("/me/repos/randouser/repo/pr-nudges", json={"token": "ghp_byo"})
    assert resp.status_code == 200
    row = db.query(AuditLog).filter(AuditLog.action == "pr_nudge.sweep").one()
    assert row.tenant_id is None


def test_org_without_membership_audits_without_a_tenant(client, db, user):
    org_repo.get_or_create(db, github_login="acme")
    db.commit()
    set_config("pr_nudge_mode", "comment")
    with patch("src.routers.pr_nudges.GitHubClient") as mock:
        _wire(mock, prs=[_pr(1, 10)])
        resp = client.post("/me/repos/acme/api/pr-nudges", json={"token": "ghp_byo"})
    assert resp.status_code == 200
    row = db.query(AuditLog).filter(AuditLog.action == "pr_nudge.sweep").one()
    assert row.tenant_id is None


# --- org-scoped route ----------------------------------------------------


def test_org_route_nudges_and_audits(client, db, user):
    _admin_org(db, user, login="acme")
    set_config("pr_nudge_stale_days", "3")
    set_config("pr_nudge_mode", "comment")
    with patch("src.routers.pr_nudges.GitHubClient") as mock:
        _wire(mock, prs=[_pr(1, 10)])
        resp = client.post("/orgs/acme/repos/acme/api/pr-nudges", json={"token": "ghp_admin"})
    assert resp.status_code == 200
    assert [r["action"] for r in resp.json()["results"]] == ["commented"]
    assert db.query(AuditLog).filter(AuditLog.action == "pr_nudge.sweep").count() == 1


def test_org_route_without_a_token_returns_400(client, db, user):
    _admin_org(db, user, login="acme")
    resp = client.post("/orgs/acme/repos/acme/api/pr-nudges", json={})
    assert resp.status_code == 400


# --- service-level branches --------------------------------------------


class _FakeClient:
    def __init__(self, prs):
        self._prs = prs
        self.writes = []

    def request_paginated(self, path, params=None):
        if path.endswith("/pulls"):
            return self._prs
        return []

    def request(self, method, path, params=None, json=None):
        self.writes.append((method, path, json))
        return {"id": 1}


def test_run_nudge_sweep_rejects_an_unknown_mode():
    with pytest.raises(ValueError):
        pr_nudge.run_nudge_sweep(_FakeClient([]), "o", "r", stale_days=3, mode="weird")


def test_prs_with_missing_or_unparseable_timestamps_are_not_stale():
    prs = [
        {"number": 1, "title": "no timestamps", "draft": False},
        {"number": 2, "title": "bad created_at", "draft": False, "created_at": "not-a-date"},
    ]
    results = pr_nudge.run_nudge_sweep(_FakeClient(prs), "o", "r", stale_days=3, mode="comment")
    assert {r.number: r.action for r in results} == {
        1: "skipped-not-stale",
        2: "skipped-not-stale",
    }


def test_per_sweep_cap_stops_acting_after_the_limit():
    now = datetime.now(timezone.utc)
    stale = [
        {
            "number": n,
            "title": f"PR {n}",
            "draft": False,
            "created_at": (now - timedelta(days=30)).isoformat(),
            "updated_at": (now - timedelta(days=30)).isoformat(),
        }
        for n in range(pr_nudge._MAX_PER_SWEEP + 1)
    ]
    fake = _FakeClient(stale)
    results = pr_nudge.run_nudge_sweep(fake, "o", "r", stale_days=3, mode="label")
    assert results[-1].action == "skipped-per-sweep-cap"
    assert sum(1 for r in results if r.action == "labeled") == pr_nudge._MAX_PER_SWEEP
