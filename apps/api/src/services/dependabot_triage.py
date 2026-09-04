"""Auto-triage low-risk Dependabot PRs (issue #290).

**The highest-risk item on the write-access roadmap** — approving, and especially
merging, is hard to undo — so every safety rail is on by default:

- **per-repo opt-in** via ``automation_repo_settings`` (feature ``dependabot_triage``),
  default **disabled** — the sweep touches nothing until an admin turns it on for a
  specific repo;
- ``mode`` defaults to ``approve_only``; ``approve_and_merge`` must be set explicitly
  per repo;
- a PR is acted on only when **all** of: author is ``dependabot[bot]``, not a draft,
  the bump is **patch-level** (parsed from the PR body; unparseable → skip), every
  check on the head SHA is a completed success *and* the combined commit status is
  success, and there is no pending/requested human review and no
  ``CHANGES_REQUESTED``;
- a per-run cap (default 5);
- **every** decision — act or skip, with the reason — is written to ``audit_logs`` by
  the router.

Requires ``pull_requests: write`` (approve) + ``contents: write`` (merge); documented
in docs/self-hosting.md. A 403 from GitHub becomes a 400 pointing there.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.services.github_client import GitHubClient

DEPENDABOT_LOGIN = "dependabot[bot]"
MODE_APPROVE_ONLY = "approve_only"
MODE_APPROVE_AND_MERGE = "approve_and_merge"
MODES = (MODE_APPROVE_ONLY, MODE_APPROVE_AND_MERGE)
DEFAULT_CAP = 5

# Dependabot PR bodies open with e.g. "Bumps [lodash](...) from 4.17.20 to 4.17.21.".
# A grouped PR has several such lines ("Updates `x` from ... to ...", "Updates `y` ...").
# The trailing (?![.\w-]) rejects a 4-part or pre-release target ("1.2.3.4", "1.2.3-rc1")
# so those fall through to "undeterminable" -> skip.
_BUMP_RE = re.compile(
    r"\bfrom\s+v?(\d+\.\d+\.\d+)\s+to\s+v?(\d+\.\d+\.\d+)(?![-\w]|\.\d)",
    re.IGNORECASE,
)


@dataclass
class Decision:
    number: int | None
    title: str
    action: str  # "approved" | "merged" | "would_approve" | "would_merge" | "skipped"
    reason: str = ""


def _bump_is_patch(body: str) -> bool | None:
    """``True`` only when **every** "from X.Y.Z to X.Y.Z" line in the body is a
    same-major, same-minor, non-decreasing patch bump. ``None`` when the body has no
    recognisable bump line at all (caller skips — fail closed). A grouped Dependabot PR
    that bundles a minor/major bump alongside a patch one returns ``False``, not
    ``True``. Pre-release / 4-part target versions aren't matched by ``_BUMP_RE``, so a
    body containing only those yields ``None``."""
    matches = _BUMP_RE.findall(body or "")
    if not matches:
        return None
    for old_s, new_s in matches:
        old = tuple(int(x) for x in old_s.split("."))
        new = tuple(int(x) for x in new_s.split("."))
        if not (old[0] == new[0] and old[1] == new[1] and new[2] >= old[2]):
            return False
    return True


def _checks_all_green(client: GitHubClient, owner: str, repo: str, sha: str) -> bool:
    """Every check-run *and* every classic commit status on ``sha`` is a completed
    success. Requires at least one signal — a head SHA with no CI at all is treated as
    not-green (this feature does not auto-merge unverified code). If GitHub reports more
    check-runs than the one page we fetched, we can't verify them all, so → not green."""
    runs = client.request(
        "GET", f"/repos/{owner}/{repo}/commits/{sha}/check-runs", params={"per_page": 100}
    )
    check_runs = runs.get("check_runs", []) if isinstance(runs, dict) else []
    total = runs.get("total_count", len(check_runs)) if isinstance(runs, dict) else 0
    if total > len(check_runs):
        return False  # a failing run could be hiding on a page we didn't fetch
    for run in check_runs:
        if run.get("status") != "completed":
            return False
        if run.get("conclusion") not in ("success", "neutral", "skipped"):
            return False

    status = client.request("GET", f"/repos/{owner}/{repo}/commits/{sha}/status")
    state = status.get("state") if isinstance(status, dict) else None
    statuses = status.get("statuses", []) if isinstance(status, dict) else []
    if statuses and state != "success":
        return False

    return bool(check_runs) or state == "success"


def _human_review_blocks(client: GitHubClient, owner: str, repo: str, pr: dict) -> bool:
    if pr.get("requested_reviewers") or pr.get("requested_teams"):
        return True
    reviews = client.request_paginated(f"/repos/{owner}/{repo}/pulls/{pr['number']}/reviews")
    return any(r.get("state") == "CHANGES_REQUESTED" for r in reviews)


def _evaluate(client: GitHubClient, owner: str, repo: str, pr: dict) -> str | None:
    """A skip-reason string, or ``None`` when the PR is eligible to be acted on."""
    if (pr.get("user") or {}).get("login") != DEPENDABOT_LOGIN:
        return "not a Dependabot PR"
    if pr.get("draft"):
        return "draft PR"
    patch = _bump_is_patch(pr.get("body", ""))
    if patch is None:
        return "could not determine the bump level from the PR body"
    if not patch:
        return "not a patch-level bump"
    sha = (pr.get("head") or {}).get("sha")
    if not sha or not _checks_all_green(client, owner, repo, sha):
        return "checks are not all green"
    if _human_review_blocks(client, owner, repo, pr):
        return "a human review is pending or requested changes"
    return None


def triage(
    client: GitHubClient,
    owner: str,
    repo: str,
    *,
    enabled: bool,
    mode: str,
    merge_method: str = "squash",
    cap: int = DEFAULT_CAP,
    dry_run: bool = False,
) -> list[Decision]:
    """One repo. Returns a decision per open PR (acted on, or skipped-with-reason).
    ``enabled=False`` short-circuits to an empty list."""
    if not enabled:
        return []

    prs = client.request_paginated(
        f"/repos/{owner}/{repo}/pulls", params={"state": "open"}
    )
    decisions: list[Decision] = []
    acted = 0
    for pr in prs:
        number, title = pr.get("number"), pr.get("title", "")
        reason = _evaluate(client, owner, repo, pr)
        if reason is not None:
            decisions.append(Decision(number, title, "skipped", reason))
            continue
        if acted >= cap:
            decisions.append(Decision(number, title, "skipped", "per-run cap reached"))
            continue

        will_merge = mode == MODE_APPROVE_AND_MERGE
        if dry_run:
            decisions.append(Decision(number, title, "would_merge" if will_merge else "would_approve"))
            acted += 1
            continue

        client.request(
            "POST",
            f"/repos/{owner}/{repo}/pulls/{number}/reviews",
            json={
                "event": "APPROVE",
                "body": "Auto-approved by Clevis: patch-level Dependabot bump, all checks green.",
            },
        )
        if will_merge:
            client.request(
                "PUT",
                f"/repos/{owner}/{repo}/pulls/{number}/merge",
                json={"merge_method": merge_method},
            )
            decisions.append(Decision(number, title, "merged"))
        else:
            decisions.append(Decision(number, title, "approved"))
        acted += 1

    return decisions
