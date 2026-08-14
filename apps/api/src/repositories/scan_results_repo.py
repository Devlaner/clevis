import json

from sqlalchemy.orm import Session

from src.core.db import ScanResult


def insert(
    db: Session,
    owner: str,
    score: int,
    total_checks: int,
    failed_checks: int,
    checks: list[dict],
    tenant_id: int | None = None,
    scanned_by_user_id: int | None = None,
) -> None:
    db.add(
        ScanResult(
            owner=owner,
            score=score,
            total_checks=total_checks,
            failed_checks=failed_checks,
            checks_json=json.dumps(checks),
            tenant_id=tenant_id,
            scanned_by_user_id=scanned_by_user_id,
        )
    )
    db.commit()


def exists_for_user(db: Session, owner: str, user_id: int) -> bool:
    return (
        db.query(ScanResult.id)
        .filter(ScanResult.owner == owner, ScanResult.scanned_by_user_id == user_id)
        .first()
        is not None
    )


def list_recent(db: Session, owner: str, limit: int = 30) -> list[dict]:
    rows = (
        db.query(ScanResult)
        .filter(ScanResult.owner == owner)
        .order_by(ScanResult.created_at.desc(), ScanResult.id.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": r.id,
            "owner": r.owner,
            "score": r.score,
            "total_checks": r.total_checks,
            "failed_checks": r.failed_checks,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
