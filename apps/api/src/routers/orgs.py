"""GET /me/orgs — the current user's org memberships, for the UI's org/personal context switcher."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from src.core.auth import UserOut, require_auth
from src.core.db import get_db
from src.repositories import tenant_repo
from src.schemas.org import MyOrgMembershipOut

router = APIRouter()


@router.get("/me/orgs", response_model=list[MyOrgMembershipOut])
def list_my_orgs(user: UserOut = Depends(require_auth), db: Session = Depends(get_db)):
    return [
        {"org_login": org.github_login, "role": membership.role}
        for org, membership in tenant_repo.list_org_memberships_for_user(db, user_id=user.id)
    ]
