import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from src.core.auth import UserOut, require_auth
from src.core.db import get_db
from src.repositories import tenant_repo
from src.schemas.cache import CacheClearInput, CacheClearResponse, CacheListInput, CacheListResponse
from src.services.cache_service import clear
from src.services.github_client import GitHubClient
from src.services.token_resolution import (
    InsufficientOrgRole,
    NoGitHubTokenAvailable,
    resolve_owner_token,
)

router = APIRouter()


def _github_cache_error(exc: Exception) -> HTTPException:
    if isinstance(exc, httpx.HTTPStatusError):
        return HTTPException(status_code=400, detail=f"GitHub API error: {exc.response.status_code}")
    if isinstance(exc, httpx.RequestError):
        return HTTPException(status_code=503, detail="GitHub API unreachable")
    raise exc


def _list_caches(owner: str, repo: str, token: str) -> CacheListResponse:
    try:
        client = GitHubClient(token)
        data = client.request("GET", f"/repos/{owner}/{repo}/actions/caches")
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        raise _github_cache_error(exc) from exc
    return {"repository": f"{owner}/{repo}", "total": data.get("total_count", 0), "actions_caches": data.get("actions_caches", [])}


def _client_token(payload: CacheListInput | CacheClearInput) -> str | None:
    return payload.token.get_secret_value() if payload.token else None


@router.post("/me/repos/{owner}/{repo}/actions-caches", response_model=CacheListResponse)
def personal_list_caches(
    owner: str,
    repo: str,
    payload: CacheListInput,
    user: UserOut = Depends(require_auth),
    db: Session = Depends(get_db),
):
    try:
        token = resolve_owner_token(db, user_id=user.id, owner=owner, client_token=_client_token(payload))
    except NoGitHubTokenAvailable as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _list_caches(owner, repo, token)


@router.post("/me/repos/{owner}/{repo}/actions-caches/clear", response_model=CacheClearResponse)
def personal_clear_caches(
    owner: str,
    repo: str,
    payload: CacheClearInput,
    db: Session = Depends(get_db),
    user: UserOut = Depends(require_auth),
):
    token = ""
    if not payload.dry_run:
        try:
            token = resolve_owner_token(
                db, user_id=user.id, owner=owner, client_token=_client_token(payload), min_role="admin"
            )
        except InsufficientOrgRole as exc:
            raise HTTPException(status_code=403, detail=str(exc))
        except NoGitHubTokenAvailable as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    personal_tenant = tenant_repo.ensure_personal_tenant(db, user.id)
    return clear(db, owner, repo, payload, actor=user.email, token=token, tenant_id=personal_tenant.id)
