"""Add repo_events table + clevis_worker grants (issue #191, S4 PR 1 of N).

Normalized, deduplicated event store consumed by the API (S6, not this PR) instead of
polling GitHub live. Populated by apps/worker's new Redis Streams consumer
(src/event_consumer.py), which reads webhook_events, normalizes each entry, and
INSERTs here with `ON CONFLICT (delivery_id) DO NOTHING` -- delivery_id is the actual
idempotency key (GitHub redelivers the same X-GitHub-Delivery on retry; webhook_deliveries
deliberately does not dedupe, per its own docstring, leaving that to this table).

tenant_id is NOT NULL by design: the consumer skips normalizing any webhook_deliveries
row whose tenant_id is null (an unresolved installation) rather than write a row here
with no tenant to scope it to -- a deliberate scope decision (see S4 PR 1's plan notes),
not an oversight. That keeps this table's RLS policy in the simpler "Group A" (strict
equality) shape from migration 0030, no OR-NULL clause needed.

RLS is ENABLE-only, no FORCE -- same reasoning as migration 0030: the migrating
(table-owning) role is unaffected either way, and this only starts enforcing for a
non-owner role (clevis_api, once it reads this table in a future S6 PR) once that role
is actually granted access to it, which isn't happening in this migration.

clevis_worker (issue #190 step 1) is granted SELECT+INSERT on repo_events (it inserts,
never updates/deletes a normalized row) and SELECT+UPDATE on webhook_deliveries (it
already had no grant there at all -- needs SELECT to read the payload and UPDATE to
flip status to 'processed' once normalized). Both grants are guarded by
`IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'clevis_worker')`, matching migration
0020's pattern exactly, and are a no-op on any deployment that hasn't opted into
WORKER_DB_PASSWORD (the default -- worker keeps sharing the API's role, unaffected).

clevis_api is also granted SELECT+INSERT on repo_events and (already had SELECT/UPDATE
via webhook_deliveries's existing full-CRUD grant, see migration 0032/the
existing-deployment script) -- not because the API writes either table in this PR, but
because CI's "Run Python tests" step runs the *entire* `pytest -q` suite, apps/worker's
tests included, under DATABASE_URL=clevis_api (there's no clevis_worker CI provisioning
at all -- production's worker process itself uses clevis_worker, or the shared
superuser role by default, never clevis_api). Without this, the consumer's own tests
would fail in CI with the exact InsufficientPrivilege class of bug migration 0035/#337
already hit once. Same `IF EXISTS` guard as migration 0032's own clevis_api grants.

The consumer satisfies this table's RLS WITH CHECK by issuing `SET app.tenant_id = <n>`
per event before each insert (mirroring src/core/rbac.py's set_tenant_session_context),
not by granting clevis_worker BYPASSRLS -- a narrower, explicit mechanism, consistent
with migration 0035's SECURITY DEFINER function preferring the same over a blanket
role-wide bypass.

Upgrade is purely additive (one new table, two conditional grants) -- zero data-loss
risk. Downgrade drops the table; safe since nothing reads it yet (S6 hasn't shipped).

Revision ID: 0036
Revises: 0035
Create Date: 2026-08-21
"""

import sqlalchemy as sa
from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None

_TENANT_FILTER = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::int"


def upgrade() -> None:
    op.create_table(
        "repo_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("delivery_id", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("actor_avatar", sa.String(), nullable=False),
        sa.Column("repo", sa.String(), nullable=False),
        sa.Column("summary", sa.String(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_repo_events_tenant_id", "repo_events", ["tenant_id"])
    op.create_unique_constraint("uq_repo_events_delivery_id", "repo_events", ["delivery_id"])

    op.execute(sa.text("ALTER TABLE repo_events ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"CREATE POLICY tenant_isolation ON repo_events USING ({_TENANT_FILTER}) WITH CHECK ({_TENANT_FILTER})"))

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'clevis_worker') THEN
                GRANT SELECT, INSERT ON repo_events TO clevis_worker;
                GRANT USAGE, SELECT ON repo_events_id_seq TO clevis_worker;
                GRANT SELECT, UPDATE ON webhook_deliveries TO clevis_worker;
            END IF;
        END
        $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'clevis_api') THEN
                GRANT SELECT, INSERT ON repo_events TO clevis_api;
                GRANT USAGE, SELECT ON repo_events_id_seq TO clevis_api;
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
                REVOKE SELECT, INSERT ON repo_events FROM clevis_worker;
                REVOKE USAGE, SELECT ON repo_events_id_seq FROM clevis_worker;
                REVOKE SELECT, UPDATE ON webhook_deliveries FROM clevis_worker;
            END IF;
        END
        $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'clevis_api') THEN
                REVOKE SELECT, INSERT ON repo_events FROM clevis_api;
                REVOKE USAGE, SELECT ON repo_events_id_seq FROM clevis_api;
            END IF;
        END
        $$;
        """
    )
    op.execute(sa.text("DROP POLICY IF EXISTS tenant_isolation ON repo_events"))
    op.drop_table("repo_events")
