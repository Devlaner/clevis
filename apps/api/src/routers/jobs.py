from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from src.core.auth import UserOut, require_auth, require_workspace_admin
from src.core.db import get_db
from src.repositories import job_repo
from src.schemas.job import JobOut

router = APIRouter()


@router.get("", response_model=list[JobOut])
def jobs(db: Session = Depends(get_db), _user: UserOut = Depends(require_workspace_admin)):
    return job_repo.list_jobs(db)


@router.get("/{job_id}", response_model=JobOut)
def job(job_id: int, db: Session = Depends(get_db), _user: UserOut = Depends(require_auth)):
    # Any signed-in user can poll a single job by id -- the cache-clear panel uses this to
    # show a queued job's real terminal status (done/failed) instead of claiming success
    # the moment it's enqueued. The `jobs` table has no tenant column; the payload is
    # never returned (only status + result), and `result` holds a small status blob or a
    # sanitized error string, so cross-tenant id enumeration exposes nothing sensitive.
    row = job_repo.get_job(db, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown job")
    return row
