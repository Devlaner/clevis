"""Tests for the repos router (Phase 8 groundwork)."""

from datetime import date, timedelta
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from src.core.auth import UserOut, require_auth
from src.core.db import User, get_db
from src.repositories import installation_repo, org_membership_repo, org_repo
from src.routers.repos import router as repos_router
from src.routers.repos import _stats_cache

_ADMIN = UserOut(id=1, email="admin@example.com", name=None, is_workspace_admin=False)


@pytest.fixture(autouse=True)
def _clear_stats_cache():
    _stats_cache.clear()
    yield
    _stats_cache.clear()


@pytest.fixture()
def acme_org(db):
    user = User(id=_ADMIN.id, email=_ADMIN.email, name=None, password_hash=None, is_workspace_admin=False)
    db.add(user)
    db.commit()
    org = org_repo.get_or_create(db, github_login="acme")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=user.id, role="member")
    return org


@pytest.fixture()
def repos_client(db, acme_org):
    app = FastAPI()
    app.include_router(repos_router)
    app.dependency_overrides[require_auth] = lambda: _ADMIN
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def test_list_repos_returns_paginated_results(repos_client):
    with patch("src.routers.repos.GitHubClient") as mock_client:
        mock_client.return_value.request_paginated.return_value = [
            {
                "name": "demo",
                "full_name": "acme/demo",
                "private": False,
                "description": "A demo repository",
                "language": "Python",
                "stargazers_count": 3,
                "forks_count": 1,
                "watchers_count": 3,
                "open_issues_count": 1,
                "pushed_at": "2026-07-01T00:00:00Z",
                "default_branch": "main",
                "html_url": "https://github.com/acme/demo",
            }
        ]
        resp = repos_client.post("/orgs/acme/repos", json={"token": "ghp_testtoken123456789012345678901234"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["repos"][0]["name"] == "demo"
    mock_client.return_value.request_paginated.assert_called_once_with(
        "/orgs/acme/repos", params={"type": "all", "sort": "pushed"}
    )


def test_list_repos_returns_the_full_org_list_spanning_multiple_github_pages(repos_client):
    # Regression test for issue #220 ("repo name filter only searches the already-loaded
    # first page"): request_paginated() already follows GitHub's Link header across every
    # page before returning (see test_github_client.py::test_follows_link_header_across_pages
    # for that mechanism in isolation) -- this asserts the repos router doesn't truncate that
    # result on the way out, i.e. a 150-repo org (spanning two 100-per-page GitHub responses)
    # comes back as all 150 repos in one response, not just the first page's worth.
    repos = [
        {
            "name": f"repo-{i}",
            "full_name": f"acme/repo-{i}",
            "private": False,
            "description": None,
            "language": None,
            "stargazers_count": 0,
            "forks_count": 0,
            "watchers_count": 0,
            "open_issues_count": 0,
            "pushed_at": "2026-07-01T00:00:00Z",
            "default_branch": "main",
            "html_url": f"https://github.com/acme/repo-{i}",
        }
        for i in range(150)
    ]
    with patch("src.routers.repos.GitHubClient") as mock_client:
        mock_client.return_value.request_paginated.return_value = repos
        resp = repos_client.post("/orgs/acme/repos", json={"token": "ghp_testtoken123456789012345678901234"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 150
    assert len(body["repos"]) == 150
    assert {r["name"] for r in body["repos"]} == {f"repo-{i}" for i in range(150)}


def test_list_repos_no_installation_and_no_token_returns_400(repos_client):
    resp = repos_client.post("/orgs/acme/repos", json={})
    assert resp.status_code == 400


def test_list_repos_maps_github_status_error_to_400(repos_client):
    response = httpx.Response(404, request=httpx.Request("GET", "https://api.github.com/x"))
    error = httpx.HTTPStatusError("missing", request=response.request, response=response)
    with patch("src.routers.repos.GitHubClient") as mock_client:
        mock_client.return_value.request_paginated.side_effect = error
        resp = repos_client.post("/orgs/acme/repos", json={"token": "ghp_testtoken123456789012345678901234"})
    assert resp.status_code == 400
    assert "404" in resp.json()["detail"]


_REPO_META = {
    "stargazers_count": 24,
    "forks_count": 3,
    "watchers_count": 24,
    "open_issues_count": 12,
    "default_branch": "main",
}


def _not_found() -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.github.com/x")
    return httpx.HTTPStatusError("missing", request=request, response=httpx.Response(404, request=request))


def _stats_side_effect(
    *,
    repo_meta=None,
    commit_activity=None,
    participation=None,
    contributors=None,
    release=None,
    release_error=None,
    repo_meta_error=None,
):
    # The 5 stats calls now run concurrently (fix for sequential-latency finding), so
    # unlike a plain ordered side_effect list, responses must be matched by URL --
    # call order across threads isn't guaranteed.
    def fn(method, path, **kwargs):
        if path == "/repos/acme/demo/releases/latest":
            if release_error is not None:
                raise release_error
            return release if release is not None else {}
        if path == "/repos/acme/demo":
            if repo_meta_error is not None:
                raise repo_meta_error
            return repo_meta if repo_meta is not None else {}
        if path == "/repos/acme/demo/stats/commit_activity":
            return commit_activity if commit_activity is not None else {}
        if path == "/repos/acme/demo/stats/participation":
            return participation if participation is not None else {}
        if path == "/repos/acme/demo/stats/contributors":
            return contributors if contributors is not None else {}
        raise AssertionError(f"unexpected path {path!r}")

    return fn


def test_repo_stats_treats_202_empty_body_as_not_ready(repos_client):
    with patch("src.routers.repos.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = _stats_side_effect(
            repo_meta=_REPO_META, release_error=_not_found()
        )
        resp = repos_client.post(
            "/orgs/acme/repos/acme/demo/stats", json={"token": "ghp_testtoken123456789012345678901234"}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["commit_activity"] == []
    assert body["participation"] == {}
    assert body["contributors"] == []


def test_repo_stats_includes_repo_metadata_and_latest_release(repos_client):
    release = {
        "tag_name": "v0.4.1",
        "published_at": "2026-07-15T00:00:00Z",
        "html_url": "https://github.com/acme/demo/releases/tag/v0.4.1",
    }
    with patch("src.routers.repos.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = _stats_side_effect(repo_meta=_REPO_META, release=release)
        resp = repos_client.post(
            "/orgs/acme/repos/acme/demo/stats", json={"token": "ghp_testtoken123456789012345678901234"}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["stargazers_count"] == 24
    assert body["forks_count"] == 3
    assert body["watchers_count"] == 24
    assert body["open_issues_count"] == 12
    assert body["default_branch"] == "main"
    assert body["latest_release"] == release


def test_repo_stats_latest_release_is_null_when_repo_has_no_releases(repos_client):
    with patch("src.routers.repos.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = _stats_side_effect(
            repo_meta=_REPO_META, release_error=_not_found()
        )
        resp = repos_client.post(
            "/orgs/acme/repos/acme/demo/stats", json={"token": "ghp_testtoken123456789012345678901234"}
        )
    assert resp.status_code == 200
    assert resp.json()["latest_release"] is None


def test_repo_stats_latest_release_is_null_on_non_404_error_without_failing_the_request(repos_client):
    # Regression test: a transient failure on the new latest_release call must not
    # discard the commit_activity/participation/contributors data that succeeded.
    server_error = httpx.HTTPStatusError(
        "boom",
        request=httpx.Request("GET", "https://api.github.com/x"),
        response=httpx.Response(503, request=httpx.Request("GET", "https://api.github.com/x")),
    )
    with patch("src.routers.repos.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = _stats_side_effect(
            repo_meta=_REPO_META,
            commit_activity=[{"week": 1, "total": 5}],
            release_error=server_error,
        )
        resp = repos_client.post(
            "/orgs/acme/repos/acme/demo/stats", json={"token": "ghp_testtoken123456789012345678901234"}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["commit_activity"] == [{"week": 1, "total": 5}]
    assert body["latest_release"] is None


def test_repo_stats_defaults_metadata_on_repo_meta_failure_without_failing_the_request(repos_client):
    # Regression test: a transient failure on the new repo_meta call must not discard
    # the commit_activity/participation/contributors data that succeeded.
    server_error = httpx.HTTPStatusError(
        "boom",
        request=httpx.Request("GET", "https://api.github.com/x"),
        response=httpx.Response(503, request=httpx.Request("GET", "https://api.github.com/x")),
    )
    with patch("src.routers.repos.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = _stats_side_effect(
            repo_meta_error=server_error,
            commit_activity=[{"week": 1, "total": 5}],
            release_error=_not_found(),
        )
        resp = repos_client.post(
            "/orgs/acme/repos/acme/demo/stats", json={"token": "ghp_testtoken123456789012345678901234"}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["commit_activity"] == [{"week": 1, "total": 5}]
    assert body["stargazers_count"] == 0
    assert body["default_branch"] == ""


def test_repo_stats_second_call_is_served_from_cache(repos_client):
    with patch("src.routers.repos.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = _stats_side_effect(
            repo_meta=_REPO_META,
            commit_activity=[{"week": 1, "total": 5}],
            participation={"all": [1], "owner": [1]},
            contributors=[{"login": "octocat", "total": 5}],
            release_error=_not_found(),
        )
        first = repos_client.post(
            "/orgs/acme/repos/acme/demo/stats", json={"token": "ghp_testtoken123456789012345678901234"}
        )
        second = repos_client.post(
            "/orgs/acme/repos/acme/demo/stats", json={"token": "ghp_testtoken123456789012345678901234"}
        )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    assert mock_client.return_value.request.call_count == 5


def test_repo_stats_evicts_expired_entries_from_the_cache(repos_client):
    # Installation tokens rotate hourly, so a long-running instance would otherwise
    # accumulate one stale entry per org x repo x hour forever -- a cache miss now sweeps
    # any entry whose TTL has already elapsed (#247).
    import time as time_module

    stale_key = ("other-owner", "other-repo", "deadbeef")
    _stats_cache[stale_key] = (time_module.monotonic() - 10_000, {"repository": "other-owner/other-repo"})

    with patch("src.routers.repos.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = _stats_side_effect(
            repo_meta=_REPO_META,
            commit_activity=[{"week": 1, "total": 5}],
            release_error=_not_found(),
        )
        resp = repos_client.post(
            "/orgs/acme/repos/acme/demo/stats", json={"token": "ghp_testtoken123456789012345678901234"}
        )

    assert resp.status_code == 200
    assert stale_key not in _stats_cache


def test_repo_stats_different_tokens_are_not_served_from_the_same_cache_entry(repos_client):
    with patch("src.routers.repos.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = _stats_side_effect(
            repo_meta=_REPO_META,
            commit_activity=[{"week": 1, "total": 5}],
            release_error=_not_found(),
        )
        first = repos_client.post(
            "/orgs/acme/repos/acme/demo/stats", json={"token": "ghp_token_one_1234567890123456789"}
        )
        second = repos_client.post(
            "/orgs/acme/repos/acme/demo/stats", json={"token": "ghp_token_two_1234567890123456789"}
        )
    assert first.status_code == 200
    assert second.status_code == 200
    # Each distinct token triggers its own fetch (5 calls) rather than reusing the
    # other token's cached response -- 10 total, not 5.
    assert mock_client.return_value.request.call_count == 10


# ---------------------------------------------------------------------------
# S6: commit_activity served from repo_event_daily_counts for orgs with a
# connected GitHub App installation -- partial re-point, everything else in
# RepoStatsResponse still comes from live GitHub calls (see repos.py's
# _fetch_stats docstring and the plan.md status update for the accuracy
# tradeoff -- push-event counts, not exact commit counts).
# ---------------------------------------------------------------------------


@pytest.fixture()
def acme_org_with_installation(db, acme_org):
    installation_repo.create(
        db, account_login="acme", account_type="Organization", auth_mode="app", installation_id=7, org_id=acme_org.id
    )
    return acme_org


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


def test_repo_stats_uses_aggregate_commit_activity_when_installation_connected(repos_client, db, acme_org_with_installation):
    today = date.today()
    _insert_daily_count(db, acme_org_with_installation.tenant_id, repo="acme/demo", event_type="push", day=today, count=3)

    with patch("src.routers.repos.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = _stats_side_effect(
            repo_meta=_REPO_META,
            participation={"all": [1], "owner": [1]},
            contributors=[{"login": "octocat", "total": 5}],
            release_error=_not_found(),
        )
        resp = repos_client.post(
            "/orgs/acme/repos/acme/demo/stats", json={"token": "ghp_testtoken123456789012345678901234"}
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["commit_activity_source"] == "aggregate"
    assert len(body["commit_activity"]) == 52
    assert body["commit_activity"][-1]["total"] == 3
    # commit_activity is skipped entirely -- 4 live calls, not 5.
    assert mock_client.return_value.request.call_count == 4
    called_paths = {c.args[1] for c in mock_client.return_value.request.call_args_list}
    assert "/repos/acme/demo/stats/commit_activity" not in called_paths
    # Other fields still come from the live GitHub calls, unchanged.
    assert body["stargazers_count"] == 24
    assert body["contributors"] == [{"login": "octocat", "total": 5}]


def test_repo_stats_aggregate_commit_activity_buckets_by_week_and_excludes_outside_window(
    repos_client, db, acme_org_with_installation
):
    today = date.today()
    _insert_daily_count(db, acme_org_with_installation.tenant_id, repo="acme/demo", event_type="push", day=today, count=2)
    _insert_daily_count(
        db, acme_org_with_installation.tenant_id, repo="acme/demo", event_type="push", day=today - timedelta(days=1), count=1
    )
    # Well outside the 52-week window -- must not be counted anywhere.
    _insert_daily_count(
        db, acme_org_with_installation.tenant_id, repo="acme/demo", event_type="push", day=today - timedelta(weeks=60), count=99
    )
    # A different event type on the same day -- must not be counted (commit_activity is push-only).
    _insert_daily_count(db, acme_org_with_installation.tenant_id, repo="acme/demo", event_type="issues", day=today, count=50)

    with patch("src.routers.repos.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = _stats_side_effect(repo_meta=_REPO_META, release_error=_not_found())
        resp = repos_client.post(
            "/orgs/acme/repos/acme/demo/stats", json={"token": "ghp_testtoken123456789012345678901234"}
        )

    body = resp.json()
    weeks = body["commit_activity"]
    assert sum(w["total"] for w in weeks) == 3
    assert weeks[-1]["total"] == 3


def test_repo_stats_falls_back_to_github_when_the_installation_has_no_installation_id(repos_client, db, acme_org):
    # Mirrors the same gap covered in test_github_events.py -- a row can exist with
    # installation_id IS NULL (sync_org_installation's known-admin re-sync path) with no
    # actual App installation, and therefore no repo_event_daily_counts rows, behind it.
    installation_repo.create(
        db, account_login="acme", account_type="Organization", auth_mode="app", installation_id=None, org_id=acme_org.id
    )

    with patch("src.routers.repos.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = _stats_side_effect(
            repo_meta=_REPO_META, commit_activity=[{"week": 1, "total": 5}], release_error=_not_found()
        )
        resp = repos_client.post(
            "/orgs/acme/repos/acme/demo/stats", json={"token": "ghp_testtoken123456789012345678901234"}
        )

    body = resp.json()
    assert body["commit_activity_source"] == "github"
    assert body["commit_activity"] == [{"week": 1, "total": 5}]
    assert mock_client.return_value.request.call_count == 5


def test_repo_stats_commit_activity_source_is_github_for_a_legacy_pat_org(repos_client):
    # acme_org (no installation fixture) -- regression check that the fully unchanged
    # live-GitHub path is still what a legacy PAT-only org gets.
    with patch("src.routers.repos.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = _stats_side_effect(
            repo_meta=_REPO_META, commit_activity=[{"week": 1, "total": 5}], release_error=_not_found()
        )
        resp = repos_client.post(
            "/orgs/acme/repos/acme/demo/stats", json={"token": "ghp_testtoken123456789012345678901234"}
        )

    assert resp.json()["commit_activity_source"] == "github"


def test_list_repos_maps_github_request_error_to_503(repos_client):
    with patch("src.routers.repos.GitHubClient") as mock_client:
        mock_client.return_value.request_paginated.side_effect = httpx.RequestError("boom")
        resp = repos_client.post("/orgs/acme/repos", json={"token": "ghp_testtoken123456789012345678901234"})
    assert resp.status_code == 503


def test_repo_stats_no_installation_and_no_token_returns_400(repos_client):
    resp = repos_client.post("/orgs/acme/repos/acme/demo/stats", json={})
    assert resp.status_code == 400


def test_repo_stats_maps_github_status_error_to_400(repos_client):
    response = httpx.Response(500, request=httpx.Request("GET", "https://api.github.com/x"))
    error = httpx.HTTPStatusError("boom", request=response.request, response=response)
    with patch("src.routers.repos.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = error
        resp = repos_client.post(
            "/orgs/acme/repos/acme/demo/stats", json={"token": "ghp_testtoken123456789012345678901234"}
        )
    assert resp.status_code == 400


def test_list_pulls_maps_github_status_error_to_400(repos_client):
    response = httpx.Response(404, request=httpx.Request("GET", "https://api.github.com/x"))
    error = httpx.HTTPStatusError("missing", request=response.request, response=response)
    with patch("src.routers.repos.GitHubClient") as mock_client:
        mock_client.return_value.request_paginated.side_effect = error
        resp = repos_client.post(
            "/orgs/acme/repos/acme/demo/pulls", json={"token": "ghp_testtoken123456789012345678901234"}
        )
    assert resp.status_code == 400


def test_list_pulls_no_installation_and_no_token_returns_400(repos_client):
    resp = repos_client.post("/orgs/acme/repos/acme/demo/pulls", json={})
    assert resp.status_code == 400


def test_repo_stats_owner_mismatch_returns_403(repos_client):
    resp = repos_client.post(
        "/orgs/acme/repos/someone-else/demo/stats", json={"token": "ghp_testtoken123456789012345678901234"}
    )
    assert resp.status_code == 403


def test_list_pulls_returns_summaries(repos_client):
    with patch("src.routers.repos.GitHubClient") as mock_client:
        mock_client.return_value.request_paginated.return_value = [
            {
                "number": 7,
                "title": "Add feature",
                "user": {"login": "octocat"},
                "created_at": "2026-07-01T00:00:00Z",
                "html_url": "https://github.com/acme/demo/pull/7",
            }
        ]
        resp = repos_client.post(
            "/orgs/acme/repos/acme/demo/pulls", json={"token": "ghp_testtoken123456789012345678901234"}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["pulls"][0]["user"] == "octocat"
    mock_client.return_value.request_paginated.assert_called_once_with(
        "/repos/acme/demo/pulls", params={"state": "open"}
    )


def test_repo_security_returns_protected_and_enabled(repos_client):
    with (
        patch("src.routers.repos.GitHubClient") as mock_client,
        patch(
            "checks.github_checks.BranchProtectionEnabled.run",
            return_value={"status": "pass", "value": {"checked": 1, "protected": 1, "unknown": 0}},
        ),
        patch(
            "checks.github_checks.SecretScanningEnabled.run",
            return_value={"status": "pass", "value": {"enabled": 1, "total": 1}},
        ),
    ):
        mock_client.return_value.request.return_value = {
            "name": "demo",
            "default_branch": "main",
            "security_and_analysis": {"secret_scanning": {"status": "enabled"}},
        }
        resp = repos_client.post(
            "/orgs/acme/repos/acme/demo/security", json={"token": "ghp_testtoken123456789012345678901234"}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["branch_protection"] == "protected"
    assert body["secret_scanning"] == "enabled"


def test_repo_security_returns_unprotected_and_disabled(repos_client):
    with (
        patch("src.routers.repos.GitHubClient") as mock_client,
        patch(
            "checks.github_checks.BranchProtectionEnabled.run",
            return_value={"status": "fail", "value": {"checked": 1, "protected": 0, "unknown": 0}},
        ),
        patch(
            "checks.github_checks.SecretScanningEnabled.run",
            return_value={"status": "fail", "value": {"enabled": 0, "total": 1}},
        ),
    ):
        mock_client.return_value.request.return_value = {
            "name": "demo",
            "default_branch": "main",
            "security_and_analysis": {"secret_scanning": {"status": "disabled"}},
        }
        resp = repos_client.post(
            "/orgs/acme/repos/acme/demo/security", json={"token": "ghp_testtoken123456789012345678901234"}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["branch_protection"] == "unprotected"
    assert body["secret_scanning"] == "disabled"


def test_repo_security_returns_unknown_when_branch_check_inconclusive(repos_client):
    # e.g. the token lacks permission to read branch protection (403/429 inside the check).
    with (
        patch("src.routers.repos.GitHubClient") as mock_client,
        patch(
            "checks.github_checks.BranchProtectionEnabled.run",
            return_value={"status": "error", "value": {"checked": 1, "protected": 0, "unknown": 1}},
        ),
        patch(
            "checks.github_checks.SecretScanningEnabled.run",
            return_value={"status": "pass", "value": {"enabled": 1, "total": 1}},
        ),
    ):
        mock_client.return_value.request.return_value = {"name": "demo", "default_branch": "main"}
        resp = repos_client.post(
            "/orgs/acme/repos/acme/demo/security", json={"token": "ghp_testtoken123456789012345678901234"}
        )
    assert resp.status_code == 200
    assert resp.json()["branch_protection"] == "unknown"


def test_repo_security_returns_unknown_secret_scanning_when_field_is_absent(repos_client):
    # GitHub only includes `security_and_analysis` for admin-scoped tokens; a lesser-
    # privileged token gets a repo payload without the key at all, which must read as
    # "unknown" rather than a confidently-wrong "disabled".
    with (
        patch("src.routers.repos.GitHubClient") as mock_client,
        patch(
            "checks.github_checks.BranchProtectionEnabled.run",
            return_value={"status": "pass", "value": {"checked": 1, "protected": 1, "unknown": 0}},
        ),
    ):
        mock_client.return_value.request.return_value = {"name": "demo", "default_branch": "main"}
        resp = repos_client.post(
            "/orgs/acme/repos/acme/demo/security", json={"token": "ghp_testtoken123456789012345678901234"}
        )
    assert resp.status_code == 200
    assert resp.json()["secret_scanning"] == "unknown"


def test_repo_security_no_installation_and_no_token_returns_400(repos_client):
    resp = repos_client.post("/orgs/acme/repos/acme/demo/security", json={})
    assert resp.status_code == 400


def test_repo_security_owner_mismatch_returns_403(repos_client):
    resp = repos_client.post(
        "/orgs/acme/repos/someone-else/demo/security", json={"token": "ghp_testtoken123456789012345678901234"}
    )
    assert resp.status_code == 403


def test_repo_security_maps_github_status_error_to_400(repos_client):
    response = httpx.Response(404, request=httpx.Request("GET", "https://api.github.com/x"))
    error = httpx.HTTPStatusError("missing", request=response.request, response=response)
    with patch("src.routers.repos.GitHubClient") as mock_client:
        mock_client.return_value.request.side_effect = error
        resp = repos_client.post(
            "/orgs/acme/repos/acme/demo/security", json={"token": "ghp_testtoken123456789012345678901234"}
        )
    assert resp.status_code == 400


def test_non_member_forbidden(db):
    org_repo.get_or_create(db, github_login="acme")
    app = FastAPI()
    app.include_router(repos_router)
    app.dependency_overrides[require_auth] = lambda: UserOut(
        id=99, email="outsider@example.com", name=None, is_workspace_admin=False
    )
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    resp = client.post("/orgs/acme/repos", json={})
    assert resp.status_code == 403
