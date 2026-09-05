import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from src.core.auth import UserOut, require_auth, require_workspace_admin
from src.core.db import get_db
from src.repositories import job_repo
from src.schemas.job import JobOut

router = APIRouter()

# Job types a non-admin caller is allowed to poll by id, and which record the enqueuing
# user's email in their payload (`actor`) so we can scope the read to its owner.
_SELF_READABLE_JOB_TYPES = {"github.clear_actions_cache"}


@router.get("", response_model=list[JobOut])
def jobs(db: Session = Depends(get_db), _user: UserOut = Depends(require_workspace_admin)):
    return job_repo.list_jobs(db)


@router.get("/{job_id}", response_model=JobOut)
def job(job_id: int, db: Session = Depends(get_db), user: UserOut = Depends(require_auth)):
    # The cache-clear panel polls this to show a queued job's real terminal status
    # (done/failed) instead of claiming success the moment it's enqueued. The `jobs` table
    # has no owner/tenant column, so to avoid cross-tenant id enumeration this is scoped to
    # the job's own `actor` (the email of the user who enqueued it) and to job types that
    # record one. Anything else 404s -- workspace admins use the list endpoint above.
    row = job_repo.get_job(db, job_id)
    if row is None or row.job_type not in _SELF_READABLE_JOB_TYPES:
        raise HTTPException(status_code=404, detail="Unknown job")
    try:
        actor = json.loads(row.payload or "{}").get("actor")
    except (ValueError, TypeError):
        actor = None
    if actor != user.email:
        raise HTTPException(status_code=404, detail="Unknown job")
    return row
