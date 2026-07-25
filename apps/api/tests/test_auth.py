"""Tests for auth router and config router."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import bcrypt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Query

from src.core.auth import UserOut, require_auth, require_workspace_admin
from src.core.db import User, get_db
from src.core.rate_limit import _account_buckets as _account_rate_limit_buckets
from src.core.rate_limit import _buckets as _rate_limit_buckets
from src.repositories import invitation_repo, org_repo
from src.routers.auth import _SETUP_LOCK_KEY
from src.routers.auth import router as auth_router
from src.routers.config import router as config_router


# ── Auth router ───────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """/auth/login and /auth/register are rate-limited per client IP, and /auth/login is
    additionally rate-limited per submitted email, both via module-level in-memory buckets
    that persist across tests in the same process — reset both so one test's request
    volume can't tip a later, unrelated test over the threshold."""
    _rate_limit_buckets.clear()
    _account_rate_limit_buckets.clear()
    yield
    _rate_limit_buckets.clear()
    _account_rate_limit_buckets.clear()


@pytest.fixture()
def auth_app(db):
    a = FastAPI()
    a.include_router(auth_router, prefix="/auth")
    a.dependency_overrides[get_db] = lambda: db
    return a


@pytest.fixture()
def auth_client(auth_app):
    return TestClient(auth_app)


def _setup_owner(client, email="owner@example.com", password="supersecret1234"):
    """POST /auth/setup and return the response body."""
    resp = client.post("/auth/setup", json={"email": email, "password": password})
    assert resp.status_code == 201
    return resp.json()


# setup-required

def test_setup_required_no_users(auth_client):
    resp = auth_client.get("/auth/setup-required")
    assert resp.status_code == 200
    assert resp.json()["setup_required"] is True


def test_setup_required_with_user(auth_client):
    _setup_owner(auth_client)
    resp = auth_client.get("/auth/setup-required")
    assert resp.status_code == 200
    assert resp.json()["setup_required"] is False


# setup

def test_setup_returns_token_and_owner(auth_client):
    body = _setup_owner(auth_client)
    assert "access_token" in body
    assert body["user"]["is_workspace_admin"] is True
    assert body["user"]["email"] == "owner@example.com"


def test_setup_stores_email_lowercased(auth_client):
    resp = auth_client.post(
        "/auth/setup", json={"email": "Owner@Example.com", "password": "supersecret1234"}
    )
    assert resp.status_code == 201
    assert resp.json()["user"]["email"] == "owner@example.com"


def test_setup_rejects_short_password(auth_client):
    resp = auth_client.post("/auth/setup", json={"email": "a@b.com", "password": "tooshort"})
    assert resp.status_code == 422


def test_setup_rejects_password_over_72_bytes(auth_client):
    # Regression test: bcrypt silently truncates input past 72 bytes rather than raising
    # (bcrypt==4.2.1 pinned here) -- unvalidated, two different passwords sharing the same
    # first 72 bytes would hash identically. Must be a clean 422, not silent truncation.
    resp = auth_client.post("/auth/setup", json={"email": "a@b.com", "password": "x" * 73})
    assert resp.status_code == 422


def test_setup_rejects_duplicate(auth_client):
    _setup_owner(auth_client)
    resp = auth_client.post(
        "/auth/setup", json={"email": "other@example.com", "password": "supersecret1234"}
    )
    assert resp.status_code == 409


def test_setup_applies_a_per_ip_rate_limit(auth_client):
    # Regression test for issue #279: /auth/setup was missing the rate_limit() dependency
    # applied to every sibling auth endpoint (register/login/verify-email/resend-verification).
    _setup_owner(auth_client)  # call 1 (201)
    for _ in range(9):  # calls 2-10 (409, setup already complete) -- still count toward the limit
        auth_client.post(
            "/auth/setup", json={"email": "other@example.com", "password": "supersecret1234"}
        )
    resp = auth_client.post(  # call 11 -- exceeds the default max_requests=10
        "/auth/setup", json={"email": "another@example.com", "password": "supersecret1234"}
    )
    assert resp.status_code == 429


def test_setup_advisory_lock_serializes_concurrent_holders(_engine):
    # Regression test for issue #218: two concurrent /auth/setup calls with *different*
    # emails both pass the count()==0 check before either commits, and (unlike the
    # register-email race) there's no unique-constraint collision to catch it -- only a
    # lock can. Verifies the lock primitive itself: a second connection can't acquire the
    # same key while the first transaction holds it, and can once the first releases it.
    with _engine.connect() as conn1, _engine.connect() as conn2:
        conn1.begin()
        conn2.begin()
        try:
            got1 = conn1.execute(
                text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": _SETUP_LOCK_KEY}
            ).scalar()
            got2 = conn2.execute(
                text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": _SETUP_LOCK_KEY}
            ).scalar()
            assert got1 is True
            assert got2 is False

            conn1.commit()  # releases conn1's advisory lock

            got2_retry = conn2.execute(
                text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": _SETUP_LOCK_KEY}
            ).scalar()
            assert got2_retry is True
        finally:
            conn2.rollback()


def test_setup_concurrent_duplicate_email_returns_409_not_500(auth_client, db):
    # Regression test for issue #218: the try/except around setup()'s db.commit() is a
    # second line of defense behind the advisory lock (which serializes the count()==0
    # check but doesn't stop two callers from racing on the *same* email specifically) --
    # same pattern as test_register_concurrent_same_email_returns_409_not_500: fake the
    # count() check to a miss so the real insert hits the genuine users.email unique
    # constraint and returns a clean 409 instead of an unhandled 500.
    _setup_owner(auth_client, email="owner@example.com")
    from src.core.db import User

    def racy_count(self):
        return 0

    with patch.object(Query, "count", racy_count):
        resp = auth_client.post(
            "/auth/setup", json={"email": "owner@example.com", "password": "supersecret1234"}
        )
    assert resp.status_code == 409
    assert db.query(User).filter(User.email == "owner@example.com").count() == 1


# register

def test_register_creates_non_owner(auth_client):
    _setup_owner(auth_client)
    resp = auth_client.post(
        "/auth/register", json={"email": "member@example.com", "password": "supersecret1234"}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert "access_token" in body
    assert body["user"]["is_workspace_admin"] is False
    assert body["user"]["email"] == "member@example.com"


def test_register_before_setup_rejected(auth_client):
    """/auth/setup must run first to create the workspace admin — registering before that
    would leave the instance with no admin, since /setup 409s once any user exists."""
    resp = auth_client.post(
        "/auth/register", json={"email": "first@example.com", "password": "supersecret1234"}
    )
    assert resp.status_code == 409


def test_register_rejects_short_password(auth_client):
    _setup_owner(auth_client)
    resp = auth_client.post("/auth/register", json={"email": "a@b.com", "password": "tooshort"})
    assert resp.status_code == 422


def test_register_rejects_password_over_72_bytes(auth_client):
    _setup_owner(auth_client)
    resp = auth_client.post("/auth/register", json={"email": "a@b.com", "password": "x" * 73})
    assert resp.status_code == 422


def test_register_rejects_duplicate_email(auth_client):
    _setup_owner(auth_client, email="dupe@example.com")
    resp = auth_client.post(
        "/auth/register", json={"email": "dupe@example.com", "password": "supersecret1234"}
    )
    assert resp.status_code == 409


def test_register_concurrent_same_email_returns_409_not_500(auth_client, db):
    # Regression test for issue #218: simulates two near-simultaneous /auth/register calls
    # for the same email, same pattern as test_org_repo.py's concurrent-insert-race test --
    # a row for this email is already committed (the "other request" that won the race),
    # then the existence check is patched to fake a miss so this call proceeds to the real
    # insert, which must collide on the genuine users.email unique constraint and return a
    # clean 409 instead of an unhandled 500.
    _setup_owner(auth_client, email="owner@example.com")
    from src.core.db import User

    db.add(User(email="race@example.com", name=None, password_hash="x", is_workspace_admin=False))
    db.commit()

    def racy_first(self):
        return None

    with patch.object(Query, "first", racy_first):
        resp = auth_client.post(
            "/auth/register", json={"email": "race@example.com", "password": "supersecret1234"}
        )
    assert resp.status_code == 409
    assert db.query(User).filter(User.email == "race@example.com").count() == 1


def test_register_rejects_duplicate_email_different_case(auth_client):
    # Regression test for issue #268: emails are effectively case-insensitive in real
    # mailboxes -- "Dupe@Example.com" and "dupe@example.com" must not both be able to
    # register, unlike before this fix where they'd create two independent accounts.
    _setup_owner(auth_client, email="dupe@example.com")
    resp = auth_client.post(
        "/auth/register", json={"email": "Dupe@Example.com", "password": "supersecret1234"}
    )
    assert resp.status_code == 409


def test_register_stores_email_lowercased(auth_client):
    _setup_owner(auth_client)
    resp = auth_client.post(
        "/auth/register", json={"email": "MixedCase@Example.com", "password": "supersecret1234"}
    )
    assert resp.status_code == 201
    assert resp.json()["user"]["email"] == "mixedcase@example.com"


def test_register_disabled_returns_403(auth_client):
    _setup_owner(auth_client)
    with patch("src.routers.auth.get_config", return_value="false"):
        resp = auth_client.post(
            "/auth/register", json={"email": "blocked@example.com", "password": "supersecret1234"}
        )
    assert resp.status_code == 403


# email verification (issue #217)

def test_setup_creates_a_verified_admin(auth_client, db):
    _setup_owner(auth_client, email="owner@example.com")
    user = db.query(User).filter(User.email == "owner@example.com").first()
    assert user.email_verified is True
    assert user.email_verify_token is None


def test_register_creates_an_unverified_user_with_a_pending_token(auth_client, db):
    _setup_owner(auth_client)
    resp = auth_client.post(
        "/auth/register", json={"email": "member@example.com", "password": "supersecret1234"}
    )
    assert resp.status_code == 201
    user = db.query(User).filter(User.email == "member@example.com").first()
    assert user.email_verified is False
    assert user.email_verify_token is not None
    assert user.email_verify_token_expires_at is not None


def test_register_still_succeeds_when_smtp_is_not_configured(auth_client):
    # Default test settings have no SMTP configured -- registration must succeed
    # regardless (account creation must never depend on email sending working).
    _setup_owner(auth_client)
    resp = auth_client.post(
        "/auth/register", json={"email": "member@example.com", "password": "supersecret1234"}
    )
    assert resp.status_code == 201


def test_register_logs_a_warning_but_does_not_fail_when_email_sending_raises(auth_client):
    _setup_owner(auth_client)
    with patch("src.routers.auth.send_verification_email", side_effect=RuntimeError("smtp exploded")):
        resp = auth_client.post(
            "/auth/register", json={"email": "member@example.com", "password": "supersecret1234"}
        )
    assert resp.status_code == 201


def test_register_still_succeeds_when_cors_origins_is_misconfigured_empty(auth_client):
    # Regression test: verify_url construction (f"{settings.cors_origins[0]}/...") used to
    # sit outside the try/except in _send_verification_email_best_effort, so an operator
    # misconfiguring CORS_ORIGINS=[] would raise an uncaught IndexError there and break
    # registration itself, not just the email send.
    from src.core.config import settings

    _setup_owner(auth_client)
    with patch.object(settings, "cors_origins", []):
        resp = auth_client.post(
            "/auth/register", json={"email": "member@example.com", "password": "supersecret1234"}
        )
    assert resp.status_code == 201


def test_verify_email_marks_the_account_verified_and_clears_the_token(auth_client, db):
    _setup_owner(auth_client)
    auth_client.post("/auth/register", json={"email": "member@example.com", "password": "supersecret1234"})
    user = db.query(User).filter(User.email == "member@example.com").first()
    token = user.email_verify_token

    resp = auth_client.post("/auth/verify-email", json={"token": token})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    db.refresh(user)
    assert user.email_verified is True
    assert user.email_verify_token is None
    assert user.email_verify_token_expires_at is None


def test_verify_email_rejects_an_unknown_token(auth_client):
    resp = auth_client.post("/auth/verify-email", json={"token": "not-a-real-token"})
    assert resp.status_code == 400


def test_verify_email_rejects_an_expired_token(auth_client, db):
    _setup_owner(auth_client)
    auth_client.post("/auth/register", json={"email": "member@example.com", "password": "supersecret1234"})
    user = db.query(User).filter(User.email == "member@example.com").first()
    user.email_verify_token_expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db.commit()

    resp = auth_client.post("/auth/verify-email", json={"token": user.email_verify_token})
    assert resp.status_code == 400
    db.refresh(user)
    assert user.email_verified is False


def test_resend_verification_regenerates_the_token(auth_client, db):
    _setup_owner(auth_client)
    register_resp = auth_client.post(
        "/auth/register", json={"email": "member@example.com", "password": "supersecret1234"}
    )
    token = register_resp.json()["access_token"]
    user = db.query(User).filter(User.email == "member@example.com").first()
    original_token = user.email_verify_token

    resp = auth_client.post("/auth/resend-verification", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "already_verified": False}

    db.refresh(user)
    assert user.email_verify_token is not None
    assert user.email_verify_token != original_token


def test_resend_verification_is_a_noop_for_an_already_verified_account(auth_client):
    token = _setup_owner(auth_client, email="owner@example.com")["access_token"]

    resp = auth_client.post("/auth/resend-verification", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "already_verified": True}


def test_resend_verification_requires_auth(auth_client):
    resp = auth_client.post("/auth/resend-verification")
    assert resp.status_code == 401


# login

def test_login_valid(auth_client):
    _setup_owner(auth_client)
    resp = auth_client.post(
        "/auth/login", json={"email": "owner@example.com", "password": "supersecret1234"}
    )
    assert resp.status_code == 200
    assert "access_token" in resp.json()


def test_login_valid_with_different_case_email(auth_client):
    # Regression test for issue #268: login must match the stored (lowercased) email
    # regardless of the case the client submits.
    _setup_owner(auth_client, email="owner@example.com")
    resp = auth_client.post(
        "/auth/login", json={"email": "Owner@Example.com", "password": "supersecret1234"}
    )
    assert resp.status_code == 200
    assert "access_token" in resp.json()


def test_login_wrong_password(auth_client):
    _setup_owner(auth_client)
    resp = auth_client.post(
        "/auth/login", json={"email": "owner@example.com", "password": "wrongpassword12"}
    )
    assert resp.status_code == 401


def test_login_rejects_password_over_72_bytes(auth_client):
    # Regression test: since setup()/register() now reject any password over 72 bytes,
    # no real stored password can be that long -- an oversized login guess must always
    # fail with 401. Without this, bcrypt's silent truncation (bcrypt==4.2.1 pinned here)
    # would let an oversized guess match on nothing more than a shared 72-byte prefix.
    _setup_owner(auth_client)
    resp = auth_client.post(
        "/auth/login", json={"email": "owner@example.com", "password": "x" * 200}
    )
    assert resp.status_code == 401


def test_login_unknown_email(auth_client):
    resp = auth_client.post(
        "/auth/login", json={"email": "nobody@example.com", "password": "supersecret1234"}
    )
    assert resp.status_code == 401


def test_login_unknown_email_still_pays_the_bcrypt_cost(auth_client):
    # Regression test: a nonexistent email used to short-circuit before bcrypt ran, so
    # response timing alone revealed whether an email was registered. Must now always
    # run one bcrypt check, same as the real-account path.
    with patch("src.routers.auth.bcrypt.checkpw", wraps=bcrypt.checkpw) as mock_checkpw:
        resp = auth_client.post(
            "/auth/login", json={"email": "nobody@example.com", "password": "supersecret1234"}
        )
    assert resp.status_code == 401
    mock_checkpw.assert_called_once()


def test_login_applies_a_per_account_rate_limit(auth_client):
    with patch("src.routers.auth.check_account_rate_limit") as mock_limit:
        auth_client.post(
            "/auth/login", json={"email": "Someone@Example.com", "password": "supersecret1234"}
        )
    # Lowercased so "Someone@Example.com" and "someone@example.com" share a bucket.
    mock_limit.assert_called_once_with("login:someone@example.com")


def test_login_github_only_user_returns_401(auth_client, db):
    from src.core.db import User

    db.add(User(email="github-only@example.com", password_hash=None, is_workspace_admin=False))
    db.commit()
    resp = auth_client.post(
        "/auth/login", json={"email": "github-only@example.com", "password": "anypassword12"}
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid credentials"


# pending invitations surfaced at register/login

def test_register_never_surfaces_pending_invitation(auth_client, db):
    """Registration has no email-verification step, so a self-asserted email is not proof
    of inbox control. Even though the same email/lookup would find a real pending
    invitation (confirmed below via list_pending_for_email directly), the register
    response must never expose it — otherwise anyone who merely knows a victim's email
    could learn whether/where they have a pending org invite by registering with it."""
    owner = _setup_owner(auth_client, email="owner@example.com")
    org = org_repo.get_or_create(db, github_login="acme")
    invitation_repo.create(db, org_id=org.id, email="newmember@example.com", invited_by_user_id=owner["user"]["id"])

    # Sanity check: the invitation genuinely exists and matches — this isn't a case
    # of "there was nothing to leak".
    assert len(invitation_repo.list_pending_for_email(db, "newmember@example.com")) == 1

    resp = auth_client.post(
        "/auth/register", json={"email": "newmember@example.com", "password": "supersecret1234"}
    )

    assert resp.status_code == 201
    assert resp.json()["pending_invitations"] == []


def test_login_surfaces_pending_invitation(auth_client, db):
    owner = _setup_owner(auth_client, email="owner@example.com")
    auth_client.post(
        "/auth/register", json={"email": "member@example.com", "password": "supersecret1234"}
    )
    org = org_repo.get_or_create(db, github_login="acme")
    invitation_repo.create(db, org_id=org.id, email="member@example.com", invited_by_user_id=owner["user"]["id"])

    resp = auth_client.post(
        "/auth/login", json={"email": "member@example.com", "password": "supersecret1234"}
    )

    assert resp.status_code == 200
    pending = resp.json()["pending_invitations"]
    assert len(pending) == 1
    assert pending[0]["org_login"] == "acme"


def test_login_omits_expired_invitation(auth_client, db):
    owner = _setup_owner(auth_client, email="owner@example.com")
    auth_client.post(
        "/auth/register", json={"email": "member@example.com", "password": "supersecret1234"}
    )
    org = org_repo.get_or_create(db, github_login="acme")
    invitation = invitation_repo.create(
        db, org_id=org.id, email="member@example.com", invited_by_user_id=owner["user"]["id"]
    )
    invitation.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    db.commit()

    resp = auth_client.post(
        "/auth/login", json={"email": "member@example.com", "password": "supersecret1234"}
    )

    assert resp.status_code == 200
    assert resp.json()["pending_invitations"] == []


def test_login_omits_accepted_invitation(auth_client, db):
    owner = _setup_owner(auth_client, email="owner@example.com")
    auth_client.post(
        "/auth/register", json={"email": "member@example.com", "password": "supersecret1234"}
    )
    org = org_repo.get_or_create(db, github_login="acme")
    invitation = invitation_repo.create(
        db, org_id=org.id, email="member@example.com", invited_by_user_id=owner["user"]["id"]
    )
    invitation.status = "accepted"
    db.commit()

    resp = auth_client.post(
        "/auth/login", json={"email": "member@example.com", "password": "supersecret1234"}
    )

    assert resp.status_code == 200
    assert resp.json()["pending_invitations"] == []


def test_login_with_no_pending_invitations_returns_empty_list(auth_client):
    _setup_owner(auth_client, email="owner@example.com")
    resp = auth_client.post(
        "/auth/login", json={"email": "owner@example.com", "password": "supersecret1234"}
    )
    assert resp.status_code == 200
    assert resp.json()["pending_invitations"] == []


# me

def test_me_unauthenticated(auth_client):
    resp = auth_client.get("/auth/me")
    assert resp.status_code == 401


def test_me_returns_profile(auth_client):
    token = _setup_owner(auth_client)["access_token"]
    resp = auth_client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["email"] == "owner@example.com"
    assert data["is_workspace_admin"] is True


def test_patch_me_unauthenticated(auth_client):
    resp = auth_client.patch("/auth/me", json={"name": "Alice"})
    assert resp.status_code == 401


def test_patch_me_updates_name(auth_client):
    token = _setup_owner(auth_client)["access_token"]
    resp = auth_client.patch(
        "/auth/me",
        json={"name": "Alice"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Alice"


# revoke-sessions

def test_revoke_sessions_unauthenticated(auth_client):
    resp = auth_client.post("/auth/me/revoke-sessions")
    assert resp.status_code == 401


def test_revoke_sessions_invalidates_existing_token(auth_client):
    token = _setup_owner(auth_client)["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    resp = auth_client.post("/auth/me/revoke-sessions", headers=headers)
    assert resp.status_code == 200

    resp = auth_client.get("/auth/me", headers=headers)
    assert resp.status_code == 401


def test_revoke_sessions_new_login_still_works(auth_client):
    _setup_owner(auth_client)
    auth_client.post(
        "/auth/login", json={"email": "owner@example.com", "password": "supersecret1234"}
    )
    first_token = auth_client.post(
        "/auth/login", json={"email": "owner@example.com", "password": "supersecret1234"}
    ).json()["access_token"]
    auth_client.post(
        "/auth/me/revoke-sessions", headers={"Authorization": f"Bearer {first_token}"}
    )
    new_token = auth_client.post(
        "/auth/login", json={"email": "owner@example.com", "password": "supersecret1234"}
    ).json()["access_token"]
    resp = auth_client.get("/auth/me", headers={"Authorization": f"Bearer {new_token}"})
    assert resp.status_code == 200


# ── Config router ─────────────────────────────────────────────────────────────

_OWNER = UserOut(id=1, email="owner@example.com", name=None, is_workspace_admin=True)
_VIEWER = UserOut(id=2, email="viewer@example.com", name=None, is_workspace_admin=False)

_MOCK_CONFIG = {
    "worker_poll_seconds": "5",
}


@pytest.fixture()
def config_client():
    """No auth override — all protected endpoints return 401."""
    a = FastAPI()
    a.include_router(config_router, prefix="/config")
    return TestClient(a)


@pytest.fixture()
def config_client_viewer():
    """Authenticated as a non-owner user."""
    a = FastAPI()
    a.dependency_overrides[require_auth] = lambda: _VIEWER
    a.include_router(config_router, prefix="/config")
    return TestClient(a)


@pytest.fixture()
def config_client_owner():
    """Authenticated as the owner."""
    a = FastAPI()
    a.dependency_overrides[require_auth] = lambda: _OWNER
    a.dependency_overrides[require_workspace_admin] = lambda: _OWNER
    a.include_router(config_router, prefix="/config")
    return TestClient(a)


def test_get_config_unauthenticated(config_client):
    resp = config_client.get("/config")
    assert resp.status_code == 401


def test_get_config_non_owner_forbidden(config_client_viewer):
    resp = config_client_viewer.get("/config")
    assert resp.status_code == 403


def test_get_config_owner(config_client_owner):
    with patch("src.routers.config.read_all", return_value=_MOCK_CONFIG):
        resp = config_client_owner.get("/config")
    assert resp.status_code == 200
    assert resp.json()["worker_poll_seconds"] == "5"


def test_update_config_unauthenticated(config_client):
    resp = config_client.put("/config/worker_poll_seconds", json={"value": "10"})
    assert resp.status_code == 401


def test_update_config_non_owner_forbidden(config_client_viewer):
    resp = config_client_viewer.put("/config/worker_poll_seconds", json={"value": "10"})
    assert resp.status_code == 403


def test_update_config_unknown_key(config_client_owner):
    resp = config_client_owner.put("/config/unknown_key", json={"value": "x"})
    assert resp.status_code == 400


def test_update_config_invalid_int(config_client_owner):
    resp = config_client_owner.put("/config/worker_poll_seconds", json={"value": "notanint"})
    assert resp.status_code == 422


@pytest.mark.parametrize("value", ["0", "-5"])
def test_update_config_int_below_minimum(config_client_owner, value):
    resp = config_client_owner.put("/config/worker_poll_seconds", json={"value": value})
    assert resp.status_code == 422


def test_update_config_valid_worker_poll_seconds(config_client_owner):
    with (
        patch("src.routers.config.set_config") as mock_set,
        patch("src.routers.config.read_all", return_value=_MOCK_CONFIG),
    ):
        resp = config_client_owner.put("/config/worker_poll_seconds", json={"value": "10"})
    assert resp.status_code == 200
    mock_set.assert_called_once_with("worker_poll_seconds", "10")


# github_api_base and cors_origins moved to env vars — no longer runtime-editable.
@pytest.mark.parametrize("key", ["github_api_base", "cors_origins"])
def test_update_config_removed_keys_rejected(config_client_owner, key):
    resp = config_client_owner.put(f"/config/{key}", json={"value": "https://x.com"})
    assert resp.status_code == 400


@pytest.mark.parametrize("value", ["yes", "1", ""])
def test_update_config_invalid_bool(config_client_owner, value):
    resp = config_client_owner.put("/config/registration_enabled", json={"value": value})
    assert resp.status_code == 422


def test_update_config_valid_bool(config_client_owner):
    with (
        patch("src.routers.config.set_config") as mock_set,
        patch("src.routers.config.read_all", return_value={**_MOCK_CONFIG, "registration_enabled": "false"}),
    ):
        resp = config_client_owner.put("/config/registration_enabled", json={"value": "false"})
    assert resp.status_code == 200
    mock_set.assert_called_once_with("registration_enabled", "false")


def test_update_config_success(config_client_owner):
    with (
        patch("src.routers.config.set_config") as mock_set,
        patch("src.routers.config.read_all", return_value={**_MOCK_CONFIG, "worker_poll_seconds": "10"}),
    ):
        resp = config_client_owner.put("/config/worker_poll_seconds", json={"value": "10"})
    assert resp.status_code == 200
    mock_set.assert_called_once_with("worker_poll_seconds", "10")
    assert resp.json()["worker_poll_seconds"] == "10"
