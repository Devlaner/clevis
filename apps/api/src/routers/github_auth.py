"""GitHub OAuth sign-in router — /auth/github/*.

  GET /auth/github/login     -> 307 redirect to GitHub's authorize page (with a browser-bound
                                CSRF state -- see src.services.github_oauth)
  GET /auth/github/callback  -> verify state, exchange code, fetch identity, find-or-create the
                                local user, sync verified GitHub org-admin memberships, set the
                                httpOnly session cookie, redirect to the UI

Find-or-create policy: link to an existing user by GitHub id; otherwise create a new user (the
first-ever user becomes the workspace admin, mirroring the email/password setup flow). We
deliberately do NOT fall back to matching by email -- self-registration (POST /auth/register)
has no email-ownership verification anywhere in this app, so auto-linking onto an existing
account by email match alone would let an attacker who pre-registers a victim's email silently
inherit that victim's real GitHub identity (and whatever org-admin access it earns via
src.services.org_provisioning) the next time the victim signs in with GitHub, while the attacker
keeps their own password on the shared account. If GitHub reports an email that already belongs
to a different local account, the callback redirects with an explanatory error instead of
linking; the user needs to sign in with their password first.
"""

import logging

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from src.core.auth import create_access_token, set_session_cookie
from src.core.config import settings
from src.core.db import User, get_db, set_session_user
from src.core.rate_limit import rate_limit
from src.repositories import tenant_repo
from src.services import github_oauth, org_provisioning

logger = logging.getLogger(__name__)
router = APIRouter()


def _callback_url(request: Request) -> str:
    return str(request.url_for("github_callback"))


def _ui_redirect_target() -> str:
    return settings.cors_origins[0] if settings.cors_origins else "/"


def _ui_login_error_redirect(error_code: str) -> RedirectResponse:
    base = settings.cors_origins[0] if settings.cors_origins else ""
    return RedirectResponse(f"{base}/login?error={error_code}", status_code=303)


class EmailAlreadyRegistered(Exception):
    """GitHub reported a verified email that already belongs to a different local account
    with no GitHub identity linked. See the module docstring for why we refuse to
    auto-link in this case instead of silently taking over that account."""


def find_or_create_user(db: Session, identity: github_oauth.GitHubIdentity) -> User:
    user = db.query(User).filter(User.github_user_id == identity.github_user_id).first()
    if user is not None:
        # Returning GitHub user -- refresh their profile fields, keep their role.
        user.github_login = identity.login
        user.avatar_url = identity.avatar_url
        if not user.name and identity.name:
            user.name = identity.name
        db.commit()
        db.refresh(user)
        return user

    if db.query(User).filter(func.lower(User.email) == identity.email.lower()).first() is not None:
        raise EmailAlreadyRegistered(identity.email)

    is_workspace_admin = db.query(User).count() == 0
    user = User(
        email=identity.email.lower(),
        name=identity.name,
        password_hash=None,
        is_workspace_admin=is_workspace_admin,
        github_user_id=identity.github_user_id,
        github_login=identity.login,
        avatar_url=identity.avatar_url,
        # GitHub already vouches for this email -- fetch_identity() only ever returns a
        # GitHub-verified address (github_oauth._primary_verified_email filters on
        # verified=True; GitHub itself requires every email on an account to be verified
        # before it can be used). Same trust basis as the first-run setup admin. Issue #217.
        email_verified=True,
    )
    db.add(user)
    # flush (not commit): land the personal tenant/membership in the same transaction as
    # this user, then commit once -- a failure between two separate commits could otherwise
    # leave a User row with no personal tenant (#323 CodeRabbit finding).
    db.flush()
    set_session_user(db, user.id)
    tenant_repo.ensure_personal_tenant(db, user.id, commit=False)
    db.commit()
    db.refresh(user)
    return user


def _clear_state_cookie(response: Response) -> None:
    response.delete_cookie(key=github_oauth.STATE_COOKIE_NAME, domain=settings.session_cookie_domain, path="/")


@router.get("/login")
def github_login(request: Request, next: str | None = None):
    try:
        state, nonce = github_oauth.sign_state(next=next)
        url = github_oauth.build_authorize_url(state=state, redirect_uri=_callback_url(request))
    except github_oauth.GitHubOAuthNotConfigured:
        return _ui_login_error_redirect("github_not_configured")
    response = RedirectResponse(url, status_code=307)
    # Binds `state` to this browser -- must be SameSite=Lax (not Strict) so it's still sent
    # on the top-level GET redirect back from github.com, regardless of how the instance's
    # main session_cookie_samesite is configured.
    response.set_cookie(
        key=github_oauth.STATE_COOKIE_NAME,
        value=nonce,
        max_age=github_oauth.STATE_COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        domain=settings.session_cookie_domain,
        path="/",
    )
    return response


@router.get("/callback", name="github_callback", dependencies=[Depends(rate_limit())])
def github_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    db: Session = Depends(get_db),
):
    cookie_nonce = request.cookies.get(github_oauth.STATE_COOKIE_NAME)
    if not code or not state or not github_oauth.verify_state(state, cookie_nonce=cookie_nonce):
        error_response = _ui_login_error_redirect("github_invalid_state")
        _clear_state_cookie(error_response)
        return error_response
    next_path = github_oauth.decode_state_next(state)
    try:
        user_token = github_oauth.exchange_code_for_token(code, redirect_uri=_callback_url(request))
        identity = github_oauth.fetch_identity(user_token)
    except github_oauth.GitHubOAuthNotConfigured:
        error_response = _ui_login_error_redirect("github_not_configured")
        _clear_state_cookie(error_response)
        return error_response
    except github_oauth.GitHubOAuthError as exc:
        logger.warning("GitHub OAuth callback failed: %s", exc)
        error_response = _ui_login_error_redirect("github_oauth_failed")
        _clear_state_cookie(error_response)
        return error_response
    try:
        user = find_or_create_user(db, identity)
    except EmailAlreadyRegistered:
        error_response = _ui_login_error_redirect("github_email_registered")
        _clear_state_cookie(error_response)
        return error_response
    org_provisioning.sync_org_admin_memberships(db, user, user_token)
    token = create_access_token(user.id, user.email, user.is_workspace_admin, user.name, user.token_version)
    redirect_target = f"{_ui_redirect_target()}{next_path}" if next_path else _ui_redirect_target()
    response = RedirectResponse(redirect_target, status_code=303)
    set_session_cookie(response, token)
    _clear_state_cookie(response)
    return response
