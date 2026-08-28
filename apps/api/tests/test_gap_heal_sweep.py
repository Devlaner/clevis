"""Tests for the scheduled gap-heal sweep (issue #192/S5 PR 2)."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import text

from src.core.db import Job, User
from src.repositories import org_repo, tenant_repo
from src.services import backfill_service, gap_heal_sweep
from src.services.gap_heal_sweep import run_gap_heal_sweep
from src.services.token_resolution import NoGitHubTokenAvailable


def _seed_cursor(db, tenant_id, account_login, account_type, last_synced_at):
    db.execute(text(f"SET app.tenant_id = {int(tenant_id)}"))
    db.execute(
        text(
            "INSERT INTO activity_sync_cursors (tenant_id, account_login, account_type, last_synced_at) "
            "VALUES (:tenant_id, :account_login, :account_type, :last_synced_at)"
        ),
        {
            "tenant_id": tenant_id,
            "account_login": account_login,
            "account_type": account_type,
            "last_synced_at": last_synced_at,
        },
    )
    db.commit()


def _backfill_jobs(db):
    return db.query(Job).filter(Job.job_type == "github.backfill_repo_events").all()


def test_sweep_enqueues_a_backfill_for_a_stale_cursor(db):
    org = org_repo.get_or_create(db, github_login="acme-sweep-stale")
    stale = datetime.now(timezone.utc) - timedelta(hours=10)
    _seed_cursor(db, org.tenant_id, "acme-sweep-stale", "Organization", stale)

    with patch("src.services.gap_heal_sweep.resolve_org_token", return_value="tok"):
        run_gap_heal_sweep(db)

    jobs = _backfill_jobs(db)
    assert len(jobs) == 1
    payload = json.loads(jobs[0].payload)
    assert payload["account_login"] == "acme-sweep-stale"
    assert payload["tenant_id"] == org.tenant_id


def test_sweep_skips_a_fresh_cursor(db):
    org = org_repo.get_or_create(db, github_login="acme-sweep-fresh")
    fresh = datetime.now(timezone.utc) - timedelta(minutes=5)
    _seed_cursor(db, org.tenant_id, "acme-sweep-fresh", "Organization", fresh)

    with patch("src.services.gap_heal_sweep.resolve_org_token") as mock_resolve:
        run_gap_heal_sweep(db)

    mock_resolve.assert_not_called()
    assert _backfill_jobs(db) == []


def test_sweep_skips_a_tenant_with_no_cursor_row_yet(db):
    # No cursor row means the tenant's install-time backfill never completed -- retrying
    # that is deliberately out of scope for the gap-heal sweep (see gap_heal_sweep.py's
    # module docstring); it must not be treated as "infinitely stale".
    org_repo.get_or_create(db, github_login="acme-sweep-never-synced")

    with patch("src.services.gap_heal_sweep.resolve_org_token") as mock_resolve:
        run_gap_heal_sweep(db)

    mock_resolve.assert_not_called()
    assert _backfill_jobs(db) == []


def test_sweep_resolves_personal_tenants_via_resolve_personal_token(db):
    user = User(email="octocat-sweep@example.com", name=None, password_hash=None, is_workspace_admin=False)
    db.add(user)
    db.commit()
    db.refresh(user)
    tenant = tenant_repo.ensure_personal_tenant(db, user.id)
    stale = datetime.now(timezone.utc) - timedelta(hours=10)
    _seed_cursor(db, tenant.id, "octocat-sweep", "User", stale)

    with patch("src.services.gap_heal_sweep.resolve_personal_token", return_value="tok") as mock_resolve:
        run_gap_heal_sweep(db)

    mock_resolve.assert_called_once()
    _, kwargs = mock_resolve.call_args
    assert kwargs["owner_user_id"] == user.id
    assert kwargs["account_login"] == "octocat-sweep"
    jobs = _backfill_jobs(db)
    assert len(jobs) == 1
    assert json.loads(jobs[0].payload)["account_type"] == "User"


def test_sweep_survives_a_token_resolution_failure_for_one_tenant(db):
    org_fail = org_repo.get_or_create(db, github_login="acme-sweep-no-token")
    org_ok = org_repo.get_or_create(db, github_login="acme-sweep-ok")
    stale = datetime.now(timezone.utc) - timedelta(hours=10)
    _seed_cursor(db, org_fail.tenant_id, "acme-sweep-no-token", "Organization", stale)
    _seed_cursor(db, org_ok.tenant_id, "acme-sweep-ok", "Organization", stale)

    def _resolve(_db, *, org_id, account_login, client_token):
        if account_login == "acme-sweep-no-token":
            raise NoGitHubTokenAvailable("no installation")
        return "tok"

    with patch("src.services.gap_heal_sweep.resolve_org_token", side_effect=_resolve):
        run_gap_heal_sweep(db)

    jobs = _backfill_jobs(db)
    assert len(jobs) == 1
    assert json.loads(jobs[0].payload)["account_login"] == "acme-sweep-ok"


def test_sweep_does_not_double_enqueue_while_a_backfill_job_is_still_active(db):
    # The cursor only advances once the worker's handler reaches _mark_done -- a sweep that
    # fires again before a slow/retried job finishes must not pile up a second job for the
    # same tenant (issue found on this PR's own review).
    org = org_repo.get_or_create(db, github_login="acme-sweep-dedupe")
    stale = datetime.now(timezone.utc) - timedelta(hours=10)
    _seed_cursor(db, org.tenant_id, "acme-sweep-dedupe", "Organization", stale)

    with patch("src.services.gap_heal_sweep.resolve_org_token", return_value="tok"):
        run_gap_heal_sweep(db)
        run_gap_heal_sweep(db)

    jobs = _backfill_jobs(db)
    assert len(jobs) == 1


def test_sweep_skips_a_tenant_whose_lock_is_held_by_another_connection(db, _engine):
    # Regression test: the sweep's check-then-enqueue is only safe within a single sweep
    # pass. Two concurrent passes (e.g. two API replicas) could both see "no active job"
    # before either commits and both enqueue -- proven here with a second real connection
    # holding the (job_type, tenant_id) advisory lock for the whole call, which the running
    # sweep must lose and skip this tenant entirely (not just avoid enqueueing twice within
    # one connection, which the dedupe test above already covers).
    org = org_repo.get_or_create(db, github_login="acme-sweep-locked")
    stale = datetime.now(timezone.utc) - timedelta(hours=10)
    _seed_cursor(db, org.tenant_id, "acme-sweep-locked", "Organization", stale)

    with _engine.connect() as other_conn:
        other_conn.begin()
        other_conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:job_type), :tenant_id)"),
            {"job_type": "github.backfill_repo_events", "tenant_id": org.tenant_id},
        )
        with patch("src.services.gap_heal_sweep.resolve_org_token", return_value="tok"):
            run_gap_heal_sweep(db)

    assert _backfill_jobs(db) == []


def test_sweep_re_enqueues_once_the_active_job_reaches_a_terminal_state(db):
    org = org_repo.get_or_create(db, github_login="acme-sweep-retry")
    stale = datetime.now(timezone.utc) - timedelta(hours=10)
    _seed_cursor(db, org.tenant_id, "acme-sweep-retry", "Organization", stale)

    with patch("src.services.gap_heal_sweep.resolve_org_token", return_value="tok"):
        run_gap_heal_sweep(db)
        db.execute(text("UPDATE jobs SET status = 'failed' WHERE job_type = 'github.backfill_repo_events'"))
        db.commit()
        run_gap_heal_sweep(db)

    assert len(_backfill_jobs(db)) == 2


def test_sweep_survives_a_generic_error_during_enqueue_for_one_tenant(db):
    # A DB-level error here (or any other unexpected exception) must not abort the whole
    # sweep -- the shared Session's transaction is rolled back and the loop moves on to
    # the next tenant, same posture as installations.py's _enqueue_backfill_best_effort.
    org_boom = org_repo.get_or_create(db, github_login="acme-sweep-boom")
    org_fine = org_repo.get_or_create(db, github_login="acme-sweep-fine")
    stale = datetime.now(timezone.utc) - timedelta(hours=10)
    _seed_cursor(db, org_boom.tenant_id, "acme-sweep-boom", "Organization", stale)
    _seed_cursor(db, org_fine.tenant_id, "acme-sweep-fine", "Organization", stale)

    real_enqueue = backfill_service.enqueue

    def _enqueue(db, *, tenant_id, account_login, account_type, token):
        if account_login == "acme-sweep-boom":
            raise RuntimeError("simulated enqueue failure")
        return real_enqueue(db, tenant_id=tenant_id, account_login=account_login, account_type=account_type, token=token)

    with (
        patch("src.services.gap_heal_sweep.resolve_org_token", return_value="tok"),
        patch("src.services.gap_heal_sweep.backfill_service.enqueue", side_effect=_enqueue),
    ):
        run_gap_heal_sweep(db)

    jobs = _backfill_jobs(db)
    assert len(jobs) == 1
    assert json.loads(jobs[0].payload)["account_login"] == "acme-sweep-fine"


def test_read_stale_hours_default():
    with patch("src.services.gap_heal_sweep.get_config", return_value="6"):
        assert gap_heal_sweep._read_stale_hours() == 6


def test_read_stale_hours_clamps_high_values():
    with patch("src.services.gap_heal_sweep.get_config", return_value="9999"):
        assert gap_heal_sweep._read_stale_hours() == 168


def test_read_stale_hours_clamps_low_values():
    with patch("src.services.gap_heal_sweep.get_config", return_value="0"):
        assert gap_heal_sweep._read_stale_hours() == 1


def test_read_stale_hours_falls_back_on_non_integer():
    with patch("src.services.gap_heal_sweep.get_config", return_value="not-an-int"):
        assert gap_heal_sweep._read_stale_hours() == 6
