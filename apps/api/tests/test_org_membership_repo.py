"""Tests for src.repositories.org_membership_repo's Membership dual-write (issue #190 PR 4):
every OrgMembership create/update/delete must be mirrored onto the tenants/memberships
tables, keyed off the org's tenant."""

from src.core.db import Membership, Tenant, User
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


def test_dual_write_uses_the_same_tenant_for_every_membership_in_an_org(db):
    org = org_repo.get_or_create(db, github_login="acme")
    admin = _make_user(db, "erin@example.com")
    member = _make_user(db, "frank@example.com")

    org_membership_repo.get_or_create(db, org_id=org.id, user_id=admin.id, role="admin")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=member.id, role="member")

    admin_membership = _membership_row(db, org.id, admin.id)
    member_membership = _membership_row(db, org.id, member.id)
    assert admin_membership.tenant_id == member_membership.tenant_id
