"""Tests for src.repositories.installation_repo.upsert, focused on the concurrent-sync
race path (see apps/api/tests/test_installations.py for router-level coverage)."""

from unittest.mock import patch

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Query

from src.repositories import installation_repo, org_repo


def _acme_org_id(db) -> int:
    # github_installations.org_id has a foreign key to orgs.id -- needs a real row.
    return org_repo.get_or_create(db, github_login="acme").id


def test_upsert_creates_new_row(db):
    org_id = _acme_org_id(db)
    row = installation_repo.upsert(
        db, account_login="acme", account_type="Organization", auth_mode="app", installation_id=1, org_id=org_id
    )
    assert row.account_login == "acme"
    assert row.installation_id == 1


def test_upsert_sets_tenant_id_for_org_scoped_installation(db):
    from src.core.db import Tenant

    org_id = _acme_org_id(db)
    row = installation_repo.upsert(
        db, account_login="acme", account_type="Organization", auth_mode="app", installation_id=1, org_id=org_id
    )
    assert row.tenant_id is not None
    tenant = db.query(Tenant).filter(Tenant.id == row.tenant_id).first()
    assert tenant.kind == "org"
    assert tenant.org_id == org_id


def test_upsert_sets_tenant_id_for_personal_installation(db):
    from src.core.db import Tenant, User

    user = User(email="dev2@example.com", name=None, password_hash=None, is_workspace_admin=False)
    db.add(user)
    db.commit()
    db.refresh(user)

    row = installation_repo.upsert(
        db, account_login="octocat", account_type="User", auth_mode="app", installation_id=7, owner_user_id=user.id
    )

    assert row.tenant_id is not None
    tenant = db.query(Tenant).filter(Tenant.id == row.tenant_id).first()
    assert tenant.kind == "personal"
    assert tenant.personal_user_id == user.id


def test_upsert_update_branch_backfills_tenant_id(db):
    org_id = _acme_org_id(db)
    installation_repo.upsert(
        db, account_login="acme", account_type="Organization", auth_mode="app", installation_id=1, org_id=org_id
    )
    updated = installation_repo.upsert(
        db, account_login="acme", account_type="Organization", auth_mode="app", installation_id=2, org_id=org_id
    )
    assert updated.tenant_id is not None


def test_upsert_updates_existing_row(db):
    org_id = _acme_org_id(db)
    installation_repo.upsert(
        db, account_login="acme", account_type="Organization", auth_mode="app", installation_id=1, org_id=org_id
    )
    updated = installation_repo.upsert(
        db, account_login="acme", account_type="Organization", auth_mode="app", installation_id=2, org_id=org_id
    )
    assert updated.installation_id == 2


def test_upsert_falls_back_to_update_on_concurrent_sync_race(db):
    # Simulates two near-simultaneous syncs for the same org/account: this call's initial
    # existence check misses (the other request's row hasn't committed yet from this call's
    # point of view), so it attempts an insert -- which collides on the real unique
    # constraint once the other request's commit has actually landed. Must recover by
    # updating the row that's actually there, not raise an unhandled IntegrityError.
    org_id = _acme_org_id(db)
    existing = installation_repo.upsert(
        db, account_login="acme", account_type="Organization", auth_mode="app", installation_id=1, org_id=org_id
    )

    original_first = Query.first
    calls = {"n": 0}

    def racy_first(self):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return original_first(self)

    with patch.object(Query, "first", racy_first):
        result = installation_repo.upsert(
            db, account_login="acme", account_type="Organization", auth_mode="app", installation_id=2, org_id=org_id
        )

    assert result.id == existing.id
    assert result.installation_id == 2


def test_upsert_reraises_when_the_integrity_error_was_not_actually_a_race(db):
    # A different IntegrityError (here: a foreign-key violation from a nonexistent org_id)
    # also lands in the except block, but the re-query genuinely finds nothing to fall back
    # to (no row was ever inserted) -- must re-raise rather than silently swallow it.
    with pytest.raises(IntegrityError):
        installation_repo.upsert(
            db,
            account_login="acme",
            account_type="Organization",
            auth_mode="app",
            installation_id=1,
            org_id=999999,
        )


def test_get_for_org_matches_regardless_of_login_casing(db):
    # account_login is stored verbatim from GitHub's install payload; RBAC/ownership checks
    # elsewhere (assert_owner_matches_org, _verify_installation) already compare logins
    # case-insensitively, so this lookup must too -- otherwise a case mismatch (e.g. a
    # renamed org, or a caller passing different casing) passes those checks but fails to
    # find an installation that's actually there (#246).
    org_id = _acme_org_id(db)
    installation_repo.upsert(
        db, account_login="Acme", account_type="Organization", auth_mode="app", installation_id=1, org_id=org_id
    )

    found = installation_repo.get_for_org(db, org_id=org_id, account_login="acme")

    assert found is not None
    assert found.installation_id == 1


def test_get_for_user_matches_regardless_of_login_casing(db):
    from src.core.db import User

    user = User(email="dev@example.com", name=None, password_hash=None, is_workspace_admin=False)
    db.add(user)
    db.commit()
    db.refresh(user)
    installation_repo.upsert(
        db,
        account_login="Octocat",
        account_type="User",
        auth_mode="app",
        installation_id=7,
        owner_user_id=user.id,
    )

    found = installation_repo.get_for_user(db, owner_user_id=user.id, account_login="octocat")

    assert found is not None
    assert found.installation_id == 7
