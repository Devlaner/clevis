"""Tests for the compliance scan-history export endpoints (issue #293)."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from src.core.auth import UserOut, require_auth
from src.core.db import ScanResult, User, get_db
from src.repositories import org_membership_repo, org_repo, scan_results_repo
from src.routers.analytics import router

CHECKS = [
    {
        "id": "organization_members_mfa_required",
        "title": "Organization requires 2FA/MFA",
        "severity": "high",
        "remediation": "Enable 2FA.",
        "status": "fail",
        "value": False,
    },
    {
        "id": "repository_secret_scanning_enabled",
        "title": "Secret scanning enabled",
        "severity": "medium",
        "remediation": "Turn it on.",
        "status": "pass",
        "value": {"enabled": 3, "total": 3},
    },
]


def _make_user(db, email: str) -> UserOut:
    user = User(email=email, name=None, password_hash=None, is_workspace_admin=False)
    db.add(user)
    db.commit()
    db.refresh(user)
    return UserOut(id=user.id, email=user.email, name=user.name, is_workspace_admin=False)


@pytest.fixture()
def mock_user(db):
    return _make_user(db, "export@example.com")


@pytest.fixture()
def app(db, mock_user):
    a = FastAPI()
    a.dependency_overrides[require_auth] = lambda: mock_user
    a.dependency_overrides[get_db] = lambda: db
    a.include_router(router)
    return a


@pytest.fixture()
def http(app):
    return TestClient(app)


def _seed(db, owner: str, tenant_id: int, user_id: int | None = None, score: int = 70, checks=CHECKS):
    scan_results_repo.insert(
        db,
        owner=owner,
        score=score,
        total_checks=len(checks),
        failed_checks=1,
        checks=checks,
        tenant_id=tenant_id,
        scanned_by_user_id=user_id,
    )


def test_org_export_outsider_forbidden(http, db):
    org_repo.get_or_create(db, github_login="acme")
    assert http.get("/orgs/acme/analytics/export").status_code == 403


def test_org_export_member_gets_full_check_detail(http, db, mock_user):
    org = org_repo.get_or_create(db, github_login="acme")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=mock_user.id, role="member")
    _seed(db, "acme", org.tenant_id)

    resp = http.get("/orgs/acme/analytics/export")
    assert resp.status_code == 200
    body = resp.json()
    assert body["truncated"] is False
    assert body["row_count"] == 1
    assert [c["id"] for c in body["entries"][0]["checks"]] == [c["id"] for c in CHECKS]
    assert body["entries"][0]["checks"][0]["status"] == "fail"


def test_personal_export_forbidden_without_relationship(http, db):
    org = org_repo.get_or_create(db, github_login="acme")
    _seed(db, "acme", org.tenant_id)
    assert http.get("/me/analytics/export?owner=acme").status_code == 403


def test_personal_export_own_scope_excludes_other_users_scans(http, db, mock_user):
    # mock_user's only claim to "acme" is a personal scan they ran themselves -> they
    # must not see a scan another user ran against the same login.
    org = org_repo.get_or_create(db, github_login="acme")
    other = _make_user(db, "other@example.com")
    _seed(db, "acme", org.tenant_id, user_id=mock_user.id, score=11)
    _seed(db, "acme", org.tenant_id, user_id=other.id, score=99)

    resp = http.get("/me/analytics/export?owner=acme")
    assert resp.status_code == 200
    body = resp.json()
    assert [e["score"] for e in body["entries"]] == [11]


def test_personal_export_org_member_sees_all_scans(http, db, mock_user):
    org = org_repo.get_or_create(db, github_login="acme")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=mock_user.id, role="member")
    other = _make_user(db, "other2@example.com")
    _seed(db, "acme", org.tenant_id, user_id=mock_user.id, score=11)
    _seed(db, "acme", org.tenant_id, user_id=other.id, score=99)

    resp = http.get("/me/analytics/export?owner=acme")
    assert sorted(e["score"] for e in resp.json()["entries"]) == [11, 99]


def test_export_until_is_an_inclusive_day(http, db, mock_user):
    org = org_repo.get_or_create(db, github_login="acme")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=mock_user.id, role="member")
    _seed(db, "acme", org.tenant_id, score=42)
    # Pin the row to 14:00 UTC on a known day.
    day = datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)
    db.execute(text("UPDATE scan_results SET created_at = :ts WHERE owner = 'acme'"), {"ts": day})
    db.commit()

    resp = http.get("/orgs/acme/analytics/export?since=2026-06-15&until=2026-06-15")
    assert resp.status_code == 200
    assert [e["score"] for e in resp.json()["entries"]] == [42]


def test_export_since_until_window_filters_rows(http, db, mock_user):
    org = org_repo.get_or_create(db, github_login="acme")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=mock_user.id, role="member")
    _seed(db, "acme", org.tenant_id, score=10)
    _seed(db, "acme", org.tenant_id, score=20)
    old = datetime.now(timezone.utc) - timedelta(days=7)
    db.execute(
        text("UPDATE scan_results SET created_at = :ts WHERE score = 10 AND owner = 'acme'"),
        {"ts": old},
    )
    db.commit()

    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    resp = http.get(f"/orgs/acme/analytics/export?since={yesterday}")
    assert resp.status_code == 200
    assert sorted(e["score"] for e in resp.json()["entries"]) == [20]


def test_export_rejects_inverted_window(http, db, mock_user):
    org = org_repo.get_or_create(db, github_login="acme")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=mock_user.id, role="member")
    resp = http.get("/orgs/acme/analytics/export?since=2026-02-01&until=2026-01-01")
    assert resp.status_code == 422


def test_export_truncated_flag_is_set_when_the_cap_is_hit(http, db, mock_user):
    org = org_repo.get_or_create(db, github_login="acme")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=mock_user.id, role="member")
    for i in range(4):
        _seed(db, "acme", org.tenant_id, score=i)

    resp = http.get("/orgs/acme/analytics/export?limit=2")
    assert resp.status_code == 200
    body = resp.json()
    assert body["truncated"] is True
    assert body["row_count"] == 2
    assert len(body["entries"]) == 2


def test_export_tolerates_malformed_or_legacy_checks_json(http, db, mock_user):
    org = org_repo.get_or_create(db, github_login="acme")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=mock_user.id, role="member")
    _seed(db, "acme", org.tenant_id, score=5)
    # Corrupt one row's checks_json and give another an older/looser check shape.
    db.execute(text("UPDATE scan_results SET checks_json = 'not json{' WHERE owner = 'acme'"))
    db.commit()
    _seed(db, "acme", org.tenant_id, score=6, checks=[{"id": "legacy", "status": "pass"}])

    resp = http.get("/orgs/acme/analytics/export")
    assert resp.status_code == 200
    by_score = {e["score"]: e for e in resp.json()["entries"]}
    assert by_score[5]["checks"] == []
    assert by_score[6]["checks"] == [{"id": "legacy", "status": "pass"}]


def test_export_repo_helper_parses_checks_json(db):
    org = org_repo.get_or_create(db, github_login="acme")
    _seed(db, "acme", org.tenant_id)
    rows = scan_results_repo.list_for_export(db, owner="acme")
    assert rows[0]["checks"][1]["id"] == "repository_secret_scanning_enabled"
