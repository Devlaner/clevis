"""S5 PR 1: enqueue a one-shot install-time activity backfill.

Mirrors cache_service.clear()'s exact job-enqueue pattern: Fernet-encrypt the token for
the worker payload (apps/worker decrypts via its own _crypto.py thin wrapper, matching
issue #190/#191's established convention), then insert a `jobs` row for the worker's
existing SELECT ... FOR UPDATE SKIP LOCKED poll loop to pick up. See
apps/worker/src/backfill.py and worker.py's _handle_backfill_repo_events for the
worker-side handler.
"""

from sqlalchemy.orm import Session

from src.core._crypto import encrypt_job_token
from src.core.config import settings
from src.repositories import job_repo


def enqueue(db: Session, *, tenant_id: int, account_login: str, account_type: str, token: str) -> int:
    encrypted_token = encrypt_job_token(token, settings.job_secret_key.get_secret_value())
    return job_repo.enqueue(
        db,
        "github.backfill_repo_events",
        {
            "tenant_id": tenant_id,
            "account_login": account_login,
            "account_type": account_type,
            "token": encrypted_token,
        },
    )
