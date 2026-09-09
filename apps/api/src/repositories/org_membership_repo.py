from sqlalchemy import text
from sqlalchemy.orm import Session

from src.core.db import Membership
from src.repositories import tenant_repo

# The legacy org_memberships table (org_id-keyed) was dropped in migration 0045 / issue
# #331 -- memberships (tenant-scoped) is the sole store, and has been the source of truth
# for org RBAC reads since #190 step 6a. This module stays as a thin org_id-keyed adapter
# over tenant_repo (tenant_id-keyed) so the many call sites that seed / mutate an org
# membership by (org_id, user_id) -- routers, org provisioning, ~30 test modules -- don't
# each have to resolve the org's tenant themselves.
#
# The write functions take a SELECT ... FOR UPDATE on the memberships row and issue exactly
# one commit per operation, so a concurrent grant and revoke for the same (org, user) stay
# serialized (issue #334): the tenant_repo helpers run with commit=False so that lock is
# held from acquisition to the single commit here. Every write self-establishes
# app.user_id first (SET LOCAL) -- otherwise, under the FORCE-RLS clevis_api runtime role
# (issue #330), the lock query would be filtered to zero rows and acquire nothing.
#
# Read paths (get, list_*) that hit an org with no tenant row yet return empty rather than
# provisioning one -- only get_or_create creates the tenant.


def _set_session_user(db: Session, user_id: int) -> None:
    db.execute(text(f"SET LOCAL app.user_id = {int(user_id)}"))


def get(db: Session, org_id: int, user_id: int) -> Membership | None:
    tenant = tenant_repo.get_org_tenant(db, org_id)
    if tenant is None:
        return None
    return tenant_repo.get_membership(db, tenant.id, user_id)


def get_or_create(db: Session, org_id: int, user_id: int, role: str) -> Membership:
    _set_session_user(db, user_id)
    tenant_id = tenant_repo.get_or_create_org_tenant(db, org_id, commit=False).id
    # Lock the row if it exists so a concurrent delete() can't land before our commit.
    # If it doesn't exist yet there's nothing to lock; upsert_membership's own
    # IntegrityError/SAVEPOINT recovery handles a lost insert race, and the whole call is
    # one transaction (no early commit) so a concurrent delete runs fully before or fully
    # after, never mid-write.
    tenant_repo.get_membership(db, tenant_id, user_id, for_update=True)
    membership = tenant_repo.upsert_membership(
        db, tenant_id=tenant_id, user_id=user_id, role=role, commit=False
    )
    db.commit()
    return membership


def update_role(db: Session, org_id: int, user_id: int, role: str) -> Membership | None:
    tenant = tenant_repo.get_org_tenant(db, org_id)
    if tenant is None:
        return None
    _set_session_user(db, user_id)
    if tenant_repo.get_membership(db, tenant.id, user_id, for_update=True) is None:
        db.commit()  # close the read transaction cleanly; nothing to update
        return None
    tenant_repo.update_membership_role(db, tenant_id=tenant.id, user_id=user_id, role=role, commit=False)
    db.commit()
    # Not a refresh: the commit released the lock, so a concurrent delete() may have
    # removed the row -- re-query (re-establishing context the commit cleared) so that
    # legitimate outcome returns None rather than raising on a detached instance.
    _set_session_user(db, user_id)
    return tenant_repo.get_membership(db, tenant.id, user_id)


def delete(db: Session, org_id: int, user_id: int) -> None:
    tenant = tenant_repo.get_org_tenant(db, org_id)
    if tenant is None:
        return
    _set_session_user(db, user_id)
    # Lock (if present) then delete + commit as one operation, so a concurrent
    # get_or_create() blocks on the row until this finishes rather than racing it.
    tenant_repo.get_membership(db, tenant.id, user_id, for_update=True)
    tenant_repo.delete_membership(db, tenant_id=tenant.id, user_id=user_id, commit=False)
    db.commit()
