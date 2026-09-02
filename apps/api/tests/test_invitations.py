"""Tests for the org invitation create/list/revoke/accept flow."""

import secrets
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.auth import UserOut, require_auth
from src.core.db import User, get_db
from src.repositories import invitation_repo, org_membership_repo, org_repo
from src.routers.invitations import router as invitations_router


def _make_user(db, email: str, email_verified: bool = True) -> UserOut:
    # Verified by default so the existing email-match tests below aren't also implicitly
    # testing the (separately covered) email-verification requirement -- see
    # test_accept_invitation_unverified_email_forbidden for that.
    user = User(email=email, name=None, password_hash=None, is_workspace_admin=False, email_verified=email_verified)
    db.add(user)
    db.commit()
    db.refresh(user)
    return UserOut(id=user.id, email=user.email, name=user.name, is_workspace_admin=False)


@pytest.fixture()
def acme_org(db):
    admin = _make_user(db, "admin@e.com")
    member = _make_user(db, "member@e.com")
    invitee = _make_user(db, "bob@acme.com")
    wrong_email = _make_user(db, "carol@acme.com")
    org = org_repo.get_or_create(db, github_login="acme")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=admin.id, role="admin")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=member.id, role="member")
    return {"org": org, "admin": admin, "member": member, "invitee": invitee, "wrong_email": wrong_email}


def _client(db, user):
    app = FastAPI()
    app.include_router(invitations_router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_auth] = lambda: user
    return TestClient(app)


def test_create_invitation_requires_admin(db, acme_org):
    resp = _client(db, acme_org["member"]).post("/orgs/acme/invitations", json={"email": "bob@acme.com"})
    assert resp.status_code == 403


def test_create_invitation_admin_ok(db, acme_org):
    resp = _client(db, acme_org["admin"]).post("/orgs/acme/invitations", json={"email": "bob@acme.com"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["invitation"]["email"] == "bob@acme.com"
    assert body["invitation"]["status"] == "pending"
    assert "/invite/" in body["invite_link"]


def test_create_invitation_sets_tenant_id(db, acme_org):
    from src.core.db import Tenant

    resp = _client(db, acme_org["admin"]).post("/orgs/acme/invitations", json={"email": "bob@acme.com"})
    assert resp.status_code == 200
    token = resp.json()["invite_link"].rsplit("/", 1)[-1]

    invitation = invitation_repo.get_by_token(db, token)

    assert invitation.tenant_id is not None
    tenant = db.query(Tenant).filter(Tenant.id == invitation.tenant_id).first()
    assert tenant.kind == "org"
    assert tenant.org_id == acme_org["org"].id


def test_create_invitation_rejects_duplicate_pending_invite(db, acme_org):
    client = _client(db, acme_org["admin"])
    first = client.post("/orgs/acme/invitations", json={"email": "bob@acme.com"})
    assert first.status_code == 200

    second = client.post("/orgs/acme/invitations", json={"email": "bob@acme.com"})
    assert second.status_code == 409

    # Only the first invite exists -- the rejected attempt didn't create a second row.
    listing = client.get("/orgs/acme/invitations").json()
    assert len(listing) == 1


def test_create_invitation_duplicate_check_is_case_insensitive(db, acme_org):
    client = _client(db, acme_org["admin"])
    client.post("/orgs/acme/invitations", json={"email": "bob@acme.com"})

    resp = client.post("/orgs/acme/invitations", json={"email": "BOB@ACME.COM"})
    assert resp.status_code == 409


def test_create_invitation_allows_new_invite_after_the_pending_one_is_revoked(db, acme_org):
    client = _client(db, acme_org["admin"])
    created = client.post("/orgs/acme/invitations", json={"email": "bob@acme.com"}).json()
    client.post(f"/orgs/acme/invitations/{created['invitation']['id']}/revoke")

    resp = client.post("/orgs/acme/invitations", json={"email": "bob@acme.com"})
    assert resp.status_code == 200


def test_create_invitation_allows_new_invite_after_the_pending_one_expires(db, acme_org):
    client = _client(db, acme_org["admin"])
    created = client.post("/orgs/acme/invitations", json={"email": "bob@acme.com"}).json()
    _expire_invitation(db, created["invitation"]["id"])

    resp = client.post("/orgs/acme/invitations", json={"email": "bob@acme.com"})
    assert resp.status_code == 200


def test_invitation_repo_rejects_a_second_pending_row(db, acme_org):
    # Issue #270: the DB-level guard. Bypasses the router's pre-check by calling the
    # repo directly -- without the partial unique index (migration 0042) this second
    # insert would silently create a duplicate pending row.
    org, admin = acme_org["org"], acme_org["admin"]
    invitation_repo.create(db, org_id=org.id, email="dup@acme.com", invited_by_user_id=admin.id)

    with pytest.raises(invitation_repo.DuplicatePendingInvitation):
        invitation_repo.create(db, org_id=org.id, email="DUP@ACME.COM", invited_by_user_id=admin.id)

    pending = invitation_repo.list_pending_for_email(db, "dup@acme.com")
    assert len(pending) == 1


def test_concurrent_create_invitation_leaves_exactly_one_pending_row():
    # Issue #270: the actual race -- two real, independently-committing connections
    # insert the same pending invite concurrently. The partial unique index must let
    # exactly one win; the loser must surface as DuplicatePendingInvitation, not a
    # 500 or a second row. Self-contained (no savepoint-scoped `db` fixture, whose
    # open transaction would deadlock the worker threads on tenants/orgs), with
    # explicit cleanup -- same approach as test_rls_isolation.py.
    import threading

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session as RawSession

    from src.core.config import settings
    from src.repositories import org_repo

    engine = create_engine(settings.database_url.get_secret_value())
    login = f"race-org-{secrets.token_hex(4)}"
    org_id = admin_id = None
    try:
        with RawSession(engine) as seed:
            admin = User(email=f"{login}-admin@e.com", name=None, password_hash=None, email_verified=True)
            seed.add(admin)
            seed.flush()
            admin_id = admin.id
            org = org_repo.get_or_create(seed, github_login=login)
            org_id = org.id
            seed.commit()

        barrier = threading.Barrier(2)
        results: list[str] = []
        lock = threading.Lock()

        def worker() -> None:
            outcome = "ok"
            try:
                with RawSession(engine) as s:
                    barrier.wait(timeout=10)
                    try:
                        invitation_repo.create(s, org_id=org_id, email="race@acme.com", invited_by_user_id=admin_id)
                    except invitation_repo.DuplicatePendingInvitation:
                        outcome = "duplicate"
            except Exception as exc:  # noqa: BLE001 -- surface anything unexpected
                outcome = f"unexpected:{type(exc).__name__}"
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert sorted(results) == ["duplicate", "ok"], results
        with engine.connect() as c:
            n = c.execute(
                text(
                    "SELECT count(*) FROM invitations "
                    "WHERE org_id = :o AND lower(email) = 'race@acme.com' AND status = 'pending'"
                ),
                {"o": org_id},
            ).scalar()
        assert n == 1
    finally:
        with engine.begin() as c:
            if org_id is not None:
                # orgs.tenant_id <-> tenants.org_id is a reciprocal FK cycle; break it first.
                c.execute(text("UPDATE orgs SET tenant_id = NULL WHERE id = :o"), {"o": org_id})
                c.execute(text("DELETE FROM invitations WHERE org_id = :o"), {"o": org_id})
                c.execute(text("DELETE FROM tenants WHERE org_id = :o"), {"o": org_id})
                c.execute(text("DELETE FROM orgs WHERE id = :o"), {"o": org_id})
            if admin_id is not None:
                c.execute(text("DELETE FROM users WHERE id = :u"), {"u": admin_id})
        engine.dispose()


def test_list_invitations_admin_only(db, acme_org):
    _client(db, acme_org["admin"]).post("/orgs/acme/invitations", json={"email": "bob@acme.com"})
    resp = _client(db, acme_org["admin"]).get("/orgs/acme/invitations")
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    resp = _client(db, acme_org["member"]).get("/orgs/acme/invitations")
    assert resp.status_code == 403


def test_preview_invitation_unauthenticated(db, acme_org):
    created = _client(db, acme_org["admin"]).post("/orgs/acme/invitations", json={"email": "bob@acme.com"}).json()
    token = created["invite_link"].rsplit("/", 1)[-1]

    app = FastAPI()
    app.include_router(invitations_router)
    app.dependency_overrides[get_db] = lambda: db
    resp = TestClient(app).get(f"/invitations/{token}")
    assert resp.status_code == 200
    # The invitee's email is intentionally not disclosed on this unauthenticated endpoint.
    assert resp.json() == {"org_login": "acme", "status": "pending"}


def test_accept_invitation_wrong_email_forbidden(db, acme_org):
    created = _client(db, acme_org["admin"]).post("/orgs/acme/invitations", json={"email": "bob@acme.com"}).json()
    token = created["invite_link"].rsplit("/", 1)[-1]

    resp = _client(db, acme_org["wrong_email"]).post(f"/invitations/{token}/accept")
    assert resp.status_code == 403


def test_accept_invitation_ok(db, acme_org):
    created = _client(db, acme_org["admin"]).post("/orgs/acme/invitations", json={"email": "bob@acme.com"}).json()
    token = created["invite_link"].rsplit("/", 1)[-1]

    resp = _client(db, acme_org["invitee"]).post(f"/invitations/{token}/accept")
    assert resp.status_code == 200
    assert resp.json() == {"org_login": "acme", "role": "member"}

    # Accepting again fails — invitation is no longer pending.
    resp = _client(db, acme_org["invitee"]).post(f"/invitations/{token}/accept")
    assert resp.status_code == 409


def test_accept_invitation_unverified_email_forbidden(db, acme_org):
    # Regression test for issue #217: email match alone must not be enough for a
    # self-registered account that hasn't proven it controls the invited inbox.
    unverified = _make_user(db, "dave@acme.com", email_verified=False)
    created = _client(db, acme_org["admin"]).post("/orgs/acme/invitations", json={"email": "dave@acme.com"}).json()
    token = created["invite_link"].rsplit("/", 1)[-1]

    resp = _client(db, unverified).post(f"/invitations/{token}/accept")
    assert resp.status_code == 403
    assert "verify" in resp.json()["detail"].lower()


def test_accept_invitation_succeeds_once_verified(db, acme_org):
    unverified = _make_user(db, "erin@acme.com", email_verified=False)
    created = _client(db, acme_org["admin"]).post("/orgs/acme/invitations", json={"email": "erin@acme.com"}).json()
    token = created["invite_link"].rsplit("/", 1)[-1]

    resp = _client(db, unverified).post(f"/invitations/{token}/accept")
    assert resp.status_code == 403

    user_row = db.query(User).filter(User.id == unverified.id).first()
    user_row.email_verified = True
    db.commit()

    resp = _client(db, unverified).post(f"/invitations/{token}/accept")
    assert resp.status_code == 200
    assert resp.json() == {"org_login": "acme", "role": "member"}


def test_accept_invitation_case_insensitive_email_match(db, acme_org):
    created = _client(db, acme_org["admin"]).post("/orgs/acme/invitations", json={"email": "Bob@Acme.com"}).json()
    token = created["invite_link"].rsplit("/", 1)[-1]

    # Invitee's account email differs only in case from the invited address.
    resp = _client(db, acme_org["invitee"]).post(f"/invitations/{token}/accept")
    assert resp.status_code == 200
    assert resp.json() == {"org_login": "acme", "role": "member"}


def test_revoke_invitation(db, acme_org):
    created = _client(db, acme_org["admin"]).post("/orgs/acme/invitations", json={"email": "bob@acme.com"}).json()
    invitation_id = created["invitation"]["id"]

    resp = _client(db, acme_org["admin"]).post(f"/orgs/acme/invitations/{invitation_id}/revoke")
    assert resp.status_code == 200
    assert resp.json()["status"] == "revoked"

    token = created["invite_link"].rsplit("/", 1)[-1]
    resp = _client(db, acme_org["invitee"]).post(f"/invitations/{token}/accept")
    assert resp.status_code == 409


def _expire_invitation(db, invitation_id: int) -> None:
    from src.core.db import Invitation

    invitation = db.query(Invitation).filter(Invitation.id == invitation_id).first()
    invitation.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()


def test_create_invitation_sets_expiry(db, acme_org):
    before = datetime.now(timezone.utc)
    resp = _client(db, acme_org["admin"]).post("/orgs/acme/invitations", json={"email": "bob@acme.com"})
    after = datetime.now(timezone.utc)
    expires_at = datetime.fromisoformat(resp.json()["invitation"]["expires_at"])
    assert before + timedelta(days=7) <= expires_at <= after + timedelta(days=7)


def test_accept_expired_invitation_returns_410(db, acme_org):
    created = _client(db, acme_org["admin"]).post("/orgs/acme/invitations", json={"email": "bob@acme.com"}).json()
    token = created["invite_link"].rsplit("/", 1)[-1]
    _expire_invitation(db, created["invitation"]["id"])

    resp = _client(db, acme_org["invitee"]).post(f"/invitations/{token}/accept")
    assert resp.status_code == 410


def test_preview_expired_invitation_shows_expired_status(db, acme_org):
    created = _client(db, acme_org["admin"]).post("/orgs/acme/invitations", json={"email": "bob@acme.com"}).json()
    token = created["invite_link"].rsplit("/", 1)[-1]
    _expire_invitation(db, created["invitation"]["id"])

    app = FastAPI()
    app.include_router(invitations_router)
    app.dependency_overrides[get_db] = lambda: db
    resp = TestClient(app).get(f"/invitations/{token}")
    assert resp.status_code == 200
    assert resp.json() == {"org_login": "acme", "status": "expired"}


def test_list_invitations_reflects_expired_status(db, acme_org):
    created = _client(db, acme_org["admin"]).post("/orgs/acme/invitations", json={"email": "bob@acme.com"}).json()
    _expire_invitation(db, created["invitation"]["id"])

    resp = _client(db, acme_org["admin"]).get("/orgs/acme/invitations")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["status"] == "expired"
    assert "expires_at" in body[0]
