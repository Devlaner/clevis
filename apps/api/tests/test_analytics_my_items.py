"""Tests for the My PRs / My Reviews / My Issues paginated list endpoints."""

from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.auth import UserOut, require_auth
from src.core.db import User, get_db
from src.routers.analytics import router


def _make_user(db, email: str) -> UserOut:
    user = User(email=email, name=None, password_hash=None, is_workspace_admin=False)
    db.add(user)
    db.commit()
    db.refresh(user)
    return UserOut(id=user.id, email=user.email, name=user.name, is_workspace_admin=False)


@pytest.fixture()
def mock_user(db):
    return _make_user(db, "myitems@example.com")


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


@pytest.mark.parametrize("path", ["/me/github/my-prs", "/me/github/my-reviews", "/me/github/my-issues"])
def test_requires_auth(db, path):
    a = FastAPI()
    a.dependency_overrides[get_db] = lambda: db
    a.include_router(router)
    resp = TestClient(a).get(f"{path}?owner=acme")
    assert resp.status_code == 401


@pytest.mark.parametrize("path", ["/me/github/my-prs", "/me/github/my-reviews", "/me/github/my-issues"])
def test_no_token_available_returns_400(http, path):
    resp = http.get(f"{path}?owner=acme")
    assert resp.status_code == 400


@pytest.mark.parametrize("path", ["/me/github/my-prs", "/me/github/my-reviews", "/me/github/my-issues"])
def test_degrades_to_empty_when_login_unresolvable(http, path):
    with (
        patch("src.routers.analytics.resolve_owner_token", return_value="ghp_test"),
        patch("src.routers.analytics.GitHubClient") as mock_client,
    ):
        mock_client.return_value.request.side_effect = httpx.HTTPStatusError(
            "boom",
            request=httpx.Request("GET", "https://api.github.com/user"),
            response=httpx.Response(403, request=httpx.Request("GET", "https://api.github.com/user")),
        )
        resp = http.get(f"{path}?owner=acme")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"items": [], "total_count": 0, "page": 1, "per_page": 25, "identity_unresolved": True}


@pytest.mark.parametrize("path", ["/me/github/my-prs", "/me/github/my-reviews", "/me/github/my-issues"])
def test_propagates_unexpected_user_endpoint_error(http, path):
    """A non-403 failure calling GET /user (a real auth/server problem, not the expected
    "installation token can't call /user" case) must propagate rather than being silently
    swallowed as identity_unresolved."""
    with (
        patch("src.routers.analytics.resolve_owner_token", return_value="ghp_test"),
        patch("src.routers.analytics.GitHubClient") as mock_client,
    ):
        mock_client.return_value.request.side_effect = httpx.HTTPStatusError(
            "boom",
            request=httpx.Request("GET", "https://api.github.com/user"),
            response=httpx.Response(500, request=httpx.Request("GET", "https://api.github.com/user")),
        )
        with pytest.raises(httpx.HTTPStatusError):
            http.get(f"{path}?owner=acme")


@pytest.mark.parametrize("path", ["/me/github/my-prs", "/me/github/my-reviews", "/me/github/my-issues"])
def test_propagates_network_error_from_user_endpoint(http, path):
    """A network-level failure (timeout, DNS, connection reset) calling GET /user must
    also propagate rather than being treated as the expected App-token case."""
    with (
        patch("src.routers.analytics.resolve_owner_token", return_value="ghp_test"),
        patch("src.routers.analytics.GitHubClient") as mock_client,
    ):
        mock_client.return_value.request.side_effect = httpx.RequestError(
            "boom", request=httpx.Request("GET", "https://api.github.com/user")
        )
        with pytest.raises(httpx.RequestError):
            http.get(f"{path}?owner=acme")


def test_falls_back_to_users_github_login_when_user_endpoint_unresolvable(app, http):
    """A GitHub App installation token can't call GET /user, but if the signed-in Clevis
    user linked their own GitHub identity via OAuth, my-prs should use that login rather
    than degrading to empty."""
    app.dependency_overrides[require_auth] = lambda: UserOut(
        id=1, email="myitems@example.com", name=None, is_workspace_admin=False, github_login="octocat"
    )

    def _request_side_effect(method, path, params=None):
        if path == "/user":
            raise httpx.HTTPStatusError(
                "boom",
                request=httpx.Request("GET", "https://api.github.com/user"),
                response=httpx.Response(403, request=httpx.Request("GET", "https://api.github.com/user")),
            )
        if path == "/search/issues":
            assert "author:octocat" in params["q"]
            return {"items": [], "total_count": 0}
        return {}

    with (
        patch("src.routers.analytics.resolve_owner_token", return_value="ghp_test"),
        patch("src.routers.analytics.GitHubClient") as mock_client,
    ):
        mock_client.return_value.request.side_effect = _request_side_effect
        resp = http.get("/me/github/my-prs?owner=acme")

    assert resp.status_code == 200
    assert resp.json()["identity_unresolved"] is False


def test_my_prs_success_returns_pagination_fields(http):
    pr_item = {
        "number": 12,
        "title": "Fix bug",
        "repository_url": "https://api.github.com/repos/acme/api",
        "html_url": "https://github.com/acme/api/pull/12",
        "updated_at": "2026-07-20T00:00:00Z",
    }

    def _request_side_effect(method, path, params=None):
        if path == "/user":
            return {"login": "octocat"}
        if path == "/search/issues":
            assert "author:octocat" in params["q"]
            assert params["page"] == 2
            assert params["per_page"] == 10
            return {"items": [pr_item], "total_count": 37}
        return {}

    with (
        patch("src.routers.analytics.resolve_owner_token", return_value="ghp_test"),
        patch("src.routers.analytics.GitHubClient") as mock_client,
    ):
        mock_client.return_value.request.side_effect = _request_side_effect
        resp = http.get("/me/github/my-prs?owner=acme&page=2&per_page=10")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_count"] == 37
    assert body["page"] == 2
    assert body["per_page"] == 10
    assert len(body["items"]) == 1
    assert body["items"][0]["repository"] == "acme/api"


def test_my_reviews_uses_review_requested_query(http):
    def _request_side_effect(method, path, params=None):
        if path == "/user":
            return {"login": "octocat"}
        if path == "/search/issues":
            assert "review-requested:octocat" in params["q"]
            return {"items": [], "total_count": 0}
        return {}

    with (
        patch("src.routers.analytics.resolve_owner_token", return_value="ghp_test"),
        patch("src.routers.analytics.GitHubClient") as mock_client,
    ):
        mock_client.return_value.request.side_effect = _request_side_effect
        resp = http.get("/me/github/my-reviews?owner=acme")

    assert resp.status_code == 200
    assert resp.json()["items"] == []


def test_my_issues_success_maps_issue_summaries(http):
    issue_item = {
        "number": 3,
        "title": "Investigate flake",
        "repository_url": "https://api.github.com/repos/acme/worker",
        "html_url": "https://github.com/acme/worker/issues/3",
        "updated_at": "2026-07-19T00:00:00Z",
    }

    def _request_side_effect(method, path, params=None):
        if path == "/user":
            return {"login": "octocat"}
        if path == "/search/issues":
            assert "assignee:octocat" in params["q"]
            return {"items": [issue_item], "total_count": 1}
        return {}

    with (
        patch("src.routers.analytics.resolve_owner_token", return_value="ghp_test"),
        patch("src.routers.analytics.GitHubClient") as mock_client,
    ):
        mock_client.return_value.request.side_effect = _request_side_effect
        resp = http.get("/me/github/my-issues?owner=acme")

    assert resp.status_code == 200
    body = resp.json()
    assert body["items"][0]["repository"] == "acme/worker"


@pytest.mark.parametrize("path", ["/me/github/my-prs", "/me/github/my-reviews", "/me/github/my-issues"])
@pytest.mark.parametrize("params", ["per_page=101", "per_page=0", "page=0"])
def test_pagination_params_are_validated(http, path, params):
    resp = http.get(f"{path}?owner=acme&{params}")
    assert resp.status_code == 422


def test_page_times_per_page_over_1000_returns_empty_without_calling_github(http):
    with (
        patch("src.routers.analytics.resolve_owner_token", return_value="ghp_test"),
        patch("src.routers.analytics.GitHubClient") as mock_client,
    ):
        mock_client.return_value.request.side_effect = lambda method, path, params=None: (
            {"login": "octocat"} if path == "/user" else pytest.fail("should not call /search/issues")
        )
        resp = http.get("/me/github/my-prs?owner=acme&page=11&per_page=100")

    assert resp.status_code == 200
    body = resp.json()
    # total_count is capped at the reachable max (not 0) so page/lastPage math on the
    # client stays consistent instead of regressing once paging crosses GitHub's window.
    assert body == {"items": [], "total_count": 1000, "page": 11, "per_page": 100, "identity_unresolved": False}


def test_search_failure_degrades_to_empty_not_500(http):
    def _request_side_effect(method, path, params=None):
        if path == "/user":
            return {"login": "octocat"}
        if path == "/search/issues":
            raise httpx.HTTPStatusError(
                "boom",
                request=httpx.Request("GET", "https://api.github.com/search/issues"),
                response=httpx.Response(403, request=httpx.Request("GET", "https://api.github.com/search/issues")),
            )
        return {}

    with (
        patch("src.routers.analytics.resolve_owner_token", return_value="ghp_test"),
        patch("src.routers.analytics.GitHubClient") as mock_client,
    ):
        mock_client.return_value.request.side_effect = _request_side_effect
        resp = http.get("/me/github/my-prs?owner=acme")

    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["total_count"] == 0


def test_non_dict_search_response_degrades_to_empty(http):
    def _request_side_effect(method, path, params=None):
        if path == "/user":
            return {"login": "octocat"}
        if path == "/search/issues":
            return []
        return {}

    with (
        patch("src.routers.analytics.resolve_owner_token", return_value="ghp_test"),
        patch("src.routers.analytics.GitHubClient") as mock_client,
    ):
        mock_client.return_value.request.side_effect = _request_side_effect
        resp = http.get("/me/github/my-prs?owner=acme")

    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["total_count"] == 0


def test_falls_back_to_client_supplied_token_header(http):
    with patch("src.routers.analytics.GitHubClient") as mock_client:
        # 403 (not a bare network error) -- this test is exercising the client-supplied
        # X-GitHub-Token header path (no 400 from a missing token), not /user's error
        # handling, so it uses the "expected" degrade-to-empty case rather than a
        # network failure that would now (correctly) propagate.
        mock_client.return_value.request.side_effect = httpx.HTTPStatusError(
            "boom",
            request=httpx.Request("GET", "https://api.github.com/user"),
            response=httpx.Response(403, request=httpx.Request("GET", "https://api.github.com/user")),
        )
        resp = http.get("/me/github/my-prs?owner=acme", headers={"X-GitHub-Token": "ghp_client"})

    assert resp.status_code == 200
    assert resp.json()["items"] == []
