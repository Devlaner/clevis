import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from src.core.db import Invitation
from src.repositories import tenant_repo

INVITATION_LIFETIME = timedelta(days=7)


def create(db: Session, org_id: int, email: str, invited_by_user_id: int) -> Invitation:
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
    db.commit()
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
            Invitation.email.ilike(email),
            Invitation.status == "pending",
            Invitation.expires_at > datetime.now(timezone.utc),
        )
        .first()
    )


def list_pending_for_email(db: Session, email: str) -> list[Invitation]:
    return (
        db.query(Invitation)
        .filter(
            Invitation.email.ilike(email),
            Invitation.status == "pending",
            Invitation.expires_at > datetime.now(timezone.utc),
        )
        .all()
    )
