"""
Org-scoped RBAC dependencies.

Unlike require_auth/require_workspace_admin (JWT-only, no DB hit), these dependencies
resolve role fresh from the DB on every request, because org membership and invite
status can change while a 30-day JWT is still valid.
"""

from dataclasses import dataclass
from typing import Literal

from fastapi import Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.core.auth import UserOut, require_auth
from src.core.db import Membership, Org, Tenant, get_db
from src.repositories import org_repo, tenant_repo

_ROLE_RANK = {"member": 0, "admin": 1}


@dataclass
class OrgContext:
    org: Org
    membership: Membership


@dataclass
class PersonalTenantContext:
    tenant: Tenant


def set_tenant_session_context(db: Session, tenant_id: int, user_id: int) -> None:
    # Plain SET (not SET LOCAL): a request's Session lifetime doesn't cleanly map to one
    # transaction, so a transaction-scoped SET LOCAL could stop applying mid-request. Read
    # by migration 0030's RLS policies (tenant_id match) and migration 0031's widened
    # self-access clause (user_id match, via src.core.db.set_session_user -- this function's
    # narrower sibling for write paths that know the acting user but not a single tenant).
    #
    # SET does not accept bind parameters (it's a configuration command, not a regular
    # query) -- Postgres rejects `SET app.tenant_id = $1` with a syntax error. Both values
    # are always int (SQLAlchemy-mapped primary keys, never caller-supplied strings), so
    # formatting them directly into the statement is safe; int() here is a defensive type
    # check, not a workaround.
    db.execute(text(f"SET app.tenant_id = {int(tenant_id)}"))
    db.execute(text(f"SET app.user_id = {int(user_id)}"))


def require_org_role(min_role: Literal["member", "admin"]):
    """Dependency factory: 404 if org_login (path param) doesn't exist, 403 if the
    current user isn't a member of it or is below min_role."""

    def dependency(
        org_login: str,
        db: Session = Depends(get_db),
        user: UserOut = Depends(require_auth),
    ) -> OrgContext:
        org = org_repo.get_by_login(db, org_login)
        if org is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Org not found")
        # org.tenant_id is nullable (see db.py's Org.tenant_id docstring) -- a legacy row
        # resolved here via get_by_login (not get_or_create) never gets org_repo's own
        # self-healing dual-write, so reuse the exact same helper org_repo.get_or_create
        # itself uses, rather than a separate hand-rolled copy of the same logic. Resolved
        # ahead of the role check (issue #190 step 6a) so the role lookup itself can read
        # from `memberships` (tenant_id-keyed), the new source of truth for org RBAC, instead
        # of the legacy `org_memberships` table (org_id-keyed) it used to read.
        org = org_repo.ensure_tenant_linked(db, org)
        membership = tenant_repo.get_membership(db, org.tenant_id, user.id)
        if membership is None or _ROLE_RANK.get(membership.role, -1) < _ROLE_RANK[min_role]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Org access required")
        set_tenant_session_context(db, org.tenant_id, user.id)
        return OrgContext(org=org, membership=membership)

    return dependency


def require_personal_tenant(
    db: Session = Depends(get_db),
    user: UserOut = Depends(require_auth),
) -> PersonalTenantContext:
    """Dependency for routes unambiguously scoped to the caller's own personal tenant --
    not for /me/... routes that can resolve to either an org or personal tenant depending
    on an `owner` path param (see src.services.token_resolution.resolve_owner_token);
    wiring those is a separate design decision, deferred out of issue #190's PR 5."""
    tenant = tenant_repo.ensure_personal_tenant(db, user.id)
    set_tenant_session_context(db, tenant.id, user.id)
    return PersonalTenantContext(tenant=tenant)


def assert_owner_matches_org(owner: str, ctx: OrgContext) -> None:
    """Raises 403 if a repo-level `owner` path/body value doesn't match the org context
    require_org_role already resolved — keeps an org-scoped route from acting on a
    GitHub owner outside the org the caller was authorized for."""
    if owner.lower() != ctx.org.github_login.lower():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="owner must match the org in the URL")
