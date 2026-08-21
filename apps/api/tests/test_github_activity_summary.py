"""Tests for the S6 aggregates-first activity-summary endpoints (JSON + SSE), the first
concrete piece of the feat/aggregates-api-sse foundation -- see docs/plan.md's S6 section."""

import json
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from src.core.auth import UserOut, require_auth
from src.core.db import User, get_db
from src.repositories import installation_repo, org_membership_repo, org_repo
from src.routers.github import _activity_summary_stream, router as github_router

_MEMBER = UserOut(id=1, email="member@example.com", name=None, is_workspace_admin=False)


@pytest.fixture()
def acme_org(db):
    user = User(id=_MEMBER.id, email=_MEMBER.email, name=None, password_hash=None, is_workspace_admin=False)
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
    app.include_router(github_router)
    app.dependency_overrides[require_auth] = lambda: _MEMBER
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def _insert_daily_count(db, tenant_id, *, repo, event_type, day, count):
    db.execute(text(f"SET app.tenant_id = {int(tenant_id)}"))
    db.execute(
        text(
            "INSERT INTO repo_event_daily_counts (tenant_id, repo, event_type, day, count) "
            "VALUES (:tenant_id, :repo, :event_type, :day, :count)"
        ),
        {"tenant_id": tenant_id, "repo": repo, "event_type": event_type, "day": day, "count": count},
    )
    db.commit()


def test_activity_summary_sums_counts_within_the_window(client, db, acme_org_with_installation):
    today = date.today()
    _insert_daily_count(db, acme_org_with_installation.tenant_id, repo="acme/api", event_type="push", day=today, count=3)
    _insert_daily_count(
        db, acme_org_with_installation.tenant_id, repo="acme/api", event_type="push", day=today - timedelta(days=1), count=2
    )

    resp = client.get("/github/orgs/acme/activity-summary")

    assert resp.status_code == 200
    body = resp.json()
    assert body["connected"] is True
    assert body["totals"] == [{"repo": "acme/api", "event_type": "push", "count": 5}]


def test_activity_summary_excludes_counts_outside_the_window(client, db, acme_org_with_installation):
    today = date.today()
    _insert_daily_count(db, acme_org_with_installation.tenant_id, repo="acme/api", event_type="push", day=today, count=3)
    _insert_daily_count(
        db, acme_org_with_installation.tenant_id, repo="acme/api", event_type="push", day=today - timedelta(days=10), count=99
    )

    resp = client.get("/github/orgs/acme/activity-summary", params={"days": 7})

    assert resp.json()["totals"] == [{"repo": "acme/api", "event_type": "push", "count": 3}]


def test_activity_summary_groups_by_repo_and_event_type(client, db, acme_org_with_installation):
    today = date.today()
    _insert_daily_count(db, acme_org_with_installation.tenant_id, repo="acme/api", event_type="push", day=today, count=1)
    _insert_daily_count(db, acme_org_with_installation.tenant_id, repo="acme/api", event_type="issues", day=today, count=2)
    _insert_daily_count(db, acme_org_with_installation.tenant_id, repo="acme/worker", event_type="push", day=today, count=4)

    resp = client.get("/github/orgs/acme/activity-summary")

    totals = {(t["repo"], t["event_type"]): t["count"] for t in resp.json()["totals"]}
    assert totals == {("acme/api", "push"): 1, ("acme/api", "issues"): 2, ("acme/worker", "push"): 4}


def test_activity_summary_days_param_is_clamped_to_max(client, db, acme_org_with_installation):
    resp = client.get("/github/orgs/acme/activity-summary", params={"days": 100_000})
    assert resp.status_code == 200
    assert resp.json()["days"] == 90


def test_activity_summary_days_param_is_clamped_to_min(client, db, acme_org_with_installation):
    resp = client.get("/github/orgs/acme/activity-summary", params={"days": 0})
    assert resp.status_code == 200
    assert resp.json()["days"] == 1


def test_activity_summary_returns_empty_and_unconnected_for_legacy_pat_org(client, acme_org):
    # acme_org has no installation fixture -- repo_event_daily_counts is never populated
    # for a tenant without one (same gating as org_events' repo_events path).
    resp = client.get("/github/orgs/acme/activity-summary")

    assert resp.status_code == 200
    body = resp.json()
    assert body["connected"] is False
    assert body["totals"] == []


def test_activity_summary_treats_installation_without_installation_id_as_unconnected(client, db, acme_org):
    installation_repo.create(
        db, account_login="acme", account_type="Organization", auth_mode="app", installation_id=None, org_id=acme_org.id
    )

    resp = client.get("/github/orgs/acme/activity-summary")

    assert resp.json()["connected"] is False


def test_activity_summary_unknown_org_returns_404(client):
    resp = client.get("/github/orgs/does-not-exist/activity-summary")
    assert resp.status_code == 404


def test_activity_summary_non_member_forbidden(db, acme_org):
    app = FastAPI()
    app.include_router(github_router)
    app.dependency_overrides[require_auth] = lambda: UserOut(
        id=99, email="outsider@example.com", name=None, is_workspace_admin=False
    )
    app.dependency_overrides[get_db] = lambda: db
    outsider_client = TestClient(app)

    resp = outsider_client.get("/github/orgs/acme/activity-summary")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# SSE stream -- tested against the generator function directly (not through
# TestClient's own streaming, which would require a real multi-second wait per
# poll interval); a tiny poll_interval keeps these tests fast.
# ---------------------------------------------------------------------------


def test_stream_emits_a_real_event_first_then_heartbeats_when_unchanged(db, acme_org_with_installation, rbac_ctx_factory):
    today = date.today()
    _insert_daily_count(db, acme_org_with_installation.tenant_id, repo="acme/api", event_type="push", day=today, count=1)
    ctx = rbac_ctx_factory(acme_org_with_installation)

    gen = _activity_summary_stream(db, "acme", ctx, days=7, poll_interval=0)
    first = next(gen)
    second = next(gen)
    gen.close()

    assert first.startswith("event: activity_summary\ndata: ")
    payload = json.loads(first.removeprefix("event: activity_summary\ndata: ").strip())
    assert payload["totals"] == [{"repo": "acme/api", "event_type": "push", "count": 1}]
    assert second == ": heartbeat\n\n"


def test_stream_emits_a_new_event_when_the_aggregate_changes(db, acme_org_with_installation, rbac_ctx_factory):
    today = date.today()
    ctx = rbac_ctx_factory(acme_org_with_installation)

    gen = _activity_summary_stream(db, "acme", ctx, days=7, poll_interval=0)
    first = next(gen)
    _insert_daily_count(db, acme_org_with_installation.tenant_id, repo="acme/api", event_type="push", day=today, count=1)
    second = next(gen)
    gen.close()

    assert json.loads(first.removeprefix("event: activity_summary\ndata: ").strip())["totals"] == []
    assert second.startswith("event: activity_summary\ndata: ")


def test_stream_reports_unconnected_for_a_legacy_pat_org(db, acme_org, rbac_ctx_factory):
    ctx = rbac_ctx_factory(acme_org)

    gen = _activity_summary_stream(db, "acme", ctx, days=7, poll_interval=0)
    first = next(gen)
    gen.close()

    payload = json.loads(first.removeprefix("event: activity_summary\ndata: ").strip())
    assert payload["connected"] is False


@pytest.fixture()
def rbac_ctx_factory(db):
    from src.core.rbac import OrgContext, set_tenant_session_context
    from src.repositories import tenant_repo

    def _make(org):
        set_tenant_session_context(db, org.tenant_id, _MEMBER.id)
        membership = tenant_repo.get_membership(db, org.tenant_id, _MEMBER.id)
        return OrgContext(org=org, membership=membership)

    return _make
