from fastapi import APIRouter, Depends
from pydantic import BaseModel

from src.services.audit import write_audit
from src.services.github_client import GitHubClient
from src.services.jobs import enqueue
from src.services.rbac import require_role

router = APIRouter()


class CacheListInput(BaseModel):
    token: str


class CacheClearInput(BaseModel):
    token: str
    actor: str
    key: str | None = None
    ref: str | None = None
    dry_run: bool = True


@router.post("/{owner}/{repo}/actions-caches")
def list_caches(owner: str, repo: str, payload: CacheListInput):
    client = GitHubClient(payload.token)
    data = client.request("GET", f"/repos/{owner}/{repo}/actions/caches")
    return {"repository": f"{owner}/{repo}", "total": data.get("total_count", 0), "actions_caches": data.get("actions_caches", [])}


@router.post("/{owner}/{repo}/actions-caches/clear")
def clear_caches(owner: str, repo: str, payload: CacheClearInput, _role: str = Depends(lambda: require_role("admin"))):
    target = f"{owner}/{repo}"
    if payload.dry_run:
        write_audit(payload.actor, "cache.clear.dry_run", target, payload.model_dump())
        return {"queued": False, "dry_run": True, "message": "Dry run completed."}

    job_id = enqueue("github.clear_actions_cache", {
        "owner": owner,
        "repo": repo,
        "token": payload.token,
        "key": payload.key,
        "ref": payload.ref,
        "actor": payload.actor,
    })
    write_audit(payload.actor, "cache.clear.queued", target, {"job_id": job_id, **payload.model_dump()})
    return {"queued": True, "job_id": job_id}
