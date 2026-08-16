"""Regression test for issue #191/S3 PR 2: the GitHub webhook receiver's
installation.deleted handler was silently no-op'ing under the real, non-superuser
clevis_api role (issue #330) because it never set the app.tenant_id/app.user_id session
vars migration 0031's RLS policy on github_installations reads -- see
installation_repo.delete_by_installation_id's docstring and migration 0035's docstring
for the full explanation.

Deliberately does NOT use the shared `db` fixture (conftest.py). That fixture joins an
already-open outer transaction via SQLAlchemy's `join_transaction_mode="create_savepoint"`
-- each `Session.commit()` inside a test only releases a SAVEPOINT, it doesn't end the
real Postgres transaction. Plain `SET` (used by set_tenant_session_context) is
connection-scoped, so that's fine either way, but this test needs to prove the bug exists
with NO session context carried over from an unrelated earlier write in the same test --
using a real connection with real commits (mirroring production's one-connection-per-
request lifecycle: get_db() hands out a brand-new session per request, so a webhook
request's connection never inherits an admin request's app.tenant_id) makes that explicit
rather than relying on savepoint semantics happening to keep it isolated.

Same skip-when-unavailable convention as test_rls_isolation.py: reuses DATABASE_URL when
the suite is already running as clevis_api (CI), or swaps credentials via API_DB_PASSWORD
for an opt-in local run.
"""

import os
from urllib.parse import quote, urlsplit, urlunsplit

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from src.core.config import settings
from src.core.db import GitHubInstallation, Org, Tenant, User
from src.core.rbac import set_tenant_session_context
from src.repositories import installation_repo, org_repo


def _clevis_api_engine():
    url = urlsplit(settings.database_url.get_secret_value())
    if url.username == "clevis_api":
        return create_engine(settings.database_url.get_secret_value())

    password = os.environ.get("API_DB_PASSWORD")
    if not password:
        pytest.skip(
            "neither DATABASE_URL nor API_DB_PASSWORD point at clevis_api -- "
            "provision the role (docker/provision-api-role-existing-deployment.sh) "
            "and set API_DB_PASSWORD to exercise this test locally"
        )
    port = f":{url.port}" if url.port is not None else ""
    netloc = f"clevis_api:{quote(password, safe='')}@{url.hostname}{port}"
    return create_engine(urlunsplit((url.scheme, netloc, url.path, url.query, url.fragment)))


def test_installation_deleted_removes_the_row_with_no_session_context_set():
    engine = _clevis_api_engine()
    with engine.connect() as conn:
        session = Session(bind=conn, expire_on_commit=False)
        user = org = tenant = installation = None
        try:
            try:
                # Setup mirrors the real authenticated sync path (installations.py's
                # sync_org_installation): an admin's request resolves the org's tenant and
                # explicitly sets session context before writing github_installations,
                # exactly like migration 0031's docstring documents as the one org-scoped
                # write's fix.
                user = User(
                    email="installation-rls-fix@test.local", name=None, password_hash=None, is_workspace_admin=False
                )
                session.add(user)
                session.commit()

                org = Org(github_login="installation-rls-fix-org")
                session.add(org)
                session.commit()

                # org_repo.ensure_tenant_linked (not a direct org.tenant_id assignment):
                # orgs' WITH CHECK (migration 0033) requires app.tenant_id to already match
                # on any UPDATE, and this is the one call site allowed to set it to a
                # brand-new tenant it just created -- see that function's own docstring.
                org = org_repo.ensure_tenant_linked(session, org)
                tenant = session.query(Tenant).filter(Tenant.id == org.tenant_id).first()

                set_tenant_session_context(session, tenant.id, user.id)
                installation = installation_repo.create(
                    session,
                    account_login="installation-rls-fix-org",
                    account_type="Organization",
                    auth_mode="app",
                    installation_id=987654,
                    org_id=org.id,
                )
                assert installation.tenant_id == tenant.id

                # This is the crux of the regression: reset session context to model the
                # webhook receiver's actual runtime state -- an unauthenticated request on
                # a connection that has never called set_tenant_session_context/
                # set_session_user. Before the fix, delete_by_installation_id's own
                # SELECT/DELETE against github_installations evaluated RLS's
                # tenant_id/owner_user_id-equality policy to NULL OR NULL and silently
                # matched zero rows.
                session.execute(text("RESET app.tenant_id"))
                session.execute(text("RESET app.user_id"))

                removed, resolved_tenant_id = installation_repo.delete_by_installation_id(session, 987654)

                assert removed == 1
                assert resolved_tenant_id == tenant.id

                # Confirm it's actually gone, not just miscounted -- re-resolve via the
                # SECURITY DEFINER lookup (migration 0035), which bypasses RLS the same way
                # the fix itself does, so this assertion isn't fooled by the same bug.
                still_present = session.execute(
                    text("SELECT resolve_installation_tenant_id(:installation_id)"), {"installation_id": 987654}
                ).scalar()
                assert still_present is None
            finally:
                session.rollback()
                # Restore tenant context (if the test's own delete-under-test didn't
                # already remove the row via the fix, e.g. when run against the pre-fix
                # code) so this cleanup's own writes against RLS-protected
                # github_installations/tenants aren't blocked by the very same gap this
                # PR fixes -- deliberately using the raw SET here, not the fix's
                # SECURITY DEFINER path, since cleanup already has tenant/user in scope.
                if tenant is not None and user is not None:
                    set_tenant_session_context(session, tenant.id, user.id)
                if installation is not None:
                    session.execute(
                        text("DELETE FROM github_installations WHERE installation_id = :iid"), {"iid": 987654}
                    )
                if org is not None:
                    session.execute(text("UPDATE orgs SET tenant_id = NULL WHERE id = :id"), {"id": org.id})
                if tenant is not None:
                    session.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tenant.id})
                if org is not None:
                    session.execute(text("DELETE FROM orgs WHERE id = :id"), {"id": org.id})
                if user is not None:
                    session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user.id})
                session.execute(text("RESET app.tenant_id"))
                session.execute(text("RESET app.user_id"))
                session.commit()
        finally:
            session.close()
    engine.dispose()


def test_resolve_installation_tenant_id_bypasses_rls_with_no_session_context():
    """Narrower unit check on migration 0035's function itself, isolated from the repo-
    level delete flow above: it must resolve tenant_id purely from installation_id,
    regardless of session context, since that's precisely the pre-authentication
    condition it exists to handle."""
    engine = _clevis_api_engine()
    with engine.connect() as conn:
        session = Session(bind=conn, expire_on_commit=False)
        user = org = tenant = None
        try:
            try:
                user = User(
                    email="resolve-fn-rls-fix@test.local", name=None, password_hash=None, is_workspace_admin=False
                )
                session.add(user)
                session.commit()

                org = Org(github_login="resolve-fn-rls-fix-org")
                session.add(org)
                session.commit()

                org = org_repo.ensure_tenant_linked(session, org)
                tenant = session.query(Tenant).filter(Tenant.id == org.tenant_id).first()

                set_tenant_session_context(session, tenant.id, user.id)
                installation_repo.create(
                    session,
                    account_login="resolve-fn-rls-fix-org",
                    account_type="Organization",
                    auth_mode="app",
                    installation_id=987655,
                    org_id=org.id,
                )
                session.commit()

                session.execute(text("RESET app.tenant_id"))
                session.execute(text("RESET app.user_id"))

                # Sanity check: with no session context, a plain SELECT against the
                # RLS-protected table itself is blocked (proving this test's "no context"
                # state is real, not accidentally leaked).
                blocked = session.execute(
                    text("SELECT id FROM github_installations WHERE installation_id = :iid"), {"iid": 987655}
                ).fetchall()
                assert blocked == []

                resolved = session.execute(
                    text("SELECT resolve_installation_tenant_id(:iid)"), {"iid": 987655}
                ).scalar()
                assert resolved == tenant.id
            finally:
                session.rollback()
                if tenant is not None and user is not None:
                    set_tenant_session_context(session, tenant.id, user.id)
                session.execute(
                    text("DELETE FROM github_installations WHERE installation_id = :iid"), {"iid": 987655}
                )
                if org is not None:
                    session.execute(text("UPDATE orgs SET tenant_id = NULL WHERE id = :id"), {"id": org.id})
                if tenant is not None:
                    session.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tenant.id})
                if org is not None:
                    session.execute(text("DELETE FROM orgs WHERE id = :id"), {"id": org.id})
                if user is not None:
                    session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user.id})
                session.execute(text("RESET app.tenant_id"))
                session.execute(text("RESET app.user_id"))
                session.commit()
        finally:
            session.close()
    engine.dispose()
