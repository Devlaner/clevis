"""Tests for src.repositories.org_membership_repo.

Since #331 dropped the legacy org_memberships table, this module is a thin org_id->
tenant_id adapter over tenant_repo: every create/update/delete lands in the tenant-scoped
`memberships` table, keyed off the org's tenant. The write paths still take a
SELECT ... FOR UPDATE on the memberships row and commit once (issue #334), so a concurrent
grant and revoke for the same (org, user) stay serialized.
"""

import threading
from unittest.mock import patch

from sqlalchemy import text

from src.core.db import Membership, Org, SessionLocal, Tenant, User
from src.repositories import org_membership_repo, org_repo, tenant_repo


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


def test_get_or_create_writes_the_membership_row(db):
    org = org_repo.get_or_create(db, github_login="acme")
    user = _make_user(db, "alice@example.com")

    org_membership_repo.get_or_create(db, org_id=org.id, user_id=user.id, role="admin")

    membership = _membership_row(db, org.id, user.id)
    assert membership is not None
    assert membership.role == "admin"


def test_get_or_create_is_idempotent(db):
    org = org_repo.get_or_create(db, github_login="acme")
    user = _make_user(db, "bob@example.com")

    org_membership_repo.get_or_create(db, org_id=org.id, user_id=user.id, role="member")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=user.id, role="member")

    tenant = db.query(Tenant).filter(Tenant.kind == "org", Tenant.org_id == org.id).first()
    rows = db.query(Membership).filter(Membership.tenant_id == tenant.id, Membership.user_id == user.id).all()
    assert len(rows) == 1


def test_get_returns_none_for_an_org_with_no_tenant(db):
    # A read for an org that was never provisioned must not create the tenant as a side effect.
    org = Org(github_login="unprovisioned")
    db.add(org)
    db.commit()
    db.refresh(org)

    assert org_membership_repo.get(db, org_id=org.id, user_id=1) is None
    assert db.query(Tenant).filter(Tenant.org_id == org.id).first() is None


def test_update_role_changes_the_membership_role(db):
    org = org_repo.get_or_create(db, github_login="acme")
    user = _make_user(db, "carol@example.com")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=user.id, role="member")

    org_membership_repo.update_role(db, org_id=org.id, user_id=user.id, role="admin")

    membership = _membership_row(db, org.id, user.id)
    assert membership is not None
    assert membership.role == "admin"


def test_update_role_returns_none_when_there_is_no_membership(db):
    org = org_repo.get_or_create(db, github_login="acme")
    user = _make_user(db, "wanda@example.com")

    assert org_membership_repo.update_role(db, org_id=org.id, user_id=user.id, role="admin") is None


def test_update_role_returns_none_for_an_unprovisioned_org(db):
    org = Org(github_login="unprovisioned-3")
    db.add(org)
    db.commit()
    db.refresh(org)

    assert org_membership_repo.update_role(db, org_id=org.id, user_id=1, role="admin") is None
    assert db.query(Tenant).filter(Tenant.org_id == org.id).first() is None


def test_delete_removes_the_membership_row(db):
    org = org_repo.get_or_create(db, github_login="acme")
    user = _make_user(db, "dave@example.com")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=user.id, role="admin")

    org_membership_repo.delete(db, org_id=org.id, user_id=user.id)

    assert _membership_row(db, org.id, user.id) is None


def test_delete_is_a_noop_for_an_unprovisioned_org(db):
    org = Org(github_login="unprovisioned-2")
    db.add(org)
    db.commit()
    db.refresh(org)

    org_membership_repo.delete(db, org_id=org.id, user_id=1)  # must not raise
    assert db.query(Tenant).filter(Tenant.org_id == org.id).first() is None


def test_get_or_create_recreates_a_row_deleted_out_of_band(db):
    # org_provisioning.py's reconcile loop calls get_or_create on every login; if the
    # membership row went missing out of band it must be recreated, not skipped.
    org = org_repo.get_or_create(db, github_login="acme")
    user = _make_user(db, "grace@example.com")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=user.id, role="member")
    tenant = db.query(Tenant).filter(Tenant.kind == "org", Tenant.org_id == org.id).first()
    db.query(Membership).filter(Membership.tenant_id == tenant.id, Membership.user_id == user.id).delete()
    db.commit()
    assert _membership_row(db, org.id, user.id) is None

    org_membership_repo.get_or_create(db, org_id=org.id, user_id=user.id, role="member")

    membership = _membership_row(db, org.id, user.id)
    assert membership is not None
    assert membership.role == "member"


def test_get_or_create_repairs_a_stale_role(db):
    org = org_repo.get_or_create(db, github_login="acme")
    user = _make_user(db, "heidi@example.com")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=user.id, role="member")
    tenant = db.query(Tenant).filter(Tenant.kind == "org", Tenant.org_id == org.id).first()
    stale = db.query(Membership).filter(Membership.tenant_id == tenant.id, Membership.user_id == user.id).first()
    stale.role = "admin"
    db.commit()

    org_membership_repo.get_or_create(db, org_id=org.id, user_id=user.id, role="member")

    assert _membership_row(db, org.id, user.id).role == "member"


def test_dual_write_uses_the_same_tenant_for_every_membership_in_an_org(db):
    org = org_repo.get_or_create(db, github_login="acme")
    admin = _make_user(db, "erin@example.com")
    member = _make_user(db, "frank@example.com")

    org_membership_repo.get_or_create(db, org_id=org.id, user_id=admin.id, role="admin")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=member.id, role="member")

    assert _membership_row(db, org.id, admin.id).tenant_id == _membership_row(db, org.id, member.id).tenant_id


# ── Concurrency: the FOR UPDATE lock serializes a grant against a concurrent revoke ──
#
# These use two or three genuinely separate SessionLocal() connections (not the
# savepoint-per-test `db` fixture) because the race only exists across real concurrent
# transactions. They pause a tenant_repo helper *inside* org_membership_repo's locked
# section (after get_membership(for_update=True), before the single commit) and assert a
# concurrent delete()/get_or_create() blocks on the row until that commit lands.


def _cleanup(session, org_id: int, user_id: int) -> None:
    session.rollback()
    tenant = session.query(Tenant).filter(Tenant.kind == "org", Tenant.org_id == org_id).first()
    if tenant is not None:
        # memberships has FORCE row-level security (migration 0031); under the non-superuser
        # clevis_api role this bulk delete matches nothing unless a tenant context is set.
        # SET LOCAL, not SET: transaction-scoped so it can't leak onto the pooled connection.
        session.execute(text(f"SET LOCAL app.tenant_id = {tenant.id}"))
        session.query(Membership).filter(Membership.tenant_id == tenant.id).delete()
        org_row = session.query(Org).filter(Org.id == org_id).first()
        if org_row is not None:
            # orgs.tenant_id and tenants.org_id reference each other (composite reciprocal
            # FK) -- null the org side first so either row can then be deleted freely.
            org_row.tenant_id = None
            session.commit()
        session.query(Tenant).filter(Tenant.id == tenant.id).delete()
    session.query(Org).filter(Org.id == org_id).delete()
    session.query(User).filter(User.id == user_id).delete()
    session.commit()
    session.close()


def _membership_after(session, org_id: int, user_id: int) -> Membership | None:
    tenant = session.query(Tenant).filter(Tenant.kind == "org", Tenant.org_id == org_id).first()
    if tenant is None:
        return None
    # The operation under test committed and cleared session's SET LOCAL app.user_id;
    # re-establish a context so this assertion isn't RLS-filtered to a vacuous None.
    session.execute(text(f"SET LOCAL app.tenant_id = {tenant.id}"))
    return (
        session.query(Membership)
        .filter(Membership.tenant_id == tenant.id, Membership.user_id == user_id)
        .first()
    )


def test_update_role_blocks_a_concurrent_delete_until_it_commits():
    setup = SessionLocal()
    org = org_repo.get_or_create(setup, github_login="acme-lock-test")
    user = User(email="ivy@example.com", name=None, password_hash=None, is_workspace_admin=False)
    setup.add(user)
    setup.commit()
    setup.refresh(user)
    org_id, user_id = org.id, user.id
    org_membership_repo.get_or_create(setup, org_id=org_id, user_id=user_id, role="member")
    setup.close()

    reached_lock = threading.Event()
    release_lock = threading.Event()
    original = tenant_repo.update_membership_role

    def paused(db, tenant_id, user_id, role, *, commit=True):
        reached_lock.set()
        assert release_lock.wait(timeout=5), "test setup never released the paused update"
        return original(db, tenant_id, user_id, role, commit=commit)

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
        with patch.object(tenant_repo, "update_membership_role", paused):
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

        # The delete landed after update_role's commit released the lock -- final state
        # must reflect the delete.
        assert _membership_after(session_a, org_id, user_id) is None
    finally:
        _cleanup(session_a, org_id, user_id)


def test_delete_blocks_a_concurrent_get_or_create_until_it_commits():
    setup = SessionLocal()
    org = org_repo.get_or_create(setup, github_login="acme-lock-test-2")
    user = User(email="mallory@example.com", name=None, password_hash=None, is_workspace_admin=False)
    setup.add(user)
    setup.commit()
    setup.refresh(user)
    org_id, user_id = org.id, user.id
    org_membership_repo.get_or_create(setup, org_id=org_id, user_id=user_id, role="member")
    setup.close()

    reached_lock = threading.Event()
    release_lock = threading.Event()
    original = tenant_repo.delete_membership

    def paused(db, tenant_id, user_id, *, commit=True):
        reached_lock.set()
        assert release_lock.wait(timeout=5), "test setup never released the paused delete"
        original(db, tenant_id=tenant_id, user_id=user_id, commit=commit)

    delete_result: dict[str, bool] = {}
    recreate_result: dict[str, bool] = {}

    def run_delete(session_a):
        org_membership_repo.delete(session_a, org_id=org_id, user_id=user_id)
        delete_result["finished"] = True

    def run_get_or_create():
        session_b = SessionLocal()
        try:
            assert reached_lock.wait(timeout=5), "delete() never reached its locked section"
            org_membership_repo.get_or_create(session_b, org_id=org_id, user_id=user_id, role="member")
            recreate_result["finished"] = True
        finally:
            session_b.close()

    session_a = SessionLocal()
    try:
        with patch.object(tenant_repo, "delete_membership", paused):
            delete_thread = threading.Thread(target=run_delete, args=(session_a,))
            delete_thread.start()
            assert reached_lock.wait(timeout=5), "delete() never reached its locked section"

            recreate_thread = threading.Thread(target=run_get_or_create)
            recreate_thread.start()
            recreate_thread.join(timeout=0.3)
            assert not recreate_result, "get_or_create() must block while delete() holds the row lock"

            release_lock.set()
            delete_thread.join(timeout=5)
            recreate_thread.join(timeout=5)
        assert delete_result.get("finished") is True
        assert recreate_result.get("finished") is True

        # get_or_create() ran after delete()'s commit released the lock, legitimately
        # re-creating the membership.
        recreated = _membership_after(session_a, org_id, user_id)
        assert recreated is not None
        assert recreated.role == "member"
    finally:
        _cleanup(session_a, org_id, user_id)


def test_concurrent_get_or_create_and_delete_on_a_fresh_row_end_consistently():
    # With no pre-existing row there is nothing for get_or_create to FOR UPDATE lock, so
    # it and a concurrent delete() aren't strictly serialized -- but the whole get_or_create
    # is one transaction and upsert_membership's own IntegrityError/SAVEPOINT recovery means
    # the outcome is always consistent: the row is present with the granted role, or absent.
    # Never a half-written row, never an exception. (Supersedes the pre-#331 test that relied
    # on get_or_create's now-removed commit-then-mirror-sync gap.)
    setup = SessionLocal()
    org = org_repo.get_or_create(setup, github_login="acme-lock-test-3")
    user = User(email="judy@example.com", name=None, password_hash=None, is_workspace_admin=False)
    setup.add(user)
    setup.commit()
    setup.refresh(user)
    org_id, user_id = org.id, user.id
    setup.close()

    errors: list[BaseException] = []

    def run_get_or_create():
        session = SessionLocal()
        try:
            org_membership_repo.get_or_create(session, org_id=org_id, user_id=user_id, role="member")
        except BaseException as exc:  # noqa: BLE001 -- surfaced via `errors` below
            errors.append(exc)
        finally:
            session.close()

    def run_delete():
        session = SessionLocal()
        try:
            org_membership_repo.delete(session, org_id=org_id, user_id=user_id)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            session.close()

    session_a = SessionLocal()
    try:
        t1 = threading.Thread(target=run_get_or_create)
        t2 = threading.Thread(target=run_delete)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert not errors, f"a concurrent get_or_create/delete raised: {errors}"
        final = _membership_after(session_a, org_id, user_id)
        assert final is None or final.role == "member"
    finally:
        _cleanup(session_a, org_id, user_id)
