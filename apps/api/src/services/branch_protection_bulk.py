"""Bulk branch-protection apply across many repos (issue #288).

Instead of fixing each repo's default-branch protection one repo at a time on GitHub,
an org admin picks a preset and a set of repos and applies it in one action — with a
dry-run diff first.

**Requires the ``administration`` repository permission at Read and write** on the App
/ PAT (documented in docs/self-hosting.md; a 403 from GitHub becomes a 400 pointing
there). The preset shape mirrors ``check_remediation._DEFAULT_BRANCH_PROTECTION``.

Unlike ``check_remediation._protect_default_branch``, an explicit bulk apply *sets* the
chosen preset rather than preserving whatever is already configured — the admin picked
these values on purpose. The dry-run diff shows exactly what each repo would change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import quote

import httpx

from src.services.github_client import GitHubClient

# The PUT-body keys a preset may override. Anything else in a submitted preset is ignored.
_PRESET_KEYS = (
    "required_status_checks",
    "enforce_admins",
    "required_pull_request_reviews",
    "restrictions",
    "allow_force_pushes",
    "allow_deletions",
)

# Conservative default, same intent as check_remediation._DEFAULT_BRANCH_PROTECTION:
# one approving review, no force-push, no deletion, don't enforce on admins, and no
# required status checks (Clevis can't know a repo's CI job names).
DEFAULT_PRESET: dict = {
    "required_status_checks": None,
    "enforce_admins": False,
    "required_pull_request_reviews": {"required_approving_review_count": 1},
    "restrictions": None,
    "allow_force_pushes": False,
    "allow_deletions": False,
}


@dataclass
class RepoDiff:
    repo: str
    branch: str
    currently_protected: bool
    changes: dict = field(default_factory=dict)  # key -> {"from": ..., "to": ...}
    error: str | None = None

    @property
    def would_change(self) -> bool:
        return bool(self.changes)


@dataclass
class RepoResult:
    repo: str
    applied: bool
    error: str | None = None


def normalize_preset(preset: dict | None) -> dict:
    merged = dict(DEFAULT_PRESET)
    for key, value in (preset or {}).items():
        if key in _PRESET_KEYS:
            merged[key] = value
    return merged


def _default_branch(client: GitHubClient, owner: str, repo: str) -> str:
    info = client.request("GET", f"/repos/{owner}/{repo}")
    return info.get("default_branch", "main") if isinstance(info, dict) else "main"


def _protection_path(owner: str, repo: str, branch: str) -> str:
    # A branch name can contain slashes ("release/1.x"); keep it one path segment.
    return f"/repos/{owner}/{repo}/branches/{quote(branch, safe='')}/protection"


def _get_protection(client: GitHubClient, owner: str, repo: str, branch: str) -> dict | None:
    try:
        result = client.request("GET", _protection_path(owner, repo, branch))
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None  # branch has no protection at all
        raise
    return result if isinstance(result, dict) else None


def _current_value(protection: dict | None, key: str):
    """The current effective value of ``key``, normalised to the same shape ``key``
    takes in a preset so ``!=`` is a meaningful comparison."""
    if not protection:
        return None
    if key in ("enforce_admins", "allow_force_pushes", "allow_deletions"):
        value = protection.get(key)
        return bool(value.get("enabled")) if isinstance(value, dict) else bool(value)
    if key == "required_pull_request_reviews":
        reviews = protection.get("required_pull_request_reviews")
        if not isinstance(reviews, dict):
            return None
        return {"required_approving_review_count": reviews.get("required_approving_review_count")}
    if key == "required_status_checks":
        checks = protection.get("required_status_checks")
        if not isinstance(checks, dict):
            return None
        return {"strict": bool(checks.get("strict")), "contexts": list(checks.get("contexts") or [])}
    # key == "restrictions" (the only remaining _PRESET_KEYS entry)
    return protection.get("restrictions")


def plan_bulk(client: GitHubClient, owner: str, repos: list[str], preset: dict | None) -> list[RepoDiff]:
    """Per repo: read the default branch's current protection and diff it against the
    preset. Per-repo errors are captured on the ``RepoDiff`` so one bad repo doesn't
    abort the preview."""
    desired = normalize_preset(preset)
    diffs: list[RepoDiff] = []
    for repo in repos:
        try:
            branch = _default_branch(client, owner, repo)
            protection = _get_protection(client, owner, repo, branch)
        except httpx.HTTPStatusError as exc:
            diffs.append(RepoDiff(repo, "", False, error=f"GitHub API error: {exc.response.status_code}"))
            continue
        except httpx.RequestError:
            diffs.append(RepoDiff(repo, "", False, error="GitHub API unreachable"))
            continue

        changes: dict = {}
        for key in _PRESET_KEYS:
            current = _current_value(protection, key)
            want = desired[key]
            if current != want:
                changes[key] = {"from": current, "to": want}
        diffs.append(RepoDiff(repo, branch, protection is not None, changes))
    return diffs


def apply_bulk(client: GitHubClient, owner: str, repos: list[str], preset: dict | None) -> list[RepoResult]:
    """PUT the normalised preset to each repo's default branch. Per-repo try/except so a
    partial failure (one repo the token can't touch) doesn't abort the rest."""
    desired = normalize_preset(preset)
    results: list[RepoResult] = []
    for repo in repos:
        try:
            branch = _default_branch(client, owner, repo)
            client.request("PUT", _protection_path(owner, repo, branch), json=desired)
            results.append(RepoResult(repo, applied=True))
        except httpx.HTTPStatusError as exc:
            results.append(RepoResult(repo, applied=False, error=f"GitHub API error: {exc.response.status_code}"))
        except httpx.RequestError:
            results.append(RepoResult(repo, applied=False, error="GitHub API unreachable"))
    return results
