"""Bulk default-branch protection apply across an org's repos (issue #288).

``POST /orgs/{org_login}/branch-protection/bulk`` — org-admin only. With
``dry_run=true`` it returns a per-repo diff and writes nothing; with ``dry_run=false``
it PUTs the preset to each repo's default branch, capturing per-repo failures so one
repo the token can't touch doesn't abort the rest.

**Requires the ``administration`` repository permission at Read and write** on the
connected GitHub App (or the pasted PAT). When every repo comes back 403 the whole
call is turned into a 400 pointing at docs/self-hosting.md so the UI can show the
"grant Administration: write" hint instead of a wall of per-repo errors.

Optionally (``save_preset=true``) the applied preset is stored per repo in
``automation_repo_settings`` (feature ``branch_protection``) so it can be reused.
"""

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.core.auth import UserOut, require_auth
from src.core.db import get_db
from src.core.rbac import OrgContext, require_org_role
from src.repositories import audit_repo, automation_settings_repo
from src.services import branch_protection_bulk
from src.services.github_client import GitHubClient
from src.services.token_resolution import NoGitHubTokenAvailable, resolve_org_token

router = APIRouter()

_FEATURE = "branch_protection"
_PERMISSION_HINT = (
    "GitHub returned 403 for every repo. The most likely cause is a missing scope — "
    "reading and writing branch protection needs the repository 'Administration' "
    "permission at Read and write on Clevis's GitHub App (or the pasted token). If the "
    "App already has it, re-approve the installation. See docs/self-hosting.md."
)


class BulkRequest(BaseModel):
    repos: list[str] = Field(min_length=1)
    preset: dict | None = None
    dry_run: bool = True
    save_preset: bool = False
    token: str | None = None


class RepoDiffOut(BaseModel):
    repo: str
    branch: str
    currently_protected: bool
    would_change: bool
    changes: dict
    error: str | None = None


class RepoResultOut(BaseModel):
    repo: str
    applied: bool
    error: str | None = None


class BulkDryRunResponse(BaseModel):
    dry_run: bool = True
    diffs: list[RepoDiffOut]


class BulkApplyResponse(BaseModel):
    dry_run: bool = False
    results: list[RepoResultOut]


def _all_forbidden(errors: list[str | None]) -> bool:
    real = [e for e in errors if e]
    return bool(real) and len(real) == len(errors) and all("403" in e for e in real)


@router.post("/orgs/{org_login}/branch-protection/bulk")
def bulk_branch_protection(
    org_login: str,
    body: BulkRequest,
    ctx: OrgContext = Depends(require_org_role(min_role="admin")),
    user: UserOut = Depends(require_auth),
    db: Session = Depends(get_db),
    x_github_token: str | None = Header(default=None),
):
    # require_org_role already ran set_tenant_session_context for this org's tenant.
    try:
        token = resolve_org_token(
            db,
            org_id=ctx.org.id,
            account_login=ctx.org.github_login,
            client_token=body.token or x_github_token,
        )
    except NoGitHubTokenAvailable as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    owner = ctx.org.github_login
    action = "branch_protection.bulk_dryrun" if body.dry_run else "branch_protection.bulk_apply"
    audit_repo.write(
        db, user.email, action, owner,
        {"repos": body.repos, "dry_run": body.dry_run}, tenant_id=ctx.org.tenant_id,
    )

    # plan_bulk / apply_bulk capture every httpx error per repo onto the result — a
    # whole-batch failure surfaces as every RepoResult/RepoDiff carrying an error, which
    # _all_forbidden turns into the permission hint below.
    client = GitHubClient(token)
    if body.dry_run:
        diffs = branch_protection_bulk.plan_bulk(client, owner, body.repos, body.preset)
        if _all_forbidden([d.error for d in diffs]):
            raise HTTPException(status_code=400, detail=_PERMISSION_HINT)
        return BulkDryRunResponse(
            diffs=[
                RepoDiffOut(
                    repo=d.repo, branch=d.branch, currently_protected=d.currently_protected,
                    would_change=d.would_change, changes=d.changes, error=d.error,
                )
                for d in diffs
            ]
        )

    results = branch_protection_bulk.apply_bulk(client, owner, body.repos, body.preset)
    if _all_forbidden([r.error for r in results]):
        raise HTTPException(status_code=400, detail=_PERMISSION_HINT)

    if body.save_preset:
        preset = branch_protection_bulk.normalize_preset(body.preset)
        for r in results:
            if r.applied:
                automation_settings_repo.upsert(
                    db, ctx.org.tenant_id, f"{owner}/{r.repo}", _FEATURE,
                    enabled=True, extra=preset,
                )
        db.commit()

    return BulkApplyResponse(
        results=[RepoResultOut(repo=r.repo, applied=r.applied, error=r.error) for r in results]
    )
