"""
Auth router — /auth/*

Endpoints:
  POST /auth/setup          First-run only; creates the owner account (409 if users exist)
  POST /auth/register       Creates a non-owner account; 403 if registration_enabled=false
  POST /auth/verify-email   Marks the account owning the given token as email-verified
  POST /auth/resend-verification Resends the verification email to the caller, if unverified
  POST /auth/login          Returns a JWT on valid credentials
  GET  /auth/me             Returns the current authenticated user
  PATCH /auth/me            Updates the current user's display name
  POST /auth/me/revoke-sessions Invalidates all previously issued JWTs for the caller
  GET  /auth/setup-required Returns { setup_required: bool } for the UI routing decision

/auth/login and /auth/register are rate-limited per client IP (see src.core.rate_limit);
/auth/login is additionally rate-limited per submitted email, so an attacker spread across
many source IPs can't bypass the IP-keyed limit by targeting one account.

Email verification (issue #217): register() creates accounts with email_verified=False
and emails a verification link; setup() and GitHub-OAuth-linked accounts (see
src.routers.github_auth) are verified immediately since their email is already trusted
(the deploying operator, or GitHub itself). Unverified accounts work normally everywhere
except accepting an org invitation (src.routers.invitations), which requires proof the
account actually controls the invited inbox.
"""

from datetime import datetime, timedelta, timezone
import logging
import secrets

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, EmailStr
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.core.app_config import get_config
from src.core.auth import UserOut, clear_session_cookie, create_access_token, require_auth
from src.core.config import settings
from src.core.db import Org, User, get_db
from src.core.rate_limit import check_account_rate_limit, rate_limit
from src.repositories import invitation_repo
from src.services.email import EmailNotConfigured, send_verification_email

logger = logging.getLogger(__name__)
router = APIRouter()

_MIN_PASSWORD_LEN = 12
# bcrypt's hard limit -- bcrypt.hashpw/checkpw raise ValueError on any input over 72 bytes
# rather than truncating, so this must be validated before either is ever called.
_MAX_PASSWORD_LEN = 72
_VERIFY_TOKEN_TTL = timedelta(hours=24)


def _send_verification_email_best_effort(user: User) -> None:
    """Generates a fresh verification token/expiry on `user`, then tries to email it.
    Caller is responsible for db.commit(). Never raises -- registration/resend must
    succeed regardless of whether SMTP is configured or the send itself fails."""
    user.email_verify_token = secrets.token_urlsafe(32)
    user.email_verify_token_expires_at = datetime.now(timezone.utc) + _VERIFY_TOKEN_TTL
    try:
        verify_url = f"{settings.cors_origins[0]}/verify-email?token={user.email_verify_token}"
        send_verification_email(user.email, verify_url)
    except EmailNotConfigured:
        logger.warning("SMTP not configured -- skipping verification email for %s", user.email)
    except Exception:
        # Covers both a send failure and a misconfigured empty CORS_ORIGINS (cors_origins[0]
        # would otherwise raise IndexError here, uncaught, breaking registration itself --
        # this function must never raise regardless of the cause.
        logger.exception("failed to send verification email to %s", user.email)

# Arbitrary constant identifying the /auth/setup critical section for pg_advisory_xact_lock
# (see setup()). Any int works; this one has no other significance.
_SETUP_LOCK_KEY = 727100

# Fixed hash checked when no real user/password_hash exists, so a login attempt for a
# nonexistent email still pays the same bcrypt cost as a real one -- otherwise response
# timing alone (short-circuit vs. a real ~100-300ms bcrypt check) reveals whether an
# email is registered.
_DUMMY_PASSWORD_HASH = bcrypt.hashpw(b"clevis-timing-safety-dummy", bcrypt.gensalt()).decode()


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _verify_password(password: str, password_hash: str | None) -> bool:
    password_bytes = password.encode()
    # No account's real password can be over _MAX_PASSWORD_LEN bytes -- setup()/register()
    # reject it before hashing -- so an oversized guess is always wrong. Still run a real
    # (truncated) bcrypt call rather than short-circuiting, so response timing doesn't
    # reveal the oversized-password case any more than it reveals a nonexistent email below.
    if len(password_bytes) > _MAX_PASSWORD_LEN:
        password_bytes = password_bytes[:_MAX_PASSWORD_LEN]
        bcrypt.checkpw(password_bytes, (password_hash or _DUMMY_PASSWORD_HASH).encode())
        return False
    if not password_hash:
        bcrypt.checkpw(password_bytes, _DUMMY_PASSWORD_HASH.encode())
        return False
    return bcrypt.checkpw(password_bytes, password_hash.encode())


# ── Schemas ───────────────────────────────────────────────────────────────────

class SetupRequest(BaseModel):
    email: EmailStr
    name: str | None = None
    password: str


class RegisterRequest(BaseModel):
    email: EmailStr
    name: str | None = None
    password: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class PendingInvitationSummary(BaseModel):
    # Deliberately excludes the invitation token: the email isn't verified yet at
    # register()/login() time (verification is async, via the emailed link -- see
    # accept_invitation's email_verified check in src.routers.invitations), so "an
    # account with email X" is still not immediate proof of controlling inbox X. Handing
    # back the accept-capability token here would let anyone who merely knows a victim's
    # email address (self-asserted at register, not yet verified) claim their pending org
    # invitation without ever seeing the real invite link — this must stay informational
    # only ("an invite exists"), never a shortcut to accepting it.
    org_login: str
    expires_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut
    pending_invitations: list[PendingInvitationSummary] = []


class MeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    name: str | None
    is_workspace_admin: bool
    email_verified: bool
    created_at: datetime


class PatchMeRequest(BaseModel):
    name: str | None = None


class SetupRequiredResponse(BaseModel):
    setup_required: bool


class VerifyEmailRequest(BaseModel):
    token: str


class VerifyEmailResponse(BaseModel):
    ok: bool


class ResendVerificationResponse(BaseModel):
    ok: bool
    already_verified: bool = False


def _pending_invitations_for(db: Session, email: str) -> list[PendingInvitationSummary]:
    """Invitations sent to this email that are still pending and unexpired — surfaced
    at register/login so a user doesn't need the original invite link to discover them."""
    invitations = invitation_repo.list_pending_for_email(db, email)
    summaries = []
    for inv in invitations:
        org = db.query(Org).filter(Org.id == inv.org_id).first()
        if org is None:
            continue
        summaries.append(PendingInvitationSummary(org_login=org.github_login, expires_at=inv.expires_at))
    return summaries


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/setup-required", response_model=SetupRequiredResponse)
def setup_required(db: Session = Depends(get_db)):
    """No auth required — used by the UI before any redirect decision."""
    count = db.query(User).count()
    return {"setup_required": count == 0}


@router.post("/setup", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def setup(body: SetupRequest, db: Session = Depends(get_db)):
    """First-run only. Returns 409 if any user already exists."""
    # Two concurrent /auth/setup calls with *different* emails can both pass the
    # count()==0 check before either commits -- unlike /auth/register's email race, there's
    # no unique-constraint collision to catch this (the two rows don't share a column
    # value), so it would otherwise silently create two workspace admins. The advisory
    # lock serializes the whole check-then-insert critical section; it's transaction-scoped,
    # so it's released whenever this transaction ends -- an explicit commit/rollback below,
    # or (on the early-409 path, which does neither) get_db()'s finally: db.close(), which
    # implicitly rolls back and returns the connection to the pool at the end of the request.
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _SETUP_LOCK_KEY})
    if db.query(User).count() > 0:
        raise HTTPException(status_code=409, detail="Setup already complete")
    if len(body.password) < _MIN_PASSWORD_LEN:
        raise HTTPException(
            status_code=422,
            detail=f"Password must be at least {_MIN_PASSWORD_LEN} characters",
        )
    if len(body.password.encode()) > _MAX_PASSWORD_LEN:
        raise HTTPException(
            status_code=422,
            detail=f"Password must be at most {_MAX_PASSWORD_LEN} bytes",
        )
    user = User(
        email=body.email,
        name=body.name,
        password_hash=_hash_password(body.password),
        is_workspace_admin=True,
        # The deploying operator's own account -- implicitly trusted, same reasoning as
        # is_workspace_admin=True here (no one else could have run first-boot setup).
        email_verified=True,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Setup already complete") from None
    db.refresh(user)
    token = create_access_token(user.id, user.email, user.is_workspace_admin, user.name, user.token_version)
    return {"access_token": token, "user": UserOut(id=user.id, email=user.email, name=user.name, is_workspace_admin=user.is_workspace_admin)}


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED, dependencies=[Depends(rate_limit())])
def register(body: RegisterRequest, db: Session = Depends(get_db)):
    """Creates a non-admin account. 409 if setup hasn't run yet; 403 if registration is disabled."""
    if db.query(User).count() == 0:
        raise HTTPException(status_code=409, detail="Setup must be completed before registration")
    if get_config("registration_enabled", "true") != "true":
        raise HTTPException(status_code=403, detail="Registration is disabled")
    if len(body.password) < _MIN_PASSWORD_LEN:
        raise HTTPException(
            status_code=422,
            detail=f"Password must be at least {_MIN_PASSWORD_LEN} characters",
        )
    if len(body.password.encode()) > _MAX_PASSWORD_LEN:
        raise HTTPException(
            status_code=422,
            detail=f"Password must be at most {_MAX_PASSWORD_LEN} bytes",
        )
    if db.query(User).filter(User.email == body.email).first():
        raise HTTPException(status_code=409, detail="An account with this email already exists")
    user = User(
        email=body.email,
        name=body.name,
        password_hash=_hash_password(body.password),
        is_workspace_admin=False,
        email_verified=False,
    )
    db.add(user)
    try:
        # flush (not commit) so user.id is assigned in time for the verification token
        # below -- this is also where a race with a concurrent /auth/register for the same
        # email actually surfaces: both passed the existence check above before either
        # flushed, and users.email is unique, so the second flush raises here instead of
        # racing to a raw 500 at commit time.
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="An account with this email already exists") from None
    _send_verification_email_best_effort(user)
    db.commit()
    db.refresh(user)
    token = create_access_token(user.id, user.email, user.is_workspace_admin, user.name, user.token_version)
    return {
        "access_token": token,
        "user": UserOut(id=user.id, email=user.email, name=user.name, is_workspace_admin=user.is_workspace_admin),
        # Never populated here: the email isn't verified yet at this point (verification is
        # async, via the emailed link), so a self-asserted email is still not proof of inbox
        # control -- looking this up would let an attacker learn whether/where a victim's
        # email has a pending invite just by registering with it.
        "pending_invitations": [],
    }


@router.post("/verify-email", response_model=VerifyEmailResponse, dependencies=[Depends(rate_limit())])
def verify_email(body: VerifyEmailRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email_verify_token == body.token).first()
    if user is None:
        raise HTTPException(status_code=400, detail="Invalid or expired verification link")
    if (
        user.email_verify_token_expires_at is None
        or user.email_verify_token_expires_at < datetime.now(timezone.utc)
    ):
        raise HTTPException(status_code=400, detail="Invalid or expired verification link")
    user.email_verified = True
    user.email_verify_token = None
    user.email_verify_token_expires_at = None
    db.commit()
    return {"ok": True}


@router.post("/resend-verification", response_model=ResendVerificationResponse, dependencies=[Depends(rate_limit())])
def resend_verification(
    current_user: UserOut = Depends(require_auth),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.id == current_user.id).first()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user.email_verified:
        return {"ok": True, "already_verified": True}
    check_account_rate_limit(f"resend-verification:{user.email.lower()}")
    _send_verification_email_best_effort(user)
    db.commit()
    return {"ok": True}


@router.post("/logout")
def logout(response: Response):
    """Clear the httpOnly session cookie. Bearer-token clients also drop their local token."""
    clear_session_cookie(response)
    return {"ok": True}


@router.post("/login", response_model=TokenResponse, dependencies=[Depends(rate_limit())])
def login(body: LoginRequest, db: Session = Depends(get_db)):
    """Returns a JWT on valid credentials. Always returns 401 on failure (no field leaking)."""
    # Per-account limit, in addition to the per-IP one above -- an attacker spread across
    # many source IPs targeting one victim's password wouldn't otherwise trip anything.
    check_account_rate_limit(f"login:{body.email.lower()}")
    user = db.query(User).filter(User.email == body.email).first()
    # Call _verify_password unconditionally (not "not user or not _verify_password(...)")
    # -- that would short-circuit on a nonexistent user and skip bcrypt entirely, which is
    # exactly the timing side-channel _verify_password's dummy-hash path exists to close.
    password_ok = _verify_password(body.password, user.password_hash if user else None)
    if not user or not password_ok:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_access_token(user.id, user.email, user.is_workspace_admin, user.name, user.token_version)
    return {
        "access_token": token,
        "user": UserOut(id=user.id, email=user.email, name=user.name, is_workspace_admin=user.is_workspace_admin),
        "pending_invitations": _pending_invitations_for(db, user.email),
    }


@router.get("/me", response_model=MeResponse)
def get_me(current_user: UserOut = Depends(require_auth), db: Session = Depends(get_db)):
    """Returns full profile of the authenticated user."""
    user = db.query(User).filter(User.id == current_user.id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.patch("/me", response_model=MeResponse)
def patch_me(
    body: PatchMeRequest,
    current_user: UserOut = Depends(require_auth),
    db: Session = Depends(get_db),
):
    """Update the authenticated user's display name."""
    user = db.query(User).filter(User.id == current_user.id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if body.name is not None:
        user.name = body.name
    db.commit()
    db.refresh(user)
    return user


@router.post("/me/revoke-sessions")
def revoke_sessions(
    current_user: UserOut = Depends(require_auth),
    db: Session = Depends(get_db),
):
    """Invalidate every previously issued JWT for the caller (log out everywhere)."""
    user = db.query(User).filter(User.id == current_user.id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.token_version += 1
    db.commit()
    return {"ok": True}
