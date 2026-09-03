"""Org invitation router.

  POST   /orgs/{org_login}/invitations             admin: create a pending invite, returns a
                                                     shareable link (no email is sent)
  GET    /orgs/{org_login}/invitations             admin: list pending/accepted/revoked invites
  POST   /orgs/{org_login}/invitations/{id}/revoke admin: revoke a pending invite
  GET    /invitations/{token}                      unauthenticated: preview an invite by token
  POST   /invitations/{token}/accept               any authenticated user whose account email
                                                     case-insensitively matches the invite AND
                                                     is verified (see issue #217)
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from src.core.auth import UserOut, require_auth
from src.core.config import settings
from src.core.db import Org, User, get_db
from src.core.rbac import OrgContext, require_org_role
from src.repositories import invitation_repo, org_membership_repo
from src.schemas.invitation import (
    InvitationAcceptResponse,
    InvitationCreate,
    InvitationCreateResponse,
    InvitationOut,
    InvitationPreview,
)

router = APIRouter()


def _ui_base() -> str:
    return settings.cors_origins[0] if settings.cors_origins else ""


def _is_expired(invitation) -> bool:
    return invitation.status == "pending" and invitation.expires_at <= datetime.now(timezone.utc)


def _effective_status(invitation) -> str:
    return "expired" if _is_expired(invitation) else invitation.status


def _expire(db: Session, invitation) -> None:
    invitation.status = "expired"
    db.commit()
    db.refresh(invitation)


@router.post("/orgs/{org_login}/invitations", response_model=InvitationCreateResponse)
def create_invitation(
    body: InvitationCreate,
    ctx: OrgContext = Depends(require_org_role(min_role="admin")),
    user: UserOut = Depends(require_auth),
    db: Session = Depends(get_db),
):
    existing = invitation_repo.get_pending_for_org_and_email(db, org_id=ctx.org.id, email=body.email)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A pending invitation already exists for {body.email} in this organization",
        )
    try:
        invitation = invitation_repo.create(db, org_id=ctx.org.id, email=body.email, invited_by_user_id=user.id)
    except invitation_repo.DuplicatePendingInvitation as exc:
        # A concurrent request won the race between the pre-check above and its own
        # insert; the DB constraint rejected this one.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return {
        "invitation": invitation,
        "invite_link": f"{_ui_base()}/invite/{invitation.token}",
    }


@router.get("/orgs/{org_login}/invitations", response_model=list[InvitationOut])
def list_invitations(
    ctx: OrgContext = Depends(require_org_role(min_role="admin")),
    db: Session = Depends(get_db),
):
    invitations = invitation_repo.list_for_org(db, org_id=ctx.org.id)
    return [
        InvitationOut(
            id=inv.id,
            org_id=inv.org_id,
            email=inv.email,
            status=_effective_status(inv),
            created_at=inv.created_at,
            accepted_at=inv.accepted_at,
            expires_at=inv.expires_at,
        )
        for inv in invitations
    ]


@router.post("/orgs/{org_login}/invitations/{invitation_id}/revoke", response_model=InvitationOut)
def revoke_invitation(
    invitation_id: int,
    ctx: OrgContext = Depends(require_org_role(min_role="admin")),
    db: Session = Depends(get_db),
):
    invitation = invitation_repo.get_by_id_and_org(db, invitation_id=invitation_id, org_id=ctx.org.id)
    if invitation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invitation not found")
    if invitation.status != "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Invitation is not pending")
    invitation.status = "revoked"
    db.commit()
    db.refresh(invitation)
    return invitation


@router.get("/invitations/{token}", response_model=InvitationPreview)
def preview_invitation(token: str, db: Session = Depends(get_db)):
    invitation = invitation_repo.get_by_token(db, token)
    if invitation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invitation not found")
    if _is_expired(invitation):
        _expire(db, invitation)
    org = db.query(Org).filter(Org.id == invitation.org_id).first()
    return {"org_login": org.github_login, "status": invitation.status}


@router.post("/invitations/{token}/accept", response_model=InvitationAcceptResponse)
def accept_invitation(
    token: str,
    user: UserOut = Depends(require_auth),
    db: Session = Depends(get_db),
):
    invitation = invitation_repo.get_by_token(db, token)
    if invitation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invitation not found")
    if _is_expired(invitation):
        _expire(db, invitation)
    if invitation.status == "expired":
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="Invitation has expired")
    if invitation.status != "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Invitation is no longer pending")
    if invitation.email.lower() != user.email.lower():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invitation email does not match account")
    # Email match alone isn't proof of ownership for a self-registered account -- see
    # issue #217. GitHub-linked accounts and the first-run setup admin are verified
    # immediately (src.routers.auth); self-registered accounts must click the emailed
    # verification link first, closing the same class of hijack the GitHub OAuth path
    # already defends against via EmailAlreadyRegistered (src.routers.github_auth).
    db_user = db.query(User).filter(User.id == user.id).first()
    if db_user is None or not db_user.email_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Verify your email before accepting this invitation",
        )

    membership = org_membership_repo.get_or_create(db, org_id=invitation.org_id, user_id=user.id, role="member")
    invitation.status = "accepted"
    invitation.accepted_at = datetime.now(timezone.utc)
    db.commit()

    org = db.query(Org).filter(Org.id == invitation.org_id).first()
    return {"org_login": org.github_login, "role": membership.role}
