"""Add repo_event_daily_counts table + clevis_worker/clevis_api grants (issue #191, S4 PR 2 of N).

Materialized aggregate half of S4: per-tenant/repo/event_type/day event counts, upserted by
apps/worker's event_consumer.py (src/event_consumer.py) in the same transaction as each
repo_events insert. Not read by the API yet -- that's S6's job, same deferral as repo_events
itself (migration 0036).

Composite primary key (tenant_id, repo, event_type, day) instead of a surrogate id: this table is
purely a rollup keyed by those four values, an upsert target, never referenced by foreign key from
elsewhere -- a surrogate key would add a sequence to grant/manage for no benefit.

tenant_id is NOT NULL by the same reasoning as repo_events: the consumer never normalizes a
null-tenant webhook_deliveries row, so it never has a null-tenant aggregate to upsert either. RLS
policy reuses migration 0030's _TENANT_FILTER, ENABLE-only (no FORCE), same reasoning as 0036 --
the table-owning migration role is unaffected either way, and no non-owner role reads this table
yet.

Grants mirror migration 0036 exactly, including clevis_api's grant: CI runs apps/worker's tests
under DATABASE_URL=clevis_api (no clevis_worker CI provisioning exists), so the consumer's own
tests need clevis_api access here too, same as repo_events. clevis_worker additionally gets UPDATE
(not just INSERT) since upserting requires it.

Upgrade is purely additive (one new table, two conditional grants) -- zero data-loss risk.
Downgrade drops the table; safe since nothing reads it yet (S6 hasn't shipped).

Revision ID: 0037
Revises: 0036
Create Date: 2026-08-21
"""

import sqlalchemy as sa
from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None

_TENANT_FILTER = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::int"


def upgrade() -> None:
    op.create_table(
        "repo_event_daily_counts",
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("repo", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("tenant_id", "repo", "event_type", "day", name="pk_repo_event_daily_counts"),
    )
    op.create_index("ix_repo_event_daily_counts_tenant_id_day", "repo_event_daily_counts", ["tenant_id", "day"])

    op.execute(sa.text("ALTER TABLE repo_event_daily_counts ENABLE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            f"CREATE POLICY tenant_isolation ON repo_event_daily_counts "
            f"USING ({_TENANT_FILTER}) WITH CHECK ({_TENANT_FILTER})"
        )
    )

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'clevis_worker') THEN
                GRANT SELECT, INSERT, UPDATE ON repo_event_daily_counts TO clevis_worker;
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
                GRANT SELECT, INSERT, UPDATE ON repo_event_daily_counts TO clevis_api;
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
                REVOKE SELECT, INSERT, UPDATE ON repo_event_daily_counts FROM clevis_worker;
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
                REVOKE SELECT, INSERT, UPDATE ON repo_event_daily_counts FROM clevis_api;
            END IF;
        END
        $$;
        """
    )
    op.execute(sa.text("DROP POLICY IF EXISTS tenant_isolation ON repo_event_daily_counts"))
    op.drop_table("repo_event_daily_counts")
