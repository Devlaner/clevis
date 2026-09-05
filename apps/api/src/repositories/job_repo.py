import json

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.core._sanitize import sanitize_error
from src.core.db import Job


def enqueue(db: Session, job_type: str, payload: dict) -> int:
    job = Job(job_type=job_type, payload=json.dumps(payload), status="queued")
    db.add(job)
    db.commit()
    db.refresh(job)
    return job.id


def list_jobs(db: Session, limit: int = 50) -> list[dict]:
    rows = db.query(Job).order_by(Job.id.desc()).limit(limit).all()
    return [
        {
            "id": r.id,
            "job_type": r.job_type,
            "status": r.status,
            "result": r.result,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rows
    ]


def get_job(db: Session, job_id: int) -> Job | None:
    return db.query(Job).filter(Job.id == job_id).one_or_none()


def list_recent_by_type(db: Session, job_type: str, limit: int = 20) -> list[dict]:
    rows = (
        db.query(Job)
        .filter(Job.job_type == job_type)
        .order_by(Job.id.desc())
        .limit(limit)
        .all()
    )
    return [{"id": r.id, "status": r.status} for r in rows]


def mark_done(db: Session, job_id: int, result: str) -> None:
    db.query(Job).filter(Job.id == job_id).update(
        {"status": "done", "result": result, "updated_at": func.now()}
    )
    db.commit()


def mark_failed(db: Session, job_id: int, error: str) -> None:
    db.query(Job).filter(Job.id == job_id).update(
        {"status": "failed", "result": sanitize_error(error), "updated_at": func.now()}
    )
    db.commit()
