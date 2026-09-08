"""Tests for individual GitHub security checks."""

import time
from unittest.mock import patch

import httpx
import pytest

from checks.github_checks import (
    _MAX_PAGES,
    _MAX_RETRY_AFTER_SECONDS,
    BranchProtectionEnabled,
    CodeScanningCheck,
    DefaultBranchNoForcePushCheck,
    DependabotAlertsCheck,
    OrgMFARequired,
    SecretScanningEnabled,
    _get,
    _get_all_pages,
)


def test_org_mfa_missing_field_returns_error():
    check = OrgMFARequired()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("checks.github_checks._get", lambda url, token: {"login": "acme"})
        result = check.run(owner="acme", token="tok")
    assert result["status"] == "error"


def test_secret_scanning_null_security_and_analysis_does_not_crash():
    check = SecretScanningEnabled()
    repos = [{"name": "demo", "security_and_analysis": None}]
    result = check.run(owner="acme", token="tok", repos=repos)
    assert result["status"] in {"pass", "fail"}


def test_secret_scanning_empty_org_is_not_applicable():
    check = SecretScanningEnabled()
    result = check.run(owner="acme", token="tok", repos=[])
    assert result["status"] == "not_applicable"
    assert result["value"] == {"enabled": 0, "total": 0}


def test_branch_protection_empty_org_is_not_applicable():
    check = BranchProtectionEnabled()
    result = check.run(owner="acme", token="tok", repos=[])
    assert result["status"] == "not_applicable"
    assert result["value"] == {"checked": 0, "protected": 0, "unknown": 0}


def test_branch_protection_rate_limit_counts_as_unknown():
    check = BranchProtectionEnabled()
    repos = [{"name": "demo", "default_branch": "main"}]

    def fake_get(url, token):
        response = httpx.Response(403, request=httpx.Request("GET", url))
        raise httpx.HTTPStatusError("forbidden", request=response.request, response=response)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("checks.github_checks._get", fake_get)
        result = check.run(owner="acme", token="tok", repos=repos)
    assert result["status"] == "error"
    assert result["value"]["unknown"] == 1


def test_branch_protection_5xx_counts_as_unknown_not_unprotected():
    # Regression test for #245: _branch_protection_status previously fell through to
    # "unprotected" for any status code that wasn't 404/403/429 (e.g. a transient
    # 500/502), which could flip an org's whole branch-protection check to "fail" even
    # though every real repo was protected.
    check = BranchProtectionEnabled()
    repos = [{"name": "demo", "default_branch": "main"}]

    def fake_get(url, token):
        response = httpx.Response(500, request=httpx.Request("GET", url))
        raise httpx.HTTPStatusError("server error", request=response.request, response=response)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("checks.github_checks._get", fake_get)
        result = check.run(owner="acme", token="tok", repos=repos)
    assert result["status"] == "error"
    assert result["value"]["unknown"] == 1
    assert result["value"]["protected"] == 0


def test_branch_protection_network_error_counts_as_unknown():
    check = BranchProtectionEnabled()
    repos = [{"name": "demo", "default_branch": "main"}]

    def fake_get(url, token):
        raise httpx.ConnectError("network down", request=httpx.Request("GET", url))

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("checks.github_checks._get", fake_get)
        result = check.run(owner="acme", token="tok", repos=repos)
    assert result["status"] == "error"
    assert result["value"]["unknown"] == 1


def test_get_retries_on_transient_connection_error_then_succeeds():
    # Matches GitHubClient.request's retry contract (apps/api/src/services/github_client.py):
    # a transient RequestError shouldn't fail the call outright.
    ok_response = httpx.Response(200, json={"login": "acme"}, request=httpx.Request("GET", "https://x/y"))
    call_count = 0

    def fake_get(url, headers):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise httpx.ConnectError("network down", request=httpx.Request("GET", url))
        return ok_response

    with patch("time.sleep"), patch("httpx.Client.get", side_effect=fake_get):
        result = _get("https://x/y", "tok")
    assert result == {"login": "acme"}
    assert call_count == 2


def test_get_retries_on_429_then_succeeds():
    request = httpx.Request("GET", "https://x/y")
    rate_limited = httpx.Response(429, request=request)
    ok_response = httpx.Response(200, json={"login": "acme"}, request=request)
    responses = iter([rate_limited, ok_response])

    with patch("time.sleep"), patch("httpx.Client.get", side_effect=lambda url, headers: next(responses)):
        result = _get("https://x/y", "tok")
    assert result == {"login": "acme"}


def test_get_retries_on_secondary_rate_limit_403_then_succeeds():
    # Regression test for issue #219: GitHub's secondary/abuse rate limit commonly
    # returns 403 (not 429), often with a Retry-After header -- especially under this
    # codebase's concurrent ThreadPoolExecutor fan-out.
    request = httpx.Request("GET", "https://x/y")
    rate_limited = httpx.Response(403, headers={"Retry-After": "1"}, request=request)
    ok_response = httpx.Response(200, json={"login": "acme"}, request=request)
    responses = iter([rate_limited, ok_response])

    with patch("time.sleep"), patch("httpx.Client.get", side_effect=lambda url, headers: next(responses)):
        result = _get("https://x/y", "tok")
    assert result == {"login": "acme"}


def test_get_retries_on_5xx_then_succeeds():
    # Regression test for #245: a 5xx was previously returned as-is with no retry,
    # immediately raised via raise_for_status -- presumed transient (GitHub-side issue),
    # same as every other GitHub-talking client in this codebase.
    request = httpx.Request("GET", "https://x/y")
    server_error = httpx.Response(502, request=request)
    ok_response = httpx.Response(200, json={"login": "acme"}, request=request)
    responses = iter([server_error, ok_response])

    with patch("time.sleep"), patch("httpx.Client.get", side_effect=lambda url, headers: next(responses)):
        result = _get("https://x/y", "tok")
    assert result == {"login": "acme"}


def test_get_honors_the_actual_retry_after_value_not_a_blind_backoff():
    # Regression test for #245: the retry loop detected Retry-After's *presence* but
    # ignored its value, always sleeping a fixed 2**attempt. If GitHub asks for a 60s
    # backoff, blindly sleeping 1s/2s just burns the fixed attempt budget hitting the
    # same limit again.
    request = httpx.Request("GET", "https://x/y")
    rate_limited = httpx.Response(429, headers={"Retry-After": "5"}, request=request)
    ok_response = httpx.Response(200, json={"login": "acme"}, request=request)
    responses = iter([rate_limited, ok_response])

    with patch("time.sleep") as mock_sleep, patch("httpx.Client.get", side_effect=lambda url, headers: next(responses)):
        result = _get("https://x/y", "tok")
    assert result == {"login": "acme"}
    mock_sleep.assert_called_once_with(5.0)


def test_get_caps_an_excessive_retry_after_value():
    request = httpx.Request("GET", "https://x/y")
    rate_limited = httpx.Response(429, headers={"Retry-After": "600"}, request=request)
    ok_response = httpx.Response(200, json={"login": "acme"}, request=request)
    responses = iter([rate_limited, ok_response])

    with patch("time.sleep") as mock_sleep, patch("httpx.Client.get", side_effect=lambda url, headers: next(responses)):
        _get("https://x/y", "tok")
    mock_sleep.assert_called_once_with(_MAX_RETRY_AFTER_SECONDS)


def test_get_falls_back_to_ratelimit_reset_when_no_retry_after():
    # GitHub's docs say to wait until X-RateLimit-Reset (epoch seconds) when
    # X-RateLimit-Remaining is 0 but no Retry-After was sent -- the common primary
    # rate-limit shape. Previously this hit a blind 2**attempt = 1s retry that just
    # burned the attempt budget.
    request = httpx.Request("GET", "https://x/y")
    reset_at = time.time() + 30
    rate_limited = httpx.Response(
        429, headers={"X-RateLimit-Reset": str(int(reset_at))}, request=request
    )
    ok_response = httpx.Response(200, json={"login": "acme"}, request=request)
    responses = iter([rate_limited, ok_response])

    with patch("time.sleep") as mock_sleep, patch("httpx.Client.get", side_effect=lambda url, headers: next(responses)):
        result = _get("https://x/y", "tok")
    assert result == {"login": "acme"}
    slept = mock_sleep.call_args[0][0]
    assert 20 <= slept <= 30


def test_get_waits_the_cap_for_a_ratelimit_with_no_usable_header():
    # A 429 with neither Retry-After nor X-RateLimit-Reset waits the full documented
    # minimum backoff, not a fast exponential retry into the same limit.
    request = httpx.Request("GET", "https://x/y")
    rate_limited = httpx.Response(429, request=request)
    ok_response = httpx.Response(200, json={"login": "acme"}, request=request)
    responses = iter([rate_limited, ok_response])

    with patch("time.sleep") as mock_sleep, patch("httpx.Client.get", side_effect=lambda url, headers: next(responses)):
        _get("https://x/y", "tok")
    mock_sleep.assert_called_once_with(_MAX_RETRY_AFTER_SECONDS)


def test_get_does_not_retry_a_plain_permission_denied_403():
    # A genuine permission-denied 403 (token lacks scope) has neither Retry-After nor
    # X-RateLimit-Remaining: 0 -- must still surface immediately, not be mistaken for a
    # rate limit and retried away. (_get itself doesn't inspect status, just returns/
    # raises via raise_for_status -- assert the retry loop doesn't sleep/retry it.)
    request = httpx.Request("GET", "https://x/y")
    forbidden = httpx.Response(403, request=request)

    with (
        patch("time.sleep") as mock_sleep,
        patch("httpx.Client.get", return_value=forbidden) as mock_get,
    ):
        with pytest.raises(httpx.HTTPStatusError):
            _get("https://x/y", "tok")

    mock_sleep.assert_not_called()
    assert mock_get.call_count == 1


def test_get_gives_up_after_3_attempts_on_persistent_connection_error():
    def fake_get(url, headers):
        raise httpx.ConnectError("network down", request=httpx.Request("GET", url))

    with patch("time.sleep"), patch("httpx.Client.get", side_effect=fake_get) as mock_get:
        with pytest.raises(httpx.ConnectError):
            _get("https://x/y", "tok")
    assert mock_get.call_count == 3


# ── _get_all_pages pagination cap (issue #214: unbounded pagination -> OOM) ────


def test_get_all_pages_follows_link_header_across_pages():
    def fake_get(url, headers):
        page = int(url.split("&page=")[-1]) if "&page=" in url else 1
        if page < 3:
            link = f'<https://x/y?per_page=100&page={page + 1}>; rel="next"'
            return httpx.Response(200, json=[{"id": page}], headers={"Link": link}, request=httpx.Request("GET", url))
        return httpx.Response(200, json=[{"id": page}], request=httpx.Request("GET", url))

    with patch("httpx.Client.get", side_effect=fake_get):
        results = _get_all_pages("https://x", "/y", "tok")
    assert results == [{"id": 1}, {"id": 2}, {"id": 3}]


def test_get_all_pages_truncates_after_max_pages_and_logs_warning(caplog):
    call_count = 0

    def fake_get(url, headers):
        nonlocal call_count
        call_count += 1
        link = '<https://x/y?page=next>; rel="next"'
        return httpx.Response(200, json=[{"id": call_count}], headers={"Link": link}, request=httpx.Request("GET", url))

    with patch("httpx.Client.get", side_effect=fake_get):
        with caplog.at_level("WARNING"):
            results = _get_all_pages("https://x", "/y", "tok")

    # Stops at the cap rather than following the (still-present) Link header forever --
    # this is the behavior that prevents an unbounded in-memory list on a huge org.
    assert len(results) == _MAX_PAGES
    assert call_count == _MAX_PAGES
    assert "Truncating pagination" in caplog.text


# ── DependabotAlertsCheck ────────────────────────────────────────────────────


def test_dependabot_empty_org_is_not_applicable():
    check = DependabotAlertsCheck()
    result = check.run(owner="acme", token="tok", repos=[])
    assert result["status"] == "not_applicable"
    assert result["value"] == {"critical": 0, "high": 0, "medium": 0, "low": 0}


def test_dependabot_aggregates_severity_counts_across_repos():
    check = DependabotAlertsCheck()
    repos = [{"name": "api"}, {"name": "ui"}]
    responses = {
        "api": [{"security_advisory": {"severity": "critical"}}, {"security_advisory": {"severity": "low"}}],
        "ui": [{"security_advisory": {"severity": "high"}}],
    }

    def fake_get(url, token):
        for name, alerts in responses.items():
            if f"/{name}/dependabot/alerts" in url:
                return alerts
        raise AssertionError(f"unexpected url {url}")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("checks.github_checks._get", fake_get)
        result = check.run(owner="acme", token="tok", repos=repos)
    assert result["status"] == "fail"
    assert result["value"] == {"critical": 1, "high": 1, "medium": 0, "low": 1}


def test_dependabot_passes_when_no_critical_or_high():
    check = DependabotAlertsCheck()
    repos = [{"name": "api"}]

    def fake_get(url, token):
        return [{"security_advisory": {"severity": "low"}}]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("checks.github_checks._get", fake_get)
        result = check.run(owner="acme", token="tok", repos=repos)
    assert result["status"] == "pass"


def test_dependabot_disabled_repo_404_counts_as_zero_alerts():
    check = DependabotAlertsCheck()
    repos = [{"name": "api"}]

    def fake_get(url, token):
        response = httpx.Response(404, request=httpx.Request("GET", url))
        raise httpx.HTTPStatusError("not found", request=response.request, response=response)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("checks.github_checks._get", fake_get)
        result = check.run(owner="acme", token="tok", repos=repos)
    assert result["status"] == "pass"
    assert result["value"] == {"critical": 0, "high": 0, "medium": 0, "low": 0}


def test_dependabot_all_repos_forbidden_returns_error_not_false_pass():
    # A token lacking the security-events scope gets a 403 (not 404) from every
    # repo -- that must surface as "unknown", not silently render as "clear".
    check = DependabotAlertsCheck()
    repos = [{"name": "api"}, {"name": "ui"}]

    def fake_get(url, token):
        response = httpx.Response(403, request=httpx.Request("GET", url))
        raise httpx.HTTPStatusError("forbidden", request=response.request, response=response)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("checks.github_checks._get", fake_get)
        result = check.run(owner="acme", token="tok", repos=repos)
    assert result["status"] == "error"


def test_dependabot_mixed_403_and_otherwise_clean_repos_reports_error_not_false_pass():
    # Regression test for #245: 403 means unknown, not zero. A clean result from only
    # the *reachable* repos must not be reported as "pass" while some repos' real alert
    # counts are still unknown -- e.g. a fine-grained PAT missing the security-events
    # scope for a subset of repos, with real critical/high alerts hiding on those.
    check = DependabotAlertsCheck()
    repos = [{"name": "forbidden"}, {"name": "clean"}]

    def fake_get(url, token):
        if "/forbidden/" in url:
            response = httpx.Response(403, request=httpx.Request("GET", url))
            raise httpx.HTTPStatusError("forbidden", request=response.request, response=response)
        return []

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("checks.github_checks._get", fake_get)
        result = check.run(owner="acme", token="tok", repos=repos)
    assert result["status"] == "error"


def test_dependabot_mixed_403_and_a_real_alert_still_fails():
    # A "fail" from the repos we could actually see stays valid regardless of unknowns
    # elsewhere -- only the false-clean ("pass") case needs downgrading to "error".
    check = DependabotAlertsCheck()
    repos = [{"name": "forbidden"}, {"name": "vulnerable"}]

    def fake_get(url, token):
        if "/forbidden/" in url:
            response = httpx.Response(403, request=httpx.Request("GET", url))
            raise httpx.HTTPStatusError("forbidden", request=response.request, response=response)
        return [{"security_advisory": {"severity": "critical"}}]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("checks.github_checks._get", fake_get)
        result = check.run(owner="acme", token="tok", repos=repos)
    assert result["status"] == "fail"


def test_dependabot_non_404_403_error_propagates():
    check = DependabotAlertsCheck()
    repos = [{"name": "api"}]

    def fake_get(url, token):
        response = httpx.Response(500, request=httpx.Request("GET", url))
        raise httpx.HTTPStatusError("server error", request=response.request, response=response)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("checks.github_checks._get", fake_get)
        with pytest.raises(httpx.HTTPStatusError):
            check.run(owner="acme", token="tok", repos=repos)


# ── CodeScanningCheck ────────────────────────────────────────────────────────


def test_code_scanning_empty_org_is_not_applicable():
    check = CodeScanningCheck()
    result = check.run(owner="acme", token="tok", repos=[])
    assert result["status"] == "not_applicable"
    assert result["value"] == {"open": 0, "repos_with_alerts": 0, "total_repos": 0}


def test_code_scanning_counts_open_alerts_and_affected_repos():
    check = CodeScanningCheck()
    repos = [{"name": "api"}, {"name": "ui"}]

    def fake_get(url, token):
        if "/api/" in url:
            return [{"number": 1}, {"number": 2}]
        return []

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("checks.github_checks._get", fake_get)
        result = check.run(owner="acme", token="tok", repos=repos)
    assert result["status"] == "fail"
    assert result["value"] == {"open": 2, "repos_with_alerts": 1, "total_repos": 2}


def test_code_scanning_disabled_repo_404_counts_as_zero_alerts():
    check = CodeScanningCheck()
    repos = [{"name": "api"}]

    def fake_get(url, token):
        response = httpx.Response(404, request=httpx.Request("GET", url))
        raise httpx.HTTPStatusError("not found", request=response.request, response=response)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("checks.github_checks._get", fake_get)
        result = check.run(owner="acme", token="tok", repos=repos)
    assert result["status"] == "pass"
    assert result["value"] == {"open": 0, "repos_with_alerts": 0, "total_repos": 1}


def test_code_scanning_all_repos_forbidden_returns_error_not_false_pass():
    check = CodeScanningCheck()
    repos = [{"name": "api"}]

    def fake_get(url, token):
        response = httpx.Response(403, request=httpx.Request("GET", url))
        raise httpx.HTTPStatusError("forbidden", request=response.request, response=response)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("checks.github_checks._get", fake_get)
        result = check.run(owner="acme", token="tok", repos=repos)
    assert result["status"] == "error"


def test_code_scanning_mixed_403_and_otherwise_clean_repos_reports_error_not_false_pass():
    # Regression test for #245: same reasoning as the Dependabot equivalent -- 403 means
    # unknown, not zero, so a clean result from the reachable repos alone must not be
    # reported as "pass" while some repos' real alert status is still unknown.
    check = CodeScanningCheck()
    repos = [{"name": "forbidden"}, {"name": "clean"}]

    def fake_get(url, token):
        if "/forbidden/" in url:
            response = httpx.Response(403, request=httpx.Request("GET", url))
            raise httpx.HTTPStatusError("forbidden", request=response.request, response=response)
        return []

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("checks.github_checks._get", fake_get)
        result = check.run(owner="acme", token="tok", repos=repos)
    assert result["status"] == "error"


def test_code_scanning_mixed_403_and_a_real_alert_still_fails():
    check = CodeScanningCheck()
    repos = [{"name": "forbidden"}, {"name": "vulnerable"}]

    def fake_get(url, token):
        if "/forbidden/" in url:
            response = httpx.Response(403, request=httpx.Request("GET", url))
            raise httpx.HTTPStatusError("forbidden", request=response.request, response=response)
        return [{"number": 1}]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("checks.github_checks._get", fake_get)
        result = check.run(owner="acme", token="tok", repos=repos)
    assert result["status"] == "fail"


# ── DefaultBranchNoForcePushCheck ────────────────────────────────────────────


def test_force_push_empty_org_is_not_applicable():
    check = DefaultBranchNoForcePushCheck()
    result = check.run(owner="acme", token="tok", repos=[])
    assert result["status"] == "not_applicable"
    assert result["value"] == {"repos_checked": 0, "force_push_allowed": 0}


def test_force_push_passes_when_disallowed():
    check = DefaultBranchNoForcePushCheck()
    repos = [{"name": "api", "default_branch": "main"}]

    def fake_get(url, token):
        # The dedicated /branches/{branch}/protection sub-resource -- this is where
        # allow_force_pushes actually lives (the plain branch endpoint never returns it).
        assert url.endswith("/repos/acme/api/branches/main/protection")
        return {"allow_force_pushes": {"enabled": False}}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("checks.github_checks._get", fake_get)
        result = check.run(owner="acme", token="tok", repos=repos)
    assert result["status"] == "pass"
    assert result["value"] == {"repos_checked": 1, "force_push_allowed": 0}


def test_force_push_fails_when_allowed():
    check = DefaultBranchNoForcePushCheck()
    repos = [{"name": "api", "default_branch": "main"}]

    def fake_get(url, token):
        assert url.endswith("/repos/acme/api/branches/main/protection")
        return {"allow_force_pushes": {"enabled": True}}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("checks.github_checks._get", fake_get)
        result = check.run(owner="acme", token="tok", repos=repos)
    assert result["status"] == "fail"
    assert result["value"] == {"repos_checked": 1, "force_push_allowed": 1}


def test_force_push_unprotected_branch_404_means_force_push_is_allowed():
    # Regression test for #245: a 404 from the branch-protection endpoint ("Branch not
    # protected") means the default branch has NO protection at all -- force pushes are
    # unambiguously allowed, not
    # merely "unknown". Previously this was excluded from the denominator entirely, so
    # an org where every repo's default branch was completely unprotected (the exact
    # worst case this check exists to catch) reported checked == 0 -> "error", never
    # the "fail" it should.
    check = DefaultBranchNoForcePushCheck()
    repos = [{"name": "api", "default_branch": "main"}]

    def fake_get(url, token):
        response = httpx.Response(404, request=httpx.Request("GET", url))
        raise httpx.HTTPStatusError("not found", request=response.request, response=response)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("checks.github_checks._get", fake_get)
        result = check.run(owner="acme", token="tok", repos=repos)
    assert result["status"] == "fail"
    assert result["value"] == {"repos_checked": 1, "force_push_allowed": 1}


def test_force_push_rate_limited_branch_excluded_from_denominator():
    # A 403/429 genuinely can't be evaluated -- distinct from the 404 case above, this
    # must still be excluded from the denominator rather than counted either way.
    check = DefaultBranchNoForcePushCheck()
    repos = [{"name": "api", "default_branch": "main"}]

    def fake_get(url, token):
        response = httpx.Response(403, request=httpx.Request("GET", url))
        raise httpx.HTTPStatusError("forbidden", request=response.request, response=response)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("checks.github_checks._get", fake_get)
        result = check.run(owner="acme", token="tok", repos=repos)
    assert result["status"] == "error"
    assert result["value"] == {"repos_checked": 0, "force_push_allowed": 0}
