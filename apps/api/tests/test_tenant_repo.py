"""Tests for src.repositories.tenant_repo."""

from unittest.mock import patch

from sqlalchemy.orm import Query

from src.core.db import Membership, User
from src.repositories import tenant_repo


def _make_user(db, email: str) -> User:
    user = User(email=email, name=None, password_hash=None, is_workspace_admin=False)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _acme_org_id(db) -> int:
    # org_repo.get_or_create already dual-writes a tenant -- create the org row directly
    # instead so these tests exercise tenant_repo's own get-or-create behavior in isolation.
    from src.core.db import Org

    org = Org(github_login="acme")
    db.add(org)
    db.commit()
    db.refresh(org)
    return org.id


# ── get_or_create_org_tenant ─────────────────────────────────────────────────

def test_get_or_create_org_tenant_creates_new(db):
    org_id = _acme_org_id(db)
    tenant = tenant_repo.get_or_create_org_tenant(db, org_id)
    assert tenant.kind == "org"
    assert tenant.org_id == org_id


def test_get_or_create_org_tenant_idempotent(db):
    org_id = _acme_org_id(db)
    first = tenant_repo.get_or_create_org_tenant(db, org_id)
    second = tenant_repo.get_or_create_org_tenant(db, org_id)
    assert second.id == first.id


def test_get_or_create_org_tenant_falls_back_on_concurrent_insert_race(db):
    org_id = _acme_org_id(db)
    existing = tenant_repo.get_or_create_org_tenant(db, org_id)

    original_first = Query.first
    calls = {"n": 0}

    def racy_first(self):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return original_first(self)

    with patch.object(Query, "first", racy_first):
        result = tenant_repo.get_or_create_org_tenant(db, org_id)

    assert result.id == existing.id


# ── ensure_personal_tenant ────────────────────────────────────────────────────

def test_ensure_personal_tenant_creates_tenant_and_self_membership(db):
    user = _make_user(db, "alice@example.com")

    tenant = tenant_repo.ensure_personal_tenant(db, user.id)

    assert tenant.kind == "personal"
    assert tenant.personal_user_id == user.id
    membership = (
        db.query(Membership).filter(Membership.tenant_id == tenant.id, Membership.user_id == user.id).first()
    )
    assert membership is not None
    assert membership.role == "admin"


def test_ensure_personal_tenant_idempotent(db):
    user = _make_user(db, "bob@example.com")

    first = tenant_repo.ensure_personal_tenant(db, user.id)
    second = tenant_repo.ensure_personal_tenant(db, user.id)

    assert second.id == first.id
    memberships = db.query(Membership).filter(Membership.tenant_id == first.id, Membership.user_id == user.id).all()
    assert len(memberships) == 1


def test_ensure_personal_tenant_falls_back_on_concurrent_insert_race(db):
    user = _make_user(db, "carol@example.com")
    existing = tenant_repo.ensure_personal_tenant(db, user.id)

    original_first = Query.first
    calls = {"n": 0}

    def racy_first(self):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return original_first(self)

    with patch.object(Query, "first", racy_first):
        result = tenant_repo.ensure_personal_tenant(db, user.id)

    assert result.id == existing.id


# ── get_or_create_membership / update_membership_role / delete_membership ────

def test_get_or_create_membership_creates_new(db):
    org_id = _acme_org_id(db)
    tenant = tenant_repo.get_or_create_org_tenant(db, org_id)
    user = _make_user(db, "dave@example.com")

    membership = tenant_repo.get_or_create_membership(db, tenant_id=tenant.id, user_id=user.id, role="member")

    assert membership.tenant_id == tenant.id
    assert membership.user_id == user.id
    assert membership.role == "member"


def test_get_or_create_membership_idempotent(db):
    org_id = _acme_org_id(db)
    tenant = tenant_repo.get_or_create_org_tenant(db, org_id)
    user = _make_user(db, "erin@example.com")

    first = tenant_repo.get_or_create_membership(db, tenant_id=tenant.id, user_id=user.id, role="member")
    second = tenant_repo.get_or_create_membership(db, tenant_id=tenant.id, user_id=user.id, role="member")

    assert second.id == first.id


def test_update_membership_role_updates_existing(db):
    org_id = _acme_org_id(db)
    tenant = tenant_repo.get_or_create_org_tenant(db, org_id)
    user = _make_user(db, "frank@example.com")
    tenant_repo.get_or_create_membership(db, tenant_id=tenant.id, user_id=user.id, role="member")

    updated = tenant_repo.update_membership_role(db, tenant_id=tenant.id, user_id=user.id, role="admin")

    assert updated.role == "admin"


def test_update_membership_role_returns_none_when_missing(db):
    org_id = _acme_org_id(db)
    tenant = tenant_repo.get_or_create_org_tenant(db, org_id)
    user = _make_user(db, "grace@example.com")

    result = tenant_repo.update_membership_role(db, tenant_id=tenant.id, user_id=user.id, role="admin")

    assert result is None


def test_delete_membership_removes_row(db):
    org_id = _acme_org_id(db)
    tenant = tenant_repo.get_or_create_org_tenant(db, org_id)
    user = _make_user(db, "heidi@example.com")
    tenant_repo.get_or_create_membership(db, tenant_id=tenant.id, user_id=user.id, role="member")

    tenant_repo.delete_membership(db, tenant_id=tenant.id, user_id=user.id)

    remaining = (
        db.query(Membership).filter(Membership.tenant_id == tenant.id, Membership.user_id == user.id).first()
    )
    assert remaining is None


def test_delete_membership_is_a_noop_when_missing(db):
    org_id = _acme_org_id(db)
    tenant = tenant_repo.get_or_create_org_tenant(db, org_id)
    user = _make_user(db, "ivan@example.com")

    tenant_repo.delete_membership(db, tenant_id=tenant.id, user_id=user.id)  # must not raise
