"""Tests for src.repositories.org_membership_repo's Membership dual-write (issue #190 PR 4):
every OrgMembership create/update/delete must be mirrored onto the tenants/memberships
tables, keyed off the org's tenant."""

import threading
from unittest.mock import patch

from sqlalchemy import text

from src.core.db import Membership, Org, OrgMembership, SessionLocal, Tenant, User
from src.repositories import org_membership_repo, org_repo

# The 3 concurrency tests below were previously xfail (issue #330/#334): tenant_repo's
# mirror-sync helpers each committed internally, which released org_membership_repo's outer
# FOR UPDATE lock mid-operation and let a concurrent delete() interleave -- reliably so
# under the RLS-forced clevis_api role's extra round-trips. Fixed in #334: the helpers now
# take commit=False and flush into the caller's transaction, so the lock is held for the
# whole logical operation until org_membership_repo's single commit.


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
        # session_a's SET LOCAL app.user_id was cleared by the commit inside the operation
        # under test; re-establish a tenant context so this assertion query can actually
        # see memberships rows (FORCE row-level security, migration 0031) rather than
        # getting an RLS-filtered empty result and passing vacuously.
        session_a.execute(text(f"SET LOCAL app.tenant_id = {tenant.id}"))
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
            # memberships has FORCE row-level security (migration 0031); under the
            # non-superuser clevis_api role this bulk delete matches nothing unless a
            # tenant session context is set first.
            session_a.execute(text(f"SET LOCAL app.tenant_id = {tenant.id}"))
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


def test_delete_blocks_a_concurrent_get_or_create_until_the_mirror_delete_commits():
    """Regression test for a code-review finding on #324: delete()'s original two-phase
    commit (commit the OrgMembership delete, *then* delete the mirror) released its row
    lock before the mirror was touched. A concurrent get_or_create() could see the row
    already gone, legitimately re-create a fresh OrgMembership + mirror, and then have
    delete()'s now-unblocked mirror deletion remove that brand-new mirror out from under
    it -- membership drift from the opposite direction of the get_or_create/update_role
    race fixed above. Needs two genuinely separate connections, same reasoning as above."""
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
    original_delete_membership = org_membership_repo.tenant_repo.delete_membership

    def paused_delete_membership(db, tenant_id, user_id, *, commit=True):
        reached_lock.set()
        assert release_lock.wait(timeout=5), "test setup never released the paused delete"
        original_delete_membership(db, tenant_id=tenant_id, user_id=user_id, commit=commit)

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
        with patch.object(org_membership_repo.tenant_repo, "delete_membership", paused_delete_membership):
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
        # re-creating the membership -- its mirror must exist and match, not be missing
        # (which is what the pre-fix ordering would have left behind).
        remaining = (
            session_a.query(OrgMembership)
            .filter(OrgMembership.org_id == org_id, OrgMembership.user_id == user_id)
            .first()
        )
        assert remaining is not None
        tenant = session_a.query(Tenant).filter(Tenant.kind == "org", Tenant.org_id == org_id).first()
        # session_a's SET LOCAL app.user_id was cleared by the commit inside the operation
        # under test; re-establish a tenant context so this assertion query can actually
        # see memberships rows (FORCE row-level security, migration 0031) rather than
        # getting an RLS-filtered empty result and passing vacuously.
        session_a.execute(text(f"SET LOCAL app.tenant_id = {tenant.id}"))
        mirror = (
            session_a.query(Membership).filter(Membership.tenant_id == tenant.id, Membership.user_id == user_id).first()
        )
        assert mirror is not None
        assert mirror.role == remaining.role
    finally:
        session_a.rollback()
        session_a.query(OrgMembership).filter(OrgMembership.org_id == org_id).delete()
        tenant = session_a.query(Tenant).filter(Tenant.kind == "org", Tenant.org_id == org_id).first()
        if tenant is not None:
            # memberships has FORCE row-level security (migration 0031); under the
            # non-superuser clevis_api role this bulk delete matches nothing unless a
            # tenant session context is set first.
            session_a.execute(text(f"SET LOCAL app.tenant_id = {tenant.id}"))
            session_a.query(Membership).filter(Membership.tenant_id == tenant.id).delete()
            org_row = session_a.query(Org).filter(Org.id == org_id).first()
            if org_row is not None:
                org_row.tenant_id = None
                session_a.commit()
            session_a.query(Tenant).filter(Tenant.id == tenant.id).delete()
        session_a.query(Org).filter(Org.id == org_id).delete()
        session_a.query(User).filter(User.id == user_id).delete()
        session_a.commit()
        session_a.close()


def test_get_or_create_blocks_a_concurrent_delete_until_the_new_memberships_mirror_sync_commits():
    """Regression test for a CodeRabbit finding on #323's 2nd review: get_or_create's
    new-row path used to commit the freshly-inserted OrgMembership, then sync its mirror
    with no lock held across that gap. A concurrent delete() landing in the gap would
    remove the just-created row and find no mirror yet, then have this call resume and
    create a mirror for a membership that's already revoked. Fixed by re-locking the row
    immediately after the insert commits, before syncing -- same lock-then-sync ordering
    as update_role's fix. Needs two genuinely separate connections, same reasoning as the
    other concurrency tests in this file (the race only exists across real transactions)."""
    setup = SessionLocal()
    org = org_repo.get_or_create(setup, github_login="acme-lock-test-3")
    user = User(email="judy@example.com", name=None, password_hash=None, is_workspace_admin=False)
    setup.add(user)
    setup.commit()
    setup.refresh(user)
    org_id, user_id = org.id, user.id
    setup.close()
    # No get_or_create() call yet for (org_id, user_id) -- the pair doesn't exist, so the
    # first call below goes down the new-row insert path, not the early-return path.

    reached_lock = threading.Event()
    delete_started = threading.Event()
    release_lock = threading.Event()
    original_sync = org_membership_repo._sync_membership_mirror

    def paused_sync(db, org_id, membership):
        reached_lock.set()
        assert release_lock.wait(timeout=5), "test setup never released the paused sync"
        original_sync(db, org_id, membership)

    create_result: dict[str, bool] = {}
    delete_result: dict[str, bool] = {}

    def run_get_or_create(session_a):
        org_membership_repo.get_or_create(session_a, org_id=org_id, user_id=user_id, role="member")
        create_result["finished"] = True

    def run_delete():
        session_b = SessionLocal()
        try:
            assert reached_lock.wait(timeout=5), "get_or_create never reached its locked section"
            delete_started.set()
            org_membership_repo.delete(session_b, org_id=org_id, user_id=user_id)
            delete_result["finished"] = True
        finally:
            session_b.close()

    session_a = SessionLocal()
    try:
        with patch.object(org_membership_repo, "_sync_membership_mirror", paused_sync):
            create_thread = threading.Thread(target=run_get_or_create, args=(session_a,))
            create_thread.start()
            assert reached_lock.wait(timeout=5), "get_or_create never reached its locked section"

            delete_thread = threading.Thread(target=run_delete)
            delete_thread.start()
            # Wait for the delete thread to actually reach delete() (not just start()) before
            # checking it's blocked -- otherwise a slow scheduler could let the 0.3s timeout
            # below expire before run_delete has even called delete(), passing "not
            # delete_result" for the wrong reason instead of proving the lock blocked it.
            assert delete_started.wait(timeout=5), "delete thread never reached delete()"
            delete_thread.join(timeout=0.3)
            assert not delete_result, "delete() must block while get_or_create holds the row lock"

            release_lock.set()
            create_thread.join(timeout=5)
            delete_thread.join(timeout=5)
        assert create_result.get("finished") is True
        assert delete_result.get("finished") is True

        # The delete landed after get_or_create's sync committed the mirror, so the final
        # state must reflect the delete -- no leftover membership or mirror.
        remaining = (
            session_a.query(OrgMembership)
            .filter(OrgMembership.org_id == org_id, OrgMembership.user_id == user_id)
            .first()
        )
        assert remaining is None
        tenant = session_a.query(Tenant).filter(Tenant.kind == "org", Tenant.org_id == org_id).first()
        # session_a's SET LOCAL app.user_id was cleared by the commit inside the operation
        # under test; re-establish a tenant context so this assertion query can actually
        # see memberships rows (FORCE row-level security, migration 0031) rather than
        # getting an RLS-filtered empty result and passing vacuously.
        session_a.execute(text(f"SET LOCAL app.tenant_id = {tenant.id}"))
        mirror = (
            session_a.query(Membership).filter(Membership.tenant_id == tenant.id, Membership.user_id == user_id).first()
        )
        assert mirror is None
    finally:
        session_a.rollback()
        session_a.query(OrgMembership).filter(OrgMembership.org_id == org_id).delete()
        tenant = session_a.query(Tenant).filter(Tenant.kind == "org", Tenant.org_id == org_id).first()
        if tenant is not None:
            # memberships has FORCE row-level security (migration 0031); under the
            # non-superuser clevis_api role this bulk delete matches nothing unless a
            # tenant session context is set first.
            session_a.execute(text(f"SET LOCAL app.tenant_id = {tenant.id}"))
            session_a.query(Membership).filter(Membership.tenant_id == tenant.id).delete()
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
