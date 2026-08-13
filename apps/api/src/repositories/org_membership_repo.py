from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.core.db import OrgMembership
from src.repositories import tenant_repo


def get(db: Session, org_id: int, user_id: int, for_update: bool = False) -> OrgMembership | None:
    query = db.query(OrgMembership).filter(OrgMembership.org_id == org_id, OrgMembership.user_id == user_id)
    if for_update:
        # Locks the row (or blocks until a concurrent delete()'s own implicit row lock
        # releases) so get_or_create/update_role can't read a membership that's mid-deletion
        # and resurrect its tenant mirror via _sync_membership_mirror right after revocation.
        query = query.with_for_update()
    return query.first()


def _sync_membership_mirror(db: Session, org_id: int, membership: OrgMembership) -> None:
    tenant = tenant_repo.get_or_create_org_tenant(db, org_id)
    tenant_repo.upsert_membership(db, tenant_id=tenant.id, user_id=membership.user_id, role=membership.role)


def get_or_create(db: Session, org_id: int, user_id: int, role: str) -> OrgMembership:
    membership = get(db, org_id, user_id, for_update=True)
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
        membership = get(db, org_id, user_id, for_update=True)
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
    membership = get(db, org_id, user_id, for_update=True)
    if membership is None:
        return None
    membership.role = role
    # Sync the mirror before committing -- committing first would release the FOR UPDATE
    # lock get() just took, reopening the same window a concurrent delete() could land in.
    # _sync_membership_mirror's own internal commit (tenant_repo.upsert_membership) flushes
    # this pending role change too, so the OrgMembership update and the mirror update land
    # together; this commit() is a no-op in that case and only matters if the mirror's role
    # already matched (nothing to update inside the sync, so nothing committed it yet).
    _sync_membership_mirror(db, org_id, membership)
    db.commit()
    # Not db.refresh(membership): the row lock is released by the commit above, so a
    # concurrent delete() can remove this row before we get here -- re-query instead of
    # refreshing so that legitimate outcome returns None rather than raising.
    return get(db, org_id, user_id)


def delete(db: Session, org_id: int, user_id: int) -> None:
    # Delete the mirror before committing the OrgMembership delete -- committing first (the
    # original ordering) released the DELETE's own implicit row lock before the mirror was
    # touched, leaving a window where a concurrent get_or_create() could find the row gone,
    # legitimately re-create a fresh OrgMembership + mirror, and then have this function's
    # now-unblocked mirror delete remove that brand-new mirror out from under it. Same
    # principle as update_role's lock-then-sync-then-commit ordering above.
    tenant = tenant_repo.get_or_create_org_tenant(db, org_id)
    db.query(OrgMembership).filter(
        OrgMembership.org_id == org_id, OrgMembership.user_id == user_id
    ).delete()
    tenant_repo.delete_membership(db, tenant_id=tenant.id, user_id=user_id)
    db.commit()
