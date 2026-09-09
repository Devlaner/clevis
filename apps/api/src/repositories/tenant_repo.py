from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.core.db import Membership, Org, Tenant


def _set_session_user(db: Session, user_id: int) -> None:
    # Issue #330: memberships' RLS policy (migration 0031) allows a row when EITHER
    # tenant_id matches app.tenant_id OR user_id matches app.user_id. Every write in this
    # module always targets a specific, already-known user_id -- setting app.user_id to
    # that same value here is never a privilege escalation (it just self-identifies the
    # row being written), and lets these writes succeed even when no caller has
    # established a full tenant session context yet (e.g. ensure_personal_tenant creating
    # a brand-new tenant + its own self-membership atomically, before rbac.py's
    # set_tenant_session_context would otherwise run). SET LOCAL (not the plain SET
    # rbac.py's set_tenant_session_context uses) scopes this to only the transaction the
    # caller is about to commit, so it doesn't leak app.user_id into whatever the
    # session does next.
    db.execute(text(f"SET LOCAL app.user_id = {int(user_id)}"))


def _persist_new(db: Session, obj, refetch, *, commit: bool):
    """Insert ``obj``, tolerating a lost race with a concurrent insert of the same row.

    ``commit=True`` (standalone caller): real ``db.commit()``; on a unique-violation,
    full ``db.rollback()`` then ``refetch()`` the row the other transaction committed.

    ``commit=False`` (issue #334): the caller owns the transaction and may be holding a
    ``SELECT ... FOR UPDATE`` lock it needs kept across the whole logical operation, so
    this must not commit or top-level-rollback. The insert lands via a SAVEPOINT
    (``begin_nested``); a unique-violation rolls back only that savepoint, leaving the
    caller's outer transaction and its lock intact, then ``refetch()`` the existing row.
    """
    if commit:
        db.add(obj)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = refetch()
            if existing is None:
                raise
            return existing
        db.refresh(obj)
        return obj

    try:
        with db.begin_nested():
            db.add(obj)
            db.flush()
    except IntegrityError:
        existing = refetch()
        if existing is None:
            raise
        return existing
    return obj


def get_or_create_org_tenant(db: Session, org_id: int, *, commit: bool = True) -> Tenant:
    def _find():
        return db.query(Tenant).filter(Tenant.kind == "org", Tenant.org_id == org_id).first()

    tenant = _find()
    if tenant is not None:
        return tenant
    return _persist_new(db, Tenant(kind="org", org_id=org_id), _find, commit=commit)


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
            _set_session_user(db, user_id)
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


def get_or_create_membership(
    db: Session, tenant_id: int, user_id: int, role: str, *, commit: bool = True
) -> Membership:
    def _find():
        return (
            db.query(Membership)
            .filter(Membership.tenant_id == tenant_id, Membership.user_id == user_id)
            .first()
        )

    membership = _find()
    if membership is not None:
        return membership
    _set_session_user(db, user_id)
    return _persist_new(db, Membership(tenant_id=tenant_id, user_id=user_id, role=role), _find, commit=commit)


def upsert_membership(
    db: Session, tenant_id: int, user_id: int, role: str, *, commit: bool = True
) -> Membership:
    """get_or_create_membership plus role reconciliation -- unlike get_or_create_membership
    alone, this also fixes a stale role on an already-existing row, so callers that resolve
    a membership through more than one code path (existing found / newly created / recovered
    from a concurrent-insert race) can call this unconditionally and always end up in sync.

    commit=False threads through to the sub-calls so the whole get-or-create + role-fix
    sequence stays in the caller's transaction (issue #334)."""
    membership = get_or_create_membership(db, tenant_id, user_id, role, commit=commit)
    if membership.role != role:
        updated = update_membership_role(db, tenant_id, user_id, role, commit=commit)
        # A concurrent delete_membership could remove the row between the get-or-create
        # above and this update -- re-create it rather than returning None despite this
        # function's Membership (non-Optional) return type.
        membership = (
            updated
            if updated is not None
            else get_or_create_membership(db, tenant_id, user_id, role, commit=commit)
        )
    return membership


def update_membership_role(
    db: Session, tenant_id: int, user_id: int, role: str, *, commit: bool = True
) -> Membership | None:
    _set_session_user(db, user_id)
    membership = (
        db.query(Membership)
        .filter(Membership.tenant_id == tenant_id, Membership.user_id == user_id)
        .first()
    )
    if membership is None:
        return None
    membership.role = role
    # commit=False (issue #334): flush the role change into the caller's open transaction
    # instead of committing it -- the caller owns the single commit for the whole logical
    # operation, keeping its FOR UPDATE lock held until then. No db.refresh(): the value we
    # just set is already current in-session, and a refresh is a needless round-trip.
    if commit:
        db.commit()
        db.refresh(membership)
    else:
        db.flush()
    return membership


def delete_membership(db: Session, tenant_id: int, user_id: int, *, commit: bool = True) -> None:
    _set_session_user(db, user_id)
    db.query(Membership).filter(
        Membership.tenant_id == tenant_id, Membership.user_id == user_id
    ).delete()
    # commit=False (issue #334): see update_membership_role -- flush into the caller's
    # transaction so its lock survives until the caller's own single commit.
    if commit:
        db.commit()
    else:
        db.flush()
