import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.core.db import Invitation
from src.repositories import tenant_repo

INVITATION_LIFETIME = timedelta(days=7)


class DuplicatePendingInvitation(Exception):
    """Raised by create() when an active pending invitation already exists for this
    (org, email). Mirrors tenant_repo._persist_new's IntegrityError handling:
    the partial unique index uq_invitations_org_email_pending (migration 0042) is what
    makes the losing side of a concurrent double-insert fail instead of both winning."""

    def __init__(self, email: str):
        self.email = email
        super().__init__(f"A pending invitation already exists for {email} in this organization")


def _expire_lapsed(db: Session, org_id: int, email: str) -> None:
    """Collapse any already-lapsed 'pending' rows for this (org, email) to 'expired' so
    a legitimate re-invite after the previous one expired isn't blocked by the partial
    unique index (which keys on status='pending', not on expiry). The app already treats
    an expired 'pending' row as expired everywhere else (see the router's _effective_status)."""
    db.query(Invitation).filter(
        Invitation.org_id == org_id,
        func.lower(Invitation.email) == email.lower(),
        Invitation.status == "pending",
        Invitation.expires_at <= datetime.now(timezone.utc),
    ).update({Invitation.status: "expired"}, synchronize_session=False)


def create(db: Session, org_id: int, email: str, invited_by_user_id: int) -> Invitation:
    _expire_lapsed(db, org_id, email)
    tenant = tenant_repo.get_or_create_org_tenant(db, org_id)
    invitation = Invitation(
        org_id=org_id,
        email=email,
        token=secrets.token_urlsafe(32),
        status="pending",
        invited_by_user_id=invited_by_user_id,
        expires_at=datetime.now(timezone.utc) + INVITATION_LIFETIME,
        tenant_id=tenant.id,
    )
    db.add(invitation)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        # Only convert to a 409 if the failure was actually a duplicate pending invite
        # (the concurrent-insert race). Any other IntegrityError -- an FK violation, a
        # token collision, a future constraint -- must surface as itself, not as a
        # misleading "already exists". Mirrors tenant_repo._persist_new, which
        # re-queries the specific row and re-raises when it isn't there.
        if get_pending_for_org_and_email(db, org_id=org_id, email=email) is None:
            raise
        raise DuplicatePendingInvitation(email) from exc
    db.refresh(invitation)
    return invitation


def get_by_token(db: Session, token: str) -> Invitation | None:
    return db.query(Invitation).filter(Invitation.token == token).first()


def get_by_id_and_org(db: Session, invitation_id: int, org_id: int) -> Invitation | None:
    return (
        db.query(Invitation)
        .filter(Invitation.id == invitation_id, Invitation.org_id == org_id)
        .first()
    )


def list_for_org(db: Session, org_id: int) -> list[Invitation]:
    return db.query(Invitation).filter(Invitation.org_id == org_id).order_by(Invitation.created_at.desc()).all()


def get_pending_for_org_and_email(db: Session, org_id: int, email: str) -> Invitation | None:
    return (
        db.query(Invitation)
        .filter(
            Invitation.org_id == org_id,
            # Case-insensitive *exact* match -- ilike(email) would treat `_`/`%` in
            # the address (a `_` is common in local parts) as wildcards, matching
            # unrelated invites. Mirrors _expire_lapsed and the lower(email) unique index.
            func.lower(Invitation.email) == email.lower(),
            Invitation.status == "pending",
            Invitation.expires_at > datetime.now(timezone.utc),
        )
        .first()
    )


def list_pending_for_email(db: Session, email: str) -> list[Invitation]:
    return (
        db.query(Invitation)
        .filter(
            # Exact, case-insensitive -- see get_pending_for_org_and_email.
            func.lower(Invitation.email) == email.lower(),
            Invitation.status == "pending",
            Invitation.expires_at > datetime.now(timezone.utc),
        )
        .all()
    )
