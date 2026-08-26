"""Collaborators PR 2 of 3: enqueue a one-shot org membership reconciliation.

Mirrors backfill_service.enqueue exactly (Fernet-encrypt the token for the worker payload,
insert a `jobs` row for the worker's existing poll loop to pick up) -- see that module's
docstring. apps/worker/src/membership_reconcile.py + worker.py's
_handle_reconcile_org_membership do the actual GitHub calls and DB writes.
"""

from sqlalchemy.orm import Session

from src.core._crypto import encrypt_job_token
from src.core.config import settings
from src.repositories import job_repo


def enqueue(db: Session, *, tenant_id: int, org_login: str, token: str) -> int:
    encrypted_token = encrypt_job_token(token, settings.job_secret_key.get_secret_value())
    return job_repo.enqueue(
        db,
        "github.reconcile_org_membership",
        {
            "tenant_id": tenant_id,
            "org_login": org_login,
            "token": encrypted_token,
        },
    )
