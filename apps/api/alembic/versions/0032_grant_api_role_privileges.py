"""grant clevis_api role privileges on all API-owned tables

Issue #330: the API currently connects as the initdb bootstrap superuser (DB_USER),
which unconditionally bypasses Row-Level Security regardless of ENABLE/FORCE -- so
migrations 0030/0031's RLS policies are logically correct but currently enforce
nothing in production. This mirrors migration 0020's clevis_worker pattern:
docker/postgres-init/02-create-api-role.sh creates a `clevis_api` login role on a
fresh Postgres data volume when API_DB_PASSWORD is set; this migration grants it
exactly the privileges the API's runtime request handling needs across every table
apps/api/src/core/db.py defines a model for.

Grants are enumerated explicitly per table (not `GRANT ... ON ALL TABLES IN SCHEMA`)
so a future new table requires its own reviewed migration to grant clevis_api access,
rather than silently inheriting it.

Table-level DML privileges alone are not sufficient (confirmed by direct testing
against a real Postgres role before this was folded in, and independently flagged
by CodeRabbit on the version of this migration that only granted table privileges):

1. USAGE on the `public` schema. This specific cluster does not grant schema USAGE to
   PUBLIC by default (confirmed via `has_schema_privilege('public', 'public', 'usage')`
   returning false) -- without it, a role with table-level grants still can't resolve
   the table at all (`relation "users" does not exist`), reproduced directly.
2. USAGE, SELECT on each table's backing sequence. All of the API's tables use classic
   sequence-backed integer primary keys (`nextval('..._id_seq'::regclass)`, not
   identity columns) -- table-level INSERT privilege does not implicitly cover the
   sequence nextval() calls SQLAlchemy relies on for those inserts. `app_config` is the
   one table without a sequence (its primary key is a text `key` column), so it's
   excluded from the sequence-grant list.

This migration is purely additive -- nothing connects as clevis_api yet (that cutover,
switching the API's runtime engine to actually use this role, is a deliberately
separate, later change; see issue #330). No-op, in both directions, when the
clevis_api role doesn't exist, so it's safe to run in every environment, including
ones that haven't opted into API_DB_PASSWORD yet.

This migration alone is not sufficient to provision an *existing* deployment's role,
since Alembic won't replay an already-applied migration once the role is later
created by hand -- see docs/self-hosting.md's manual-provisioning step, which bundles
role creation with all three grant kinds (table, schema, sequence) in one atomic step
for that case.

Data-loss / backfill risk: none. This migration only grants/revokes schema, sequence,
and table privileges; it does not alter any table's schema or data.

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

# All _TABLES except app_config, whose primary key is a text `key` column with no
# backing sequence.
_SEQUENCE_TABLES = [
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
]


def upgrade() -> None:
    table_grants = "\n".join(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO clevis_api;" for table in _TABLES
    )
    sequence_grants = "\n".join(
        f"GRANT USAGE, SELECT ON SEQUENCE {table}_id_seq TO clevis_api;" for table in _SEQUENCE_TABLES
    )
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'clevis_api') THEN
                GRANT USAGE ON SCHEMA public TO clevis_api;
                {table_grants}
                {sequence_grants}
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    table_revokes = "\n".join(
        f"REVOKE SELECT, INSERT, UPDATE, DELETE ON {table} FROM clevis_api;" for table in _TABLES
    )
    sequence_revokes = "\n".join(
        f"REVOKE USAGE, SELECT ON SEQUENCE {table}_id_seq FROM clevis_api;" for table in _SEQUENCE_TABLES
    )
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'clevis_api') THEN
                {sequence_revokes}
                {table_revokes}
                REVOKE USAGE ON SCHEMA public FROM clevis_api;
            END IF;
        END
        $$;
        """
    )
