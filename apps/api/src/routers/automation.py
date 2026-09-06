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
    DispatchAllInput,
    DispatchAllResponse,
    DispatchAllResult,
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


# Bounds one bulk-dispatch request: each active workflow is one sequential GitHub POST,
# and the request shouldn't fan out unboundedly. Repos with more workflows than this
# should dispatch individually.
_BULK_DISPATCH_MAX = 40


def _github_message(exc: httpx.HTTPStatusError) -> str:
    try:
        return exc.response.json().get("message") or ""
    except (ValueError, AttributeError):
        return ""


def _list_all_workflows(client: GitHubClient, owner: str, repo: str) -> list[dict]:
    """Every workflow across every page. GitHub's `/actions/workflows` returns a
    ``{total_count, workflows}`` object (not a bare array), so request_paginated's
    Link-following can't be reused -- page through it explicitly instead."""
    workflows: list[dict] = []
    page = 1
    while True:
        data = client.request(
            "GET",
            f"/repos/{owner}/{repo}/actions/workflows",
            params={"per_page": 100, "page": page},
        )
        batch = data.get("workflows", [])
        workflows.extend(batch)
        total = data.get("total_count")
        # Stop on an empty page (guards against a misbehaving API and infinite loops),
        # a short page, or once total_count says we've seen everything.
        if not batch or len(batch) < 100 or (total is not None and len(workflows) >= total):
            break
        page += 1
    return workflows


def _dispatch_all(
    db: Session,
    owner: str,
    repo: str,
    payload: DispatchAllInput,
    token: str,
    actor: str,
    tenant_id: int | None = None,
) -> DispatchAllResponse:
    client = GitHubClient(token)
    try:
        all_workflows = _list_all_workflows(client, owner, repo)
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        raise _github_error(exc) from exc

    active = [w for w in all_workflows if w.get("state") == "active"]
    if len(active) > _BULK_DISPATCH_MAX:
        raise HTTPException(
            status_code=422,
            detail=f"Too many workflows for bulk dispatch ({_BULK_DISPATCH_MAX} max); dispatch individually.",
        )

    results: list[DispatchAllResult] = []
    for w in active:
        wf_id, name = w["id"], w["name"]
        # One audit row per attempted workflow, written before the call -- same
        # convention as _dispatch, so a rejected bulk dispatch still leaves a record.
        audit_repo.write(
            db,
            actor,
            "automation.workflow.dispatch",
            f"{owner}/{repo}#{wf_id}",
            {"ref": payload.ref, "inputs": {}, "bulk": True},
            tenant_id=tenant_id,
        )
        try:
            client.request(
                "POST",
                f"/repos/{owner}/{repo}/actions/workflows/{wf_id}/dispatches",
                json={"ref": payload.ref, "inputs": {}},
            )
        except httpx.HTTPStatusError as exc:
            message = _github_message(exc)
            if exc.response.status_code == 422 and "workflow_dispatch" in message:
                results.append(
                    DispatchAllResult(
                        workflow_id=wf_id, name=name, status="skipped",
                        message="No workflow_dispatch trigger",
                    )
                )
            else:
                results.append(
                    DispatchAllResult(
                        workflow_id=wf_id, name=name, status="failed",
                        message=message or f"GitHub API error: {exc.response.status_code}",
                    )
                )
        except httpx.RequestError:
            results.append(
                DispatchAllResult(
                    workflow_id=wf_id, name=name, status="failed",
                    message="GitHub API unreachable",
                )
            )
        else:
            results.append(DispatchAllResult(workflow_id=wf_id, name=name, status="dispatched"))

    return DispatchAllResponse(
        ref=payload.ref,
        results=results,
        dispatched_count=sum(r.status == "dispatched" for r in results),
        skipped_count=sum(r.status == "skipped" for r in results),
        failed_count=sum(r.status == "failed" for r in results),
    )


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


@router.post("/orgs/{org_login}/repos/{owner}/{repo}/workflows/dispatch-all", response_model=DispatchAllResponse)
def org_dispatch_all_workflows(
    org_login: str,
    owner: str,
    repo: str,
    payload: DispatchAllInput,
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
    return _dispatch_all(db, owner, repo, payload, token, actor=user.email, tenant_id=ctx.org.tenant_id)


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


@router.post("/me/repos/{owner}/{repo}/workflows/dispatch-all", response_model=DispatchAllResponse)
def personal_dispatch_all_workflows(
    owner: str,
    repo: str,
    payload: DispatchAllInput,
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
    return _dispatch_all(db, owner, repo, payload, token, actor=user.email, tenant_id=personal_tenant.id)
