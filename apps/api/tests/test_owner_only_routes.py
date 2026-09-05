"""Workspace-admin-only enforcement for jobs/audit/tokens (instance-wide data with no
per-org column to scope by), plus auth enforcement for the actions-cache routes.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from src.core.auth import UserOut, require_auth
from src.core.db import User, get_db
from src.routers.actions_cache import router as cache_router
from src.routers.audit import router as audit_router
from src.routers.jobs import router as jobs_router
from src.routers.tokens import router as tokens_router

_OWNER = UserOut(id=1, email="owner@example.com", name=None, is_workspace_admin=True)
_NON_OWNER = UserOut(id=2, email="member@example.com", name=None, is_workspace_admin=False)


def _client(router, db, user, prefix=""):
    app = FastAPI()
    app.include_router(router, prefix=prefix)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_auth] = lambda: user
    # Issue #330: overriding require_auth for tests bypasses its real body, including the
    # SET app.user_id side effect (src.core.db.set_session_user) RLS's self-access clauses
    # (migration 0031) depend on -- set it here directly so tests exercise the same session
    # context a real authenticated request would have.
    db.execute(text(f"SET app.user_id = {user.id}"))
    return TestClient(app)


# ── jobs ──────────────────────────────────────────────────────────────────────

def test_jobs_owner_ok(db):
    resp = _client(jobs_router, db, _OWNER, prefix="/jobs").get("/jobs")
    assert resp.status_code == 200
    assert resp.json() == []


def test_jobs_non_owner_forbidden(db):
    resp = _client(jobs_router, db, _NON_OWNER, prefix="/jobs").get("/jobs")
    assert resp.status_code == 403


def test_single_job_readable_by_any_authenticated_user(db):
    # GET /jobs/{id} is only require_auth (the cache-clear panel polls it) -- a non-owner
    # must be able to read a job's status, unlike the workspace-admin-only list.
    db.execute(
        text(
            "INSERT INTO jobs (id, job_type, payload, status, result) "
            "VALUES (9991, 'github.clear_actions_cache', '{}', 'done', '{\"ok\": true, \"deleted\": 2}')"
        )
    )
    resp = _client(jobs_router, db, _NON_OWNER, prefix="/jobs").get("/jobs/9991")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "done"
    assert body["job_type"] == "github.clear_actions_cache"


def test_single_job_unknown_id_is_404(db):
    resp = _client(jobs_router, db, _NON_OWNER, prefix="/jobs").get("/jobs/424242")
    assert resp.status_code == 404


# ── audit ─────────────────────────────────────────────────────────────────────

def test_audit_owner_ok(db):
    resp = _client(audit_router, db, _OWNER, prefix="/audit").get("/audit")
    assert resp.status_code == 200
    assert resp.json() == []


def test_audit_non_owner_forbidden(db):
    resp = _client(audit_router, db, _NON_OWNER, prefix="/audit").get("/audit")
    assert resp.status_code == 403


# ── tokens ────────────────────────────────────────────────────────────────────

def test_tokens_list_owner_ok(db):
    resp = _client(tokens_router, db, _OWNER, prefix="/tokens").get("/tokens")
    assert resp.status_code == 200
    assert resp.json() == []


def test_tokens_list_non_owner_forbidden(db):
    resp = _client(tokens_router, db, _NON_OWNER, prefix="/tokens").get("/tokens")
    assert resp.status_code == 403


def test_tokens_resolve_non_owner_forbidden(db):
    resp = _client(tokens_router, db, _NON_OWNER, prefix="/tokens").post(
        "/tokens/resolve", json={"org": "acme"}
    )
    assert resp.status_code == 403


def test_tokens_resolve_writes_audit_log(db):
    from src.core.db import AuditLog

    # No org "acme" exists yet, so upsert_token's best-effort tenant lookup finds nothing
    # and resolve_token's own fallback needs a real personal tenant for _OWNER -- that
    # requires an actual users row (tenants.personal_user_id FK), which the require_auth
    # override alone doesn't provide.
    db.add(User(id=_OWNER.id, email=_OWNER.email, name=None, password_hash=None, is_workspace_admin=True))
    db.commit()

    client = _client(tokens_router, db, _OWNER, prefix="/tokens")
    client.put("/tokens/acme", json={"token": "ghp_test", "label": "acme"})

    resp = client.post("/tokens/resolve", json={"org": "acme"})
    assert resp.status_code == 200
    assert resp.json()["token"] == "ghp_test"

    logs = db.query(AuditLog).filter(AuditLog.action == "token.resolve").all()
    assert len(logs) == 1
    assert logs[0].actor == _OWNER.email
    assert logs[0].target == "acme"


# ── actions cache ─────────────────────────────────────────────────────────────

def test_personal_actions_cache_requires_auth(db):
    app = FastAPI()
    app.include_router(cache_router)
    app.dependency_overrides[get_db] = lambda: db
    resp = TestClient(app).post(
        "/me/repos/acme/widget/actions-caches", json={"token": "ghp_test"}
    )
    assert resp.status_code == 401
