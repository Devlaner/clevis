"""
Auto-provisions and reconciles Clevis org memberships from verified GitHub org status.

Runs at OAuth login time, where a live user token is transiently available (see
src/routers/github_auth.py). Fetches the user's org memberships (role included) from
GitHub in a single call, then does two things:

1. For every org where they're currently a GitHub admin, get-or-creates the Clevis Org
   and grants/refreshes an admin OrgMembership for them. This covers both "first person
   to connect this org" and "another verified GitHub admin signing in later" — GitHub
   already vouches for admins, so no invite is required.
2. Reconciles every *existing* Clevis OrgMembership for this user against the freshly
   fetched GitHub state: demotes to "member" if GitHub now says member, and deletes the
   row entirely if GitHub no longer lists the org for this user at all (removed or the
   org went private to them). This prevents privilege escalated via GitHub admin status
   from outliving its GitHub grant — Clevis previously only ever created/no-op'd
   memberships and never revoked them.

Non-admin GitHub members who have no prior Clevis membership are never auto-added here;
they only gain access through the explicit invite-accept flow.

Best-effort: any GitHub API failure is logged and swallowed so it never blocks login (and
existing memberships are left untouched rather than being wiped on a transient failure).
"""

import logging

import httpx

from sqlalchemy.orm import Session

from src.core.db import Org, User, set_session_user
from src.repositories import audit_repo, org_membership_repo, org_repo, tenant_repo
from src.services import github_oauth

logger = logging.getLogger(__name__)


def connect_admin_org_from_token(
    db: Session, user: User, org_login: str, user_token: str
) -> Org | None:
    """Single-org version of :func:`sync_org_admin_memberships` for the legacy-PAT path
    (issue #368): when a workspace admin saves a PAT for an org Clevis has never connected,
    use that PAT to establish the ``Org`` + admin membership so the ``/orgs/{org}/...``
    dashboard pages work, instead of every one of them 404ing.

    Same authorization policy as the OAuth path: only connects when GitHub reports the caller
    as an **admin/owner** of ``org_login``. A plain GitHub member still goes through the
    explicit invite-accept flow — pasting a PAT must not be a back door around it.

    Returns the connected ``Org`` (canonical GitHub casing), or ``None`` if the PAT can't
    resolve it (missing ``read:org``, GitHub error, malformed response, or the caller isn't
    an admin). **Never raises** — the token save must succeed regardless.
    """
    try:
        memberships = github_oauth.list_user_org_memberships(user_token)
    except Exception as exc:  # noqa: BLE001 -- best-effort; a bad response must not 500 the save
        logger.info("PAT org auto-connect skipped for %s: %s", org_login, exc)
        return None

    match = next(
        (m for m in memberships if m.login.lower() == org_login.lower() and m.role == "admin"),
        None,
    )
    if match is None:
        return None

    set_session_user(db, user.id)
    org = org_repo.get_or_create(db, github_login=match.login, github_org_id=match.github_org_id)
    membership = org_membership_repo.get_or_create(db, org_id=org.id, user_id=user.id, role="admin")
    if membership.role != "admin":
        org_membership_repo.update_role(db, org_id=org.id, user_id=user.id, role="admin")
    audit_repo.write(
        db, user.email, "token.org_autolinked", match.login, {"role": "admin"},
        tenant_id=org.tenant_id,
    )
    return org


def sync_org_admin_memberships(db: Session, user: User, user_token: str) -> None:
    # Issue #190 step 6c: every membership write below is for `user` (never a different
    # user), so this alone satisfies migration 0031's self-access RLS check regardless of
    # which org/tenant each individual write targets. Defensive here rather than relying on
    # the caller (github_auth.py's OAuth callback) having already set it, since this
    # function has no other tenant context of its own across its multi-org loops.
    set_session_user(db, user.id)
    try:
        memberships = github_oauth.list_user_org_memberships(user_token)
    except httpx.HTTPError:
        logger.warning(
            "Failed to list GitHub org memberships for user %s during org provisioning", user.id, exc_info=True
        )
        return

    gh_role_by_org_id = {m.github_org_id: m.role for m in memberships}

    for gh_membership in memberships:
        if gh_membership.role != "admin":
            continue
        org = org_repo.get_or_create(db, github_login=gh_membership.login, github_org_id=gh_membership.github_org_id)
        membership = org_membership_repo.get_or_create(db, org_id=org.id, user_id=user.id, role="admin")
        if membership.role != "admin":
            org_membership_repo.update_role(db, org_id=org.id, user_id=user.id, role="admin")

    for org, membership in tenant_repo.list_org_memberships_for_user(db, user.id):
        if org.github_org_id is None:
            continue
        gh_role = gh_role_by_org_id.get(org.github_org_id)
        if gh_role is None:
            org_membership_repo.delete(db, org_id=org.id, user_id=user.id)
        elif gh_role == "member" and membership.role != "member":
            org_membership_repo.update_role(db, org_id=org.id, user_id=user.id, role="member")
