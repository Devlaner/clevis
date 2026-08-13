from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.core.db import GitHubInstallation
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


def delete_by_installation_id(db: Session, installation_id: int) -> int:
    """Remove every row referencing a GitHub installation_id (e.g. on uninstall).
    Returns the number of rows deleted."""
    count = db.query(GitHubInstallation).filter(GitHubInstallation.installation_id == installation_id).delete()
    db.commit()
    return count
