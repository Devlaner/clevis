"""Tests for the S6 aggregates-first activity-summary endpoints (JSON + SSE), the first
concrete piece of the feat/aggregates-api-sse foundation -- see docs/plan.md's S6 section."""

import json
from datetime import date, datetime, timedelta, timezone

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


def test_activity_summary_cutoff_uses_utc_calendar_not_host_local_time(client, db, acme_org_with_installation, monkeypatch):
    """Regression test (CodeRabbit finding on PR #349): RepoEventDailyCount.day is always
    a UTC date, so the cutoff must be derived from the UTC calendar, not date.today()'s
    host-local one -- a host running behind UTC could otherwise treat the newest UTC day's
    row as not-yet-arrived and omit it."""
    import src.routers.github as github_module

    fixed_now = datetime(2026, 1, 10, 0, 30, tzinfo=timezone.utc)

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now

    monkeypatch.setattr(github_module, "datetime", _FixedDateTime)

    # days=1 -> cutoff must be the UTC date of fixed_now (Jan 10), not Jan 9, which is
    # what a host running e.g. US-Pacific local time (UTC-8) would compute at this same
    # instant via date.today().
    _insert_daily_count(db, acme_org_with_installation.tenant_id, repo="acme/api", event_type="push", day=date(2026, 1, 10), count=7)
    _insert_daily_count(db, acme_org_with_installation.tenant_id, repo="acme/api", event_type="push", day=date(2026, 1, 9), count=99)

    resp = client.get("/github/orgs/acme/activity-summary", params={"days": 1})

    assert resp.json()["totals"] == [{"repo": "acme/api", "event_type": "push", "count": 7}]


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
# poll interval); a tiny poll_interval keeps these tests fast. The generators are
# async (the between-poll wait is `await asyncio.sleep`, not a blocking
# `time.sleep` on a threadpool worker -- see _activity_summary_stream).
# ---------------------------------------------------------------------------


async def _take(agen, n):
    out = []
    for _ in range(n):
        out.append(await agen.__anext__())
    return out


@pytest.mark.asyncio
async def test_stream_emits_a_real_event_first_then_heartbeats_when_unchanged(db, acme_org_with_installation, rbac_ctx_factory):
    today = date.today()
    _insert_daily_count(db, acme_org_with_installation.tenant_id, repo="acme/api", event_type="push", day=today, count=1)
    ctx = rbac_ctx_factory(acme_org_with_installation)

    gen = _activity_summary_stream(db, "acme", ctx, days=7, poll_interval=0)
    first, second = await _take(gen, 2)
    await gen.aclose()

    assert first.startswith("event: activity_summary\ndata: ")
    payload = json.loads(first.removeprefix("event: activity_summary\ndata: ").strip())
    assert payload["totals"] == [{"repo": "acme/api", "event_type": "push", "count": 1}]
    assert second == ": heartbeat\n\n"


@pytest.mark.asyncio
async def test_stream_emits_a_new_event_when_the_aggregate_changes(db, acme_org_with_installation, rbac_ctx_factory):
    today = date.today()
    ctx = rbac_ctx_factory(acme_org_with_installation)

    gen = _activity_summary_stream(db, "acme", ctx, days=7, poll_interval=0)
    (first,) = await _take(gen, 1)
    _insert_daily_count(db, acme_org_with_installation.tenant_id, repo="acme/api", event_type="push", day=today, count=1)
    (second,) = await _take(gen, 1)
    await gen.aclose()

    assert json.loads(first.removeprefix("event: activity_summary\ndata: ").strip())["totals"] == []
    assert second.startswith("event: activity_summary\ndata: ")


@pytest.mark.asyncio
async def test_stream_reports_unconnected_for_a_legacy_pat_org(db, acme_org, rbac_ctx_factory):
    ctx = rbac_ctx_factory(acme_org)

    gen = _activity_summary_stream(db, "acme", ctx, days=7, poll_interval=0)
    (first,) = await _take(gen, 1)
    await gen.aclose()

    payload = json.loads(first.removeprefix("event: activity_summary\ndata: ").strip())
    assert payload["connected"] is False


@pytest.mark.asyncio
async def test_stream_session_wrapper_opens_and_closes_its_own_session(monkeypatch):
    """Regression test (CodeRabbit finding on PR #349): the SSE route must not reuse the
    request-scoped `Depends(get_db)` session, which FastAPI 0.116.1 closes (and RESETs its
    tenant/user SET vars on) as soon as the route handler returns the StreamingResponse --
    before this generator has streamed anything. Verifies the wrapper opens its own
    session via SessionLocal, sets tenant context on it, and closes it once the stream
    ends, independent of _activity_summary_stream's own query logic (covered above)."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    import src.routers.github as github_module

    fake_db = MagicMock()
    monkeypatch.setattr(github_module, "SessionLocal", lambda: fake_db)

    captured: dict = {}

    async def _fake_inner_stream(db, org_login, ctx, days, poll_interval):
        captured["db"] = db
        yield "event: activity_summary\ndata: {}\n\n"

    monkeypatch.setattr(github_module, "_activity_summary_stream", _fake_inner_stream)

    ctx = SimpleNamespace(org=SimpleNamespace(tenant_id=42), membership=SimpleNamespace(user_id=7))
    gen = github_module._activity_summary_stream_session("acme", ctx, days=1, poll_interval=0)
    (first,) = await _take(gen, 1)
    await gen.aclose()

    assert first == "event: activity_summary\ndata: {}\n\n"
    assert captured["db"] is fake_db
    executed_sql = [c.args[0].text for c in fake_db.execute.call_args_list]
    assert "SET app.tenant_id = 42" in executed_sql
    assert "SET app.user_id = 7" in executed_sql
    fake_db.close.assert_called_once()


def test_teardown_stream_session_invalidates_connection_when_reset_fails():
    """Regression (CodeRabbit finding on PR #405): app.tenant_id/app.user_id are set with
    plain SET, so a teardown that fails partway (RESET or commit raises) must discard the
    pooled connection rather than close() it back into the pool still tenant-scoped --
    same contract as src.core.db.get_db's teardown."""
    from unittest.mock import MagicMock

    from src.routers.github import _teardown_stream_session

    db = MagicMock()
    db.execute.side_effect = RuntimeError("connection dropped mid-RESET")

    _teardown_stream_session(db)

    db.invalidate.assert_called_once()
    db.close.assert_not_called()


@pytest.fixture()
def rbac_ctx_factory(db):
    from src.core.rbac import OrgContext, set_tenant_session_context
    from src.repositories import tenant_repo

    def _make(org):
        set_tenant_session_context(db, org.tenant_id, _MEMBER.id)
        membership = tenant_repo.get_membership(db, org.tenant_id, _MEMBER.id)
        return OrgContext(org=org, membership=membership)

    return _make
