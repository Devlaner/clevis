"""grant clevis_api role schema USAGE and sequence privileges

Follow-up to migration 0032, fixing a gap CodeRabbit flagged on PR #332 and that was
independently confirmed by direct testing against a real Postgres role before that
review comment was read: granting SELECT/INSERT/UPDATE/DELETE on a table alone is not
sufficient for clevis_api to actually use it. Two things were missing:

1. USAGE on the `public` schema. This specific cluster does not grant schema USAGE to
   PUBLIC by default (confirmed via `has_schema_privilege('public', 'public', 'usage')`
   returning false) -- without it, a role with table-level grants still can't resolve
   the table at all (`relation "users" does not exist`), reproduced directly.
2. USAGE, SELECT on each table's backing sequence. All of the API's tables use classic
   sequence-backed integer primary keys (`nextval('..._id_seq'::regclass)`, not
   identity columns) -- table-level INSERT privilege does not implicitly cover the
   sequence nextval() calls SQLAlchemy relies on for those inserts. `app_config` is the
   one table without a sequence (its primary key is a text `key` column), so it's
   excluded from the sequence-grant list below.

Migration 0032 is already merged and must not be hand-edited (AGENTS.md) -- this ships
the fix as new grants instead. Same no-op-safe pattern: both blocks below only run if
the clevis_api role actually exists, so this is safe in every environment, including
ones that haven't opted into API_DB_PASSWORD.

Data-loss / backfill risk: none. This migration only grants/revokes schema and
sequence privileges; it does not alter any table's schema or data.

Revision ID: 0033
Revises: 0032
Create Date: 2026-08-14
"""

from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None

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
    sequence_grants = "\n".join(
        f"GRANT USAGE, SELECT ON SEQUENCE {table}_id_seq TO clevis_api;" for table in _SEQUENCE_TABLES
    )
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'clevis_api') THEN
                GRANT USAGE ON SCHEMA public TO clevis_api;
                {sequence_grants}
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    sequence_revokes = "\n".join(
        f"REVOKE USAGE, SELECT ON SEQUENCE {table}_id_seq FROM clevis_api;" for table in _SEQUENCE_TABLES
    )
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'clevis_api') THEN
                {sequence_revokes}
                REVOKE USAGE ON SCHEMA public FROM clevis_api;
            END IF;
        END
        $$;
        """
    )
