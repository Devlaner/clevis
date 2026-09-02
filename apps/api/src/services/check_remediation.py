"""Issue #287: turn a failing security check into a one-click "Fix this".

Each supported check_id maps to the GitHub write call(s) that enable the setting
the check verifies. Only checks with a safe, unambiguous "just turn it on" fix
are here:

- Excluded: ``organization_members_mfa_required`` -- GitHub is deprecating the
  ``two_factor_requirement_enabled`` field; real enforcement is an org
  security-settings change that isn't a single idempotent call.
- Excluded: ``repository_code_scanning_alerts_clear`` -- clearing an alert means
  *dismissing* it with a "won't fix / used in tests / false positive" reason,
  which is a human judgement, not a fix.

Requires the connected GitHub App installation (or the pasted PAT) to carry
write scopes Clevis does not request by default -- ``administration:write`` (repo
settings + branch protection) and ``security_events:write`` /
``dependabot_alerts:write`` (Dependabot alerts). See docs/self-hosting.md. A 403
from GitHub is surfaced to the caller as a clear 400.
"""

from src.services.github_client import GitHubClient


class RemediationNotSupported(Exception):
    """The given check_id has no automated fix (see the module docstring)."""


# A deliberately conservative default for a branch that has no protection at all:
# require one approving PR review, block force-pushes and branch deletion, and do
# NOT enforce on admins (so a repo admin can still merge an emergency fix). No
# required status checks -- Clevis can't know this repo's CI job names. Documented
# in the PR / docs/self-hosting.md so a self-hoster knows exactly what "Fix this"
# will apply.
_DEFAULT_BRANCH_PROTECTION = {
    "required_status_checks": None,
    "enforce_admins": False,
    "required_pull_request_reviews": {"required_approving_review_count": 1},
    "restrictions": None,
    "allow_force_pushes": False,
    "allow_deletions": False,
}


def _enable_secret_scanning(client: GitHubClient, owner: str, repo: str) -> None:
    client.request(
        "PATCH",
        f"/repos/{owner}/{repo}",
        json={"security_and_analysis": {"secret_scanning": {"status": "enabled"}}},
    )


def _enable_dependabot_alerts(client: GitHubClient, owner: str, repo: str) -> None:
    client.request("PUT", f"/repos/{owner}/{repo}/vulnerability-alerts")


def _protect_default_branch(client: GitHubClient, owner: str, repo: str) -> None:
    info = client.request("GET", f"/repos/{owner}/{repo}")
    branch = info.get("default_branch", "main") if isinstance(info, dict) else "main"
    client.request(
        "PUT",
        f"/repos/{owner}/{repo}/branches/{branch}/protection",
        json=_DEFAULT_BRANCH_PROTECTION,
    )


_REMEDIATIONS = {
    "repository_secret_scanning_enabled": _enable_secret_scanning,
    "repository_dependabot_alerts_clear": _enable_dependabot_alerts,
    # Both of these are fixed by applying branch protection with force-push disabled.
    "repository_default_branch_protection_enabled": _protect_default_branch,
    "repository_default_branch_no_force_push": _protect_default_branch,
}


def supported_check_ids() -> set[str]:
    return set(_REMEDIATIONS)


def remediate(client: GitHubClient, check_id: str, owner: str, repo: str) -> None:
    """Apply the fix for ``check_id`` in ``owner/repo``. Raises RemediationNotSupported
    for a check with no automated fix; lets httpx errors from the GitHub call propagate."""
    fn = _REMEDIATIONS.get(check_id)
    if fn is None:
        raise RemediationNotSupported(check_id)
    fn(client, owner, repo)
