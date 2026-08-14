"""grant clevis_api role privileges on all API-owned tables

Issue #330: the API currently connects as the initdb bootstrap superuser (DB_USER),
which unconditionally bypasses Row-Level Security regardless of ENABLE/FORCE -- so
migrations 0030/0031's RLS policies are logically correct but currently enforce
nothing in production. This mirrors migration 0020's clevis_worker pattern:
docker/postgres-init/02-create-api-role.sh creates a `clevis_api` login role on a
fresh Postgres data volume when API_DB_PASSWORD is set; this migration grants it
exactly the table privileges the API's runtime request handling needs across every
table apps/api/src/core/db.py defines a model for.

Grants are enumerated explicitly per table (not `GRANT ... ON ALL TABLES IN SCHEMA`)
so a future new table requires its own reviewed migration to grant clevis_api access,
rather than silently inheriting it.

This migration is purely additive -- nothing connects as clevis_api yet (that cutover,
switching the API's runtime engine to actually use this role, is a deliberately
separate, later change; see issue #330). No-op, in both directions, when the
clevis_api role doesn't exist, so it's safe to run in every environment, including
ones that haven't opted into API_DB_PASSWORD yet.

Data-loss / backfill risk: none. This migration only grants/revokes table privileges;
it does not alter any table's schema or data.

Revision ID: 0032
Revises: 0031
Create Date: 2026-08-14
"""

from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

_TABLES = [
    "users",
    "orgs",
    "org_memberships",
    "tenants",
    "memberships",
    "invitations",
    "github_installations",
    "saved_tokens",
    "audit_logs",
    "scan_results",
    "jobs",
    "app_config",
]


def upgrade() -> None:
    grants = "\n".join(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO clevis_api;" for table in _TABLES
    )
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'clevis_api') THEN
                {grants}
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    revokes = "\n".join(
        f"REVOKE SELECT, INSERT, UPDATE, DELETE ON {table} FROM clevis_api;" for table in _TABLES
    )
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'clevis_api') THEN
                {revokes}
            END IF;
        END
        $$;
        """
    )
