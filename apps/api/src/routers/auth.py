from fastapi import APIRouter
from pydantic import BaseModel

from src.core.storage import get_conn

router = APIRouter()


class SyncInstallationsInput(BaseModel):
    token: str
    auth_mode: str = "app"
    account_login: str
    account_type: str = "Organization"
    installation_id: int | None = None


@router.post("/github/app/installations/sync")
def sync_installation(payload: SyncInstallationsInput):
    token_ref = f"tok_{payload.account_login}"
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO github_installations(account_login, account_type, installation_id, auth_mode, token_ref) VALUES (?, ?, ?, ?, ?)",
            (payload.account_login, payload.account_type, payload.installation_id, payload.auth_mode, token_ref),
        )
    return {"synced": True, "token_ref": token_ref}
