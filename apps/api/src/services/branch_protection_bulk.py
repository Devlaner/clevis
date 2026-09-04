"""Bulk branch-protection apply across many repos (issue #288).

An org admin picks a small preset (required approvals, enforce-on-admins,
block-force-push, block-deletion) and a set of repos, previews a per-repo diff, then
applies it to each repo's default branch in one action.

**Requires the ``administration`` repository permission at Read and write** (a 403
becomes a 400 pointing at docs/self-hosting.md).

The preset only controls those four knobs. GitHub's ``PUT .../protection`` *replaces*
the whole protection object, so for an already-protected branch Clevis rebuilds the
full body from the existing rules (via ``check_remediation._preserving_put_body``) and
overlays only the knobs — required status checks, linear-history, code-owner reviews,
conversation-resolution, etc. are carried across untouched, and the dry-run diff shows
every key that would actually change. A branch whose protection restricts *who* can
push (a users/teams/apps allowlist) can't be round-tripped safely through the API, so
that repo is reported as an error and left alone — same guard as "Fix this" (#287).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx

from src.services.check_remediation import (
    _DEFAULT_BRANCH_PROTECTION,
    RemediationConflict,
    _preserving_put_body,
)
from src.services.github_client import GitHubClient

# GitHub repo names: letters, digits, ``.  _  -``; 1–100 chars; never "." or "..".
# ``owner`` is always the trusted org login; only ``repos[]`` is caller-supplied, so a
# name with a slash or ".." could otherwise walk the API path to another repo/org.
_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

# The knob keys a preset controls (flattened form). Everything else is preserved.
_KNOB_KEYS = ("required_approving_review_count", "enforce_admins", "allow_force_pushes", "allow_deletions")

# Keys of the PUT body worth showing in the diff.
_DIFF_KEYS = (
    "required_status_checks",
    "enforce_admins",
    "required_pull_request_reviews",
    "restrictions",
    "allow_force_pushes",
    "allow_deletions",
    "required_linear_history",
    "block_creations",
    "required_conversation_resolution",
)


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
    """Flatten the submitted preset to the four knob keys, dropping anything else.
    Accepts ``required_pull_request_reviews.required_approving_review_count`` (the UI's
    shape) or a bare ``required_approving_review_count``."""
    preset = preset or {}
    knobs: dict = {}
    reviews = preset.get("required_pull_request_reviews")
    if isinstance(reviews, dict) and "required_approving_review_count" in reviews:
        knobs["required_approving_review_count"] = reviews["required_approving_review_count"]
    for key in _KNOB_KEYS:
        if key in preset:
            knobs[key] = preset[key]
    return knobs


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


def _put_body(protection: dict | None, knobs: dict) -> dict:
    """The full ``PUT .../protection`` body: the branch's existing rules (or the
    conservative default when unprotected) with the preset's knobs overlaid. Raises
    ``RemediationConflict`` when the existing protection has a push allowlist."""
    if protection:
        body = _preserving_put_body(protection)  # raises RemediationConflict on restrictions
        # _preserving_put_body doesn't round-trip allow_force_pushes — carry it explicitly
        # so a repo that already allows force-pushes isn't silently locked down when the
        # preset leaves that knob alone.
        body["allow_force_pushes"] = _current_for(protection, "allow_force_pushes")
    else:
        body = dict(_DEFAULT_BRANCH_PROTECTION)

    if "required_approving_review_count" in knobs:
        reviews = dict(body.get("required_pull_request_reviews") or {})
        reviews["required_approving_review_count"] = int(knobs["required_approving_review_count"])
        body["required_pull_request_reviews"] = reviews
    for key in ("enforce_admins", "allow_force_pushes", "allow_deletions"):
        if key in knobs:
            body[key] = bool(knobs[key])
    return body


def _current_for(protection: dict | None, key: str):
    """The current value of ``key`` in a shape comparable to the PUT body."""
    if not protection:
        return None
    val = protection.get(key)
    if key in (
        "enforce_admins",
        "allow_force_pushes",
        "allow_deletions",
        "required_linear_history",
        "block_creations",
        "required_conversation_resolution",
    ):
        return bool(val.get("enabled")) if isinstance(val, dict) else bool(val)
    if key == "required_pull_request_reviews":
        if not isinstance(val, dict):
            return None
        return {
            "dismiss_stale_reviews": bool(val.get("dismiss_stale_reviews")),
            "require_code_owner_reviews": bool(val.get("require_code_owner_reviews")),
            "required_approving_review_count": val.get("required_approving_review_count"),
        }
    if key == "required_status_checks":
        if not isinstance(val, dict):
            return None
        return {"strict": bool(val.get("strict")), "contexts": list(val.get("contexts") or [])}
    return val  # restrictions


def _diff(protection: dict | None, body: dict) -> dict:
    changes: dict = {}
    for key in _DIFF_KEYS:
        if key not in body:
            continue
        current = _current_for(protection, key)
        want = body[key]
        if current != want:
            changes[key] = {"from": current, "to": want}
    return changes


def _prepare(client: GitHubClient, owner: str, repo: str, knobs: dict) -> tuple[str, dict | None, dict]:
    """(default_branch, current protection, computed PUT body) for one repo."""
    branch = _default_branch(client, owner, repo)
    protection = _get_protection(client, owner, repo, branch)
    return branch, protection, _put_body(protection, knobs)


def plan_bulk(client: GitHubClient, owner: str, repos: list[str], preset: dict | None) -> list[RepoDiff]:
    """Per repo: read the default branch's protection and diff the full computed PUT
    body against it. Per-repo errors are captured on the ``RepoDiff``."""
    knobs = normalize_preset(preset)
    diffs: list[RepoDiff] = []
    for repo in repos:
        if not _REPO_NAME_RE.match(repo):
            diffs.append(RepoDiff(repo, "", False, error="invalid repository name"))
            continue
        try:
            branch, protection, body = _prepare(client, owner, repo, knobs)
        except RemediationConflict as exc:
            diffs.append(RepoDiff(repo, "", True, error=str(exc)))
            continue
        except httpx.HTTPStatusError as exc:
            diffs.append(RepoDiff(repo, "", False, error=f"GitHub API error: {exc.response.status_code}"))
            continue
        except httpx.RequestError:
            diffs.append(RepoDiff(repo, "", False, error="GitHub API unreachable"))
            continue
        diffs.append(RepoDiff(repo, branch, protection is not None, _diff(protection, body)))
    return diffs


def apply_bulk(client: GitHubClient, owner: str, repos: list[str], preset: dict | None) -> list[RepoResult]:
    """PUT the computed body to each repo's default branch. Per-repo try/except so a
    partial failure doesn't abort the rest."""
    knobs = normalize_preset(preset)
    results: list[RepoResult] = []
    for repo in repos:
        if not _REPO_NAME_RE.match(repo):
            results.append(RepoResult(repo, applied=False, error="invalid repository name"))
            continue
        try:
            branch, _protection, body = _prepare(client, owner, repo, knobs)
            client.request("PUT", _protection_path(owner, repo, branch), json=body)
            results.append(RepoResult(repo, applied=True))
        except RemediationConflict as exc:
            results.append(RepoResult(repo, applied=False, error=str(exc)))
        except httpx.HTTPStatusError as exc:
            results.append(RepoResult(repo, applied=False, error=f"GitHub API error: {exc.response.status_code}"))
        except httpx.RequestError:
            results.append(RepoResult(repo, applied=False, error="GitHub API unreachable"))
    return results
