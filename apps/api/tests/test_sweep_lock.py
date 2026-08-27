"""Tests for the shared advisory-lock helper used by gap_heal_sweep.py and
membership_reconcile_sweep.py to serialize their check-then-enqueue race across concurrent
sweep passes (e.g. two API replicas)."""

from sqlalchemy import text

from src.services.sweep_lock import try_acquire_sweep_slot


def test_try_acquire_sweep_slot_serializes_concurrent_holders(db, _engine):
    # Mirrors test_auth.py's test_setup_advisory_lock_serializes_concurrent_holders: verifies
    # the lock primitive itself with two genuinely separate connections, since a single
    # session can re-acquire its own advisory lock (Postgres advisory locks are reentrant
    # per session) and would never demonstrate real contention.
    with _engine.connect() as other_conn:
        other_conn.begin()
        got_other = other_conn.execute(
            text("SELECT pg_try_advisory_xact_lock(hashtext(:job_type), :tenant_id)"),
            {"job_type": "github.backfill_repo_events", "tenant_id": 999999},
        ).scalar()
        assert got_other is True

        assert try_acquire_sweep_slot(db, "github.backfill_repo_events", 999999) is False

        other_conn.commit()  # releases other_conn's advisory lock

    assert try_acquire_sweep_slot(db, "github.backfill_repo_events", 999999) is True


def test_try_acquire_sweep_slot_is_scoped_per_job_type(db, _engine):
    # A lock held for one job_type must not block a different job_type against the same
    # tenant_id -- gap_heal_sweep and membership_reconcile_sweep run independently and must
    # not serialize against each other just because the same key int (from an equal hash
    # namespace) can appear -- hashtext(job_type) is what actually separates them.
    with _engine.connect() as other_conn:
        other_conn.begin()
        other_conn.execute(
            text("SELECT pg_try_advisory_xact_lock(hashtext(:job_type), :tenant_id)"),
            {"job_type": "github.backfill_repo_events", "tenant_id": 888888},
        )
        assert try_acquire_sweep_slot(db, "github.reconcile_org_membership", 888888) is True
