import json
from datetime import datetime

from sqlalchemy import text
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
    # Issue #330: scan_results' RLS WITH CHECK is strict equality against app.tenant_id
    # (migration 0030), with no self-access widening (unlike memberships/
    # github_installations in migration 0031). Callers here always already know a real
    # tenant_id when one is passed (org or personal analytics flows never call this with
    # tenant_id=None in production -- only test seeding does), but nothing else in the
    # request necessarily set app.tenant_id to match (personal-scoped routes only set
    # app.user_id via require_auth). SET LOCAL here to exactly the value being written is
    # always safe -- see audit_repo.write's identical reasoning -- and scoped to only this
    # transaction.
    if tenant_id is not None:
        db.execute(text(f"SET LOCAL app.tenant_id = {int(tenant_id)}"))
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


def list_for_export(
    db: Session,
    owner: str,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 500,
) -> list[dict]:
    """Scan history for a compliance/audit export (issue #293). Unlike ``list_recent``,
    this includes each scan's full per-check breakdown (``checks``, parsed from
    ``checks_json``) and accepts an optional ``[since, until]`` window so an auditor can
    pull a specific reporting period. Newest first, same as ``list_recent``."""
    query = db.query(ScanResult).filter(ScanResult.owner == owner)
    if since is not None:
        query = query.filter(ScanResult.created_at >= since)
    if until is not None:
        query = query.filter(ScanResult.created_at <= until)
    rows = query.order_by(ScanResult.created_at.desc(), ScanResult.id.desc()).limit(limit).all()
    return [
        {
            "id": r.id,
            "owner": r.owner,
            "score": r.score,
            "total_checks": r.total_checks,
            "failed_checks": r.failed_checks,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "checks": json.loads(r.checks_json) if r.checks_json else [],
        }
        for r in rows
    ]
