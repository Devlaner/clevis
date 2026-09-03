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

from urllib.parse import quote

import httpx

from src.services.github_client import GitHubClient


class RemediationNotSupported(Exception):
    """The given check_id has no automated fix (see the module docstring)."""


class RemediationConflict(Exception):
    """The fix would have to overwrite existing configuration that can't be
    faithfully reconstructed through the API (see _protect_default_branch).
    Surfaced to the caller as a 409 rather than silently clobbering it."""


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
    # A branch name can contain slashes ("release/1.x"); keep it one path segment.
    path = f"/repos/{owner}/{repo}/branches/{quote(branch, safe='')}/protection"

    current = _get_branch_protection(client, path)
    if not current:
        # No protection at all -> apply the conservative default.
        client.request("PUT", path, json=_DEFAULT_BRANCH_PROTECTION)
        return

    # Protection already exists (the "allows force pushes" check): carry every rule
    # that's already configured across unchanged and only turn force-pushes off.
    # Re-sending _DEFAULT_BRANCH_PROTECTION would silently drop required status
    # checks, stricter review rules, linear-history, etc.
    body = _preserving_put_body(current)
    body["allow_force_pushes"] = False
    client.request("PUT", path, json=body)


def _get_branch_protection(client: GitHubClient, path: str) -> dict | None:
    """Current branch protection, or None when the branch has none (GitHub 404s)."""
    try:
        result = client.request("GET", path)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise
    return result if isinstance(result, dict) and result else None


def _preserving_put_body(current: dict) -> dict:
    """Translate GitHub's *GET* branch-protection response into the *PUT* body
    shape, keeping every currently-enabled rule."""
    if isinstance(current.get("restrictions"), dict):
        # PUT wants restrictions as {users:[login], teams:[slug], apps:[slug]}, but
        # GET returns full objects and the field only works on org-owned repos.
        # Getting this wrong could lock maintainers out -- refuse instead.
        raise RemediationConflict(
            "This branch's protection restricts who can push (specific users, teams "
            "or apps). Clevis can't rewrite that safely through the API -- turn off "
            "\"Allow force pushes\" for the default branch manually."
        )

    def _enabled(key: str) -> bool:
        value = current.get(key)
        return bool(value.get("enabled")) if isinstance(value, dict) else bool(value)

    status_checks = current.get("required_status_checks")
    if isinstance(status_checks, dict):
        status_checks = {
            "strict": bool(status_checks.get("strict")),
            "contexts": list(status_checks.get("contexts") or []),
        }
    else:
        status_checks = None

    reviews = current.get("required_pull_request_reviews")
    if isinstance(reviews, dict):
        reviews = {
            "dismiss_stale_reviews": bool(reviews.get("dismiss_stale_reviews")),
            "require_code_owner_reviews": bool(reviews.get("require_code_owner_reviews")),
            "required_approving_review_count": int(
                reviews.get("required_approving_review_count", 1)
            ),
        }
    else:
        reviews = None

    return {
        "required_status_checks": status_checks,
        "enforce_admins": _enabled("enforce_admins"),
        "required_pull_request_reviews": reviews,
        "restrictions": None,
        "required_linear_history": _enabled("required_linear_history"),
        "allow_deletions": _enabled("allow_deletions"),
        "block_creations": _enabled("block_creations"),
        "required_conversation_resolution": _enabled("required_conversation_resolution"),
    }


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
