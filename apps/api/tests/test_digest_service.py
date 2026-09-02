"""Tests for src.services.digest_service (issue #292)."""
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text

from src.repositories import org_repo, scan_results_repo
from src.services import digest_service

CHECKS = [
    {"id": "organization_members_mfa_required", "title": "Org requires 2FA", "severity": "high", "status": "fail"},
    {"id": "repository_secret_scanning_enabled", "title": "Secret scanning on", "severity": "medium", "status": "pass"},
    {"id": "repository_default_branch_protection_enabled", "title": "Branch protection", "severity": "high", "status": "fail"},
]


def _set_tenant(db, tenant_id):
    db.execute(text(f"SET app.tenant_id = {int(tenant_id)}"))


def test_build_digest_is_empty_without_any_scan(db):
    org = org_repo.get_or_create(db, github_login="digest-empty")
    _set_tenant(db, org.tenant_id)
    content = digest_service.build_digest(db, tenant_id=org.tenant_id, org_login="digest-empty", period_label="weekly")
    assert content.is_empty
    assert content.latest_score is None


def test_build_digest_reports_score_delta_and_failing_checks(db):
    org = org_repo.get_or_create(db, github_login="digest-org")
    scan_results_repo.insert(db, owner="digest-org", score=60, total_checks=3, failed_checks=2, checks=[], tenant_id=org.tenant_id)
    scan_results_repo.insert(db, owner="digest-org", score=75, total_checks=3, failed_checks=2, checks=CHECKS, tenant_id=org.tenant_id)
    _set_tenant(db, org.tenant_id)
    db.execute(
        text(
            "INSERT INTO repo_event_daily_counts (tenant_id, repo, event_type, day, count) "
            "VALUES (:t, 'digest-org/api', 'push', :day, 12)"
        ),
        {"t": org.tenant_id, "day": date.today()},
    )
    db.commit()
    _set_tenant(db, org.tenant_id)

    content = digest_service.build_digest(db, tenant_id=org.tenant_id, org_login="digest-org", period_label="weekly")
    assert content.latest_score == 75
    assert content.previous_score == 60
    assert content.score_delta == 15
    assert content.failing_checks == ["Org requires 2FA", "Branch protection"]
    assert content.push_events_7d == 12

    text_body = digest_service.render_text(content)
    assert "75/100" in text_body
    assert "up 15" in text_body
    assert "Org requires 2FA" in text_body
    assert "12 push events" in text_body
    assert "score 75" in digest_service.render_subject(content)


def test_errored_checks_are_listed_as_risk_items(db):
    # analytics counts "error" toward failed_checks/score, so the digest must too.
    org = org_repo.get_or_create(db, github_login="digest-errored")
    scan_results_repo.insert(
        db, owner="digest-errored", score=55, total_checks=2, failed_checks=2,
        checks=[
            {"id": "a", "title": "Errored check", "severity": "high", "status": "error"},
            {"id": "b", "title": "Failed check", "severity": "high", "status": "fail"},
        ],
        tenant_id=org.tenant_id,
    )
    _set_tenant(db, org.tenant_id)
    content = digest_service.build_digest(db, tenant_id=org.tenant_id, org_login="digest-errored", period_label="weekly")
    assert set(content.failing_checks) == {"Errored check", "Failed check"}


def test_build_digest_tolerates_malformed_checks_json(db):
    org = org_repo.get_or_create(db, github_login="digest-bad")
    scan_results_repo.insert(db, owner="digest-bad", score=50, total_checks=1, failed_checks=1, checks=[], tenant_id=org.tenant_id)
    db.execute(text("UPDATE scan_results SET checks_json = 'nope{' WHERE owner = 'digest-bad'"))
    db.commit()
    _set_tenant(db, org.tenant_id)
    content = digest_service.build_digest(db, tenant_id=org.tenant_id, org_login="digest-bad", period_label="monthly")
    assert content.latest_score == 50
    assert content.failing_checks == []


def test_old_push_events_are_outside_the_activity_window(db):
    org = org_repo.get_or_create(db, github_login="digest-stale-activity")
    scan_results_repo.insert(db, owner="digest-stale-activity", score=90, total_checks=1, failed_checks=0, checks=[], tenant_id=org.tenant_id)
    _set_tenant(db, org.tenant_id)
    db.execute(
        text(
            "INSERT INTO repo_event_daily_counts (tenant_id, repo, event_type, day, count) "
            "VALUES (:t, 'r/x', 'push', :day, 99)"
        ),
        {"t": org.tenant_id, "day": (datetime.now(timezone.utc) - timedelta(days=30)).date()},
    )
    db.commit()
    _set_tenant(db, org.tenant_id)
    content = digest_service.build_digest(db, tenant_id=org.tenant_id, org_login="digest-stale-activity", period_label="weekly")
    assert content.push_events_7d == 0
