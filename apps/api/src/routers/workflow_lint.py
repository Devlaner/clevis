"""GitHub Actions workflow policy lint + optional auto-fix PR (issue #291).

``POST /me/repos/{owner}/{repo}/workflow-lint`` (and the org-scoped variant) always
returns the findings. With ``open_pr=true`` and a fixable finding, Clevis opens a PR
that flips a dangerous ``pull_request_target`` trigger to ``pull_request`` (only when
the workflow uses no secrets) and returns its URL.

**Requires write scopes Clevis does not request by default** — ``contents: write``,
``pull_requests: write``, and ``workflows: write`` (GitHub blocks pushing changes to
``.github/workflows/**`` without the last). A 403 from GitHub becomes a 400 pointing
at docs/self-hosting.md. Read-only scanning (``open_pr=false``) still needs the
Actions/Contents *read* the rest of Clevis already uses.
"""

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.core.auth import UserOut, require_auth
from src.core.db import get_db
from src.core.rbac import (
    OrgContext,
    assert_owner_matches_org,
    audit_tenant,
    require_org_role,
)
from src.repositories import audit_repo
from src.services import workflow_lint
from src.services.github_client import GitHubClient, github_error as _github_error
from src.services.token_resolution import (
    InsufficientOrgRole,
    NoGitHubTokenAvailable,
    resolve_org_token,
    resolve_owner_token,
)

router = APIRouter()

_PERMISSION_HINT = (
    "GitHub rejected the request (403). Clevis's GitHub App (or token) needs the "
    "repository 'Contents', 'Pull requests', and 'Workflows' permissions at Read and "
    "write to open a workflow-lint fix PR. See docs/self-hosting.md."
)


class LintRequest(BaseModel):
    token: str | None = None
    open_pr: bool = False


class FindingOut(BaseModel):
    path: str
    rule: str
    severity: str
    message: str


class LintResponse(BaseModel):
    findings: list[FindingOut]
    fixable: bool
    pr_url: str | None = None


def _run(
    db: Session, actor: str, tenant_id: int, client: GitHubClient, owner: str, repo: str, open_pr: bool
) -> LintResponse:
    audit_repo.write(
        db, actor, "workflow_lint.scan", f"{owner}/{repo}", {"open_pr": open_pr}, tenant_id=tenant_id
    )
    try:
        result = workflow_lint.lint_all(client, owner, repo)
        pr_url = None
        if open_pr and result.fixable:
            audit_repo.write(
                db, actor, "workflow_lint.autofix_pr", f"{owner}/{repo}", {}, tenant_id=tenant_id
            )
            pr_url = workflow_lint.open_fix_pr(client, owner, repo, result)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 403:
            raise HTTPException(status_code=400, detail=_PERMISSION_HINT) from exc
        raise _github_error(exc) from exc
    except httpx.RequestError as exc:
        raise _github_error(exc) from exc

    return LintResponse(
        findings=[
            FindingOut(path=f.path, rule=f.rule, severity=f.severity, message=f.message)
            for f in result.findings
        ],
        fixable=result.fixable,
        pr_url=pr_url,
    )


@router.post("/me/repos/{owner}/{repo}/workflow-lint", response_model=LintResponse)
def workflow_lint_personal(
    owner: str,
    repo: str,
    body: LintRequest,
    user: UserOut = Depends(require_auth),
    db: Session = Depends(get_db),
    x_github_token: str | None = Header(default=None),
) -> LintResponse:
    try:
        token = resolve_owner_token(
            db,
            user_id=user.id,
            owner=owner,
            client_token=body.token or x_github_token,
            # Opening a PR writes to GitHub, so it needs org-admin when owner is a
            # connected org; a read-only scan only needs membership.
            min_role="admin" if body.open_pr else "member",
        )
    except InsufficientOrgRole as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except NoGitHubTokenAvailable as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    tenant_id = audit_tenant(db, user.id, owner)
    return _run(db, user.email, tenant_id, GitHubClient(token), owner, repo, body.open_pr)


@router.post(
    "/orgs/{org_login}/repos/{owner}/{repo}/workflow-lint", response_model=LintResponse
)
def workflow_lint_org(
    org_login: str,
    owner: str,
    repo: str,
    body: LintRequest,
    ctx: OrgContext = Depends(require_org_role(min_role="admin")),
    user: UserOut = Depends(require_auth),
    db: Session = Depends(get_db),
    x_github_token: str | None = Header(default=None),
) -> LintResponse:
    assert_owner_matches_org(owner, ctx)
    try:
        token = resolve_org_token(
            db, org_id=ctx.org.id, account_login=owner, client_token=body.token or x_github_token
        )
    except NoGitHubTokenAvailable as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return _run(db, user.email, ctx.org.tenant_id, GitHubClient(token), owner, repo, body.open_pr)
