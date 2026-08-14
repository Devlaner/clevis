from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.core.db import Membership, Org, Tenant


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


def ensure_personal_tenant(db: Session, user_id: int, commit: bool = True) -> Tenant:
    """Get-or-create a user's personal tenant, plus its self-membership (role='admin') --
    mirrors migration 0023_backfill_personal_tenants.py, which backfilled both rows
    together for every pre-existing user. There's no concept of a personal tenant with
    no membership, so both rows are ensured atomically here.

    commit=False is for the brand-new-user registration flows (auth.py, github_auth.py):
    those callers flush the User row (not commit) and pass commit=False here so the tenant
    and membership inserts land in the *same* transaction as the user, then commit once --
    otherwise a failure between the user's commit and this function's own commit could leave
    a User row with no personal tenant (CodeRabbit finding on #323). Skips the concurrent-
    insert race handling in that mode: user_id was just flushed for the first time in this
    still-open transaction, so no other transaction can already hold a personal tenant for
    it. commit=True (default) keeps the original standalone behavior, used by
    require_personal_tenant (rbac.py) and any other caller not paired with a user commit."""
    tenant = db.query(Tenant).filter(Tenant.kind == "personal", Tenant.personal_user_id == user_id).first()
    if tenant is None:
        tenant = Tenant(kind="personal", personal_user_id=user_id)
        db.add(tenant)
        if commit:
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
        else:
            db.flush()

    if commit:
        get_or_create_membership(db, tenant_id=tenant.id, user_id=user_id, role="admin")
    else:
        # Query first rather than inserting unconditionally: currently always a fresh
        # user_id (see docstring), but staying idempotent here too means a future
        # commit=False caller that reuses an existing user_id degrades to a no-op instead
        # of hitting an uncaught IntegrityError on the membership's unique constraint.
        existing_membership = (
            db.query(Membership).filter(Membership.tenant_id == tenant.id, Membership.user_id == user_id).first()
        )
        if existing_membership is None:
            db.add(Membership(tenant_id=tenant.id, user_id=user_id, role="admin"))
            db.flush()
    return tenant


def get_membership(db: Session, tenant_id: int, user_id: int) -> Membership | None:
    """Read-only lookup, keyed on tenant_id -- the memberships-side equivalent of
    org_membership_repo.get's org_id-keyed lookup on the legacy org_memberships table.
    Issue #190 step 6a: RBAC callsites now read from here instead of org_memberships;
    org_membership_repo's write functions are unchanged and keep dual-writing into
    memberships via _sync_membership_mirror, so this always sees the current state."""
    return db.query(Membership).filter(Membership.tenant_id == tenant_id, Membership.user_id == user_id).first()


def list_org_memberships_for_user(db: Session, user_id: int) -> list[tuple[Org, Membership]]:
    """All of a user's org-tenant memberships, joined back to each Org row -- the
    memberships-side equivalent of org_membership_repo.list_for_user, for callers that
    need to enumerate a user's orgs rather than check one specific org."""
    return (
        db.query(Org, Membership)
        .join(Tenant, Tenant.org_id == Org.id)
        .join(Membership, Membership.tenant_id == Tenant.id)
        .filter(Membership.user_id == user_id, Tenant.kind == "org")
        .all()
    )


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
        updated = update_membership_role(db, tenant_id, user_id, role)
        # A concurrent delete_membership could remove the row between the get-or-create
        # above and this update -- re-create it rather than returning None despite this
        # function's Membership (non-Optional) return type.
        membership = updated if updated is not None else get_or_create_membership(db, tenant_id, user_id, role)
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
