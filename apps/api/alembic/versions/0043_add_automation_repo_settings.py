"""Add automation_repo_settings — per-repo opt-in + saved presets for write automations.

Issue #288 (bulk branch-protection apply) introduces this table; issue #290 (Dependabot
auto-triage) reuses it. One row per (tenant, repo, feature):

  feature   'branch_protection' | 'dependabot_triage'
  enabled   the per-repo opt-in switch — defaults FALSE, so an automation acts on
            nothing until an admin turns it on for a specific repo
  mode      feature-specific ('approve_only' | 'approve_and_merge' for dependabot_triage;
            NULL for branch_protection)
  extra     JSONB — the saved branch-protection preset, or other per-feature options

Composite primary key (tenant_id, repo, feature) instead of a surrogate id, matching
repo_event_daily_counts (migration 0037): the table is keyed entirely by those three
values, it's an upsert target, and nothing references it by foreign key — a surrogate
key would add a sequence to grant and manage for no benefit.

RLS-scoped exactly like repo_event_daily_counts: ENABLE ROW LEVEL SECURITY + a
tenant_isolation policy on migration 0030's tenant filter, plus the existence-guarded
clevis_worker / clevis_api grants (CI runs the suite under the constrained clevis_api
role to verify RLS, so that role needs access here too).

Upgrade is purely additive (one new table, two conditional grants) — zero data-loss
risk, no column touched, no data migrated. Downgrade drops the table; nothing depends
on it via foreign key.

Revision ID: 0043
Revises: 0042
Create Date: 2026-09-03
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None

_TENANT_FILTER = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::int"


def upgrade() -> None:
    op.create_table(
        "automation_repo_settings",
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("repo", sa.String(), nullable=False),
        sa.Column("feature", sa.String(), nullable=False),
        sa.Column("mode", sa.String(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("extra", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "repo", "feature", name="pk_automation_repo_settings"),
    )
    op.create_index(
        "ix_automation_repo_settings_tenant_feature",
        "automation_repo_settings",
        ["tenant_id", "feature"],
    )

    op.execute(sa.text("ALTER TABLE automation_repo_settings ENABLE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            f"CREATE POLICY tenant_isolation ON automation_repo_settings "
            f"USING ({_TENANT_FILTER}) WITH CHECK ({_TENANT_FILTER})"
        )
    )

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'clevis_worker') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON automation_repo_settings TO clevis_worker;
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
                GRANT SELECT, INSERT, UPDATE, DELETE ON automation_repo_settings TO clevis_api;
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
                REVOKE SELECT, INSERT, UPDATE, DELETE ON automation_repo_settings FROM clevis_worker;
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
                REVOKE SELECT, INSERT, UPDATE, DELETE ON automation_repo_settings FROM clevis_api;
            END IF;
        END
        $$;
        """
    )
    op.execute(sa.text("DROP POLICY IF EXISTS tenant_isolation ON automation_repo_settings"))
    op.drop_table("automation_repo_settings")
