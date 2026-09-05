"""GitHub App installation router.

  GET  /orgs/{org_login}/installations       member: list installations connected to this org
  POST /orgs/{org_login}/installations/sync  admin: re-sync installation metadata for this org.
                                              If the caller isn't already a known Clevis org
                                              admin (e.g. this is the org's first-ever
                                              installation, or their Clevis membership is stale),
                                              this bootstraps the Org/OrgMembership rows itself by
                                              live-checking the caller's GitHub org role via the
                                              installation's own token -- see
                                              _bootstrap_org_admin_from_installation below. This
                                              doesn't need the caller's OAuth user token (Clevis
                                              never persists it -- see src.services.org_provisioning)
                                              because the just-installed App can check org
                                              membership on its own behalf.
  GET  /me/installations                     list the current user's personal installations
  POST /me/installations/sync                connect a personal (User-type) GitHub installation
  GET  /me/installations/lookup/{id}          resolve an installation_id to the account it belongs
                                              to, so the post-install UI callback (which only gets
                                              installation_id/setup_action from GitHub) knows
                                              whether to call the /me or /orgs sync endpoint next.
  DELETE /orgs/{org_login}/installations/{installation_id}   admin: disconnect -- uninstalls the
                                              App on GitHub's side (a real revocation, not just
                                              removing our own row) then deletes the local row.
  DELETE /me/installations/{installation_id}  same, for the caller's own personal installation.
"""

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.core.auth import UserOut, require_auth
from src.core.db import Org, User, get_db
from src.core.rbac import OrgContext, require_org_role, resolve_org_role, set_tenant_session_context
from src.repositories import audit_repo, installation_repo, org_membership_repo, org_repo, tenant_repo
from src.schemas.installation import (
    BlockedFeatureOut,
    InstallationLookupOut,
    InstallationOut,
    SyncInstallationsInput,
    SyncInstallationsResponse,
)
from src.services import app_permissions, backfill_service, github_app
from src.services.token_resolution import NoGitHubTokenAvailable, resolve_org_token, resolve_personal_token

logger = logging.getLogger(__name__)

router = APIRouter()


def _fetch_installation(installation_id: int) -> dict:
    """Look up an installation_id via the GitHub App's own credentials, mapping
    GitHub/transport errors to the HTTPException shape callers expect."""
    try:
        return github_app.get_installation(installation_id)
    except github_app.GitHubAppNotConfigured:
        raise HTTPException(
            status_code=503,
            detail="GitHub App is not configured; cannot verify installation_id",
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise HTTPException(status_code=422, detail=f"installation_id {installation_id} does not exist")
        raise HTTPException(status_code=400, detail=f"GitHub API error: {exc.response.status_code}")
    except httpx.RequestError:
        raise HTTPException(status_code=503, detail="GitHub API unreachable")


def _verify_installation(installation_id: int | None, account_login: str, account_type: str) -> dict | None:
    """Confirm a client-supplied installation_id genuinely belongs to the claimed
    account before it's persisted as trusted data. Returns the GitHub installation
    object (so the caller can persist its `permissions` dict) or None if installation_id
    wasn't supplied — there's nothing to verify in that case."""
    if installation_id is None:
        return None
    installation = _fetch_installation(installation_id)

    account = installation.get("account") or {}
    actual_login = account.get("login", "")
    actual_type = account.get("type", "")
    if actual_login.lower() != account_login.lower() or actual_type != account_type:
        raise HTTPException(
            status_code=422,
            detail=(
                f"installation_id {installation_id} belongs to {actual_type} "
                f"'{actual_login}', not {account_type} '{account_login}'"
            ),
        )
    return installation


def _capture_permissions_best_effort(db: Session, installation_id: int | None, installation: dict | None) -> None:
    """Opportunistically persist GitHub's `permissions` dict for a just-synced install.
    Best-effort: the install already succeeded and this is enrichment, so a failure here
    must not fail the response."""
    if installation_id is None or not isinstance(installation, dict):
        return
    permissions = installation.get("permissions")
    if not isinstance(permissions, dict):
        return
    try:
        installation_repo.update_permissions(db, installation_id=installation_id, permissions=permissions)
    except Exception:
        db.rollback()
        logger.exception("failed to capture permissions for installation %s", installation_id)


def _enqueue_backfill_best_effort(db: Session, *, tenant_id: int, account_login: str, account_type: str, resolve_token) -> None:
    """S5 PR 1: best-effort install-time activity backfill. Must never fail the install
    response itself -- the install already succeeded (its own audit log entry is already
    written by the time this runs), and backfill is enrichment, not a precondition of a
    working install. `resolve_token` is a zero-arg callable so the org/personal callers
    can each pass their own resolve_org_token/resolve_personal_token call without this
    helper needing to know which one applies."""
    try:
        token = resolve_token()
        backfill_service.enqueue(db, tenant_id=tenant_id, account_login=account_login, account_type=account_type, token=token)
    except NoGitHubTokenAvailable as exc:
        logger.warning("skipping activity backfill for %s: %s", account_login, exc)
    except Exception:
        # A DB-level error here (e.g. from job_repo.enqueue's own commit) leaves the
        # shared request Session's transaction aborted -- roll it back so the response
        # this handler still has to build (row.token_ref) doesn't itself 500 trying to
        # use a poisoned session.
        db.rollback()
        logger.exception("failed to enqueue activity backfill for %s", account_login)


def _to_installation_out(row) -> InstallationOut:
    """Map a GitHubInstallation row to the API shape, computing which optional write
    automations are currently blocked by a missing permission."""
    # Only report blocked features once we've actually observed the install's permissions.
    # A never-checked row (pre-tracking, or before the first accept webhook / reconnect)
    # would otherwise show every feature as blocked; the UI renders a "not yet checked"
    # state off permissions_synced_at instead.
    blocked = (
        app_permissions.blocked_features(row.granted_permissions)
        if row.permissions_synced_at is not None
        else []
    )
    return InstallationOut(
        id=row.id,
        account_login=row.account_login,
        account_type=row.account_type,
        installation_id=row.installation_id,
        created_at=row.created_at,
        permissions_synced_at=row.permissions_synced_at,
        blocked_features=[
            BlockedFeatureOut(feature=b.feature, label=b.label, missing=b.missing) for b in blocked
        ],
    )


@router.get("/me/installations/lookup/{installation_id}", response_model=InstallationLookupOut)
def lookup_installation(
    installation_id: int,
    _user: UserOut = Depends(require_auth),
):
    installation = _fetch_installation(installation_id)
    account = installation.get("account") or {}
    return {
        "account_login": account.get("login", ""),
        "account_type": account.get("type", ""),
    }


@router.get("/orgs/{org_login}/installations", response_model=list[InstallationOut])
def list_org_installations(
    ctx: OrgContext = Depends(require_org_role(min_role="member")),
    db: Session = Depends(get_db),
):
    return [_to_installation_out(r) for r in installation_repo.list_for_org(db, org_id=ctx.org.id)]


def _uninstall_on_github(installation_id: int) -> None:
    """Shared by both disconnect endpoints. Raises HTTPException on any failure that means
    the local row must NOT be deleted (GitHub App not configured, or a real GitHub API error
    other than 404) -- callers only proceed to delete the local row once this returns cleanly."""
    try:
        github_app.delete_installation(installation_id)
    except github_app.GitHubAppNotConfigured:
        raise HTTPException(status_code=503, detail="GitHub App is not configured; cannot disconnect")
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=400, detail=f"GitHub API error: {exc.response.status_code}")
    except httpx.RequestError:
        raise HTTPException(status_code=503, detail="GitHub API unreachable")


@router.delete("/orgs/{org_login}/installations/{installation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_org_installation(
    installation_id: int,
    ctx: OrgContext = Depends(require_org_role(min_role="admin")),
    user: UserOut = Depends(require_auth),
    db: Session = Depends(get_db),
):
    installation = installation_repo.get_by_installation_id_for_org(db, org_id=ctx.org.id, installation_id=installation_id)
    if installation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Installation not found for this organization")

    _uninstall_on_github(installation_id)

    # require_org_role already set tenant session context for ctx.org.tenant_id above --
    # delete_by_installation_id's own resolve-then-set is for its other caller (the
    # unauthenticated webhook receiver), a no-op re-set here for this authenticated path.
    count, tenant_id = installation_repo.delete_by_installation_id(db, installation_id)
    audit_repo.write(
        db,
        actor=user.email,
        action="installation.disconnected",
        target=ctx.org.github_login,
        payload={"installation_id": installation_id, "rows_deleted": count},
        tenant_id=tenant_id,
    )


def _bootstrap_org_admin_from_installation(db: Session, db_user: User, org_login: str, installation_id: int) -> Org:
    """No Clevis Org exists for org_login yet, or the caller isn't a known admin of it --
    live-verify the caller is actually a GitHub admin of this org right now, using the
    just-installed App's own installation token (never the caller's unverified say-so),
    then get-or-create the Org/OrgMembership rows. Callers must already have confirmed
    db_user.github_login is set and installation_id is not None. Raises HTTPException on
    any failure to verify (GitHub API error, or the live check saying the caller isn't an
    admin).

    Serialized per org_login via pg_advisory_xact_lock (transaction-scoped, released at
    the end of this request) -- without it, two concurrent installation syncs for the
    same org_login (e.g. two admins racing to connect the same brand-new org with
    different installation_id's) could both pass the live-admin check before either
    commits, then race to attach to (or duplicate) the same Org row. Matches the pattern
    /auth/setup uses for its own check-then-insert race (see auth.py's _SETUP_LOCK_KEY).
    hashtext() maps the arbitrary org_login string to the int4 key pg_advisory_xact_lock
    expects."""
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:org_login))"), {"org_login": org_login})
    try:
        installation_token = github_app.get_installation_token(installation_id)
        role = github_app.get_org_membership_role(installation_token, org_login, db_user.github_login)
    except github_app.GitHubAppNotConfigured:
        raise HTTPException(status_code=503, detail="GitHub App is not configured")
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=400, detail=f"GitHub API error: {exc.response.status_code}")
    except httpx.RequestError:
        raise HTTPException(status_code=503, detail="GitHub API unreachable")
    if role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You must be an admin of this GitHub organization to connect it",
        )
    org = org_repo.get_or_create(db, github_login=org_login)
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=db_user.id, role="admin")
    return org


@router.post("/orgs/{org_login}/installations/sync", response_model=SyncInstallationsResponse)
def sync_org_installation(
    org_login: str,
    payload: SyncInstallationsInput,
    user: UserOut = Depends(require_auth),
    db: Session = Depends(get_db),
):
    if payload.account_login.lower() != org_login.lower():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="owner must match the org in the URL")

    # Issue #190 CodeRabbit follow-up: this reuses rbac.resolve_org_role -- the same
    # non-raising resolution logic require_org_role's dependency itself calls -- so the
    # known-admin fast-path check here can't silently drift from what require_org_role
    # considers a valid org admin.
    ctx: OrgContext | None = resolve_org_role(db, org_login, user.id, "admin")
    is_known_admin = ctx is not None
    org: Org | None = ctx.org if ctx is not None else org_repo.get_by_login(db, org_login)

    # Only a caller who ISN'T already a confirmed local admin needs the extra checks below
    # (linked GitHub account, installation_id present) -- resolved before any GitHub call
    # so an unauthorized request fails fast instead of paying for a network round-trip.
    db_user: User | None = None
    if not is_known_admin:
        db_user = db.query(User).filter(User.id == user.id).first()
        if not db_user or not db_user.github_login:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Link your GitHub account (sign in with GitHub) before connecting an organization",
            )
        if payload.installation_id is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Org not found")

    installation = _verify_installation(payload.installation_id, payload.account_login, payload.account_type)

    if not is_known_admin:
        org = _bootstrap_org_admin_from_installation(db, db_user, org_login, payload.installation_id)

    # Issue #190 step 6c: github_installations' org-scoped rows have no per-row user column,
    # so migration 0031's self-access clause (owner_user_id match) can't cover this write the
    # way it covers personal installations -- org.tenant_id is already resolved by this point
    # (ensure_tenant_linked above, or _bootstrap_org_admin_from_installation's own
    # org_repo.get_or_create), so set the real tenant context explicitly instead.
    set_tenant_session_context(db, org.tenant_id, user.id)
    row = installation_repo.create(
        db,
        account_login=payload.account_login,
        account_type=payload.account_type,
        auth_mode=payload.auth_mode,
        installation_id=payload.installation_id,
        org_id=org.id,
    )
    audit_repo.write(
        db,
        actor=user.email,
        action="installation.connected",
        target=org_login,
        payload={"account_type": payload.account_type, "installation_id": payload.installation_id},
        tenant_id=org.tenant_id,
    )
    _capture_permissions_best_effort(db, payload.installation_id, installation)
    _enqueue_backfill_best_effort(
        db,
        tenant_id=org.tenant_id,
        account_login=payload.account_login,
        account_type=payload.account_type,
        resolve_token=lambda: resolve_org_token(db, org_id=org.id, account_login=payload.account_login, client_token=None),
    )
    return {"synced": True, "token_ref": row.token_ref}


@router.get("/me/installations", response_model=list[InstallationOut])
def list_personal_installations(
    user: UserOut = Depends(require_auth),
    db: Session = Depends(get_db),
):
    return [_to_installation_out(r) for r in installation_repo.list_for_user(db, owner_user_id=user.id)]


@router.delete("/me/installations/{installation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_personal_installation(
    installation_id: int,
    user: UserOut = Depends(require_auth),
    db: Session = Depends(get_db),
):
    installation = installation_repo.get_by_installation_id_for_user(db, owner_user_id=user.id, installation_id=installation_id)
    if installation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Installation not found")

    _uninstall_on_github(installation_id)

    personal_tenant = tenant_repo.ensure_personal_tenant(db, user.id)
    set_tenant_session_context(db, personal_tenant.id, user.id)
    count, tenant_id = installation_repo.delete_by_installation_id(db, installation_id)
    audit_repo.write(
        db,
        actor=user.email,
        action="installation.disconnected.personal",
        target=installation.account_login,
        payload={"installation_id": installation_id, "rows_deleted": count},
        tenant_id=tenant_id,
    )


@router.post("/me/installations/sync", response_model=SyncInstallationsResponse)
def sync_personal_installation(
    payload: SyncInstallationsInput,
    user: UserOut = Depends(require_auth),
    db: Session = Depends(get_db),
):
    if payload.account_type != "User":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Personal installation sync only supports account_type User; use org sync for organizations",
        )
    db_user = db.query(User).filter(User.id == user.id).first()
    if not db_user or not db_user.github_login:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Link your GitHub account before syncing a personal installation",
        )
    if payload.account_login != db_user.github_login:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="account_login must match your own GitHub account",
        )
    installation = _verify_installation(payload.installation_id, payload.account_login, payload.account_type)
    row = installation_repo.create(
        db,
        account_login=payload.account_login,
        account_type=payload.account_type,
        auth_mode=payload.auth_mode,
        installation_id=payload.installation_id,
        owner_user_id=user.id,
    )
    personal_tenant = tenant_repo.ensure_personal_tenant(db, user.id)
    audit_repo.write(
        db,
        actor=user.email,
        action="installation.connected.personal",
        target=payload.account_login,
        payload={"account_type": payload.account_type, "installation_id": payload.installation_id},
        tenant_id=personal_tenant.id,
    )
    _capture_permissions_best_effort(db, payload.installation_id, installation)
    _enqueue_backfill_best_effort(
        db,
        tenant_id=personal_tenant.id,
        account_login=payload.account_login,
        account_type=payload.account_type,
        resolve_token=lambda: resolve_personal_token(
            db, owner_user_id=user.id, account_login=payload.account_login, client_token=None
        ),
    )
    return {"synced": True, "token_ref": row.token_ref}
