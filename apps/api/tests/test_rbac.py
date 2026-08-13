"""Tests for src.core.rbac's tenant-context session-variable wiring (issue #190, PR 5a).

require_org_role's HTTP-level behavior (404/403 on missing org/membership) is already
covered by router tests (e.g. test_invitations.py) that exercise it un-mocked through a
real request. These tests focus on the SET app.tenant_id / app.user_id side effect, which
router tests never assert on directly."""

from fastapi import HTTPException
from sqlalchemy import text
import pytest

from src.core.auth import UserOut
from src.core.db import User
from src.core.rbac import require_org_role, require_personal_tenant
from src.repositories import org_membership_repo, org_repo


def _make_user(db, email: str) -> User:
    user = User(email=email, name=None, password_hash=None, is_workspace_admin=False)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _current_setting(db, name: str) -> str | None:
    value = db.execute(text("SELECT current_setting(:name, true)"), {"name": name}).scalar()
    return value or None


def test_require_org_role_sets_tenant_and_user_session_vars(db):
    org = org_repo.get_or_create(db, github_login="acme")
    user = _make_user(db, "alice@example.com")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=user.id, role="admin")
    user_out = UserOut(id=user.id, email=user.email, name=user.name, is_workspace_admin=False)

    dependency = require_org_role("member")
    ctx = dependency(org_login="acme", db=db, user=user_out)

    assert ctx.org.id == org.id
    assert _current_setting(db, "app.tenant_id") == str(org.tenant_id)
    assert _current_setting(db, "app.user_id") == str(user.id)


def test_require_org_role_does_not_set_session_vars_on_403(db):
    org_repo.get_or_create(db, github_login="acme")
    user = _make_user(db, "bob@example.com")  # no membership
    user_out = UserOut(id=user.id, email=user.email, name=user.name, is_workspace_admin=False)

    dependency = require_org_role("member")
    with pytest.raises(HTTPException) as exc_info:
        dependency(org_login="acme", db=db, user=user_out)

    assert exc_info.value.status_code == 403
    assert _current_setting(db, "app.tenant_id") is None


def test_require_personal_tenant_sets_session_vars(db):
    user = _make_user(db, "carol@example.com")
    user_out = UserOut(id=user.id, email=user.email, name=user.name, is_workspace_admin=False)

    ctx = require_personal_tenant(db=db, user=user_out)

    assert ctx.tenant.kind == "personal"
    assert ctx.tenant.personal_user_id == user.id
    assert _current_setting(db, "app.tenant_id") == str(ctx.tenant.id)
    assert _current_setting(db, "app.user_id") == str(user.id)


def test_require_personal_tenant_is_idempotent(db):
    user = _make_user(db, "dave@example.com")
    user_out = UserOut(id=user.id, email=user.email, name=user.name, is_workspace_admin=False)

    first = require_personal_tenant(db=db, user=user_out)
    second = require_personal_tenant(db=db, user=user_out)

    assert second.tenant.id == first.tenant.id
