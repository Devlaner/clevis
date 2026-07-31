"""grant clevis_worker role privileges on jobs and app_config

Issue #190 (step 1 of the tenants + Row-Level Security design): the worker
currently shares the API's Postgres role (both read DATABASE_URL built from the
same DB_USER/DB_PASSWORD). Once RLS is enabled, granting BYPASSRLS to that
shared role would make RLS a no-op for the API too, so the worker needs a
distinct role first. docker/postgres-init/01-create-worker-role.sh creates a
`clevis_worker` login role on a fresh Postgres data volume when
WORKER_DB_PASSWORD is set; this migration grants it exactly the privileges the
worker currently needs (SELECT/UPDATE on jobs, SELECT on app_config -- the
only two tables apps/worker/src/worker.py touches; it never inserts into
jobs, only claims and updates existing rows via SELECT ... FOR UPDATE SKIP
LOCKED, so INSERT is deliberately not granted).

No-op, in both directions, when the clevis_worker role doesn't exist -- this
migration is safe to run in every environment, including ones that haven't
opted into WORKER_DB_PASSWORD yet (the default: the worker keeps sharing the
API's role, unaffected by this migration).

Data-loss / backfill risk: none. This migration only grants/revokes table
privileges; it does not alter any table's schema or data.

Revision ID: 0020
Revises: 0019
Create Date: 2026-07-31
"""

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'clevis_worker') THEN
                GRANT SELECT, UPDATE ON jobs TO clevis_worker;
                GRANT SELECT ON app_config TO clevis_worker;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'clevis_worker') THEN
                REVOKE SELECT, UPDATE ON jobs FROM clevis_worker;
                REVOKE SELECT ON app_config FROM clevis_worker;
            END IF;
        END
        $$;
        """
    )
