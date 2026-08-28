"""Tests for the Overview cockpit aggregate endpoint (docs/plan.md Phase 12)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from src.core.auth import UserOut, require_auth
from src.core.db import Job, User, get_db
from src.repositories import installation_repo, org_membership_repo, org_repo, scan_results_repo
from src.routers.analytics import router

_HTTP_ERROR = httpx.HTTPStatusError(
    "boom",
    request=httpx.Request("GET", "https://api.github.com/x"),
    response=httpx.Response(404, request=httpx.Request("GET", "https://api.github.com/x")),
)


def _make_user(db, email: str) -> UserOut:
    user = User(email=email, name=None, password_hash=None, is_workspace_admin=False)
    db.add(user)
    db.commit()
    db.refresh(user)
    return UserOut(id=user.id, email=user.email, name=user.name, is_workspace_admin=False)


@pytest.fixture()
def mock_user(db):
    return _make_user(db, "cockpit@example.com")


@pytest.fixture()
def app(db, mock_user):
    a = FastAPI()
    a.dependency_overrides[require_auth] = lambda: mock_user
    a.dependency_overrides[get_db] = lambda: db
    a.include_router(router)
    return a


@pytest.fixture()
def http(app):
    return TestClient(app)


_DEFAULT_SAFE_MOCKS = {
    "src.routers.analytics._safe_list_repos": {"return_value": [{"name": "api"}, {"name": "worker"}]},
    "src.routers.analytics._safe_member_count": {"return_value": (12, True)},
    "src.routers.analytics._safe_recent_events": {"return_value": ([], True)},
    "src.routers.analytics._safe_open_pr_count": {"return_value": (7, True)},
    "src.routers.analytics._safe_pr_merge_rate_4w": {"return_value": []},
    "src.routers.analytics._safe_commit_activity_4w_and_heatmap_52w": {
        "return_value": ([1, 2, 3, 4], [0] * 52, True)
    },
    "src.routers.analytics._safe_total_cache_bytes": {"return_value": (123456, True)},
    "src.routers.analytics._safe_milestones": {"return_value": ([], [])},
    "src.routers.analytics._safe_pr_cycle_time_8w": {"return_value": []},
    "src.routers.analytics._safe_release_cadence_4w": {"return_value": [0, 0, 0, 0]},
}


def _patch_all(overrides=None):
    mocks = dict(_DEFAULT_SAFE_MOCKS)
    if overrides:
        mocks.update(overrides)
    patchers = [patch(target, **kwargs) for target, kwargs in mocks.items()]
    return patchers


def _start_all(patchers):
    for p in patchers:
        p.start()


def _stop_all(patchers):
    for p in patchers:
        p.stop()


def test_cockpit_requires_auth(db):
    a = FastAPI()
    a.dependency_overrides[get_db] = lambda: db
    a.include_router(router)
    resp = TestClient(a).get("/me/analytics/cockpit/acme")
    assert resp.status_code == 401


def test_cockpit_no_token_available_returns_400(http):
    resp = http.get("/me/analytics/cockpit/acme")
    assert resp.status_code == 400


def test_cockpit_success_all_sources(http, db, mock_user):
    org = org_repo.get_or_create(db, github_login="acme")
    scan_results_repo.insert(db, owner="acme", score=70, total_checks=5, failed_checks=1, checks=[], tenant_id=org.tenant_id)
    scan_results_repo.insert(db, owner="acme", score=85, total_checks=5, failed_checks=0, checks=[], tenant_id=org.tenant_id)
    for status in ("done", "done", "done", "failed"):
        job = Job(job_type="github.clear_actions_cache", payload="{}", status=status)
        db.add(job)
    db.commit()

    patchers = _patch_all()
    _start_all(patchers)
    try:
        with patch("src.routers.analytics.resolve_owner_token", return_value="ghp_test"):
            resp = http.get("/me/analytics/cockpit/acme")
    finally:
        _stop_all(patchers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["repo_count"] == 2
    assert body["member_count"] == 12
    assert body["open_pr_count"] == 7
    assert body["commit_activity_4w"] == [1, 2, 3, 4]
    assert body["total_cache_size_bytes"] == 123456
    assert body["latest_score"] == 85
    assert body["score_trend"] == [70, 85]
    assert body["cache_job_success_rate"] == 0.75
    assert body["degraded"] is False


def test_cockpit_no_scans_yet(http, db, mock_user):
    patchers = _patch_all()
    _start_all(patchers)
    try:
        with patch("src.routers.analytics.resolve_owner_token", return_value="ghp_test"):
            resp = http.get("/me/analytics/cockpit/acme")
    finally:
        _stop_all(patchers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["latest_score"] is None
    assert body["score_trend"] == []


def test_cockpit_no_cache_jobs_yet(http, db, mock_user):
    patchers = _patch_all()
    _start_all(patchers)
    try:
        with patch("src.routers.analytics.resolve_owner_token", return_value="ghp_test"):
            resp = http.get("/me/analytics/cockpit/acme")
    finally:
        _stop_all(patchers)

    assert resp.status_code == 200
    assert resp.json()["cache_job_success_rate"] == 0.0


def test_cockpit_degrades_when_pr_search_fails(http, db, mock_user):
    patchers = _patch_all({"src.routers.analytics._safe_open_pr_count": {"return_value": (0, False)}})
    _start_all(patchers)
    try:
        with patch("src.routers.analytics.resolve_owner_token", return_value="ghp_test"):
            resp = http.get("/me/analytics/cockpit/acme")
    finally:
        _stop_all(patchers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["open_pr_count"] == 0
    assert body["member_count"] == 12  # other fields unaffected
    assert body["degraded"] is True  # a failed call must not look identical to a real 0


def test_cockpit_degrades_when_events_fetch_fails(http, db, mock_user):
    patchers = _patch_all({"src.routers.analytics._safe_recent_events": {"return_value": ([], False)}})
    _start_all(patchers)
    try:
        with patch("src.routers.analytics.resolve_owner_token", return_value="ghp_test"):
            resp = http.get("/me/analytics/cockpit/acme")
    finally:
        _stop_all(patchers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["recent_events"] == []
    assert body["degraded"] is True


def test_cockpit_not_degraded_when_every_safe_call_succeeds_but_returns_empty(http, db, mock_user):
    """A genuinely empty org (no PRs, no events) must NOT be flagged degraded -- only an actual
    failed call should set it."""
    patchers = _patch_all()
    _start_all(patchers)
    try:
        with patch("src.routers.analytics.resolve_owner_token", return_value="ghp_test"):
            resp = http.get("/me/analytics/cockpit/acme")
    finally:
        _stop_all(patchers)

    assert resp.status_code == 200
    assert resp.json()["degraded"] is False


def test_cockpit_fails_when_repo_list_fails(http, db, mock_user):
    patchers = _patch_all({"src.routers.analytics._safe_list_repos": {"side_effect": _HTTP_ERROR}})
    _start_all(patchers)
    try:
        with patch("src.routers.analytics.resolve_owner_token", return_value="ghp_test"):
            resp = http.get("/me/analytics/cockpit/acme")
    finally:
        _stop_all(patchers)

    assert resp.status_code == 400


def test_cockpit_falls_back_to_client_supplied_token_header(http, db, mock_user):
    patchers = _patch_all()
    _start_all(patchers)
    try:
        resp = http.get("/me/analytics/cockpit/acme", headers={"X-GitHub-Token": "ghp_client"})
    finally:
        _stop_all(patchers)

    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# S6: commit_activity_4w/commit_heatmap_52w + recent_events served from
# repo_event_daily_counts/repo_events (not live GitHub) for an org the caller is
# a member of with a connected GitHub App installation -- partial re-point,
# everything else in CockpitResponse still comes from live GitHub calls (see
# analytics.py's _cockpit_commit_activity_from_aggregate docstring and the
# plan.md status update for the accuracy tradeoff).
# ---------------------------------------------------------------------------


@pytest.fixture()
def acme_org_with_installation(db, mock_user):
    org = org_repo.get_or_create(db, github_login="acme")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=mock_user.id, role="member")
    installation_repo.create(
        db, account_login="acme", account_type="Organization", auth_mode="app", installation_id=7, org_id=org.id
    )
    return org


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


# Excludes the two helpers this section exercises for real -- every other GitHub-calling
# helper stays mocked so a connected-org test doesn't also need to fake GitHub responses
# for member_count/open_pr_count/etc.
_CONNECTED_SAFE_MOCKS = {
    target: kwargs
    for target, kwargs in _DEFAULT_SAFE_MOCKS.items()
    if target
    not in (
        "src.routers.analytics._safe_recent_events",
        "src.routers.analytics._safe_commit_activity_4w_and_heatmap_52w",
    )
}


def test_cockpit_connected_org_uses_aggregate_commit_activity(http, db, mock_user, acme_org_with_installation):
    today = datetime.now(timezone.utc).date()
    _insert_daily_count(db, acme_org_with_installation.tenant_id, repo="acme/demo", event_type="push", day=today, count=4)

    patchers = [patch(target, **kwargs) for target, kwargs in _CONNECTED_SAFE_MOCKS.items()]
    _start_all(patchers)
    try:
        with patch("src.routers.analytics.resolve_owner_token", return_value="ghp_test"):
            resp = http.get("/me/analytics/cockpit/acme")
    finally:
        _stop_all(patchers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["commit_activity_source"] == "aggregate"
    assert len(body["commit_heatmap_52w"]) == 52
    assert body["commit_heatmap_52w"][-1] == 4
    assert body["commit_activity_4w"][-1] == 4


def test_cockpit_connected_org_sums_commit_activity_across_repos(http, db, mock_user, acme_org_with_installation):
    today = datetime.now(timezone.utc).date()
    _insert_daily_count(db, acme_org_with_installation.tenant_id, repo="acme/demo", event_type="push", day=today, count=3)
    _insert_daily_count(db, acme_org_with_installation.tenant_id, repo="acme/worker", event_type="push", day=today, count=2)

    patchers = [patch(target, **kwargs) for target, kwargs in _CONNECTED_SAFE_MOCKS.items()]
    _start_all(patchers)
    try:
        with patch("src.routers.analytics.resolve_owner_token", return_value="ghp_test"):
            resp = http.get("/me/analytics/cockpit/acme")
    finally:
        _stop_all(patchers)

    assert resp.status_code == 200
    assert resp.json()["commit_heatmap_52w"][-1] == 5


def test_cockpit_connected_org_uses_repo_events_for_recent_events_not_live_github(
    http, db, mock_user, acme_org_with_installation
):
    _insert_repo_event(
        db,
        acme_org_with_installation.tenant_id,
        delivery_id="d1",
        event_type="push",
        actor="octocat",
        repo="acme/demo",
        summary="pushed to main",
        occurred_at=datetime.now(timezone.utc),
    )

    patchers = [patch(target, **kwargs) for target, kwargs in _CONNECTED_SAFE_MOCKS.items()]
    _start_all(patchers)
    try:
        with (
            patch("src.routers.analytics.resolve_owner_token", return_value="ghp_test"),
            patch("src.routers.analytics._cached_events") as mock_cached_events,
        ):
            resp = http.get("/me/analytics/cockpit/acme")
    finally:
        _stop_all(patchers)

    assert resp.status_code == 200
    events = resp.json()["recent_events"]
    assert len(events) == 1
    assert events[0]["actor"] == "octocat"
    mock_cached_events.assert_not_called()


def test_cockpit_falls_back_to_github_when_the_installation_has_no_installation_id(
    http, db, mock_user, acme_org_with_installation
):
    # Same sync_org_installation known-admin gap covered elsewhere (org_events,
    # repos.py's _repo_org_connected): a row can exist with installation_id IS NULL.
    inst = installation_repo.get_for_org(db, org_id=acme_org_with_installation.id, account_login="acme")
    inst.installation_id = None
    db.commit()

    patchers = _patch_all()
    _start_all(patchers)
    try:
        with patch("src.routers.analytics.resolve_owner_token", return_value="ghp_test"):
            resp = http.get("/me/analytics/cockpit/acme")
    finally:
        _stop_all(patchers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["commit_activity_source"] == "github"
    assert body["commit_activity_4w"] == [1, 2, 3, 4]  # the default GitHub-path mock's value


def test_cockpit_does_not_read_aggregate_for_org_the_caller_is_not_a_member_of(http, db, mock_user):
    # A connected org exists, but mock_user has no membership row in it -- the
    # bring-your-own-token path must not leak that unrelated org's internal
    # repo_event_daily_counts just because the `owner` login happens to match.
    org = org_repo.get_or_create(db, github_login="acme")
    installation_repo.create(
        db, account_login="acme", account_type="Organization", auth_mode="app", installation_id=7, org_id=org.id
    )
    _insert_daily_count(db, org.tenant_id, repo="acme/demo", event_type="push", day=datetime.now(timezone.utc).date(), count=99)

    patchers = _patch_all()
    _start_all(patchers)
    try:
        with patch("src.routers.analytics.resolve_owner_token", return_value="ghp_test"):
            resp = http.get("/me/analytics/cockpit/acme")
    finally:
        _stop_all(patchers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["commit_activity_source"] == "github"
    assert body["commit_activity_4w"] == [1, 2, 3, 4]  # default mock's value, not the aggregate's 99


# ---------------------------------------------------------------------------
# recent_events staleness (data-accuracy fix): a connected org's Recent Activity card must
# say so when the ingestion cursor hasn't advanced recently, instead of silently showing old
# data as if it were current.
# ---------------------------------------------------------------------------


def _set_cursor(db, tenant_id, *, account_login="acme", account_type="Organization", last_synced_at):
    db.execute(text(f"SET app.tenant_id = {int(tenant_id)}"))
    db.execute(
        text(
            "INSERT INTO activity_sync_cursors (tenant_id, account_login, account_type, last_synced_at) "
            "VALUES (:tenant_id, :account_login, :account_type, :last_synced_at)"
        ),
        {
            "tenant_id": tenant_id,
            "account_login": account_login,
            "account_type": account_type,
            "last_synced_at": last_synced_at,
        },
    )
    db.commit()


def test_activity_stale_hours_falls_back_to_default_on_unparsable_config():
    from src.routers.analytics import _activity_stale_hours

    with patch("src.routers.analytics.get_config", return_value="not-a-number"):
        assert _activity_stale_hours() == 6


def test_recent_events_staleness_treats_naive_last_synced_at_as_utc():
    """activity_sync_cursors.last_synced_at is TIMESTAMP WITH TIME ZONE, so psycopg always
    returns an aware datetime in practice -- but the naive-datetime branch is a defensive
    guard, exercised directly here rather than skipped as unreachable."""
    from unittest.mock import MagicMock

    from src.routers.analytics import _recent_events_staleness

    naive_recent = datetime.now() - timedelta(minutes=5)  # no tzinfo
    mock_db = MagicMock()
    mock_db.execute.return_value.fetchone.return_value = (naive_recent,)
    assert _recent_events_staleness(mock_db, tenant_id=1) is False


def test_recent_events_staleness_true_when_never_synced(db):
    from src.routers.analytics import _recent_events_staleness

    assert _recent_events_staleness(db, tenant_id=99999) is True


def test_recent_events_staleness_false_when_recently_synced(db, acme_org_with_installation):
    from src.routers.analytics import _recent_events_staleness

    tenant_id = acme_org_with_installation.tenant_id
    _set_cursor(db, tenant_id, last_synced_at=datetime.now(timezone.utc) - timedelta(minutes=5))
    assert _recent_events_staleness(db, tenant_id) is False


def test_recent_events_staleness_true_when_synced_long_ago(db, acme_org_with_installation):
    from src.routers.analytics import _recent_events_staleness

    tenant_id = acme_org_with_installation.tenant_id
    _set_cursor(db, tenant_id, last_synced_at=datetime.now(timezone.utc) - timedelta(hours=48))
    assert _recent_events_staleness(db, tenant_id) is True


def test_safe_recent_events_live_path_returns_empty_and_not_ok_on_error(db):
    from src.routers.analytics import _safe_recent_events

    with patch("src.routers.analytics._cached_events", side_effect=httpx.RequestError("boom")):
        events, ok = _safe_recent_events(db, "acme", "ghp_test", tenant_id=None)
    assert events == []
    assert ok is False


def test_safe_recent_events_aggregate_path_returns_empty_and_not_ok_on_error(db):
    from fastapi import HTTPException

    from src.routers.analytics import _safe_recent_events

    with patch(
        "src.routers.analytics._fetch_events_from_repo_events",
        side_effect=HTTPException(status_code=500, detail="boom"),
    ):
        events, ok = _safe_recent_events(db, "acme", "ghp_test", tenant_id=1)
    assert events == []
    assert ok is False


def test_cockpit_connected_org_surfaces_recent_events_staleness(http, db, mock_user, acme_org_with_installation):
    patchers = [patch(target, **kwargs) for target, kwargs in _CONNECTED_SAFE_MOCKS.items()]
    _start_all(patchers)
    try:
        with patch("src.routers.analytics.resolve_owner_token", return_value="ghp_test"):
            resp = http.get("/me/analytics/cockpit/acme")
    finally:
        _stop_all(patchers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["recent_events_source"] == "aggregate"
    assert body["recent_events_stale"] is True  # no cursor row was ever written in this test


# ---------------------------------------------------------------------------
# Unit tests for individual _safe_* helpers' own try/except behavior
# ---------------------------------------------------------------------------


def test_safe_member_count_returns_zero_and_not_ok_on_http_error():
    from src.routers.analytics import _safe_member_count

    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request_paginated.side_effect = _HTTP_ERROR
        assert _safe_member_count("acme", "ghp_test") == (0, False)


def test_safe_member_count_returns_zero_and_not_ok_on_request_error():
    from src.routers.analytics import _safe_member_count

    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request_paginated.side_effect = httpx.RequestError("boom")
        assert _safe_member_count("acme", "ghp_test") == (0, False)


def test_safe_member_count_returns_ok_true_on_success():
    from src.routers.analytics import _safe_member_count

    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request_paginated.return_value = [{}] * 3
        assert _safe_member_count("acme", "ghp_test") == (3, True)


def test_safe_open_pr_count_returns_zero_and_not_ok_on_http_error():
    from src.routers.analytics import _safe_open_pr_count

    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = _HTTP_ERROR
        assert _safe_open_pr_count("acme", "ghp_test") == (0, False)


def test_safe_pr_merge_rate_4w_returns_empty_list_on_error():
    from src.routers.analytics import _safe_pr_merge_rate_4w

    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = httpx.RequestError("boom")
        assert _safe_pr_merge_rate_4w("acme", "ghp_test") == []


def test_safe_pr_merge_rate_4w_returns_four_chronological_buckets():
    from src.routers.analytics import _safe_pr_merge_rate_4w

    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = {"total_count": 3}
        buckets = _safe_pr_merge_rate_4w("acme", "ghp_test")
    assert len(buckets) == 4
    weeks = [b.week for b in buckets]
    assert weeks == sorted(weeks)
    assert all(b.opened == 3 and b.merged == 3 for b in buckets)


def test_safe_commit_activity_4w_and_heatmap_52w_returns_zeros_and_not_ok_when_every_repo_fails():
    from src.routers.analytics import _safe_commit_activity_4w_and_heatmap_52w

    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = httpx.RequestError("boom")
        activity, heatmap, ok = _safe_commit_activity_4w_and_heatmap_52w("acme", "ghp_test", ["repo-a"])
    assert activity == [0, 0, 0, 0]
    assert heatmap == [0] * 52
    assert ok is False


def test_safe_commit_activity_4w_and_heatmap_52w_sums_across_repos_from_one_fetch():
    from src.routers.analytics import _safe_commit_activity_4w_and_heatmap_52w

    weeks_a = [{"total": i} for i in range(52)]  # totals 0..51, last 4 are 48,49,50,51
    weeks_b = [{"total": 1} for _ in range(52)]
    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = [weeks_a, weeks_b]
        activity, heatmap, ok = _safe_commit_activity_4w_and_heatmap_52w("acme", "ghp_test", ["repo-a", "repo-b"])

    # Only one request per repo -- not one for the 4w slice and a second for the 52w one.
    assert mock_client.return_value.request.call_count == 2
    assert activity == [49, 50, 51, 52]
    assert len(heatmap) == 52
    assert heatmap[0] == weeks_a[0]["total"] + weeks_b[0]["total"]
    assert ok is True


def test_safe_commit_activity_4w_and_heatmap_52w_one_bad_repo_sums_the_rest_but_flags_not_ok():
    """A single flaky repo must not zero the whole org's aggregate -- it's excluded from the
    sum and `ok` is False, so the caller can tell this apart from a real all-zero org."""
    from src.routers.analytics import _safe_commit_activity_4w_and_heatmap_52w

    weeks_good = [{"total": 2} for _ in range(52)]

    def _side_effect(method, path):
        if "repo-bad" in path:
            raise httpx.RequestError("boom")
        return weeks_good

    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = _side_effect
        activity, heatmap, ok = _safe_commit_activity_4w_and_heatmap_52w(
            "acme", "ghp_test", ["repo-bad", "repo-good"]
        )

    assert activity == [2, 2, 2, 2]  # repo-good's contribution survives
    assert heatmap[0] == 2
    assert ok is False  # but the caller still knows this is a partial sum


@pytest.mark.parametrize("bad_total", [True, 1.5, -1])
def test_week_total_rejects_bool_fractional_and_negative(bad_total):
    """CodeRabbit finding: a plain isinstance(x, (int, float)) check accepts bool (bool is an
    int subclass in Python), fractional values, and negatives -- none of which are a real
    GitHub commit count. Must raise so the caller degrades this repo instead of adding a
    nonsensical value to the org-wide total."""
    from src.routers.analytics import _week_total

    with pytest.raises(TypeError):
        _week_total({"total": bad_total})


def test_week_total_accepts_a_real_non_negative_int():
    from src.routers.analytics import _week_total

    assert _week_total({"total": 7}) == 7
    assert _week_total({"total": 0}) == 0


@pytest.mark.parametrize("bad_size", [True, 1.5, -1])
def test_cache_entry_bytes_rejects_bool_fractional_and_negative(bad_size):
    from src.routers.analytics import _cache_entry_bytes

    with pytest.raises(TypeError):
        _cache_entry_bytes({"size_in_bytes": bad_size})


def test_cache_entry_bytes_accepts_a_real_non_negative_int():
    from src.routers.analytics import _cache_entry_bytes

    assert _cache_entry_bytes({"size_in_bytes": 512}) == 512


@pytest.mark.parametrize("bad_total", [True, 1.5, -1])
def test_safe_commit_activity_4w_and_heatmap_52w_partial_aggregation_rejects_bad_total(bad_total):
    """End-to-end through the per-repo fan-out: a bool/fractional/negative "total" degrades
    that one repo (ok=False) instead of corrupting the org-wide sum or raising past
    future.result()."""
    from src.routers.analytics import _safe_commit_activity_4w_and_heatmap_52w

    weeks_good = [{"total": 2} for _ in range(52)]
    weeks_bad = [{"total": bad_total} for _ in range(52)]

    def _side_effect(method, path):
        if "repo-bad" in path:
            return weeks_bad
        return weeks_good

    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = _side_effect
        activity, heatmap, ok = _safe_commit_activity_4w_and_heatmap_52w(
            "acme", "ghp_test", ["repo-bad", "repo-good"]
        )

    assert activity == [2, 2, 2, 2]
    assert heatmap[0] == 2
    assert ok is False


@pytest.mark.parametrize("bad_size", [True, 1.5, -1])
def test_safe_total_cache_bytes_partial_aggregation_rejects_bad_size(bad_size):
    from src.routers.analytics import _safe_total_cache_bytes

    def _side_effect(method, path):
        if "repo-bad" in path:
            return {"actions_caches": [{"size_in_bytes": bad_size}]}
        return {"actions_caches": [{"size_in_bytes": 100}]}

    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = _side_effect
        total, ok = _safe_total_cache_bytes("acme", "ghp_test", ["repo-bad", "repo-good"])

    assert total == 100
    assert ok is False


def test_safe_commit_activity_4w_and_heatmap_52w_malformed_week_entry_degrades_not_raises():
    """A non-dict week (or a week whose "total" isn't numeric) must be treated as a per-repo
    failure, not raise past future.result() and fail the whole cockpit request -- this is
    exactly the escape-asyncio.gather scenario CodeRabbit flagged on this PR."""
    from src.routers.analytics import _safe_commit_activity_4w_and_heatmap_52w

    weeks_good = [{"total": 2} for _ in range(52)]
    weeks_malformed = [{"total": "not-a-number"} for _ in range(52)]  # e.g. a week entry as a bare string

    def _side_effect(method, path):
        if "repo-bad" in path:
            return weeks_malformed
        return weeks_good

    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = _side_effect
        activity, heatmap, ok = _safe_commit_activity_4w_and_heatmap_52w(
            "acme", "ghp_test", ["repo-bad", "repo-good"]
        )

    assert activity == [2, 2, 2, 2]  # repo-good's contribution survives, repo-bad's doesn't
    assert heatmap[0] == 2
    assert ok is False


def test_safe_commit_activity_4w_and_heatmap_52w_non_dict_week_degrades_not_raises():
    from src.routers.analytics import _safe_commit_activity_4w_and_heatmap_52w

    weeks_malformed = ["not-a-dict"] * 52
    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = weeks_malformed
        activity, heatmap, ok = _safe_commit_activity_4w_and_heatmap_52w("acme", "ghp_test", ["repo-a"])

    assert activity == [0, 0, 0, 0]
    assert ok is False


def test_safe_total_cache_bytes_malformed_entry_degrades_not_raises():
    from src.routers.analytics import _safe_total_cache_bytes

    def _side_effect(method, path):
        if "repo-bad" in path:
            return {"actions_caches": [{"size_in_bytes": "not-a-number"}]}
        return {"actions_caches": [{"size_in_bytes": 100}]}

    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = _side_effect
        total, ok = _safe_total_cache_bytes("acme", "ghp_test", ["repo-bad", "repo-good"])

    assert total == 100  # repo-good's contribution survives
    assert ok is False


def test_safe_total_cache_bytes_returns_zero_and_not_ok_on_error():
    from src.routers.analytics import _safe_total_cache_bytes

    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = httpx.RequestError("boom")
        assert _safe_total_cache_bytes("acme", "ghp_test", ["repo-a"]) == (0, False)


def test_safe_total_cache_bytes_sums_across_repos():
    from src.routers.analytics import _safe_total_cache_bytes

    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = [
            {"actions_caches": [{"size_in_bytes": 100}, {"size_in_bytes": 50}]},
            {"actions_caches": [{"size_in_bytes": 25}]},
        ]
        total, ok = _safe_total_cache_bytes("acme", "ghp_test", ["repo-a", "repo-b"])
    assert total == 175
    assert ok is True


def test_safe_commit_activity_4w_and_heatmap_52w_non_list_response_flags_not_ok():
    """A malformed (non-list) response body -- e.g. GitHub's error-shaped JSON slipping past
    status-code checks -- must be treated the same as a failed fetch, not silently summed as
    zero contribution while still claiming ok."""
    from src.routers.analytics import _safe_commit_activity_4w_and_heatmap_52w

    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = {"message": "Not Found"}
        activity, heatmap, ok = _safe_commit_activity_4w_and_heatmap_52w("acme", "ghp_test", ["repo-a"])

    assert activity == [0, 0, 0, 0]
    assert heatmap == [0] * 52
    assert ok is False


def test_safe_total_cache_bytes_non_dict_response_flags_not_ok():
    from src.routers.analytics import _safe_total_cache_bytes

    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = ["not", "a", "dict"]
        total, ok = _safe_total_cache_bytes("acme", "ghp_test", ["repo-a"])

    assert total == 0
    assert ok is False


def test_safe_total_cache_bytes_one_bad_repo_sums_the_rest_but_flags_not_ok():
    from src.routers.analytics import _safe_total_cache_bytes

    def _side_effect(method, path):
        if "repo-bad" in path:
            raise httpx.RequestError("boom")
        return {"actions_caches": [{"size_in_bytes": 50}]}

    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = _side_effect
        total, ok = _safe_total_cache_bytes("acme", "ghp_test", ["repo-bad", "repo-good"])

    assert total == 50
    assert ok is False


def test_cache_job_success_rate_mixed(db):
    from src.routers.analytics import _cache_job_success_rate

    for status in ("done", "done", "done", "failed"):
        job = Job(job_type="github.clear_actions_cache", payload="{}", status=status)
        db.add(job)
    db.commit()
    assert _cache_job_success_rate(db) == 0.75


def test_cache_job_success_rate_zero_jobs(db):
    from src.routers.analytics import _cache_job_success_rate

    assert _cache_job_success_rate(db) == 0.0


def test_cache_job_success_rate_ignores_other_job_types(db):
    from src.routers.analytics import _cache_job_success_rate

    job = Job(job_type="some.other.job", payload="{}", status="failed")
    db.add(job)
    db.commit()
    assert _cache_job_success_rate(db) == 0.0


# ---------------------------------------------------------------------------
# Milestones / at-risk repos (docs/plan.md Phase 14)
# ---------------------------------------------------------------------------


def test_safe_milestones_returns_empty_on_error():
    from src.routers.analytics import _safe_milestones

    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = httpx.RequestError("boom")
        milestones, at_risk = _safe_milestones("acme", "ghp_test", ["repo-a"])
    assert milestones == []
    assert at_risk == []


def test_safe_milestones_flags_overdue_as_critical():
    from src.routers.analytics import _safe_milestones

    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = [
            {"title": "v1", "due_on": "2020-01-01T00:00:00Z", "open_issues": 3, "closed_issues": 1}
        ]
        milestones, at_risk = _safe_milestones("acme", "ghp_test", ["repo-a"])

    assert milestones[0].state == "overdue"
    assert milestones[0].progress_pct == 25.0
    assert len(at_risk) == 1
    assert at_risk[0].repo == "repo-a"
    assert at_risk[0].severity == "critical"


def test_safe_milestones_on_track_when_no_due_date():
    from src.routers.analytics import _safe_milestones

    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = [
            {"title": "backlog", "due_on": None, "open_issues": 0, "closed_issues": 0}
        ]
        milestones, at_risk = _safe_milestones("acme", "ghp_test", ["repo-a"])

    assert milestones[0].state == "on_track"
    assert at_risk == []


def test_safe_milestones_one_bad_repo_does_not_blank_others():
    from src.routers.analytics import _safe_milestones

    def _side_effect(method, path, params=None):
        if "repo-bad" in path:
            raise httpx.RequestError("boom")
        return [{"title": "v1", "due_on": None, "open_issues": 0, "closed_issues": 0}]

    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = _side_effect
        milestones, _ = _safe_milestones("acme", "ghp_test", ["repo-bad", "repo-good"])

    assert len(milestones) == 1
    assert milestones[0].repo == "repo-good"


def test_milestone_state_unparsable_due_on_treated_as_on_track():
    from src.routers.analytics import _milestone_state

    assert _milestone_state("not-a-real-date", 0.0) == "on_track"


def test_milestone_state_at_risk_when_due_soon_and_below_threshold():
    from src.routers.analytics import _milestone_state

    soon = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    assert _milestone_state(soon, 40.0) == "at_risk"


def test_milestone_state_on_track_when_due_soon_but_above_threshold():
    from src.routers.analytics import _milestone_state

    soon = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    assert _milestone_state(soon, 90.0) == "on_track"


def test_safe_milestones_multiple_overdue_milestones_same_repo_collect_both_reasons():
    """Two overdue milestones in the same repo: the repo's aggregated at_risk entry
    should collect both reasons (not just the first one it saw)."""
    from src.routers.analytics import _safe_milestones

    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = [
            {"title": "v1-overdue", "due_on": "2020-01-01T00:00:00Z", "open_issues": 3, "closed_issues": 1},
            {"title": "v2-overdue", "due_on": "2020-02-01T00:00:00Z", "open_issues": 5, "closed_issues": 0},
        ]
        milestones, at_risk = _safe_milestones("acme", "ghp_test", ["repo-a"])

    assert len(milestones) == 2
    assert len(at_risk) == 1
    assert at_risk[0].repo == "repo-a"
    assert at_risk[0].severity == "critical"
    assert len(at_risk[0].reasons) == 2


# ---------------------------------------------------------------------------
# PR cycle time / release cadence (docs/plan.md Phase 14)
# ---------------------------------------------------------------------------


def test_safe_pr_cycle_time_8w_returns_empty_on_error():
    from src.routers.analytics import _safe_pr_cycle_time_8w

    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = httpx.RequestError("boom")
        assert _safe_pr_cycle_time_8w("acme", "ghp_test") == []


def test_safe_pr_cycle_time_8w_computes_average_days():
    from src.routers.analytics import _safe_pr_cycle_time_8w

    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = {
            "items": [
                {"created_at": "2026-01-01T00:00:00Z", "closed_at": "2026-01-03T00:00:00Z"},
                {"created_at": "2026-01-01T00:00:00Z", "closed_at": "2026-01-05T00:00:00Z"},
            ]
        }
        buckets = _safe_pr_cycle_time_8w("acme", "ghp_test")
    assert len(buckets) == 8
    assert all(b.avg_days == 3.0 for b in buckets)


def test_safe_pr_cycle_time_8w_zero_when_no_merges():
    from src.routers.analytics import _safe_pr_cycle_time_8w

    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = {"items": []}
        buckets = _safe_pr_cycle_time_8w("acme", "ghp_test")
    assert all(b.avg_days == 0.0 for b in buckets)


def test_safe_pr_cycle_time_8w_skips_malformed_search_items():
    """An item missing closed_at (or with a garbage timestamp) shouldn't blow up the
    week's average -- it's just excluded from that week's day-count."""
    from src.routers.analytics import _safe_pr_cycle_time_8w

    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = {
            "items": [
                {"created_at": "2026-01-01T00:00:00Z"},  # missing closed_at -> KeyError
                {"created_at": "2026-01-01T00:00:00Z", "closed_at": "not-a-date"},  # ValueError
            ]
        }
        buckets = _safe_pr_cycle_time_8w("acme", "ghp_test")
    assert all(b.avg_days == 0.0 for b in buckets)


def test_safe_release_cadence_4w_returns_zeros_on_error():
    from src.routers.analytics import _safe_release_cadence_4w

    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = httpx.RequestError("boom")
        assert _safe_release_cadence_4w("acme", "ghp_test", ["repo-a"]) == [0, 0, 0, 0]


def test_safe_release_cadence_4w_buckets_by_week():
    from src.routers.analytics import _safe_release_cadence_4w, _week_start

    this_week_release = {"published_at": (_week_start(0) + timedelta(days=1)).isoformat() + "T00:00:00Z"}
    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request.return_value = [this_week_release]
        totals = _safe_release_cadence_4w("acme", "ghp_test", ["repo-a"])
    assert totals[3] == 1
    assert sum(totals) == 1


def test_safe_release_cadence_4w_skips_non_list_response_and_missing_or_bad_dates():
    from src.routers.analytics import _safe_release_cadence_4w

    def _side_effect(method, path, params=None):
        if "repo-not-a-list" in path:
            return {"message": "not found"}  # not a list -> skipped entirely
        return [
            {"published_at": None},  # missing published_at -> skipped
            {"published_at": "not-a-real-timestamp"},  # unparsable -> skipped
        ]

    with patch("src.routers.analytics.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = _side_effect
        totals = _safe_release_cadence_4w("acme", "ghp_test", ["repo-not-a-list", "repo-b"])

    assert totals == [0, 0, 0, 0]
