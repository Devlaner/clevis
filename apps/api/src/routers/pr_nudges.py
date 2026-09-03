"""Stale-PR / stale-review nudge endpoints (issue #289).

Personal-scoped (``/me/repos/{owner}/{repo}/pr-nudges``) and org-scoped
(``/orgs/{org_login}/repos/{owner}/{repo}/pr-nudges``), same shape as issues.py /
remediation.py: a POST (this writes to GitHub), token resolved via
``resolve_owner_token(min_role="admin")`` for the personal route and
``resolve_org_token`` for the org route, audit row written before the GitHub call.

**Requires ``pull_requests: write``** on the App / PAT. A 403 from GitHub is turned
into a 400 with a docs pointer.
"""

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.core.app_config import get_config
from src.core.auth import UserOut, require_auth
from src.core.db import get_db
from src.core.rbac import (
    OrgContext,
    assert_owner_matches_org,
    require_org_role,
    set_tenant_session_context,
)
from src.repositories import audit_repo, org_repo, tenant_repo
from src.services import pr_nudge
from src.services.github_client import GitHubClient, github_error as _github_error
from src.services.token_resolution import (
    InsufficientOrgRole,
    NoGitHubTokenAvailable,
    resolve_org_token,
    resolve_owner_token,
)

router = APIRouter()


class NudgeRequest(BaseModel):
    token: str | None = None


class NudgeItem(BaseModel):
    number: int
    title: str
    action: str


class NudgeResponse(BaseModel):
    mode: str
    stale_days: int
    results: list[NudgeItem]


def _settings() -> tuple[int, str]:
    try:
        stale_days = int(get_config(pr_nudge.STALE_DAYS_KEY, str(pr_nudge.DEFAULT_STALE_DAYS)))
    except ValueError:
        stale_days = pr_nudge.DEFAULT_STALE_DAYS
    mode = get_config(pr_nudge.MODE_KEY, pr_nudge.DEFAULT_MODE)
    if mode not in pr_nudge.MODES:
        mode = pr_nudge.DEFAULT_MODE
    return max(1, stale_days), mode


def _run(client: GitHubClient, owner: str, repo: str) -> NudgeResponse:
    stale_days, mode = _settings()
    try:
        results = pr_nudge.run_nudge_sweep(
            client, owner, repo, stale_days=stale_days, mode=mode
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 403:
            raise HTTPException(
                status_code=400,
                detail=(
                    "GitHub rejected the nudge (403). Clevis's GitHub App (or token) needs "
                    "the 'Pull requests' permission at Read and write. See docs/self-hosting.md."
                ),
            ) from exc
        raise _github_error(exc) from exc
    except httpx.RequestError as exc:
        raise _github_error(exc) from exc

    return NudgeResponse(
        mode=mode,
        stale_days=stale_days,
        results=[NudgeItem(number=r.number, title=r.title, action=r.action) for r in results],
    )


def _connected_tenant(db: Session, user_id: int, owner: str) -> int | None:
    org = org_repo.get_by_login_ci(db, owner)
    if org is None:
        return None
    org = org_repo.ensure_tenant_linked(db, org)
    if tenant_repo.get_membership(db, org.tenant_id, user_id) is None:
        return None
    set_tenant_session_context(db, org.tenant_id, user_id)
    return org.tenant_id


@router.post("/me/repos/{owner}/{repo}/pr-nudges", response_model=NudgeResponse)
def nudge_stale_prs(
    owner: str,
    repo: str,
    body: NudgeRequest,
    user: UserOut = Depends(require_auth),
    db: Session = Depends(get_db),
    x_github_token: str | None = Header(default=None),
) -> NudgeResponse:
    try:
        token = resolve_owner_token(
            db, user_id=user.id, owner=owner,
            client_token=body.token or x_github_token, min_role="admin",
        )
    except InsufficientOrgRole as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except NoGitHubTokenAvailable as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    tenant_id = _connected_tenant(db, user.id, owner)
    audit_repo.write(
        db, user.email, "pr_nudge.sweep", f"{owner}/{repo}", {}, tenant_id=tenant_id
    )
    return _run(GitHubClient(token), owner, repo)


@router.post(
    "/orgs/{org_login}/repos/{owner}/{repo}/pr-nudges", response_model=NudgeResponse
)
def nudge_stale_prs_for_org(
    org_login: str,
    owner: str,
    repo: str,
    body: NudgeRequest,
    ctx: OrgContext = Depends(require_org_role(min_role="admin")),
    user: UserOut = Depends(require_auth),
    db: Session = Depends(get_db),
    x_github_token: str | None = Header(default=None),
) -> NudgeResponse:
    assert_owner_matches_org(owner, ctx)
    try:
        token = resolve_org_token(
            db, org_id=ctx.org.id, account_login=owner,
            client_token=body.token or x_github_token,
        )
    except NoGitHubTokenAvailable as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    audit_repo.write(
        db, user.email, "pr_nudge.sweep", f"{owner}/{repo}", {}, tenant_id=ctx.org.tenant_id
    )
    return _run(GitHubClient(token), owner, repo)
