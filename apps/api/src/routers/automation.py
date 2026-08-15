"""Workflow listing/run-history/dispatch endpoints (docs/plan.md Phase 13 — Automation).

Reads are plain GETs with an optional client-supplied PAT carried in the
`X-GitHub-Token` header (never a query string), matching collab.py's convention.
Dispatch is a write, so it stays a POST with the token in the body, matching
actions_cache.py's convention -- and per docs/plan.md's cross-cutting note,
dispatch is expected to remain a direct-GitHub-call action endpoint even after
the aggregates migration lands, so it doesn't wait on that work.

Dispatch is gated behind org-admin (require_org_role(min_role="admin")) --
GitHub Actions has no dispatch-preview API, so unlike actions-cache clear there
is no dry-run mode here. The audit log is written before the GitHub call so
there's a record even if GitHub rejects or times out the dispatch.
"""

from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.orm import Session

from src.core.auth import UserOut, require_auth
from src.core.db import get_db
from src.core.rbac import OrgContext, assert_owner_matches_org, require_org_role
from src.repositories import audit_repo, tenant_repo
from src.schemas.automation import (
    DispatchInput,
    DispatchResponse,
    RunSummary,
    RunsResponse,
    WorkflowSummary,
    WorkflowsResponse,
)
from src.services.github_client import GitHubClient, github_error as _github_error
from src.services.token_resolution import (
    InsufficientOrgRole,
    NoGitHubTokenAvailable,
    resolve_org_token,
    resolve_owner_token,
)

router = APIRouter()


def _run_duration_ms(run: dict) -> int | None:
    started = run.get("run_started_at")
    updated = run.get("updated_at")
    if not started or not updated or run.get("status") != "completed":
        return None
    try:
        start_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
    except ValueError:
        return None
    delta_ms = int((end_dt - start_dt).total_seconds() * 1000)
    return delta_ms if delta_ms >= 0 else None


def _list_workflows(owner: str, repo: str, token: str) -> WorkflowsResponse:
    client = GitHubClient(token)
    try:
        data = client.request("GET", f"/repos/{owner}/{repo}/actions/workflows")
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        raise _github_error(exc) from exc

    workflows = [
        WorkflowSummary(id=w["id"], name=w["name"], path=w["path"], state=w["state"])
        for w in data.get("workflows", [])
    ]

    # Best-effort: overlay each workflow's most recent run, same degrade-gracefully
    # pattern as collab.py's 2FA overlay -- the workflow list itself already
    # succeeded above, so a failure here shouldn't fail the whole response.
    try:
        runs_data = client.request("GET", f"/repos/{owner}/{repo}/actions/runs", params={"per_page": 100})
        latest_by_workflow: dict[int, dict] = {}
        for run in runs_data.get("workflow_runs", []):
            wf_id = run.get("workflow_id")
            if wf_id is not None and wf_id not in latest_by_workflow:
                latest_by_workflow[wf_id] = run
        for wf in workflows:
            latest = latest_by_workflow.get(wf.id)
            if latest:
                wf.last_run_status = latest.get("status")
                wf.last_run_conclusion = latest.get("conclusion")
                wf.last_run_at = latest.get("created_at")
    except (httpx.HTTPStatusError, httpx.RequestError):
        pass

    return WorkflowsResponse(repository=f"{owner}/{repo}", workflows=workflows)


def _list_runs(owner: str, repo: str, token: str, per_page: int) -> RunsResponse:
    client = GitHubClient(token)
    try:
        data = client.request("GET", f"/repos/{owner}/{repo}/actions/runs", params={"per_page": per_page})
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        raise _github_error(exc) from exc

    runs = [
        RunSummary(
            id=r["id"],
            name=r.get("name"),
            status=r["status"],
            conclusion=r.get("conclusion"),
            head_branch=r.get("head_branch", ""),
            created_at=r["created_at"],
            duration_ms=_run_duration_ms(r),
        )
        for r in data.get("workflow_runs", [])
    ]
    return RunsResponse(repository=f"{owner}/{repo}", runs=runs)


def _dispatch(
    db: Session,
    owner: str,
    repo: str,
    workflow_id: int,
    payload: DispatchInput,
    token: str,
    actor: str,
    tenant_id: int | None = None,
) -> DispatchResponse:
    target = f"{owner}/{repo}#{workflow_id}"
    audit_repo.write(
        db,
        actor,
        "automation.workflow.dispatch",
        target,
        {"ref": payload.ref, "inputs": payload.inputs or {}},
        tenant_id=tenant_id,
    )
    client = GitHubClient(token)
    try:
        client.request(
            "POST",
            f"/repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches",
            json={"ref": payload.ref, "inputs": payload.inputs or {}},
        )
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        raise _github_error(exc) from exc
    return DispatchResponse(dispatched=True, message="Workflow dispatched.")


# ── org-scoped ───────────────────────────────────────────────────────────────

@router.get("/orgs/{org_login}/repos/{owner}/{repo}/workflows", response_model=WorkflowsResponse)
def org_list_workflows(
    org_login: str,
    owner: str,
    repo: str,
    ctx: OrgContext = Depends(require_org_role(min_role="member")),
    db: Session = Depends(get_db),
    x_github_token: str | None = Header(default=None),
):
    assert_owner_matches_org(owner, ctx)
    try:
        token = resolve_org_token(db, org_id=ctx.org.id, account_login=owner, client_token=x_github_token)
    except NoGitHubTokenAvailable as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _list_workflows(owner, repo, token)


@router.get("/orgs/{org_login}/repos/{owner}/{repo}/actions/runs", response_model=RunsResponse)
def org_list_runs(
    org_login: str,
    owner: str,
    repo: str,
    per_page: int = Query(default=10, ge=1, le=100),
    ctx: OrgContext = Depends(require_org_role(min_role="member")),
    db: Session = Depends(get_db),
    x_github_token: str | None = Header(default=None),
):
    assert_owner_matches_org(owner, ctx)
    try:
        token = resolve_org_token(db, org_id=ctx.org.id, account_login=owner, client_token=x_github_token)
    except NoGitHubTokenAvailable as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _list_runs(owner, repo, token, per_page)


@router.post("/orgs/{org_login}/repos/{owner}/{repo}/workflows/{workflow_id}/dispatch", response_model=DispatchResponse)
def org_dispatch_workflow(
    org_login: str,
    owner: str,
    repo: str,
    workflow_id: int,
    payload: DispatchInput,
    ctx: OrgContext = Depends(require_org_role(min_role="admin")),
    user: UserOut = Depends(require_auth),
    db: Session = Depends(get_db),
):
    assert_owner_matches_org(owner, ctx)
    client_token = payload.token.get_secret_value() if payload.token else None
    try:
        token = resolve_org_token(db, org_id=ctx.org.id, account_login=owner, client_token=client_token)
    except NoGitHubTokenAvailable as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _dispatch(db, owner, repo, workflow_id, payload, token, actor=user.email, tenant_id=ctx.org.tenant_id)


# ── personal-scoped ──────────────────────────────────────────────────────────

@router.get("/me/repos/{owner}/{repo}/workflows", response_model=WorkflowsResponse)
def personal_list_workflows(
    owner: str,
    repo: str,
    user: UserOut = Depends(require_auth),
    db: Session = Depends(get_db),
    x_github_token: str | None = Header(default=None),
):
    try:
        token = resolve_owner_token(db, user_id=user.id, owner=owner, client_token=x_github_token)
    except NoGitHubTokenAvailable as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _list_workflows(owner, repo, token)


@router.get("/me/repos/{owner}/{repo}/actions/runs", response_model=RunsResponse)
def personal_list_runs(
    owner: str,
    repo: str,
    per_page: int = Query(default=10, ge=1, le=100),
    user: UserOut = Depends(require_auth),
    db: Session = Depends(get_db),
    x_github_token: str | None = Header(default=None),
):
    try:
        token = resolve_owner_token(db, user_id=user.id, owner=owner, client_token=x_github_token)
    except NoGitHubTokenAvailable as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _list_runs(owner, repo, token, per_page)


@router.post("/me/repos/{owner}/{repo}/workflows/{workflow_id}/dispatch", response_model=DispatchResponse)
def personal_dispatch_workflow(
    owner: str,
    repo: str,
    workflow_id: int,
    payload: DispatchInput,
    user: UserOut = Depends(require_auth),
    db: Session = Depends(get_db),
):
    client_token = payload.token.get_secret_value() if payload.token else None
    try:
        token = resolve_owner_token(db, user_id=user.id, owner=owner, client_token=client_token, min_role="admin")
    except InsufficientOrgRole as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except NoGitHubTokenAvailable as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    personal_tenant = tenant_repo.ensure_personal_tenant(db, user.id)
    return _dispatch(db, owner, repo, workflow_id, payload, token, actor=user.email, tenant_id=personal_tenant.id)
