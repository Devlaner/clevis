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
    # commit=False (issue #334): the callers below hold a SELECT ... FOR UPDATE lock on the
    # OrgMembership row and must keep it until they issue their own single db.commit(). The
    # tenant_repo helpers used to commit internally, ending the transaction and releasing
    # that lock mid-operation, which let a concurrent delete() interleave. They now flush
    # into this transaction instead, so the lock is held for the whole logical operation.
    tenant = tenant_repo.get_or_create_org_tenant(db, org_id, commit=False)
    tenant_repo.upsert_membership(
        db, tenant_id=tenant.id, user_id=membership.user_id, role=membership.role, commit=False
    )


def get_or_create(db: Session, org_id: int, user_id: int, role: str) -> OrgMembership:
    membership = get(db, org_id, user_id, for_update=True)
    if membership is not None:
        # Lock held from the get() above through the mirror sync to this commit -- the
        # tenant_repo helpers no longer commit internally (issue #334), so this is the one
        # commit that ends the operation and releases the lock.
        _sync_membership_mirror(db, org_id, membership)
        db.commit()
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
        db.commit()
        return membership
    # Re-lock the row we just committed before syncing its mirror -- the commit above
    # released any lock, opening a window where a concurrent delete() could remove this
    # row and find no mirror yet (nothing synced), then have this call resume and create a
    # mirror for a membership that's already revoked (CodeRabbit finding on #323's 2nd
    # review). If the re-lock finds the row already gone, retry from scratch instead of
    # syncing a mirror for a membership that no longer exists. The re-lock is now held
    # across the mirror sync until the final commit below (issue #334).
    membership = get(db, org_id, user_id, for_update=True)
    if membership is None:
        return get_or_create(db, org_id, user_id, role)
    _sync_membership_mirror(db, org_id, membership)
    db.commit()
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
    # lock get() just took, reopening the window a concurrent delete() could land in.
    # _sync_membership_mirror flushes (does not commit -- issue #334) both the pending
    # OrgMembership role change and the mirror row into this transaction; the single
    # commit() below persists them together and releases the lock.
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
    # principle as update_role's lock-then-sync-then-commit ordering above. Both tenant_repo
    # calls run with commit=False (issue #334) so the DELETE's implicit row lock is held
    # until this function's single commit() -- a concurrent get_or_create() blocks on it for
    # the whole operation instead of slipping in after an early internal commit.
    tenant = tenant_repo.get_or_create_org_tenant(db, org_id, commit=False)
    db.query(OrgMembership).filter(
        OrgMembership.org_id == org_id, OrgMembership.user_id == user_id
    ).delete()
    tenant_repo.delete_membership(db, tenant_id=tenant.id, user_id=user_id, commit=False)
    db.commit()
