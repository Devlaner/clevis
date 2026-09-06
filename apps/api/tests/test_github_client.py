"""Tests for GitHubClient.request — covers B-03 (single client) and B-04 (no silent None)."""
from unittest.mock import MagicMock, call, patch

import httpx
import pytest

from src.services.github_client import GitHubClient, github_error


@pytest.fixture()
def client():
    # Pass base_url directly so the test doesn't need a DB connection
    yield GitHubClient(token="ghp_test", base_url="https://api.github.com")


def _make_response(
    status_code: int,
    json_body: dict | list | None = None,
    text: str = "{}",
    link: str = "",
    headers: dict | None = None,
):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.text = text if json_body is None else str(json_body)
    resp.json.return_value = json_body if json_body is not None else {}
    resp.headers = {**({"Link": link} if link else {}), **(headers or {})}
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=resp
        )
    return resp


class TestSingleClient:
    """B-03: httpx.Client instantiated once outside retry loop."""

    def test_one_client_per_request_call(self, client):
        ok_resp = _make_response(200, {"id": 1})
        with patch("src.services.github_client.httpx.Client") as mock_client_cls:
            mock_ctx = MagicMock()
            mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_ctx.request.return_value = ok_resp
            mock_client_cls.return_value = mock_ctx

            result = client.request("GET", "/repos/acme/api")

        # Client constructor called exactly once regardless of retries
        mock_client_cls.assert_called_once_with(timeout=20)
        assert result == {"id": 1}

    def test_retries_reuse_same_client(self, client):
        """On a 429, the second attempt should use the same client instance."""
        resp_429 = _make_response(429)
        resp_429.raise_for_status = MagicMock()  # 429 is not raise_for_status'd immediately
        ok_resp = _make_response(200, {"ok": True})

        with (
            patch("src.services.github_client.httpx.Client") as mock_client_cls,
            patch("src.services.github_client.time.sleep"),
        ):
            mock_ctx = MagicMock()
            mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_ctx.request.side_effect = [resp_429, ok_resp]
            mock_client_cls.return_value = mock_ctx

            result = client.request("GET", "/test")

        mock_client_cls.assert_called_once()
        assert result == {"ok": True}


class TestExhaustedRetries:
    """B-04: exhausted retries must raise, not return None."""

    def test_request_error_raises_after_three_attempts(self, client):
        with (
            patch("src.services.github_client.httpx.Client") as mock_client_cls,
            patch("src.services.github_client.time.sleep"),
        ):
            mock_ctx = MagicMock()
            mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_ctx.request.side_effect = httpx.RequestError("connection failed")
            mock_client_cls.return_value = mock_ctx

            with pytest.raises(httpx.RequestError):
                client.request("GET", "/test")

    def test_success_on_second_attempt_returns_value(self, client):
        err_resp = _make_response(429)
        err_resp.raise_for_status = MagicMock()
        ok_resp = _make_response(200, {"data": "hello"})

        with (
            patch("src.services.github_client.httpx.Client") as mock_client_cls,
            patch("src.services.github_client.time.sleep"),
        ):
            mock_ctx = MagicMock()
            mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_ctx.request.side_effect = [err_resp, ok_resp]
            mock_client_cls.return_value = mock_ctx

            result = client.request("GET", "/test")

        assert result == {"data": "hello"}

    def test_retries_on_secondary_rate_limit_403_then_succeeds(self, client):
        # Regression test for issue #219: GitHub's secondary/abuse rate limit commonly
        # returns 403 (not 429), often with a Retry-After header.
        rate_limited = _make_response(403, headers={"Retry-After": "1"})
        ok_resp = _make_response(200, {"data": "hello"})

        with (
            patch("src.services.github_client.httpx.Client") as mock_client_cls,
            patch("src.services.github_client.time.sleep"),
        ):
            mock_ctx = MagicMock()
            mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_ctx.request.side_effect = [rate_limited, ok_resp]
            mock_client_cls.return_value = mock_ctx

            result = client.request("GET", "/test")

        assert result == {"data": "hello"}

    def test_retries_on_x_ratelimit_remaining_zero_403_then_succeeds(self, client):
        rate_limited = _make_response(403, headers={"X-RateLimit-Remaining": "0"})
        ok_resp = _make_response(200, {"data": "hello"})

        with (
            patch("src.services.github_client.httpx.Client") as mock_client_cls,
            patch("src.services.github_client.time.sleep"),
        ):
            mock_ctx = MagicMock()
            mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_ctx.request.side_effect = [rate_limited, ok_resp]
            mock_client_cls.return_value = mock_ctx

            result = client.request("GET", "/test")

        assert result == {"data": "hello"}

    def test_does_not_retry_a_plain_permission_denied_403(self, client):
        # A genuine permission-denied 403 (token lacks scope) has neither Retry-After nor
        # X-RateLimit-Remaining: 0 -- must still surface immediately, not be mistaken for
        # a rate limit and retried away.
        forbidden = _make_response(403)

        with (
            patch("src.services.github_client.httpx.Client") as mock_client_cls,
            patch("src.services.github_client.time.sleep") as mock_sleep,
        ):
            mock_ctx = MagicMock()
            mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_ctx.request.return_value = forbidden
            mock_client_cls.return_value = mock_ctx

            with pytest.raises(httpx.HTTPStatusError):
                client.request("GET", "/test")

        mock_sleep.assert_not_called()
        assert mock_ctx.request.call_count == 1


class TestGithubError:
    def test_appends_github_message_body_to_detail(self):
        resp = _make_response(403, {"message": "Resource not accessible by integration"})
        exc = httpx.HTTPStatusError("error", request=MagicMock(), response=resp)
        result = github_error(exc)
        assert result.status_code == 400
        assert result.detail == "GitHub API error: 403: Resource not accessible by integration"

    def test_bare_status_when_body_has_no_message(self):
        resp = _make_response(404, {})
        exc = httpx.HTTPStatusError("error", request=MagicMock(), response=resp)
        assert github_error(exc).detail == "GitHub API error: 404"

    def test_bare_status_when_body_is_not_json(self):
        resp = _make_response(500, text="<html>oops</html>")
        resp.json.side_effect = ValueError("not json")
        exc = httpx.HTTPStatusError("error", request=MagicMock(), response=resp)
        assert github_error(exc).detail == "GitHub API error: 500"

    def test_request_error_maps_to_503(self):
        result = github_error(httpx.RequestError("boom"))
        assert result.status_code == 503


class TestRequestPaginated:
    def test_follows_link_header_across_pages(self, client):
        page1 = _make_response(
            200, [{"id": 1}], link='<https://api.github.com/repos/acme/x?page=2>; rel="next"'
        )
        page2 = _make_response(200, [{"id": 2}])

        with patch("src.services.github_client.httpx.Client") as mock_client_cls:
            mock_ctx = MagicMock()
            mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_ctx.get.side_effect = [page1, page2]
            mock_client_cls.return_value = mock_ctx

            result = client.request_paginated("/repos/acme/x")

        assert result == [{"id": 1}, {"id": 2}]
        assert mock_ctx.get.call_count == 2

    def test_retries_on_429_then_succeeds(self, client):
        resp_429 = _make_response(429)
        resp_429.raise_for_status = MagicMock()
        ok_resp = _make_response(200, [{"id": 1}])

        with (
            patch("src.services.github_client.httpx.Client") as mock_client_cls,
            patch("src.services.github_client.time.sleep"),
        ):
            mock_ctx = MagicMock()
            mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_ctx.get.side_effect = [resp_429, ok_resp]
            mock_client_cls.return_value = mock_ctx

            result = client.request_paginated("/repos/acme/x")

        assert result == [{"id": 1}]

    def test_retries_on_secondary_rate_limit_403_then_succeeds(self, client):
        rate_limited = _make_response(403, headers={"Retry-After": "1"})
        ok_resp = _make_response(200, [{"id": 1}])

        with (
            patch("src.services.github_client.httpx.Client") as mock_client_cls,
            patch("src.services.github_client.time.sleep"),
        ):
            mock_ctx = MagicMock()
            mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_ctx.get.side_effect = [rate_limited, ok_resp]
            mock_client_cls.return_value = mock_ctx

            result = client.request_paginated("/repos/acme/x")

        assert result == [{"id": 1}]

    def test_raises_after_exhausted_retries_on_request_error(self, client):
        with (
            patch("src.services.github_client.httpx.Client") as mock_client_cls,
            patch("src.services.github_client.time.sleep"),
        ):
            mock_ctx = MagicMock()
            mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_ctx.get.side_effect = httpx.RequestError("connection failed")
            mock_client_cls.return_value = mock_ctx

            with pytest.raises(httpx.RequestError):
                client.request_paginated("/repos/acme/x")

    def test_raises_on_http_status_error(self, client):
        with patch("src.services.github_client.httpx.Client") as mock_client_cls:
            mock_ctx = MagicMock()
            mock_ctx.__enter__ = MagicMock(return_value=mock_ctx)
            mock_ctx.__exit__ = MagicMock(return_value=False)
            mock_ctx.get.return_value = _make_response(404)
            mock_client_cls.return_value = mock_ctx

            with pytest.raises(httpx.HTTPStatusError):
                client.request_paginated("/repos/acme/x")
