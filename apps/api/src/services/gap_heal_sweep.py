"""S5 PR 2: periodically re-sync tenants whose activity_sync_cursors row has gone stale.

Reuses PR #342's install-time backfill job type (github.backfill_repo_events) unchanged --
gap-healing is that same job, triggered on a schedule instead of only at install time. Only
re-syncs tenants that already have a cursor row (i.e. already synced at least once, via install
time or a previous sweep run): a tenant whose install-time backfill never completed (best-effort,
per PR #342) stays unsynced until a real retry mechanism for THAT exists -- deliberately out of
scope here, which is about keeping already-synced tenants fresh, not guaranteeing eventual sync
for a failed install.

Called from an asyncio background loop started in main.py's lifespan (see gap_heal_loop.py),
mirroring apps/worker's own poll-loop shape rather than pulling in a new scheduler dependency.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.core.app_config import get_config
from src.core.db import set_session_tenant
from src.services import backfill_service
from src.services.sweep_lock import try_acquire_sweep_slot
from src.services.token_resolution import NoGitHubTokenAvailable, resolve_org_token, resolve_personal_token

logger = logging.getLogger(__name__)

_JOB_TYPE = "github.backfill_repo_events"


def _read_stale_hours() -> int:
    raw = get_config("gap_heal_stale_hours", "6")
    try:
        return max(1, min(168, int(raw)))
    except ValueError:
        logger.warning("gap_heal_stale_hours %r is not an integer; using 6", raw)
        return 6


def _has_active_backfill_job(db: Session, tenant_id: int) -> bool:
    """True if a github.backfill_repo_events job for this tenant is already queued or
    processing. The cursor only advances once a job reaches a terminal state (worker.py's
    _handle_backfill_repo_events upserts it right before _mark_done) -- without this check,
    a job that's slow to finish (worker downtime, a retried run) would still look stale on
    every subsequent sweep tick and get re-enqueued again and again, piling up duplicate
    GitHub API work for the same tenant. jobs has no tenant_id column (it's a generic
    queue), so this matches against the JSON payload the way it's actually stored --
    job_repo.enqueue always writes tenant_id as a JSON number under this exact job_type."""
    row = db.execute(
        text(
            "SELECT 1 FROM jobs WHERE job_type = :job_type "
            "AND status IN ('queued', 'processing') "
            "AND (payload::jsonb ->> 'tenant_id')::int = :tenant_id LIMIT 1"
        ),
        {"job_type": _JOB_TYPE, "tenant_id": tenant_id},
    ).fetchone()
    return row is not None


def run_gap_heal_sweep(db: Session) -> None:
    stale_hours = _read_stale_hours()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=stale_hours)

    # tenants has no RLS of its own (it's the tenant identity table, not tenant-scoped data --
    # see migration 0030's table list), so this read needs no session tenant context.
    tenants = db.execute(text("SELECT id, kind, org_id, personal_user_id FROM tenants")).fetchall()

    for tenant_id, kind, org_id, personal_user_id in tenants:
        # activity_sync_cursors is RLS-enabled (migration 0038) -- this must be set before
        # reading this tenant's own cursor row, one tenant at a time, same as every other
        # system code path that resolves a tenant_id without an acting user (see
        # set_session_tenant's own docstring).
        set_session_tenant(db, tenant_id)
        cursor_row = db.execute(
            text(
                "SELECT account_login, account_type, last_synced_at FROM activity_sync_cursors "
                "WHERE tenant_id = :tenant_id"
            ),
            {"tenant_id": tenant_id},
        ).fetchone()
        if cursor_row is None:
            continue
        account_login, account_type, last_synced_at = cursor_row
        if last_synced_at is not None and last_synced_at >= cutoff:
            continue
        # Acquired before the active-job check (not after) so the whole check-then-enqueue
        # window for this tenant is mutually exclusive across concurrent sweep passes (e.g.
        # two API replicas) -- a losing process skips this tenant entirely this tick rather
        # than re-deriving a now-stale "no active job" answer. See sweep_lock.py.
        if not try_acquire_sweep_slot(db, _JOB_TYPE, tenant_id):
            continue
        if _has_active_backfill_job(db, tenant_id):
            db.commit()  # nothing to persist; releases the advisory lock promptly
            continue

        try:
            if kind == "org":
                token = resolve_org_token(db, org_id=org_id, account_login=account_login, client_token=None)
            else:
                token = resolve_personal_token(
                    db, owner_user_id=personal_user_id, account_login=account_login, client_token=None
                )
            backfill_service.enqueue(
                db, tenant_id=tenant_id, account_login=account_login, account_type=account_type, token=token
            )
        except NoGitHubTokenAvailable as exc:
            db.commit()  # nothing to persist; releases the advisory lock promptly
            logger.warning("gap-heal sweep skipping %s (tenant %d): %s", account_login, tenant_id, exc)
        except Exception:
            # A DB-level error here (e.g. from job_repo.enqueue's own commit) would leave this
            # shared Session's transaction aborted -- roll back so the sweep can keep going for
            # the remaining tenants instead of every subsequent iteration failing too (also
            # releases the advisory lock). Same posture as installations.py's
            # _enqueue_backfill_best_effort.
            db.rollback()
            logger.exception("gap-heal sweep failed to enqueue a backfill for %s (tenant %d)", account_login, tenant_id)
