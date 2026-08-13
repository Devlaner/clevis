"""Tests for src.repositories.org_membership_repo's Membership dual-write (issue #190 PR 4):
every OrgMembership create/update/delete must be mirrored onto the tenants/memberships
tables, keyed off the org's tenant."""

import threading
from unittest.mock import patch

from src.core.db import Membership, Org, OrgMembership, SessionLocal, Tenant, User
from src.repositories import org_membership_repo, org_repo


def _make_user(db, email: str) -> User:
    user = User(email=email, name=None, password_hash=None, is_workspace_admin=False)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _membership_row(db, org_id: int, user_id: int) -> Membership | None:
    tenant = db.query(Tenant).filter(Tenant.kind == "org", Tenant.org_id == org_id).first()
    if tenant is None:
        return None
    return db.query(Membership).filter(Membership.tenant_id == tenant.id, Membership.user_id == user_id).first()


def test_get_or_create_mirrors_a_membership_row(db):
    org = org_repo.get_or_create(db, github_login="acme")
    user = _make_user(db, "alice@example.com")

    org_membership_repo.get_or_create(db, org_id=org.id, user_id=user.id, role="admin")

    membership = _membership_row(db, org.id, user.id)
    assert membership is not None
    assert membership.role == "admin"


def test_get_or_create_is_idempotent_on_the_mirror_too(db):
    org = org_repo.get_or_create(db, github_login="acme")
    user = _make_user(db, "bob@example.com")

    org_membership_repo.get_or_create(db, org_id=org.id, user_id=user.id, role="member")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=user.id, role="member")

    tenant = db.query(Tenant).filter(Tenant.kind == "org", Tenant.org_id == org.id).first()
    rows = db.query(Membership).filter(Membership.tenant_id == tenant.id, Membership.user_id == user.id).all()
    assert len(rows) == 1


def test_update_role_mirrors_onto_membership(db):
    org = org_repo.get_or_create(db, github_login="acme")
    user = _make_user(db, "carol@example.com")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=user.id, role="member")

    org_membership_repo.update_role(db, org_id=org.id, user_id=user.id, role="admin")

    membership = _membership_row(db, org.id, user.id)
    assert membership is not None
    assert membership.role == "admin"


def test_delete_removes_the_membership_mirror_too(db):
    org = org_repo.get_or_create(db, github_login="acme")
    user = _make_user(db, "dave@example.com")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=user.id, role="admin")

    org_membership_repo.delete(db, org_id=org.id, user_id=user.id)

    membership = _membership_row(db, org.id, user.id)
    assert membership is None


def test_get_or_create_repairs_a_missing_mirror_on_an_existing_membership(db):
    # Regression test for a CodeRabbit finding on #323: get_or_create's early-return path
    # (OrgMembership already exists) used to skip the dual-write entirely, so a Membership
    # row deleted or never created out-of-band would never get repaired on subsequent calls
    # -- exactly the pattern org_provisioning.py's reconcile loop hits on every login.
    org = org_repo.get_or_create(db, github_login="acme")
    user = _make_user(db, "grace@example.com")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=user.id, role="member")
    assert _membership_row(db, org.id, user.id) is not None
    # Simulate the mirror having gone missing out-of-band.
    tenant = db.query(Tenant).filter(Tenant.kind == "org", Tenant.org_id == org.id).first()
    db.query(Membership).filter(Membership.tenant_id == tenant.id, Membership.user_id == user.id).delete()
    db.commit()
    assert _membership_row(db, org.id, user.id) is None

    org_membership_repo.get_or_create(db, org_id=org.id, user_id=user.id, role="member")

    membership = _membership_row(db, org.id, user.id)
    assert membership is not None
    assert membership.role == "member"


def test_get_or_create_repairs_a_stale_role_on_an_existing_mirror(db):
    org = org_repo.get_or_create(db, github_login="acme")
    user = _make_user(db, "heidi@example.com")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=user.id, role="member")
    # Simulate the mirror's role having drifted out-of-band from the OrgMembership's role.
    tenant = db.query(Tenant).filter(Tenant.kind == "org", Tenant.org_id == org.id).first()
    stale = db.query(Membership).filter(Membership.tenant_id == tenant.id, Membership.user_id == user.id).first()
    stale.role = "admin"
    db.commit()

    org_membership_repo.get_or_create(db, org_id=org.id, user_id=user.id, role="member")

    membership = _membership_row(db, org.id, user.id)
    assert membership.role == "member"


def test_update_role_blocks_a_concurrent_delete_until_the_mirror_sync_commits():
    """Regression test for a CodeRabbit finding on #324: without row locking, a concurrent
    delete() could interleave between update_role's membership lookup and its mirror sync,
    resurrecting the tenant Membership mirror right after revocation. Needs two genuinely
    separate connections (not the savepoint-per-test `db` fixture, same reasoning as
    test_db_get_db.py) since the race only exists across real concurrent transactions."""
    setup = SessionLocal()
    org = org_repo.get_or_create(setup, github_login="acme-lock-test")
    user = User(email="ivy@example.com", name=None, password_hash=None, is_workspace_admin=False)
    setup.add(user)
    setup.commit()
    setup.refresh(user)
    org_id, user_id = org.id, user.id
    org_membership_repo.get_or_create(setup, org_id=org_id, user_id=user_id, role="member")
    setup.close()
    # org/user are bound to `setup`, now closed -- only org_id/user_id (plain ints) are used
    # from here on, never the detached ORM objects themselves.

    reached_lock = threading.Event()
    release_lock = threading.Event()
    original_sync = org_membership_repo._sync_membership_mirror

    def paused_sync(db, org_id, membership):
        reached_lock.set()
        assert release_lock.wait(timeout=5), "test setup never released the paused sync"
        original_sync(db, org_id, membership)

    update_result: dict[str, bool] = {}
    delete_result: dict[str, bool] = {}

    def run_update_role(session_a):
        org_membership_repo.update_role(session_a, org_id=org_id, user_id=user_id, role="admin")
        update_result["finished"] = True

    def run_delete():
        session_b = SessionLocal()
        try:
            assert reached_lock.wait(timeout=5), "update_role never reached its locked section"
            org_membership_repo.delete(session_b, org_id=org_id, user_id=user_id)
            delete_result["finished"] = True
        finally:
            session_b.close()

    session_a = SessionLocal()
    try:
        with patch.object(org_membership_repo, "_sync_membership_mirror", paused_sync):
            update_thread = threading.Thread(target=run_update_role, args=(session_a,))
            update_thread.start()
            assert reached_lock.wait(timeout=5), "update_role never reached its locked section"

            delete_thread = threading.Thread(target=run_delete)
            delete_thread.start()
            delete_thread.join(timeout=0.3)
            assert not delete_result, "delete() must block while update_role holds the row lock"

            release_lock.set()
            update_thread.join(timeout=5)
            delete_thread.join(timeout=5)
        assert update_result.get("finished") is True
        assert delete_result.get("finished") is True

        # The delete landed after update_role's commit released the lock, so the final
        # state must reflect the delete -- no resurrected mirror row.
        remaining = (
            session_a.query(OrgMembership)
            .filter(OrgMembership.org_id == org_id, OrgMembership.user_id == user_id)
            .first()
        )
        assert remaining is None
        tenant = session_a.query(Tenant).filter(Tenant.kind == "org", Tenant.org_id == org_id).first()
        mirror = (
            session_a.query(Membership).filter(Membership.tenant_id == tenant.id, Membership.user_id == user_id).first()
        )
        assert mirror is None
    finally:
        # This test uses real committing connections (not the savepoint-per-test `db`
        # fixture), so the rows it creates persist unless cleaned up explicitly here.
        session_a.rollback()
        session_a.query(OrgMembership).filter(OrgMembership.org_id == org_id).delete()
        tenant = session_a.query(Tenant).filter(Tenant.kind == "org", Tenant.org_id == org_id).first()
        if tenant is not None:
            session_a.query(Membership).filter(Membership.tenant_id == tenant.id).delete()
            # orgs.tenant_id and tenants.org_id reference each other (composite reciprocal
            # FK) -- null the org side first so either row can then be deleted freely.
            org_row = session_a.query(Org).filter(Org.id == org_id).first()
            if org_row is not None:
                org_row.tenant_id = None
                session_a.commit()
            session_a.query(Tenant).filter(Tenant.id == tenant.id).delete()
        session_a.query(Org).filter(Org.id == org_id).delete()
        session_a.query(User).filter(User.id == user_id).delete()
        session_a.commit()
        session_a.close()


def test_dual_write_uses_the_same_tenant_for_every_membership_in_an_org(db):
    org = org_repo.get_or_create(db, github_login="acme")
    admin = _make_user(db, "erin@example.com")
    member = _make_user(db, "frank@example.com")

    org_membership_repo.get_or_create(db, org_id=org.id, user_id=admin.id, role="admin")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=member.id, role="member")

    admin_membership = _membership_row(db, org.id, admin.id)
    member_membership = _membership_row(db, org.id, member.id)
    assert admin_membership.tenant_id == member_membership.tenant_id
