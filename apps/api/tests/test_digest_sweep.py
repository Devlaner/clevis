"""Tests for the leadership-digest sweep (issue #292)."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import text

from src.core.db import User
from src.repositories import org_repo, scan_results_repo, tenant_repo
from src.services import digest_sweep
from src.services.digest_sweep import run_digest_sweep


def _admin(db, tenant_id, email, verified=True):
    user = User(email=email, name=None, password_hash=None, email_verified=verified)
    db.add(user)
    db.commit()
    db.refresh(user)
    tenant_repo.get_or_create_membership(db, tenant_id=tenant_id, user_id=user.id, role="admin")
    db.commit()
    return user


def _seed_org_with_scan(db, login):
    org = org_repo.get_or_create(db, github_login=login)
    scan_results_repo.insert(
        db, owner=login, score=80, total_checks=2, failed_checks=1,
        checks=[{"id": "c", "title": "Broken thing", "severity": "high", "status": "fail"}],
        tenant_id=org.tenant_id,
    )
    return org


def _run(db, cadence="weekly", configured=True):
    with (
        patch("src.services.digest_sweep.get_config", return_value=cadence),
        patch("src.services.digest_sweep.email.is_configured", return_value=configured),
        patch("src.services.digest_sweep.email.send_email") as send,
    ):
        run_digest_sweep(db)
    return send


def _audit_rows(db, tenant_id):
    db.execute(text(f"SET app.tenant_id = {int(tenant_id)}"))
    return db.execute(
        text("SELECT COUNT(*) FROM audit_logs WHERE tenant_id = :t AND action = 'digest.sent'"),
        {"t": tenant_id},
    ).scalar()


def test_cadence_off_is_a_noop(db):
    org = _seed_org_with_scan(db, "digest-sweep-off")
    _admin(db, org.tenant_id, "off-admin@e.com")
    send = _run(db, cadence="off")
    send.assert_not_called()
    assert _audit_rows(db, org.tenant_id) == 0


def test_unconfigured_smtp_is_a_noop(db):
    org = _seed_org_with_scan(db, "digest-sweep-nosmtp")
    _admin(db, org.tenant_id, "nosmtp-admin@e.com")
    send = _run(db, configured=False)
    send.assert_not_called()
    assert _audit_rows(db, org.tenant_id) == 0


def test_due_org_emails_verified_admins_and_records_an_audit_log(db):
    org = _seed_org_with_scan(db, "digest-sweep-due")
    _admin(db, org.tenant_id, "due-admin@e.com")
    _admin(db, org.tenant_id, "unverified@e.com", verified=False)

    send = _run(db)

    assert send.call_count == 1
    assert send.call_args[0][0] == "due-admin@e.com"
    assert "digest-sweep-due" in send.call_args[0][1]  # subject
    assert _audit_rows(db, org.tenant_id) == 1


def test_recently_sent_org_is_skipped(db):
    org = _seed_org_with_scan(db, "digest-sweep-recent")
    _admin(db, org.tenant_id, "recent-admin@e.com")
    db.execute(text(f"SET app.tenant_id = {int(org.tenant_id)}"))
    db.execute(
        text(
            "INSERT INTO audit_logs (actor, action, target, payload, tenant_id, created_at) "
            "VALUES ('system:digest', 'digest.sent', 'digest-sweep-recent', '{}', :t, :ts)"
        ),
        {"t": org.tenant_id, "ts": datetime.now(timezone.utc) - timedelta(days=2)},
    )
    db.commit()

    send = _run(db)
    send.assert_not_called()


def test_org_with_no_verified_admin_is_skipped(db):
    org = _seed_org_with_scan(db, "digest-sweep-noadmin")
    _admin(db, org.tenant_id, "noadmin-unverified@e.com", verified=False)
    send = _run(db)
    send.assert_not_called()
    assert _audit_rows(db, org.tenant_id) == 0


def test_org_with_no_scan_history_is_skipped(db):
    org = org_repo.get_or_create(db, github_login="digest-sweep-noscan")
    _admin(db, org.tenant_id, "noscan-admin@e.com")
    send = _run(db)
    send.assert_not_called()
    assert _audit_rows(db, org.tenant_id) == 0
