"""Shared advisory-lock helper for the periodic sweep loops (gap_heal_sweep.py,
membership_reconcile_sweep.py). Both do a plain check-then-enqueue for whether a job is
already active for a tenant -- safe within a single sweep pass (a plain sequential loop), but
not across two concurrent passes (e.g. two API replicas each running their own copy of the
asyncio background loop): both can pass the check before either enqueues, producing a
duplicate job for the same tenant.

pg_try_advisory_xact_lock serializes the check-then-enqueue around a (job_type, tenant_id)
key without a schema migration. It's transaction-scoped: acquired here, released
automatically at the caller's next commit or rollback on this session -- lines up with
job_repo.enqueue's own immediate commit, so the lock is held for exactly the
check-then-enqueue window and no longer. Same primitive as auth.py's setup lock and
installations.py's per-org-login lock (see their docstrings); non-blocking here (the `_try`
variant) because a sweep tick that loses the race should just skip this tenant and retry next
tick, not stall waiting for another replica to finish.
"""

from sqlalchemy import text
from sqlalchemy.orm import Session


def try_acquire_sweep_slot(db: Session, job_type: str, tenant_id: int) -> bool:
    """True if this session now holds the (job_type, tenant_id) advisory lock for the
    current transaction. False means another connection holds it right now -- skip this
    tenant this tick."""
    return bool(
        db.execute(
            text("SELECT pg_try_advisory_xact_lock(hashtext(:job_type), :tenant_id)"),
            {"job_type": job_type, "tenant_id": tenant_id},
        ).scalar()
    )
