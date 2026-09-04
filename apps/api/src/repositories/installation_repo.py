from datetime import datetime, timezone

from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.core.db import GitHubInstallation, set_session_tenant
from src.repositories import tenant_repo


def _resolve_tenant_id(db: Session, org_id: int | None, owner_user_id: int | None) -> int:
    if org_id is not None:
        return tenant_repo.get_or_create_org_tenant(db, org_id).id
    return tenant_repo.ensure_personal_tenant(db, owner_user_id).id


def upsert(
    db: Session,
    account_login: str,
    account_type: str,
    auth_mode: str,
    installation_id: int | None = None,
    org_id: int | None = None,
    owner_user_id: int | None = None,
) -> GitHubInstallation:
    if (org_id is None) == (owner_user_id is None):
        raise ValueError("Exactly one of org_id or owner_user_id must be set")

    query = db.query(GitHubInstallation).filter(GitHubInstallation.account_login == account_login)
    if org_id is not None:
        query = query.filter(GitHubInstallation.org_id == org_id)
    else:
        query = query.filter(GitHubInstallation.owner_user_id == owner_user_id)

    existing = query.first()
    token_ref = f"tok_{account_login}"
    tenant_id = _resolve_tenant_id(db, org_id, owner_user_id)
    if existing:
        existing.account_type = account_type
        existing.auth_mode = auth_mode
        existing.installation_id = installation_id
        existing.token_ref = token_ref
        existing.tenant_id = tenant_id
        db.commit()
        db.refresh(existing)
        return existing

    row = GitHubInstallation(
        account_login=account_login,
        account_type=account_type,
        installation_id=installation_id,
        auth_mode=auth_mode,
        token_ref=token_ref,
        org_id=org_id,
        owner_user_id=owner_user_id,
        tenant_id=tenant_id,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        # Lost a race with a concurrent sync for the same org/account (unique constraint) --
        # fall back to updating the row the other request just inserted.
        db.rollback()
        existing = query.first()
        if existing is None:
            raise
        existing.account_type = account_type
        existing.auth_mode = auth_mode
        existing.installation_id = installation_id
        existing.token_ref = token_ref
        existing.tenant_id = tenant_id
        db.commit()
        db.refresh(existing)
        return existing
    db.refresh(row)
    return row


def create(
    db: Session,
    account_login: str,
    account_type: str,
    auth_mode: str,
    installation_id: int | None = None,
    org_id: int | None = None,
    owner_user_id: int | None = None,
) -> GitHubInstallation:
    return upsert(
        db,
        account_login=account_login,
        account_type=account_type,
        auth_mode=auth_mode,
        installation_id=installation_id,
        org_id=org_id,
        owner_user_id=owner_user_id,
    )


def get_for_org(db: Session, org_id: int, account_login: str) -> GitHubInstallation | None:
    # Case-insensitive: account_login is stored verbatim from GitHub's install payload,
    # but RBAC/ownership checks elsewhere (assert_owner_matches_org, _verify_installation)
    # already compare logins case-insensitively -- an exact match here could pass those
    # checks yet fail to find an installation that's actually there (#246).
    return (
        db.query(GitHubInstallation)
        .filter(
            GitHubInstallation.org_id == org_id,
            func.lower(GitHubInstallation.account_login) == account_login.lower(),
        )
        .first()
    )


def get_for_user(db: Session, owner_user_id: int, account_login: str) -> GitHubInstallation | None:
    return (
        db.query(GitHubInstallation)
        .filter(
            GitHubInstallation.owner_user_id == owner_user_id,
            func.lower(GitHubInstallation.account_login) == account_login.lower(),
        )
        .first()
    )


def get_by_installation_id_for_org(db: Session, org_id: int, installation_id: int) -> GitHubInstallation | None:
    """Scoped lookup for the disconnect endpoint -- confirms installation_id genuinely belongs
    to this org before anything (a GitHub-side uninstall call, a DB delete) acts on it, so an
    org admin can't disconnect another tenant's installation by guessing its id."""
    return (
        db.query(GitHubInstallation)
        .filter(GitHubInstallation.org_id == org_id, GitHubInstallation.installation_id == installation_id)
        .first()
    )


def get_by_installation_id_for_user(db: Session, owner_user_id: int, installation_id: int) -> GitHubInstallation | None:
    """Same contract as get_by_installation_id_for_org, for the personal-installation
    disconnect endpoint."""
    return (
        db.query(GitHubInstallation)
        .filter(GitHubInstallation.owner_user_id == owner_user_id, GitHubInstallation.installation_id == installation_id)
        .first()
    )


def list_for_org(db: Session, org_id: int) -> list[GitHubInstallation]:
    return (
        db.query(GitHubInstallation)
        .filter(GitHubInstallation.org_id == org_id)
        .order_by(GitHubInstallation.created_at.desc())
        .all()
    )


def list_for_user(db: Session, owner_user_id: int) -> list[GitHubInstallation]:
    return (
        db.query(GitHubInstallation)
        .filter(GitHubInstallation.owner_user_id == owner_user_id)
        .order_by(GitHubInstallation.created_at.desc())
        .all()
    )


def delete_by_installation_id(db: Session, installation_id: int) -> tuple[int, int | None]:
    """Remove every row referencing a GitHub installation_id (e.g. on uninstall).
    Returns (rows deleted, tenant_id of the first deleted row) -- the tenant_id lets the
    webhook handler's audit_repo.write call attribute the deletion under RLS (issue #330);
    every row here already has tenant_id populated by upsert's _resolve_tenant_id, so this
    is just reading back what's already there before the rows are gone.

    Called from the unauthenticated webhook receiver (issue #191/S3), which never sets
    app.tenant_id/app.user_id -- under RLS (migration 0031's FORCE ROW LEVEL SECURITY on
    this table) that made both the SELECT and DELETE below silently see zero rows, every
    time (issue #191/S3 PR 2 fix). resolve_installation_tenant_id() (migration 0035, a
    SECURITY DEFINER function) resolves tenant_id first, bypassing RLS for that one lookup
    the same way the table owner already does; once resolved, set_session_tenant supplies
    the session context RLS expects so the actual SELECT/DELETE that follows are evaluated
    normally, under a session that now legitimately satisfies the tenant_id-equality
    policy branch -- not a second RLS bypass."""
    tenant_id = db.execute(
        text("SELECT resolve_installation_tenant_id(:installation_id)"), {"installation_id": installation_id}
    ).scalar()
    if tenant_id is not None:
        set_session_tenant(db, tenant_id)

    rows = db.query(GitHubInstallation).filter(GitHubInstallation.installation_id == installation_id).all()
    resolved_tenant_id = rows[0].tenant_id if rows else tenant_id
    count = db.query(GitHubInstallation).filter(GitHubInstallation.installation_id == installation_id).delete()
    db.commit()
    return count, resolved_tenant_id


def update_permissions(
    db: Session, *, installation_id: int, permissions: dict, synced_at: datetime | None = None
) -> int:
    """Record GitHub's installation `permissions` object on every row for `installation_id`.

    Returns the number of rows updated (0 if the installation isn't connected in Clevis —
    e.g. a new_permissions_accepted webhook for an install nobody has synced yet).

    Reuses the same RLS handling as delete_by_installation_id: this is called from the
    unauthenticated webhook receiver, which never sets app.tenant_id, so resolve the
    tenant via the SECURITY DEFINER function first, then set the session context the
    tenant_isolation policy expects before the UPDATE runs. Updating an already-connected
    row's permissions column is safe from the webhook — the row's ownership was
    established by the authenticated sync flow; only *creating* rows from a webhook would
    cross a trust boundary (see webhooks.py's installation.created comment).
    """
    tenant_id = db.execute(
        text("SELECT resolve_installation_tenant_id(:installation_id)"), {"installation_id": installation_id}
    ).scalar()
    if tenant_id is not None:
        set_session_tenant(db, tenant_id)

    count = (
        db.query(GitHubInstallation)
        .filter(GitHubInstallation.installation_id == installation_id)
        .update(
            {
                GitHubInstallation.granted_permissions: permissions,
                GitHubInstallation.permissions_synced_at: synced_at or datetime.now(timezone.utc),
            },
            synchronize_session=False,
        )
    )
    db.commit()
    return count
