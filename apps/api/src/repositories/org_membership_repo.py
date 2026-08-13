from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.core.db import OrgMembership
from src.repositories import tenant_repo


def get(db: Session, org_id: int, user_id: int) -> OrgMembership | None:
    return (
        db.query(OrgMembership)
        .filter(OrgMembership.org_id == org_id, OrgMembership.user_id == user_id)
        .first()
    )


def _sync_membership_mirror(db: Session, org_id: int, membership: OrgMembership) -> None:
    tenant = tenant_repo.get_or_create_org_tenant(db, org_id)
    tenant_repo.upsert_membership(db, tenant_id=tenant.id, user_id=membership.user_id, role=membership.role)


def get_or_create(db: Session, org_id: int, user_id: int, role: str) -> OrgMembership:
    membership = get(db, org_id, user_id)
    if membership is not None:
        _sync_membership_mirror(db, org_id, membership)
        return membership
    membership = OrgMembership(org_id=org_id, user_id=user_id, role=role)
    db.add(membership)
    try:
        db.commit()
    except IntegrityError:
        # Lost a race with a concurrent insert of the same (org_id, user_id) pair.
        db.rollback()
        membership = get(db, org_id, user_id)
        if membership is None:
            raise
        _sync_membership_mirror(db, org_id, membership)
        return membership
    db.refresh(membership)
    _sync_membership_mirror(db, org_id, membership)
    return membership


def list_for_user(db: Session, user_id: int) -> list[OrgMembership]:
    return db.query(OrgMembership).filter(OrgMembership.user_id == user_id).all()


def list_for_org(db: Session, org_id: int) -> list[OrgMembership]:
    return db.query(OrgMembership).filter(OrgMembership.org_id == org_id).all()


def update_role(db: Session, org_id: int, user_id: int, role: str) -> OrgMembership | None:
    membership = get(db, org_id, user_id)
    if membership is None:
        return None
    membership.role = role
    db.commit()
    db.refresh(membership)
    _sync_membership_mirror(db, org_id, membership)
    return membership


def delete(db: Session, org_id: int, user_id: int) -> None:
    db.query(OrgMembership).filter(
        OrgMembership.org_id == org_id, OrgMembership.user_id == user_id
    ).delete()
    db.commit()
    tenant = tenant_repo.get_or_create_org_tenant(db, org_id)
    tenant_repo.delete_membership(db, tenant_id=tenant.id, user_id=user_id)
