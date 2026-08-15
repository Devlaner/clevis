"""Tests for src.services.org_provisioning's GitHub-admin auto-grant logic."""

from unittest.mock import patch

from src.core.db import User
from src.repositories import org_membership_repo, org_repo
from src.services import github_oauth, org_provisioning


def _make_user(db, email: str) -> User:
    user = User(email=email, name=None, password_hash=None, is_workspace_admin=False)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_first_connector_becomes_admin(db):
    user = _make_user(db, "alice@example.com")

    with patch.object(
        github_oauth,
        "list_user_org_memberships",
        return_value=[github_oauth.GitHubOrgMembership(github_org_id=1, login="acme", role="admin")],
    ):
        org_provisioning.sync_org_admin_memberships(db, user, "fake-token")

    org = org_repo.get_by_login(db, "acme")
    assert org is not None
    membership = org_membership_repo.get(db, org_id=org.id, user_id=user.id)
    assert membership is not None
    assert membership.role == "admin"


def test_second_verified_admin_auto_joins_existing_org(db):
    org = org_repo.get_or_create(db, github_login="acme")
    existing_admin = _make_user(db, "alice@example.com")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=existing_admin.id, role="admin")

    carol = _make_user(db, "carol@example.com")
    with patch.object(
        github_oauth,
        "list_user_org_memberships",
        return_value=[github_oauth.GitHubOrgMembership(github_org_id=1, login="acme", role="admin")],
    ):
        org_provisioning.sync_org_admin_memberships(db, carol, "fake-token")

    membership = org_membership_repo.get(db, org_id=org.id, user_id=carol.id)
    assert membership is not None
    assert membership.role == "admin"


def test_non_admin_github_member_not_auto_added(db):
    dave = _make_user(db, "dave@example.com")

    with patch.object(
        github_oauth,
        "list_user_org_memberships",
        return_value=[github_oauth.GitHubOrgMembership(github_org_id=1, login="acme", role="member")],
    ):
        org_provisioning.sync_org_admin_memberships(db, dave, "fake-token")

    org = org_repo.get_by_login(db, "acme")
    assert org is None  # never created — no admin ever connected it


def test_github_api_failure_is_swallowed(db):
    import httpx

    erin = _make_user(db, "erin@example.com")
    with patch.object(github_oauth, "list_user_org_memberships", side_effect=httpx.ConnectError("boom")):
        org_provisioning.sync_org_admin_memberships(db, erin, "fake-token")  # must not raise


def test_demoted_github_admin_loses_clevis_admin_role(db):
    org = org_repo.get_or_create(db, github_login="acme", github_org_id=1)
    frank = _make_user(db, "frank@example.com")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=frank.id, role="admin")

    with patch.object(
        github_oauth,
        "list_user_org_memberships",
        return_value=[github_oauth.GitHubOrgMembership(github_org_id=1, login="acme", role="member")],
    ):
        org_provisioning.sync_org_admin_memberships(db, frank, "fake-token")

    membership = org_membership_repo.get(db, org_id=org.id, user_id=frank.id)
    assert membership is not None
    assert membership.role == "member"


def test_removed_github_member_loses_clevis_membership_entirely(db):
    org = org_repo.get_or_create(db, github_login="acme", github_org_id=1)
    grace = _make_user(db, "grace@example.com")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=grace.id, role="admin")

    with patch.object(github_oauth, "list_user_org_memberships", return_value=[]):
        org_provisioning.sync_org_admin_memberships(db, grace, "fake-token")

    membership = org_membership_repo.get(db, org_id=org.id, user_id=grace.id)
    assert membership is None


def test_unrelated_org_membership_untouched_when_other_org_reconciled(db):
    # Issue #330: capture each org's id right after creation -- creating a later org
    # commits its own tenant-link UPDATE, which expires SQLAlchemy's in-memory objects for
    # earlier orgs (default expire_on_commit=True); a later lazy-reload of one under RLS
    # with app.tenant_id now pointed at a different org's tenant would raise
    # ObjectDeletedError instead of finding the (still-present) row.
    acme = org_repo.get_or_create(db, github_login="acme", github_org_id=1)
    acme_id = acme.id
    globex = org_repo.get_or_create(db, github_login="globex", github_org_id=2)
    globex_id = globex.id
    heidi = _make_user(db, "heidi@example.com")
    org_membership_repo.get_or_create(db, org_id=acme_id, user_id=heidi.id, role="admin")
    org_membership_repo.get_or_create(db, org_id=globex_id, user_id=heidi.id, role="admin")

    with patch.object(
        github_oauth,
        "list_user_org_memberships",
        return_value=[
            github_oauth.GitHubOrgMembership(github_org_id=1, login="acme", role="admin"),
            github_oauth.GitHubOrgMembership(github_org_id=2, login="globex", role="member"),
        ],
    ):
        org_provisioning.sync_org_admin_memberships(db, heidi, "fake-token")

    acme_membership = org_membership_repo.get(db, org_id=acme_id, user_id=heidi.id)
    globex_membership = org_membership_repo.get(db, org_id=globex_id, user_id=heidi.id)
    assert acme_membership.role == "admin"
    assert globex_membership.role == "member"


def test_stale_clevis_membership_untouched_on_github_api_failure(db):
    import httpx

    org = org_repo.get_or_create(db, github_login="acme", github_org_id=1)
    ivan = _make_user(db, "ivan@example.com")
    org_membership_repo.get_or_create(db, org_id=org.id, user_id=ivan.id, role="admin")

    with patch.object(github_oauth, "list_user_org_memberships", side_effect=httpx.ConnectError("boom")):
        org_provisioning.sync_org_admin_memberships(db, ivan, "fake-token")

    membership = org_membership_repo.get(db, org_id=org.id, user_id=ivan.id)
    assert membership is not None
    assert membership.role == "admin"
