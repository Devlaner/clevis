"""Tests for the scheduled org-membership reconciliation sweep (Collaborators PR 2 of 3)."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import text

from src.core.db import Job
from src.repositories import org_repo
from src.services import membership_reconcile_service, membership_reconcile_sweep
from src.services.membership_reconcile_sweep import run_membership_reconcile_sweep
from src.services.token_resolution import NoGitHubTokenAvailable


def _seed_cursor(db, tenant_id, org_login, last_synced_at):
    db.execute(text(f"SET app.tenant_id = {int(tenant_id)}"))
    db.execute(
        text("INSERT INTO org_membership_sync_cursors (tenant_id, org_login, last_synced_at) VALUES (:t, :o, :l)"),
        {"t": tenant_id, "o": org_login, "l": last_synced_at},
    )
    db.commit()


def _reconcile_jobs(db):
    return db.query(Job).filter(Job.job_type == "github.reconcile_org_membership").all()


def test_sweep_enqueues_for_a_never_synced_org_tenant(db):
    # No cursor row at all -- unlike gap-heal (where a missing row means "install-time
    # backfill never completed, deliberately skipped"), a brand-new org-kind tenant should
    # be reconciled on the very first sweep tick that finds it.
    org = org_repo.get_or_create(db, github_login="acme-reconcile-never-synced")

    with patch("src.services.membership_reconcile_sweep.resolve_org_token", return_value="tok"):
        run_membership_reconcile_sweep(db)

    jobs = _reconcile_jobs(db)
    assert len(jobs) == 1
    payload = json.loads(jobs[0].payload)
    assert payload["org_login"] == "acme-reconcile-never-synced"
    assert payload["tenant_id"] == org.tenant_id


def test_sweep_enqueues_for_a_stale_cursor(db):
    org = org_repo.get_or_create(db, github_login="acme-reconcile-stale")
    stale = datetime.now(timezone.utc) - timedelta(hours=30)
    _seed_cursor(db, org.tenant_id, "acme-reconcile-stale", stale)

    with patch("src.services.membership_reconcile_sweep.resolve_org_token", return_value="tok"):
        run_membership_reconcile_sweep(db)

    assert len(_reconcile_jobs(db)) == 1


def test_sweep_skips_a_fresh_cursor(db):
    org = org_repo.get_or_create(db, github_login="acme-reconcile-fresh")
    fresh = datetime.now(timezone.utc) - timedelta(minutes=5)
    _seed_cursor(db, org.tenant_id, "acme-reconcile-fresh", fresh)

    with patch("src.services.membership_reconcile_sweep.resolve_org_token") as mock_resolve:
        run_membership_reconcile_sweep(db)

    mock_resolve.assert_not_called()
    assert _reconcile_jobs(db) == []


def test_sweep_survives_a_token_resolution_failure_for_one_tenant(db):
    org_fail = org_repo.get_or_create(db, github_login="acme-reconcile-no-token")
    org_ok = org_repo.get_or_create(db, github_login="acme-reconcile-ok")

    def _resolve(_db, *, org_id, account_login, client_token):
        if account_login == "acme-reconcile-no-token":
            raise NoGitHubTokenAvailable("no installation")
        return "tok"

    with patch("src.services.membership_reconcile_sweep.resolve_org_token", side_effect=_resolve):
        run_membership_reconcile_sweep(db)

    jobs = _reconcile_jobs(db)
    logins = {json.loads(j.payload)["org_login"] for j in jobs}
    assert logins == {"acme-reconcile-ok"}
    assert org_fail.tenant_id  # sanity: the failing org still exists, just wasn't enqueued


def test_sweep_does_not_double_enqueue_while_a_reconcile_job_is_still_active(db):
    org = org_repo.get_or_create(db, github_login="acme-reconcile-dedupe")

    with patch("src.services.membership_reconcile_sweep.resolve_org_token", return_value="tok"):
        run_membership_reconcile_sweep(db)
        run_membership_reconcile_sweep(db)

    assert len(_reconcile_jobs(db)) == 1


def test_sweep_skips_a_tenant_whose_lock_is_held_by_another_connection(db, _engine):
    # Regression test: the sweep's check-then-enqueue is only safe within a single sweep
    # pass. Two concurrent passes (e.g. two API replicas) could both see "no active job"
    # before either commits and both enqueue -- proven here with a second real connection
    # holding the (job_type, tenant_id) advisory lock for the whole call, which the running
    # sweep must lose and skip this tenant entirely (not just avoid enqueueing twice within
    # one connection, which the dedupe test above already covers).
    org = org_repo.get_or_create(db, github_login="acme-reconcile-locked")

    with _engine.connect() as other_conn:
        other_conn.begin()
        other_conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:job_type), :tenant_id)"),
            {"job_type": "github.reconcile_org_membership", "tenant_id": org.tenant_id},
        )
        with patch("src.services.membership_reconcile_sweep.resolve_org_token", return_value="tok"):
            run_membership_reconcile_sweep(db)

    assert _reconcile_jobs(db) == []


def test_sweep_re_enqueues_once_the_active_job_reaches_a_terminal_state(db):
    org_repo.get_or_create(db, github_login="acme-reconcile-retry")

    with patch("src.services.membership_reconcile_sweep.resolve_org_token", return_value="tok"):
        run_membership_reconcile_sweep(db)
        db.execute(text("UPDATE jobs SET status = 'failed' WHERE job_type = 'github.reconcile_org_membership'"))
        db.commit()
        run_membership_reconcile_sweep(db)

    assert len(_reconcile_jobs(db)) == 2


def test_sweep_survives_a_generic_error_during_enqueue_for_one_tenant(db):
    org_boom = org_repo.get_or_create(db, github_login="acme-reconcile-boom")
    org_fine = org_repo.get_or_create(db, github_login="acme-reconcile-fine")

    real_enqueue = membership_reconcile_service.enqueue

    def _enqueue(db, *, tenant_id, org_login, token):
        if org_login == "acme-reconcile-boom":
            raise RuntimeError("simulated enqueue failure")
        return real_enqueue(db, tenant_id=tenant_id, org_login=org_login, token=token)

    with (
        patch("src.services.membership_reconcile_sweep.resolve_org_token", return_value="tok"),
        patch("src.services.membership_reconcile_sweep.membership_reconcile_service.enqueue", side_effect=_enqueue),
    ):
        run_membership_reconcile_sweep(db)

    jobs = _reconcile_jobs(db)
    assert len(jobs) == 1
    assert json.loads(jobs[0].payload)["org_login"] == "acme-reconcile-fine"
    assert org_boom.tenant_id  # sanity: the failing org still exists


def test_read_stale_hours_default():
    with patch("src.services.membership_reconcile_sweep.get_config", return_value="24"):
        assert membership_reconcile_sweep._read_stale_hours() == 24


def test_read_stale_hours_clamps_high_values():
    with patch("src.services.membership_reconcile_sweep.get_config", return_value="9999"):
        assert membership_reconcile_sweep._read_stale_hours() == 168


def test_read_stale_hours_clamps_low_values():
    with patch("src.services.membership_reconcile_sweep.get_config", return_value="0"):
        assert membership_reconcile_sweep._read_stale_hours() == 1


def test_read_stale_hours_falls_back_on_non_integer():
    with patch("src.services.membership_reconcile_sweep.get_config", return_value="not-an-int"):
        assert membership_reconcile_sweep._read_stale_hours() == 24
