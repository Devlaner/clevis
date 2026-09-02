"""File a GitHub issue from a Clevis finding (issue #286).

Personal-scoped (``/me/...``), matching ``security.py`` -- the Security page's check
cards, where the "File as issue" button lives, scan an arbitrary ``owner`` by name, so
token resolution goes through ``resolve_owner_token`` (membership-gated, and here
``min_role="admin"`` because this writes to GitHub).

**Requires the GitHub App installation -- or the pasted PAT -- to have ``Issues: write``.**
Every other Clevis GitHub call is read-only; this is the first endpoint that *creates*
data on GitHub. A token without the permission gets GitHub's 403, surfaced as a
``400 "GitHub API error: 403"`` by ``github_error``; the UI shows a "needs Issues: write"
hint for that case.
"""

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.core.auth import UserOut, require_auth
from src.core.db import get_db
from src.core.rbac import set_tenant_session_context
from src.repositories import audit_repo, org_repo, tenant_repo
from src.services.github_client import GitHubClient, github_error as _github_error
from src.services.token_resolution import (
    InsufficientOrgRole,
    NoGitHubTokenAvailable,
    resolve_owner_token,
)

router = APIRouter()


class CreateIssueRequest(BaseModel):
    title: str = Field(min_length=1, max_length=256)
    body: str = Field(default="", max_length=65536)
    token: str | None = None


class CreateIssueResponse(BaseModel):
    number: int
    html_url: str


def _connected_tenant(db: Session, user_id: int, owner: str) -> int | None:
    """Same personal-endpoint membership check as ``security.py``'s
    ``_security_connected_tenant`` (minus the installation-presence gate -- a pasted PAT
    with ``Issues: write`` is a valid path here too), so the audit row lands under the
    right tenant when ``owner`` is a connected Clevis org."""
    org = org_repo.get_by_login_ci(db, owner)
    if org is None:
        return None
    org = org_repo.ensure_tenant_linked(db, org)
    if tenant_repo.get_membership(db, org.tenant_id, user_id) is None:
        return None
    set_tenant_session_context(db, org.tenant_id, user_id)
    return org.tenant_id


@router.post("/me/repos/{owner}/{repo}/issues", response_model=CreateIssueResponse, status_code=201)
def create_issue(
    owner: str,
    repo: str,
    body: CreateIssueRequest,
    user: UserOut = Depends(require_auth),
    db: Session = Depends(get_db),
    x_github_token: str | None = Header(default=None),
) -> CreateIssueResponse:
    """Create a GitHub issue in ``{owner}/{repo}``. If ``owner`` is a connected Clevis org
    the caller must be an **admin** of it; the resolved token (installation or PAT) must
    carry ``Issues: write``."""
    try:
        token = resolve_owner_token(
            db,
            user_id=user.id,
            owner=owner,
            client_token=body.token or x_github_token,
            min_role="admin",
        )
    except InsufficientOrgRole as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except NoGitHubTokenAvailable as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    tenant_id = _connected_tenant(db, user.id, owner)
    # Audit the attempt before it reaches GitHub, so a rejected write is still recorded.
    audit_repo.write(
        db, user.email, "issues.create", f"{owner}/{repo}", {"title": body.title}, tenant_id=tenant_id
    )

    try:
        created = GitHubClient(token).request(
            "POST",
            f"/repos/{owner}/{repo}/issues",
            json={"title": body.title, "body": body.body},
        )
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        raise _github_error(exc) from exc

    return CreateIssueResponse(number=created["number"], html_url=created["html_url"])
