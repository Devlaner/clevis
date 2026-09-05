"""
JWT helpers and FastAPI auth dependencies.

Two access levels:
  - require_auth           — any authenticated user (valid JWT, not revoked)
  - require_workspace_admin — authenticated user with is_workspace_admin=True (instance config only)

Org-scoped access levels (member/admin per org) live in src/core/rbac.py, since they
require a DB lookup rather than just the JWT claims.

require_auth hits the DB on every request to compare the JWT's token_version claim
against the user's current value, so that revoke-sessions (or any future forced logout)
takes effect immediately instead of waiting out the 30-day token expiry.
"""

from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Cookie, Depends, HTTPException, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.core.config import settings
from src.core.db import User, get_db, set_session_user

_ALGORITHM = "HS256"
_TOKEN_EXPIRE_DAYS = 30

# httpOnly cookie that carries the session JWT (set on OAuth callback / login).
SESSION_COOKIE_NAME = "clevis_session"
_COOKIE_MAX_AGE_SECONDS = _TOKEN_EXPIRE_DAYS * 24 * 60 * 60

# HTTPBearer correctly models "send Bearer token in Authorization header".
# OAuth2PasswordBearer would misrepresent /auth/login as a form-encoded OAuth2 flow.
_http_bearer = HTTPBearer(auto_error=False)


def set_session_cookie(response: Response, token: str) -> None:
    """Attach the session JWT as an httpOnly cookie."""
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=_COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
        domain=settings.session_cookie_domain,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        domain=settings.session_cookie_domain,
        path="/",
    )


class UserOut(BaseModel):
    id: int
    email: str
    name: str | None
    is_workspace_admin: bool
    # Set when the user linked/signed in via GitHub OAuth; None otherwise. Read fresh from
    # the DB in require_auth below (not carried in the JWT), same as this function already
    # re-checks token_version against the DB rather than trusting a stale claim.
    github_login: str | None = None


def create_access_token(
    user_id: int, email: str, is_workspace_admin: bool, name: str | None = None, token_version: int = 0
) -> str:
    payload = {
        "sub": str(user_id),
        "email": email,
        "is_workspace_admin": is_workspace_admin,
        "name": name,
        "token_version": token_version,
        "exp": datetime.now(timezone.utc) + timedelta(days=_TOKEN_EXPIRE_DAYS),
    }
    return jwt.encode(payload, settings.auth_secret.get_secret_value(), algorithm=_ALGORITHM)


def decode_access_token(token: str) -> dict:
    try:
        return jwt.decode(
            token,
            settings.auth_secret.get_secret_value(),
            algorithms=[_ALGORITHM],
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


def require_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(_http_bearer),
    session: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
    db: Session = Depends(get_db),
) -> UserOut:
    """Dependency: validates the JWT (Authorization header or session cookie) and returns the user.

    Prefers the Bearer header (API clients) and falls back to the httpOnly session cookie
    (browser sessions established via GitHub OAuth). Raises 401 if neither is present/valid,
    or if the token's token_version claim no longer matches the user's current value (i.e.
    the session was revoked via POST /auth/me/revoke-sessions).
    """
    token = credentials.credentials if credentials else session
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    payload = decode_access_token(token)
    sub = payload.get("sub")
    email = payload.get("email")
    if not sub or not email:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token claims")
    try:
        user_id = int(sub)
    except (ValueError, TypeError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token claims")
    db_user = db.query(User).filter(User.id == user_id).first()
    if db_user is None or db_user.token_version != payload.get("token_version", 0):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session revoked")
    # Issue #190 step 6c: every authenticated request gets app.user_id set here, ahead of
    # require_org_role/require_personal_tenant's more specific app.tenant_id+app.user_id SET
    # (src.core.rbac). Covers RLS self-access checks (migration 0031) for routes that write
    # a user's own membership/installation row without ever resolving a single tenant --
    # see that migration's docstring for why this is safe.
    set_session_user(db, user_id)
    return UserOut(
        id=user_id,
        email=email,
        name=payload.get("name"),
        is_workspace_admin=bool(payload.get("is_workspace_admin", False)),
        github_login=db_user.github_login,
    )


def require_workspace_admin(user: UserOut = Depends(require_auth)) -> UserOut:
    """Dependency: raises 403 if the authenticated user is not the workspace admin."""
    if not user.is_workspace_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Workspace admin access required")
    return user
