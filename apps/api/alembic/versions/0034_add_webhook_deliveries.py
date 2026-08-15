"""Add webhook_deliveries table (issue #191, S3 webhook ingestion PR 1 of 4).

Upgrade is purely additive and touches no existing table -- zero data-loss risk.
Downgrade drops this table and permanently deletes any stored payloads; do not
run it against a real environment without explicit confirmation (AGENTS.md).
Durable landing spot for verified GitHub webhook payloads before they're queued
onto Redis Streams for later processing (a future S4 event-processor fleet).
See docs/architecture/clevis-architecture.md's Ingestion pipeline layer for the
target design; this table is the "raw payload store" half of it. Object storage
(MinIO) is the architecture doc's eventual answer for that role, but standing up
a second stateful service for what's typically a few KB of JSON is infra ahead
of need at this stage -- Postgres is already the trusted durable store this repo
uses everywhere else. Table bloat from unbounded accumulation is a known,
deliberately deferred follow-up (a retention/pruning job, once something -- S4 --
actually consumes these rows), not solved here.

No unique constraint on delivery_id: GitHub redelivers the same delivery id on
retry, and dedupe is the future S4 consumer's job operating on the queue side,
not a write-side constraint here -- enforcing uniqueness now would mean deciding
dedupe semantics (reject vs. upsert vs. which duplicate wins) that belong to
S4's design.

No Row-Level Security on this table, despite the nullable tenant_id column --
deliberately structured like `jobs`/`app_config` (see migration 0030's docstring
for why those two are excluded), not like the Group A/B tenant-scoped tables.
The webhook receiver writes this table on an unauthenticated connection with no
app.tenant_id session var set (there's no authenticated tenant context at
webhook-receive time -- see src/core/rbac.py's _set_tenant_session_context,
only ever called from require_org_role/require_personal_tenant, neither of
which runs on this route), so a tenant-matching WITH CHECK would reject every
insert regardless of whether tenant_id resolution succeeded. And the future S4
consumer fleet needs to read pending rows across all tenants at once, not one
tenant's rows at a time -- the same structural reason `jobs` has no RLS. Access
control here is HMAC-signature verification at the receiver, not row-level
tenant isolation.

Grants clevis_api access to the new table + its backing sequence inline (see
migration 0032's docstring: "a future new table requires its own reviewed
migration to grant clevis_api access, rather than silently inheriting it").
No-op when the clevis_api role doesn't exist, matching 0032/0033.

Revision ID: 0034
Revises: 0033
Create Date: 2026-08-16
"""

import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=True),
        sa.Column("delivery_id", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("installation_id", sa.Integer(), nullable=True),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="queued"),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_webhook_deliveries_tenant_id", "webhook_deliveries", ["tenant_id"])
    op.create_index("ix_webhook_deliveries_status", "webhook_deliveries", ["status"])

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'clevis_api') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON webhook_deliveries TO clevis_api;
                GRANT USAGE, SELECT ON SEQUENCE webhook_deliveries_id_seq TO clevis_api;
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
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'clevis_api') THEN
                REVOKE USAGE, SELECT ON SEQUENCE webhook_deliveries_id_seq FROM clevis_api;
                REVOKE SELECT, INSERT, UPDATE, DELETE ON webhook_deliveries FROM clevis_api;
            END IF;
        END
        $$;
        """
    )
    op.drop_index("ix_webhook_deliveries_status", table_name="webhook_deliveries")
    op.drop_index("ix_webhook_deliveries_tenant_id", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")
