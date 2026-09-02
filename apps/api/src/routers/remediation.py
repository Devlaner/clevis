"""Issue #287: "Fix this" — apply the fix for a failing security check.

Personal-scoped (``/me/...``), same shape as issues.py: the Security page's check
cards scan an arbitrary ``owner`` by name, so token resolution goes through
``resolve_owner_token(min_role="admin")`` (this writes to GitHub). If ``owner``
is a connected Clevis org the caller must be an **admin** of it.

**Requires write scopes Clevis does not request by default** --
``administration:write`` (repo settings, branch protection) and
``dependabot_alerts:write`` / ``security_events:write`` (Dependabot alerts). See
docs/self-hosting.md. A 403 from GitHub is surfaced as a 400 with a hint to
grant the permission.
"""

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.core.auth import UserOut, require_auth
from src.core.db import get_db
from src.core.rbac import set_tenant_session_context
from src.repositories import audit_repo, org_repo, tenant_repo
from src.services import check_remediation
from src.services.github_client import GitHubClient
from src.services.token_resolution import (
    InsufficientOrgRole,
    NoGitHubTokenAvailable,
    resolve_owner_token,
)

router = APIRouter()


class RemediateRequest(BaseModel):
    token: str | None = None


class RemediateResponse(BaseModel):
    check_id: str
    repo: str
    remediated: bool = True


def _connected_tenant(db: Session, user_id: int, owner: str) -> int | None:
    org = org_repo.get_by_login_ci(db, owner)
    if org is None:
        return None
    org = org_repo.ensure_tenant_linked(db, org)
    if tenant_repo.get_membership(db, org.tenant_id, user_id) is None:
        return None
    set_tenant_session_context(db, org.tenant_id, user_id)
    return org.tenant_id


@router.post(
    "/me/repos/{owner}/{repo}/security/checks/{check_id}/remediate",
    response_model=RemediateResponse,
)
def remediate_check(
    owner: str,
    repo: str,
    check_id: str,
    body: RemediateRequest,
    user: UserOut = Depends(require_auth),
    db: Session = Depends(get_db),
    x_github_token: str | None = Header(default=None),
) -> RemediateResponse:
    if check_id not in check_remediation.supported_check_ids():
        raise HTTPException(status_code=404, detail=f"No automated fix for check {check_id!r}")

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
    # Record the attempt before it reaches GitHub, so a rejected write is still audited.
    audit_repo.write(
        db,
        user.email,
        "security.remediate",
        f"{owner}/{repo}",
        {"check_id": check_id},
        tenant_id=tenant_id,
    )

    try:
        check_remediation.remediate(GitHubClient(token), check_id, owner, repo)
    except check_remediation.RemediationNotSupported:
        raise HTTPException(status_code=404, detail=f"No automated fix for check {check_id!r}")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 403:
            raise HTTPException(
                status_code=400,
                detail=(
                    "GitHub rejected the change (403). The connected GitHub App (or token) "
                    "needs write access for this fix — grant it the 'Administration' and "
                    "'Dependabot alerts' permissions and re-approve. See docs/self-hosting.md."
                ),
            ) from exc
        raise HTTPException(status_code=400, detail=f"GitHub API error: {exc.response.status_code}") from exc
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail="GitHub API unreachable") from exc

    return RemediateResponse(check_id=check_id, repo=repo)
