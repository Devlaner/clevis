from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.core.db import Membership, Tenant


def get_or_create_org_tenant(db: Session, org_id: int) -> Tenant:
    tenant = db.query(Tenant).filter(Tenant.kind == "org", Tenant.org_id == org_id).first()
    if tenant is not None:
        return tenant
    tenant = Tenant(kind="org", org_id=org_id)
    db.add(tenant)
    try:
        db.commit()
    except IntegrityError:
        # Lost a race with a concurrent insert of the same org's tenant.
        db.rollback()
        tenant = db.query(Tenant).filter(Tenant.kind == "org", Tenant.org_id == org_id).first()
        if tenant is None:
            raise
        return tenant
    db.refresh(tenant)
    return tenant


def ensure_personal_tenant(db: Session, user_id: int) -> Tenant:
    """Get-or-create a user's personal tenant, plus its self-membership (role='admin') --
    mirrors migration 0023_backfill_personal_tenants.py, which backfilled both rows
    together for every pre-existing user. There's no concept of a personal tenant with
    no membership, so both rows are ensured atomically here."""
    tenant = db.query(Tenant).filter(Tenant.kind == "personal", Tenant.personal_user_id == user_id).first()
    if tenant is None:
        tenant = Tenant(kind="personal", personal_user_id=user_id)
        db.add(tenant)
        try:
            db.commit()
        except IntegrityError:
            # Lost a race with a concurrent insert of the same user's personal tenant.
            db.rollback()
            tenant = db.query(Tenant).filter(Tenant.kind == "personal", Tenant.personal_user_id == user_id).first()
            if tenant is None:
                raise
        else:
            db.refresh(tenant)

    get_or_create_membership(db, tenant_id=tenant.id, user_id=user_id, role="admin")
    return tenant


def get_or_create_membership(db: Session, tenant_id: int, user_id: int, role: str) -> Membership:
    membership = (
        db.query(Membership)
        .filter(Membership.tenant_id == tenant_id, Membership.user_id == user_id)
        .first()
    )
    if membership is not None:
        return membership
    membership = Membership(tenant_id=tenant_id, user_id=user_id, role=role)
    db.add(membership)
    try:
        db.commit()
    except IntegrityError:
        # Lost a race with a concurrent insert of the same (tenant_id, user_id) pair.
        db.rollback()
        membership = (
            db.query(Membership)
            .filter(Membership.tenant_id == tenant_id, Membership.user_id == user_id)
            .first()
        )
        if membership is None:
            raise
        return membership
    db.refresh(membership)
    return membership


def upsert_membership(db: Session, tenant_id: int, user_id: int, role: str) -> Membership:
    """get_or_create_membership plus role reconciliation -- unlike get_or_create_membership
    alone, this also fixes a stale role on an already-existing row, so callers that resolve
    a membership through more than one code path (existing found / newly created / recovered
    from a concurrent-insert race) can call this unconditionally and always end up in sync."""
    membership = get_or_create_membership(db, tenant_id, user_id, role)
    if membership.role != role:
        membership = update_membership_role(db, tenant_id, user_id, role)
    return membership


def update_membership_role(db: Session, tenant_id: int, user_id: int, role: str) -> Membership | None:
    membership = (
        db.query(Membership)
        .filter(Membership.tenant_id == tenant_id, Membership.user_id == user_id)
        .first()
    )
    if membership is None:
        return None
    membership.role = role
    db.commit()
    db.refresh(membership)
    return membership


def delete_membership(db: Session, tenant_id: int, user_id: int) -> None:
    db.query(Membership).filter(
        Membership.tenant_id == tenant_id, Membership.user_id == user_id
    ).delete()
    db.commit()
