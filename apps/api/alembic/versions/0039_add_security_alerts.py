"""Add security_alerts table + clevis_worker/clevis_api grants (post-S6, PR 2 of 3).

Normalized store for the three Security-alert webhook event types durably queued since
PR #350 (dependabot_alert/code_scanning_alert/secret_scanning_alert) -- populated by
apps/worker's event_consumer.py once it gains a real handler for these event types
(this PR), replacing the ack-but-skip guard (_NOT_YET_NORMALIZED_EVENT_TYPES) that PR
#350 added as a placeholder. Read by a future Security-dashboard re-point (PR 3) instead
of that dashboard's current per-request live GitHub API fan-out.

One polymorphic table, not three: dependabot/code-scanning/secret-scanning alerts share
enough shape (a per-repo `number`, a `state` that transitions over the alert's lifetime,
`created_at`/`updated_at`, an optional `severity`) that a per-repo "give me all open
alerts" query is naturally one table. `kind` discriminates the three; `details` (JSONB)
holds the kind-specific remainder GitHub sends (dependency/security_advisory for
dependabot; rule/tool for code_scanning; secret_type/secret_type_display_name for
secret_scanning) rather than three near-duplicate tables a future dashboard query would
otherwise have to UNION.

Dedup/upsert key is (tenant_id, repo, kind, number) -- the alert-level analog of
repo_events's delivery_id uniqueness. Unlike repo_events (an immutable activity log,
ON CONFLICT DO NOTHING), an alert's state legitimately changes over its lifetime (e.g.
open -> dismissed/fixed) and a redelivered webhook for the same alert must update the
existing row's state/severity/details/updated_at, not just dedupe it away -- the
consumer upserts with ON CONFLICT ... DO UPDATE, not DO NOTHING.

tenant_id is NOT NULL by design, same reasoning as migration 0036: the consumer skips
normalizing any webhook_deliveries row whose tenant_id is null (an unresolved
installation), so this table's RLS policy stays in the simpler "Group A" (strict
equality) shape from migration 0030, no OR-NULL clause needed.

RLS is ENABLE-only, no FORCE -- same reasoning as migrations 0036/0037/0038: the
table-owning migration role is unaffected either way, and this only starts enforcing
for a non-owner role once that role is actually granted access to it (this migration).

Grants mirror migration 0036/0037/0038 exactly: clevis_worker gets SELECT+INSERT+UPDATE
(it upserts, not just inserts, unlike repo_events) and clevis_api also gets the same
grant, not because the API writes this table, but because CI's "Run Python tests" step
runs apps/worker's tests under DATABASE_URL=clevis_api (no clevis_worker CI
provisioning exists) -- without this, the new consumer's own tests would fail in CI
with the same InsufficientPrivilege class of bug migrations 0035/0036 already
documented. Both guarded by the same `IF EXISTS (SELECT FROM pg_roles ...)` pattern.

Upgrade is purely additive (one new table, two conditional grants) -- zero data-loss
risk. Downgrade drops the table; safe since nothing reads it yet (PR 3 hasn't shipped).

Revision ID: 0039
Revises: 0038
Create Date: 2026-08-22
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None

_TENANT_FILTER = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::int"


def upgrade() -> None:
    op.create_table(
        "security_alerts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("repo", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("severity", sa.String(), nullable=True),
        sa.Column("details", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_security_alerts_tenant_id", "security_alerts", ["tenant_id"])
    op.create_index("ix_security_alerts_tenant_id_repo", "security_alerts", ["tenant_id", "repo"])
    op.create_unique_constraint(
        "uq_security_alerts_tenant_repo_kind_number", "security_alerts", ["tenant_id", "repo", "kind", "number"]
    )

    op.execute(sa.text("ALTER TABLE security_alerts ENABLE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            f"CREATE POLICY tenant_isolation ON security_alerts "
            f"USING ({_TENANT_FILTER}) WITH CHECK ({_TENANT_FILTER})"
        )
    )

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'clevis_worker') THEN
                GRANT SELECT, INSERT, UPDATE ON security_alerts TO clevis_worker;
                GRANT USAGE, SELECT ON security_alerts_id_seq TO clevis_worker;
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
                GRANT SELECT, INSERT, UPDATE ON security_alerts TO clevis_api;
                GRANT USAGE, SELECT ON security_alerts_id_seq TO clevis_api;
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
                REVOKE SELECT, INSERT, UPDATE ON security_alerts FROM clevis_worker;
                REVOKE USAGE, SELECT ON security_alerts_id_seq FROM clevis_worker;
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
                REVOKE SELECT, INSERT, UPDATE ON security_alerts FROM clevis_api;
                REVOKE USAGE, SELECT ON security_alerts_id_seq FROM clevis_api;
            END IF;
        END
        $$;
        """
    )
    op.execute(sa.text("DROP POLICY IF EXISTS tenant_isolation ON security_alerts"))
    op.drop_table("security_alerts")
