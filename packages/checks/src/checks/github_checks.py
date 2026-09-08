import logging
import time

import httpx

from checks.base import Check, CheckMetadata

logger = logging.getLogger(__name__)

# Hard cap on pages fetched by _get_all_pages -- at per_page=100 this is 5,000 items max.
# Without this, a large enough org (thousands of repos/members/workflow runs) can pull an
# unbounded number of objects into one in-memory list during a scan, which is a plausible
# direct cause of OOM crashes in the api process (see issue #214). Truncating with a warning
# degrades a scan's completeness rather than crashing the whole run_all_checks() call, since
# the runner.py prefetch that calls this isn't wrapped in try/except.
_MAX_PAGES = 50


def _is_secondary_rate_limit(r: httpx.Response) -> bool:
    """GitHub's secondary/abuse rate limit commonly returns 403 (not 429), often with a
    Retry-After header -- especially under this codebase's concurrent ThreadPoolExecutor
    fan-out. Without this, a 403 that would succeed on retry raises immediately instead of
    backing off like the 429 case already does."""
    if r.status_code != 403:
        return False
    return "Retry-After" in r.headers or r.headers.get("X-RateLimit-Remaining") == "0"

# Cap on honoring a server-supplied Retry-After value, so a single malformed/huge value
# can't block a scan for an unreasonable amount of time -- GitHub's real rate-limit
# Retry-After values are ordinarily well under this.
_MAX_RETRY_AFTER_SECONDS = 60


def _retry_delay_seconds(r: httpx.Response, attempt: int) -> float:
    """Prefer the server's own Retry-After value (GitHub's rate-limit responses include
    one) over a blind exponential backoff -- retrying before the window it asked for just
    burns the fixed attempt budget hitting the same limit again. Falls back to
    X-RateLimit-Reset (epoch seconds) when Retry-After is absent -- GitHub's docs say to
    wait until that reset time when X-RateLimit-Remaining is 0 but no Retry-After was sent.
    If neither header is usable, a 429 or a secondary-limit 403 waits the full cap
    (GitHub's documented minimum rate-limit backoff) rather than a fast exponential retry
    that would just hit the same limit again; any other status (a plain 5xx) still uses
    exponential backoff. Ported from apps/worker/src/backfill.py's copy of this contract."""
    raw = r.headers.get("Retry-After")
    if raw is not None:
        try:
            return min(float(raw), _MAX_RETRY_AFTER_SECONDS)
        except ValueError:
            pass
    reset_raw = r.headers.get("X-RateLimit-Reset")
    if reset_raw is not None:
        try:
            delay = float(reset_raw) - time.time()
            if delay > 0:
                return min(delay, _MAX_RETRY_AFTER_SECONDS)
        except ValueError:
            pass
    if r.status_code == 429 or _is_secondary_rate_limit(r):
        return _MAX_RETRY_AFTER_SECONDS
    return 2**attempt


def _get_with_retry(client: httpx.Client, url: str, headers: dict) -> httpx.Response:
    # Same retry/backoff contract as GitHubClient.request (apps/api/src/services/github_client.py):
    # 3 attempts, exponential backoff on connection errors, a 429, or a rate-limit-flavored 403 --
    # plus a 5xx, which is presumed transient (GitHub-side issue) same as everywhere else in this
    # codebase that talks to the GitHub API.
    for attempt in range(3):
        try:
            r = client.get(url, headers=headers)
        except httpx.RequestError:
            if attempt < 2:
                time.sleep(2**attempt)
                continue
            raise
        if (r.status_code == 429 or _is_secondary_rate_limit(r) or r.status_code >= 500) and attempt < 2:
            time.sleep(_retry_delay_seconds(r, attempt))
            continue
        return r
    raise RuntimeError("request loop exhausted without returning")


def _get(url: str, token: str) -> dict | list:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    with httpx.Client(timeout=20) as client:
        r = _get_with_retry(client, url, headers)
    r.raise_for_status()
    return r.json()


def _get_all_pages(base_url: str, path: str, token: str) -> list:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    results = []
    url: str | None = f"{base_url}{path}?per_page=100"
    pages_fetched = 0
    with httpx.Client(timeout=20) as client:
        while url:
            r = _get_with_retry(client, url, headers)
            r.raise_for_status()
            results.extend(r.json())
            pages_fetched += 1
            url = None
            for part in r.headers.get("Link", "").split(","):
                part = part.strip()
                if 'rel="next"' in part:
                    url = part.split(";")[0].strip().strip("<>")
            if url and pages_fetched >= _MAX_PAGES:
                logger.warning(
                    "Truncating pagination for %s after %d pages (%d items) -- more pages were available",
                    path,
                    pages_fetched,
                    len(results),
                )
                break
    return results


def _branch_protection_status(exc: httpx.HTTPStatusError) -> str:
    code = exc.response.status_code
    if code == 404:
        return "unprotected"
    # 403/429 (no access / rate-limited) and any other unexpected status (a transient
    # 5xx, most notably) all mean branch-protection status genuinely can't be evaluated
    # right now -- falling through to "unprotected" here previously flipped an org's
    # entire branch-protection check to "fail" on a flaky GitHub 5xx even when every
    # real repo was protected.
    return "unknown"


class OrgMFARequired(Check):
    metadata = CheckMetadata(
        check_id="organization_members_mfa_required",
        title="Organization requires 2FA/MFA",
        severity="high",
        remediation="Require two-factor authentication for all org members in org settings.",
    )

    def run(
        self,
        owner: str,
        token: str,
        base_url: str = "https://api.github.com",
        repos: list | None = None,  # unused — MFA check operates at org level
    ) -> dict:
        org = _get(f"{base_url}/orgs/{owner}", token)
        if "two_factor_requirement_enabled" not in org:
            return {
                "status": "error",
                "value": "Token lacks org-owner scope to read MFA requirement status",
            }
        enabled = bool(org["two_factor_requirement_enabled"])
        return {"status": "pass" if enabled else "fail", "value": enabled}


class BranchProtectionEnabled(Check):
    metadata = CheckMetadata(
        check_id="repository_default_branch_protection_enabled",
        title="Default branch has protection rules",
        severity="high",
        remediation="Enable branch protection for default branch on all active repositories.",
    )

    def run(
        self,
        owner: str,
        token: str,
        base_url: str = "https://api.github.com",
        repos: list | None = None,
    ) -> dict:
        if repos is None:
            repos = _get_all_pages(base_url, f"/orgs/{owner}/repos", token)
        if len(repos) == 0:
            return {"status": "not_applicable", "value": {"checked": 0, "protected": 0, "unknown": 0}}
        checked = 0
        protected = 0
        unknown = 0
        for repo in repos:
            checked += 1
            branch = repo.get("default_branch")
            try:
                details = _get(f"{base_url}/repos/{owner}/{repo['name']}/branches/{branch}", token)
                if details.get("protected"):
                    protected += 1
            except httpx.HTTPStatusError as exc:
                if _branch_protection_status(exc) == "unknown":
                    unknown += 1
            except httpx.HTTPError:
                unknown += 1
        evaluable = checked - unknown
        if evaluable == 0:
            return {"status": "error", "value": {"checked": checked, "protected": protected, "unknown": unknown}}
        compliant = protected == evaluable
        return {
            "status": "pass" if compliant else "fail",
            "value": {"checked": checked, "protected": protected, "unknown": unknown},
        }


class SecretScanningEnabled(Check):
    metadata = CheckMetadata(
        check_id="repository_secret_scanning_enabled",
        title="Secret scanning enabled",
        severity="medium",
        remediation="Enable secret scanning for repositories where available.",
    )

    def run(
        self,
        owner: str,
        token: str,
        base_url: str = "https://api.github.com",
        repos: list | None = None,
    ) -> dict:
        if repos is None:
            repos = _get_all_pages(base_url, f"/orgs/{owner}/repos", token)
        total = len(repos)
        if total == 0:
            return {"status": "not_applicable", "value": {"enabled": 0, "total": 0}}
        enabled = 0
        for repo in repos:
            sec = repo.get("security_and_analysis") or {}
            if sec.get("secret_scanning", {}).get("status") == "enabled":
                enabled += 1
        compliant = enabled == total
        return {"status": "pass" if compliant else "fail", "value": {"enabled": enabled, "total": total}}


class DependabotAlertsCheck(Check):
    metadata = CheckMetadata(
        check_id="repository_dependabot_alerts_clear",
        title="No open critical/high Dependabot alerts",
        severity="high",
        remediation="Resolve or dismiss open Dependabot alerts, prioritizing critical and high severity.",
    )

    def run(
        self,
        owner: str,
        token: str,
        base_url: str = "https://api.github.com",
        repos: list | None = None,
    ) -> dict:
        if repos is None:
            repos = _get_all_pages(base_url, f"/orgs/{owner}/repos", token)
        if len(repos) == 0:
            return {"status": "not_applicable", "value": {"critical": 0, "high": 0, "medium": 0, "low": 0}}
        counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        forbidden = 0
        for repo in repos:
            try:
                alerts = _get(
                    f"{base_url}/repos/{owner}/{repo['name']}/dependabot/alerts?state=open", token
                )
            except httpx.HTTPStatusError as exc:
                # 404 means Dependabot alerts are genuinely disabled for this repo --
                # a real "no alerts" answer. 403 means the token lacks the
                # security-events scope for it -- the alert count for that repo is
                # unknown, not zero, so it must not silently count toward "clear".
                if exc.response.status_code == 404:
                    continue
                if exc.response.status_code == 403:
                    forbidden += 1
                    continue
                raise
            for alert in alerts:
                severity = (alert.get("security_advisory") or {}).get("severity")
                if severity in counts:
                    counts[severity] += 1
        if forbidden == len(repos):
            return {"status": "error", "value": counts}
        compliant = counts["critical"] == 0 and counts["high"] == 0
        if compliant and forbidden > 0:
            # 403 means unknown, not zero -- a clean result from only the *reachable*
            # repos must not be reported as "pass" while some repos' real alert counts
            # are still unknown. A "fail" from repos we could actually see stays valid
            # regardless (a real problem was found), so only the false-clean case needs
            # downgrading here.
            return {"status": "error", "value": counts}
        return {"status": "pass" if compliant else "fail", "value": counts}


class CodeScanningCheck(Check):
    metadata = CheckMetadata(
        check_id="repository_code_scanning_alerts_clear",
        title="No open code scanning alerts",
        severity="medium",
        remediation="Resolve open code scanning alerts surfaced by CodeQL or a connected SAST tool.",
    )

    def run(
        self,
        owner: str,
        token: str,
        base_url: str = "https://api.github.com",
        repos: list | None = None,
    ) -> dict:
        if repos is None:
            repos = _get_all_pages(base_url, f"/orgs/{owner}/repos", token)
        total_repos = len(repos)
        if total_repos == 0:
            return {"status": "not_applicable", "value": {"open": 0, "repos_with_alerts": 0, "total_repos": 0}}
        open_count = 0
        repos_with_alerts = 0
        forbidden = 0
        for repo in repos:
            try:
                alerts = _get(
                    f"{base_url}/repos/{owner}/{repo['name']}/code-scanning/alerts?state=open", token
                )
            except httpx.HTTPStatusError as exc:
                # 404 means code scanning is genuinely not enabled for this repo -- a
                # real "no alerts" answer. 403 means no access -- that repo's alert
                # count is unknown, not zero, so it must not silently count as clear.
                if exc.response.status_code == 404:
                    continue
                if exc.response.status_code == 403:
                    forbidden += 1
                    continue
                raise
            if alerts:
                repos_with_alerts += 1
                open_count += len(alerts)
        value = {"open": open_count, "repos_with_alerts": repos_with_alerts, "total_repos": total_repos}
        if forbidden == total_repos:
            return {"status": "error", "value": value}
        compliant = open_count == 0
        if compliant and forbidden > 0:
            # Same reasoning as DependabotAlertsCheck: a clean result from only the
            # reachable repos must not be reported as "pass" while some repos' real
            # alert status is still unknown (403 means unknown, not zero).
            return {"status": "error", "value": value}
        return {"status": "pass" if compliant else "fail", "value": value}


class DefaultBranchNoForcePushCheck(Check):
    metadata = CheckMetadata(
        check_id="repository_default_branch_no_force_push",
        title="Default branch disallows force pushes",
        severity="high",
        remediation="Disable 'Allow force pushes' in the default branch's protection rules.",
    )

    def run(
        self,
        owner: str,
        token: str,
        base_url: str = "https://api.github.com",
        repos: list | None = None,
    ) -> dict:
        if repos is None:
            repos = _get_all_pages(base_url, f"/orgs/{owner}/repos", token)
        if len(repos) == 0:
            return {"status": "not_applicable", "value": {"repos_checked": 0, "force_push_allowed": 0}}
        checked = 0
        force_push_allowed = 0
        unknown = 0
        for repo in repos:
            branch = repo.get("default_branch")
            try:
                # The dedicated branch-protection sub-resource -- the plain
                # /repos/{owner}/{repo}/branches/{branch} response only carries a reduced
                # `protection` object (enabled + required_status_checks) and never
                # `allow_force_pushes`, so reading force-push status off it always saw
                # None and this check could never return "fail" for a protected branch.
                details = _get(
                    f"{base_url}/repos/{owner}/{repo['name']}/branches/{branch}/protection", token
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    # 404 here means "Branch not protected" -- no protection rules at all,
                    # so force pushes are unambiguously allowed, not merely "unknown".
                    # Bucketing this with the genuine unknowns below meant an org where
                    # every repo's default branch had no protection reported checked == 0
                    # -> "error", never the "fail" this check exists to catch.
                    checked += 1
                    force_push_allowed += 1
                    continue
                # 403/429 (no access / rate-limited) -- force-push status genuinely
                # can't be evaluated, so exclude the repo from the denominator rather
                # than counting it as compliant.
                unknown += 1
                continue
            except httpx.HTTPError:
                # Network error -- same reasoning as the 403/429 case above.
                unknown += 1
                continue
            checked += 1
            allow_force_pushes = (details.get("allow_force_pushes") or {}).get("enabled")
            if allow_force_pushes:
                force_push_allowed += 1
        if checked == 0:
            return {"status": "error", "value": {"repos_checked": checked, "force_push_allowed": force_push_allowed}}
        return {
            "status": "pass" if force_push_allowed == 0 else "fail",
            "value": {"repos_checked": checked, "force_push_allowed": force_push_allowed},
        }
