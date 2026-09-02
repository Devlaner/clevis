"""Tests for the compliance scan-history export endpoints (issue #293)."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from src.core.auth import UserOut, require_auth
from src.core.db import User, get_db
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


def _seed(db, owner: str, tenant_id: int, user_id: int | None = None, score: int = 70):
    scan_results_repo.insert(
        db,
        owner=owner,
        score=score,
        total_checks=len(CHECKS),
        failed_checks=1,
        checks=CHECKS,
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
    assert len(body) == 1
    assert [c["id"] for c in body[0]["checks"]] == [c["id"] for c in CHECKS]
    assert body[0]["checks"][0]["status"] == "fail"


def test_personal_export_forbidden_without_relationship(http, db):
    org = org_repo.get_or_create(db, github_login="acme")
    _seed(db, "acme", org.tenant_id)
    assert http.get("/me/analytics/export?owner=acme").status_code == 403


def test_personal_export_allowed_for_own_prior_scan(http, db, mock_user):
    org = org_repo.get_or_create(db, github_login="acme")
    _seed(db, "acme", org.tenant_id, user_id=mock_user.id)
    resp = http.get("/me/analytics/export?owner=acme")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_export_since_until_window_filters_rows(http, db, mock_user):
    org = org_repo.get_or_create(db, github_login="acme")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=mock_user.id, role="member")
    _seed(db, "acme", org.tenant_id, score=10)
    _seed(db, "acme", org.tenant_id, score=20)
    # Backdate the first row a week.
    old = datetime.now(timezone.utc) - timedelta(days=7)
    db.execute(
        text("UPDATE scan_results SET created_at = :ts WHERE score = 10 AND owner = 'acme'"),
        {"ts": old},
    )
    db.commit()

    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    resp = http.get(f"/orgs/acme/analytics/export?since={yesterday}")
    assert resp.status_code == 200
    scores = sorted(r["score"] for r in resp.json())
    assert scores == [20]


def test_export_rejects_inverted_window(http, db, mock_user):
    org = org_repo.get_or_create(db, github_login="acme")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=mock_user.id, role="member")
    resp = http.get("/orgs/acme/analytics/export?since=2026-02-01&until=2026-01-01")
    assert resp.status_code == 422


def test_export_repo_helper_parses_checks_json(db):
    org = org_repo.get_or_create(db, github_login="acme")
    _seed(db, "acme", org.tenant_id)
    rows = scan_results_repo.list_for_export(db, owner="acme")
    assert rows[0]["checks"][1]["id"] == "repository_secret_scanning_enabled"
