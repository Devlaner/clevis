"""Resolve a GitHub API token for a request, preferring a connected GitHub App
installation over a client-supplied personal access token.

If the target org/account has a `github_installations` row with an `installation_id`,
mint a short-lived installation token via `github_app.get_installation_token()`. Otherwise
fall back to whatever token the caller supplied (the legacy PAT path). Raises
`NoGitHubTokenAvailable` when neither is available.
"""

from __future__ import annotations

import logging
from typing import Literal

import httpx
from sqlalchemy.orm import Session

from src.core.config import settings
from src.core.db import GitHubInstallation
from src.repositories import installation_repo, org_repo, tenant_repo
from src.services import github_app

logger = logging.getLogger(__name__)

_ROLE_RANK = {"member": 0, "admin": 1}


class NoGitHubTokenAvailable(RuntimeError):
    """Raised when no GitHub App installation and no client-supplied token are available."""


class InsufficientOrgRole(RuntimeError):
    """Raised when the caller is a member of the target org but below the role a
    privileged action requires (e.g. a "member" hitting a cache-clear/dispatch
    endpoint that needs "admin"). Callers must map this to 403, not fall back to a
    client-supplied token -- honoring one here would let a client-supplied PAT
    bypass the org-admin requirement (AGENTS.md: never bypass require_org_role
    ("admin") on privileged org actions)."""


def _from_installation(installation: GitHubInstallation | None) -> str | None:
    if installation is None or installation.installation_id is None:
        return None
    try:
        return github_app.get_installation_token(installation.installation_id)
    except github_app.GitHubAppNotConfigured:
        # Deployment-wide: the App isn't set up at all. Distinct from "no installation for
        # this account" — worth a log line since every installation-backed request will hit
        # this until an operator configures GITHUB_APP_ID/GITHUB_APP_PRIVATE_KEY.
        logger.warning("GitHub App installation %s exists but the App is not configured", installation.installation_id)
        return None
    except (httpx.HTTPStatusError, httpx.RequestError):
        # The installation row is stale (App uninstalled/suspended on GitHub's side) or
        # GitHub's token-minting endpoint is briefly unreachable. Don't let this become an
        # unhandled 500 — fall back to a client-supplied token same as "no installation".
        logger.warning("Failed to mint an installation token for installation %s", installation.installation_id, exc_info=True)
        return None


def _github_app_configured() -> bool:
    return settings.github_app_id is not None and settings.github_app_private_key is not None


def _no_token_error(account_login: str, installation_exists: bool, *, personal: bool) -> NoGitHubTokenAvailable:
    if installation_exists:
        # Distinct from "never installed" -- the installation row is there, minting just
        # failed (stale/uninstalled on GitHub's side, or a transient API error; see the
        # warning _from_installation already logged). Telling the caller to "install" it
        # sends them down the wrong path when, per the DB, it's already installed.
        return NoGitHubTokenAvailable(
            f"A GitHub App installation exists for '{account_login}' but minting a token for it "
            "failed. The installation may need to be reinstalled, or the GitHub App may be "
            "misconfigured -- check server logs for details."
        )
    install_hint = "Install the GitHub App" if personal else "Install the GitHub App for this organization"
    return NoGitHubTokenAvailable(
        f"No GitHub App installation found for '{account_login}' and no token was provided. "
        f"{install_hint}, or add a personal access token in Settings."
    )


def resolve_org_token(db: Session, *, org_id: int, account_login: str, client_token: str | None) -> str:
    installation = (
        installation_repo.get_for_org(db, org_id=org_id, account_login=account_login)
        if _github_app_configured()
        else None
    )
    token = _from_installation(installation)
    if token:
        return token
    if client_token:
        return client_token
    raise _no_token_error(account_login, installation is not None, personal=False)


def resolve_personal_token(db: Session, *, owner_user_id: int, account_login: str, client_token: str | None) -> str:
    installation = (
        installation_repo.get_for_user(db, owner_user_id=owner_user_id, account_login=account_login)
        if _github_app_configured()
        else None
    )
    token = _from_installation(installation)
    if token:
        return token
    if client_token:
        return client_token
    raise _no_token_error(account_login, installation is not None, personal=True)


def resolve_owner_token(
    db: Session,
    *,
    user_id: int,
    owner: str,
    client_token: str | None,
    min_role: Literal["member", "admin"] = "member",
) -> str:
    """Resolve a token for the "/me/*" endpoints, which accept an arbitrary `owner` (not
    necessarily the caller's own account) -- e.g. Overview's cockpit/my-view widgets. If
    `owner` is a Clevis org the caller has at least `min_role` in, prefer that org's
    installation (same resolution the `/orgs/{org_login}/...` endpoints use) over a
    personal-account installation, since an org-scoped install is the common case for
    these widgets and resolve_personal_token's owner_user_id-scoped lookup can never
    match it (this is why Overview showed "No GitHub App installation found" despite a
    correctly connected org).

    Role-gated, not just "org exists": without this, any authenticated user could pull
    another org's GitHub data through a /me/* endpoint merely by typing its login,
    bypassing the require_org_role check its /orgs/{org_login}/... equivalent enforces.
    `min_role` defaults to "member" (read-only endpoints) but callers backing a
    privileged action (e.g. cache clear, workflow dispatch) must pass "admin" to match
    their org-scoped equivalent's requirement.

    If the caller has no membership row for `owner` at all, this falls through to
    resolve_personal_token (bring-your-own-token is fine here -- there's no Clevis org
    relationship to bypass). But if a membership row exists and its role is below
    `min_role`, this raises InsufficientOrgRole rather than falling through: silently
    honoring a client-supplied token in that case would let a "member" trigger an
    admin-only action just by pasting their own PAT, undermining the role gate above.
    """
    org = org_repo.get_by_login_ci(db, owner)
    if org is not None:
        org = org_repo.ensure_tenant_linked(db, org)
        membership = tenant_repo.get_membership(db, org.tenant_id, user_id)
        if membership is not None:
            if _ROLE_RANK.get(membership.role, -1) < _ROLE_RANK[min_role]:
                raise InsufficientOrgRole(f"'{min_role}' access to '{owner}' is required for this action.")
            return resolve_org_token(db, org_id=org.id, account_login=owner, client_token=client_token)
    return resolve_personal_token(db, owner_user_id=user_id, account_login=owner, client_token=client_token)
