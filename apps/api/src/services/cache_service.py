from sqlalchemy.orm import Session

from src.core._crypto import encrypt_job_token
from src.core.config import settings
from src.repositories import audit_repo, job_repo
from src.schemas.cache import CacheClearInput


def clear(db: Session, owner: str, repo: str, payload: CacheClearInput, actor: str, token: str, tenant_id: int | None = None) -> dict:
    target = f"{owner}/{repo}"
    if payload.dry_run:
        audit_repo.write(db, actor, "cache.clear.dry_run", target, payload.model_dump(exclude={"token"}), tenant_id=tenant_id)
        return {"queued": False, "dry_run": True, "message": "Dry run completed."}

    encrypted_token = encrypt_job_token(
        token,
        settings.job_secret_key.get_secret_value(),
    )
    job_id = job_repo.enqueue(db, "github.clear_actions_cache", {
        "owner": owner,
        "repo": repo,
        "token": encrypted_token,
        "key": payload.key,
        "ref": payload.ref,
        "actor": actor,
    })
    audit_repo.write(
        db, actor, "cache.clear.queued", target, {"job_id": job_id, **payload.model_dump(exclude={"token"})}, tenant_id=tenant_id
    )
    return {"queued": True, "job_id": job_id}
