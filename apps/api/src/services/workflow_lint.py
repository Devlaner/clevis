"""GitHub Actions workflow policy linting + optional auto-fix PR (issue #291).

Detects a small set of high-signal, well-understood dangerous patterns in a repo's
``.github/workflows/*.yml`` and — on request — opens a PR with a conservative fix.

**Requires write scopes Clevis does not request by default:** ``contents: write``
(branch + commit), ``pull_requests: write`` (open the PR), and ``workflows: write``
(GitHub blocks pushing changes to ``.github/workflows/**`` without it). Documented
in docs/self-hosting.md; a 403 from GitHub is surfaced by the router as a 400.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field

import httpx
import yaml

from src.services.github_client import GitHubClient

_FIX_BRANCH = "clevis/workflow-lint-fix"


@dataclass
class Finding:
    path: str
    rule: str
    severity: str  # "critical" | "high" | "warning"
    message: str
    line: int | None = None


@dataclass
class WorkflowFile:
    path: str
    text: str
    sha: str


@dataclass
class LintResult:
    findings: list[Finding] = field(default_factory=list)
    # path -> fixed text, for the files a conservative auto-fix could rewrite
    fixes: dict[str, str] = field(default_factory=dict)

    @property
    def fixable(self) -> bool:
        return bool(self.fixes)


def fetch_workflows(client: GitHubClient, owner: str, repo: str) -> list[WorkflowFile]:
    try:
        listing = client.request(
            "GET", f"/repos/{owner}/{repo}/contents/.github/workflows"
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return []  # repo has no .github/workflows directory — nothing to lint
        raise  # 403 (missing scope) etc. must reach the router, not be swallowed
    if not isinstance(listing, list):
        return []

    files: list[WorkflowFile] = []
    for entry in listing:
        name = entry.get("name", "")
        if not name.endswith((".yml", ".yaml")):
            continue
        blob = client.request("GET", entry["url"])
        try:
            text = base64.b64decode(blob.get("content", "")).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            continue
        files.append(WorkflowFile(path=entry["path"], text=text, sha=blob["sha"]))
    return files


def _file_uses_secrets(text: str) -> bool:
    # Conservative: any secrets reference anywhere in the file (job step, job/workflow
    # env, `with:` inputs) means the workflow relies on the elevated pull_request_target
    # token, so flipping it to pull_request would break it — report only, don't auto-fix.
    # Match both `secrets.NAME` and the `secrets['NAME']` / `secrets["NAME"]` index forms.
    return "secrets." in text or "secrets[" in text


def _checks_out_pr_head(job: dict) -> bool:
    for step in job.get("steps", []) or []:
        if not isinstance(step, dict):
            continue
        uses = str(step.get("uses", ""))
        if not uses.startswith("actions/checkout"):
            continue
        ref = str((step.get("with") or {}).get("ref", ""))
        if any(
            token in ref
            for token in (
                "github.event.pull_request.head",
                "github.head_ref",
                "github.event.pull_request.merge_commit_sha",
            )
        ):
            return True
    return False


def lint(wf: WorkflowFile) -> LintResult:
    result = LintResult()
    try:
        doc = yaml.safe_load(wf.text)
    except yaml.YAMLError as exc:
        result.findings.append(
            Finding(wf.path, "unparseable", "warning", f"Could not parse workflow YAML: {exc}")
        )
        return result
    if not isinstance(doc, dict):
        return result

    # PyYAML parses the bare key `on:` as boolean True.
    triggers = doc.get("on", doc.get(True))
    trigger_names = (
        set(triggers) if isinstance(triggers, dict)
        else {triggers} if isinstance(triggers, str)
        else set(triggers) if isinstance(triggers, list)
        else set()
    )
    jobs = doc.get("jobs", {}) or {}

    if "pull_request_target" in trigger_names:
        for job_name, job in jobs.items():
            if not isinstance(job, dict):
                continue
            if _checks_out_pr_head(job):
                result.findings.append(
                    Finding(
                        wf.path,
                        "pull_request_target_checks_out_pr_code",
                        "critical",
                        f"Job '{job_name}' runs on `pull_request_target` (which has repo "
                        "secrets and write token) but checks out the untrusted PR head. "
                        "This lets a PR author run code with your secrets.",
                    )
                )
        # Auto-fix: flip `pull_request_target` -> `pull_request`, but only in the narrow
        # case where that's provably safe as a blind text substitution:
        #   - the file uses no secrets (the trigger's elevated token isn't relied on), AND
        #   - `pull_request` isn't already a trigger (else we'd create a duplicate), AND
        #   - the token "pull_request_target" appears exactly once in the file (so it can't
        #     be sitting in a comment, a step name, or an `if: github.event_name ==` guard
        #     that a blind replace would corrupt).
        # Anything more complex is reported only — the finding still stands.
        if (
            result.findings
            and not _file_uses_secrets(wf.text)
            and "pull_request" not in trigger_names
            and wf.text.count("pull_request_target") == 1
        ):
            result.fixes[wf.path] = wf.text.replace("pull_request_target", "pull_request")

    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        for step in job.get("steps", []) or []:
            if not isinstance(step, dict):
                continue
            run = str(step.get("run", ""))
            if any(
                expr in run
                for expr in (
                    "github.event.pull_request.title",
                    "github.event.pull_request.body",
                    "github.event.issue.title",
                    "github.event.issue.body",
                    "github.event.comment.body",
                    "github.head_ref",
                )
            ):
                result.findings.append(
                    Finding(
                        wf.path,
                        "untrusted_input_in_run",
                        "high",
                        f"Job '{job_name}' interpolates attacker-controlled text "
                        "(PR/issue title, body, or branch name) directly into a `run:` "
                        "shell script — a script-injection vector. Pass it via `env:` "
                        "and reference the environment variable instead.",
                    )
                )

    return result


def lint_all(client: GitHubClient, owner: str, repo: str) -> LintResult:
    combined = LintResult()
    for wf in fetch_workflows(client, owner, repo):
        r = lint(wf)
        combined.findings.extend(r.findings)
        combined.fixes.update(r.fixes)
    return combined


_PR_TITLE = "Harden GitHub Actions workflows (Clevis policy lint)"
_PR_BODY = (
    "Clevis's workflow policy lint flagged a `pull_request_target` workflow that checks "
    "out untrusted PR code. This changes the trigger to `pull_request`, which is the safe "
    "equivalent when the workflow does not use repository secrets.\n\nReview carefully "
    "before merging."
)


def open_fix_pr(client: GitHubClient, owner: str, repo: str, result: LintResult) -> str | None:
    """Commit each fixed file to the ``clevis/workflow-lint-fix`` branch and open a PR.
    Returns the PR html_url, or None when there's nothing to fix. Idempotent: if a fix
    PR from a previous run is still open, its files are refreshed and its URL returned
    rather than 422-ing or force-resetting the branch under the open PR."""
    if not result.fixes:
        return None

    repo_meta = client.request("GET", f"/repos/{owner}/{repo}")
    default_branch = repo_meta["default_branch"]

    open_prs = client.request(
        "GET",
        f"/repos/{owner}/{repo}/pulls",
        params={"head": f"{owner}:{_FIX_BRANCH}", "state": "open"},
    )
    existing_pr_url = open_prs[0]["html_url"] if isinstance(open_prs, list) and open_prs else None

    if existing_pr_url is None:
        # No open PR — (re)create the branch at the current default-branch tip. A stale
        # branch left behind by a merged/closed PR is safe to reset here.
        base_sha = client.request(
            "GET", f"/repos/{owner}/{repo}/git/ref/heads/{default_branch}"
        )["object"]["sha"]
        try:
            client.request(
                "POST",
                f"/repos/{owner}/{repo}/git/refs",
                json={"ref": f"refs/heads/{_FIX_BRANCH}", "sha": base_sha},
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 422:
                raise
            client.request(
                "PATCH",
                f"/repos/{owner}/{repo}/git/refs/heads/{_FIX_BRANCH}",
                json={"sha": base_sha, "force": True},
            )

    for path, fixed_text in result.fixes.items():
        existing = client.request(
            "GET", f"/repos/{owner}/{repo}/contents/{path}", params={"ref": _FIX_BRANCH}
        )
        client.request(
            "PUT",
            f"/repos/{owner}/{repo}/contents/{path}",
            json={
                "message": f"ci: harden {path} against pull_request_target misuse",
                "content": base64.b64encode(fixed_text.encode("utf-8")).decode("ascii"),
                "sha": existing["sha"],
                "branch": _FIX_BRANCH,
            },
        )

    if existing_pr_url is not None:
        return existing_pr_url

    pr = client.request(
        "POST",
        f"/repos/{owner}/{repo}/pulls",
        json={"title": _PR_TITLE, "head": _FIX_BRANCH, "base": default_branch, "body": _PR_BODY},
    )
    return pr["html_url"]
