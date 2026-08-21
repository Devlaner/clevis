"""Add resolve_installation_tenant_id() SECURITY DEFINER function (issue #191, S3 PR 2 of 4).

**Fixes a genuine, already-live production bug** -- not a refactor. Migration 0031 put
`github_installations` under `FORCE ROW LEVEL SECURITY` with policy
`USING (tenant_id = app.tenant_id OR owner_user_id = app.user_id)`. The GitHub webhook
receiver (`src/routers/webhooks.py`, `POST /webhooks/github`) is unauthenticated by
design -- it verifies the request via HMAC signature, not a login session -- so it never
calls `require_org_role`/`require_personal_tenant` (the only places that set the
`app.tenant_id`/`app.user_id` session vars this policy reads). Under the real runtime
`clevis_api` role (non-superuser since #330), that means `_handle_installation_deleted`'s
lookup against `github_installations` evaluates the policy to `NULL OR NULL` -- every row
invisible, every time. The uninstall-cleanup delete has been silently no-op'ing in
production since #330 cut the API over to `clevis_api`: GitHub's `installation.deleted`
webhook fires, signature verifies, handler runs, `DELETE` affects 0 rows, no error, no
audit log entry, and the stale `github_installations` row (and its now-invalid
`token_ref`) is never cleaned up.

This migration adds a narrow SECURITY DEFINER function, owned by whichever role runs
migrations (the DB_USER bootstrap superuser -- table owner, unconditionally bypasses RLS
regardless of FORCE), that does exactly one read: resolve a GitHub installation_id to its
tenant_id. `clevis_api` is granted EXECUTE on it, not BYPASSRLS on the role itself --
BYPASSRLS is an all-or-nothing role attribute that would neuter RLS for every query the
API makes, not just this one pre-authentication lookup. The webhook handler (PR 2's other
half, in installation_repo.py) calls this function first to resolve tenant_id, then sets
`app.tenant_id` for the rest of the request via the existing plain-SET pattern
(src/core/db.py's set_session_user, src/core/rbac.py's set_tenant_session_context) so the
actual DELETE that follows is evaluated under a session that now legitimately satisfies
the policy's tenant_id-equality branch -- not bypassing RLS for the delete itself, just
supplying the session context RLS already expects to find.

Upgrade is purely additive (one new function) -- zero data-loss risk. Downgrade drops the
function; do not run it against a real environment without explicit confirmation
(AGENTS.md) since it would silently reintroduce the bug this migration fixes.

Revision ID: 0035
Revises: 0034
Create Date: 2026-08-16
"""

from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION resolve_installation_tenant_id(p_installation_id integer)
        RETURNS integer
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT tenant_id FROM github_installations
            WHERE installation_id = p_installation_id
            LIMIT 1
        $$;
        """
    )
    # Postgres grants EXECUTE on a new function to PUBLIC by default (unlike tables) --
    # left alone, the GRANT below wouldn't actually narrow anything, since every role
    # that can connect could already call this function and read any installation's
    # tenant_id, defeating the "narrow, deliberately scoped" bypass this migration's
    # docstring describes (CodeRabbit finding on PR #337).
    op.execute("REVOKE EXECUTE ON FUNCTION resolve_installation_tenant_id(integer) FROM PUBLIC")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'clevis_api') THEN
                GRANT EXECUTE ON FUNCTION resolve_installation_tenant_id(integer) TO clevis_api;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS resolve_installation_tenant_id(integer)")
