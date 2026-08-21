"""Add activity_sync_cursors table + clevis_worker/clevis_api grants (issue #192, S5 PR 2 of N).

Per-tenant "how far synced" cursor for install-time backfill (issue #191/S5 PR 1, PR #342) and the
new scheduled gap-heal sweep this PR adds: apps/worker's github.backfill_repo_events handler upserts
one row per tenant with last_synced_at = NOW() after every successful run, and apps/api's gap-heal
sweep reads rows whose last_synced_at has gone stale to decide which tenants are due for a re-sync.

Per-tenant, not per-repo: GitHub's Events API (which both the install-time backfill and the
gap-heal sweep call) is org/user-scoped, not repo-scoped -- there is nothing to key a per-repo
cursor on yet. A true per-repo cursor is deferred until deep historical backfill (composing from
repo-scoped commits/PRs/issues endpoints) exists.

tenant_id is the primary key (one row per tenant, not a log) -- an upsert target, never referenced
by foreign key from elsewhere, so no surrogate id column. last_synced_at is nullable: a tenant's
row doesn't exist until its first successful backfill/gap-heal run completes (there is nothing to
seed it with at install time itself -- the enqueue can still fail before the worker ever runs).

RLS policy reuses migration 0030's _TENANT_FILTER, ENABLE-only (no FORCE), same reasoning as
migrations 0036/0037 -- the table-owning migration role is unaffected either way, and no non-owner
role needs bypass here.

Grants mirror migration 0037 exactly, including clevis_api's grant: CI runs apps/worker's tests
under DATABASE_URL=clevis_api (no clevis_worker CI provisioning exists), so the worker's own cursor
upsert test needs clevis_api INSERT/UPDATE here too, not just SELECT.

Upgrade is purely additive (one new table, two conditional grants) -- zero data-loss risk.
Downgrade drops the table; safe since nothing else depends on it yet.

Revision ID: 0038
Revises: 0037
Create Date: 2026-08-21
"""

import sqlalchemy as sa
from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None

_TENANT_FILTER = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::int"


def upgrade() -> None:
    op.create_table(
        "activity_sync_cursors",
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), primary_key=True),
        sa.Column("account_login", sa.String(), nullable=False),
        sa.Column("account_type", sa.String(), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(), onupdate=sa.func.now()
        ),
    )

    op.execute(sa.text("ALTER TABLE activity_sync_cursors ENABLE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            f"CREATE POLICY tenant_isolation ON activity_sync_cursors "
            f"USING ({_TENANT_FILTER}) WITH CHECK ({_TENANT_FILTER})"
        )
    )

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'clevis_worker') THEN
                GRANT SELECT, INSERT, UPDATE ON activity_sync_cursors TO clevis_worker;
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
                GRANT SELECT, INSERT, UPDATE ON activity_sync_cursors TO clevis_api;
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
                REVOKE SELECT, INSERT, UPDATE ON activity_sync_cursors FROM clevis_worker;
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
                REVOKE SELECT, INSERT, UPDATE ON activity_sync_cursors FROM clevis_api;
            END IF;
        END
        $$;
        """
    )
    op.execute(sa.text("DROP POLICY IF EXISTS tenant_isolation ON activity_sync_cursors"))
    op.drop_table("activity_sync_cursors")
