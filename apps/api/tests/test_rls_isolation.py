"""End-to-end proof that RLS (migrations 0030/0031) actually blocks cross-tenant access
under the real, non-superuser clevis_api role -- not just against a hand-built throwaway
test role. This is the concrete claim issue #190's closing comment promised and, before
this test existed, had only ever been checked by hand (see docs/self-hosting.md and the
migration 0032/0033 verification notes) rather than as a standing regression test.

Deliberately does NOT use the shared `db` fixture (conftest.py) -- that fixture is bound
to whatever settings.database_url the rest of the suite runs under, which is the
bootstrap superuser DB_USER in most local/dev runs (superuser unconditionally bypasses
RLS, so this test would silently prove nothing if it ran that way). Instead this test
opens its own connection explicitly as clevis_api: reused as-is when the suite is already
running under clevis_api (CI's python job, after issue #330's verification wiring), or
built by swapping credentials onto the same host/port/db when a local run sets
API_DB_PASSWORD to opt in. Skips (not fails) when neither is true, so a plain local
`pytest -q` run without API_DB_PASSWORD configured still passes -- that's the documented,
optional "Recommended" setup per AGENTS.md, not a hard requirement.

audit_logs is used as the RLS-protected table under test: it gets the simple strict
tenant_isolation equality policy (migration 0030), with none of memberships'/
github_installations' widened self-access carve-out (migration 0031) that would
complicate a minimal two-tenant isolation check.
"""

import os
from urllib.parse import quote, urlsplit, urlunsplit

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from src.core.config import settings
from src.core.db import AuditLog, Tenant, User
from src.core.rbac import set_tenant_session_context


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


def test_rls_blocks_cross_tenant_audit_log_access():
    engine = _clevis_api_engine()
    with engine.connect() as conn:
        # expire_on_commit=False: default expire-then-refresh-on-next-access behavior would
        # try to re-SELECT log_a by identity after switching the session to tenant B's
        # context, which correctly fails (RLS makes it invisible) but raises
        # ObjectDeletedError instead of just leaving the already-fetched attributes alone.
        session = Session(bind=conn, expire_on_commit=False)
        # Initialized up front and only ever deleted in the finally block if non-None, so a
        # failure partway through setup (e.g. after users commit but before tenants do) still
        # cleans up whatever was actually committed instead of leaking rows into a local DB.
        user_a = user_b = tenant_a = tenant_b = log_a = log_b = None
        try:
            try:
                user_a = User(email="rls-isolation-a@test.local", name=None, password_hash=None, is_workspace_admin=False)
                user_b = User(email="rls-isolation-b@test.local", name=None, password_hash=None, is_workspace_admin=False)
                session.add_all([user_a, user_b])
                session.commit()

                tenant_a = Tenant(kind="personal", personal_user_id=user_a.id)
                tenant_b = Tenant(kind="personal", personal_user_id=user_b.id)
                session.add_all([tenant_a, tenant_b])
                session.commit()

                # Each insert happens under its own tenant's session context, since Group A's
                # strict equality policy has no separate WITH CHECK -- it defaults to the
                # USING clause, so a row is only insertable when it's already visible under
                # the session's current tenant_id.
                set_tenant_session_context(session, tenant_a.id, user_a.id)
                log_a = AuditLog(actor="tester", action="test", target="a", payload="{}", tenant_id=tenant_a.id)
                session.add(log_a)
                session.commit()

                set_tenant_session_context(session, tenant_b.id, user_b.id)
                log_b = AuditLog(actor="tester", action="test", target="b", payload="{}", tenant_id=tenant_b.id)
                session.add(log_b)
                session.commit()

                # Back in tenant A's context: tenant B's row must be invisible to SELECT,
                # and untouched by UPDATE/DELETE targeting it directly by id.
                set_tenant_session_context(session, tenant_a.id, user_a.id)
                visible_ids = {row[0] for row in session.execute(text("SELECT id FROM audit_logs")).all()}
                assert visible_ids == {log_a.id}

                update_result = session.execute(
                    text("UPDATE audit_logs SET action = 'tampered' WHERE id = :id"), {"id": log_b.id}
                )
                assert update_result.rowcount == 0
                session.commit()

                delete_result = session.execute(text("DELETE FROM audit_logs WHERE id = :id"), {"id": log_b.id})
                assert delete_result.rowcount == 0
                session.commit()

                # And the reverse, confirming isolation isn't just a one-directional fluke.
                set_tenant_session_context(session, tenant_b.id, user_b.id)
                visible_ids = {row[0] for row in session.execute(text("SELECT id FROM audit_logs")).all()}
                assert visible_ids == {log_b.id}
            finally:
                # Cleanup must happen per-tenant context too -- RLS applies to DELETE just
                # like SELECT, so a single DELETE can't remove both rows at once. A prior
                # exception may leave the session mid-transaction, so roll back first.
                session.rollback()
                if log_a is not None:
                    set_tenant_session_context(session, tenant_a.id, user_a.id)
                    session.execute(text("DELETE FROM audit_logs WHERE id = :id"), {"id": log_a.id})
                    session.commit()
                if log_b is not None:
                    set_tenant_session_context(session, tenant_b.id, user_b.id)
                    session.execute(text("DELETE FROM audit_logs WHERE id = :id"), {"id": log_b.id})
                    session.commit()
                session.execute(text("RESET app.tenant_id"))
                session.execute(text("RESET app.user_id"))
                if tenant_a is not None:
                    session.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_a.id})
                if tenant_b is not None:
                    session.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tenant_b.id})
                if user_a is not None:
                    session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_a.id})
                if user_b is not None:
                    session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_b.id})
                session.commit()
        finally:
            session.close()
    engine.dispose()
