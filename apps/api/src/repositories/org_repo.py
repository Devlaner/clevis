from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.core.db import Org
from src.repositories import tenant_repo


def ensure_tenant_linked(db: Session, org: Org) -> Org:
    if org.tenant_id is not None:
        return org
    tenant = tenant_repo.get_or_create_org_tenant(db, org.id)
    org.tenant_id = tenant.id
    db.commit()
    db.refresh(org)
    return org


def get_by_login(db: Session, github_login: str) -> Org | None:
    return db.query(Org).filter(Org.github_login == github_login).first()


def get_by_login_ci(db: Session, github_login: str) -> Org | None:
    # Case-insensitive: callers taking an arbitrary user-typed/query-param login (not a
    # URL path segment already known to match) need this, same reasoning as
    # installation_repo.get_for_org's case-insensitive match (#246) -- a casing variant
    # of a connected org's login must still resolve to it, not silently miss and fall
    # through to a different (personal) resolution path.
    return db.query(Org).filter(func.lower(Org.github_login) == github_login.lower()).first()


def get_by_id(db: Session, org_id: int) -> Org | None:
    return db.query(Org).filter(Org.id == org_id).first()


def get_by_org_id(db: Session, github_org_id: int) -> Org | None:
    return db.query(Org).filter(Org.github_org_id == github_org_id).first()


def get_or_create(db: Session, github_login: str, github_org_id: int | None = None) -> Org:
    org = get_by_login(db, github_login)
    if org is not None:
        if github_org_id is not None and org.github_org_id is None:
            org.github_org_id = github_org_id
            db.commit()
            db.refresh(org)
        return ensure_tenant_linked(db, org)

    if github_org_id is not None:
        # The org may have been renamed on GitHub since we last saw it -- github_org_id
        # is the stable identity, github_login isn't. Resolve by id and update the login
        # in place rather than trying (and failing) to insert a second row for the same id.
        org = get_by_org_id(db, github_org_id)
        if org is not None:
            if org.github_login != github_login:
                org.github_login = github_login
                db.commit()
                db.refresh(org)
            return ensure_tenant_linked(db, org)

    org = Org(github_login=github_login, github_org_id=github_org_id)
    db.add(org)
    try:
        db.commit()
    except IntegrityError:
        # Lost a race with a concurrent insert of the same github_login or github_org_id.
        db.rollback()
        org = get_by_login(db, github_login)
        if org is None and github_org_id is not None:
            org = get_by_org_id(db, github_org_id)
        if org is None:
            raise
        return ensure_tenant_linked(db, org)
    db.refresh(org)
    return ensure_tenant_linked(db, org)
