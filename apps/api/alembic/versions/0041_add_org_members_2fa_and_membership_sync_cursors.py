"""Add org_members.two_factor_enabled + org_membership_sync_cursors (Collaborators PR 2 of 3).

The reconciliation poll this PR adds is the *only* source of truth for two fields that have
zero webhook coverage, permanently -- not a fallback for missed webhooks, per docs/plan.md's
Collaborators research writeup (verified against GitHub's webhook docs last stage): an org
member's role changing has no webhook event at all, and neither does 2FA enrollment status.
Migration 0040 already handles role (org_members.role, populated at add-time by the webhook
path, corrected here going forward by the poll). This migration adds the other half: a
two_factor_enabled column the webhook path never touches (member/organization/membership/team
events carry nothing about 2FA), populated only by this PR's poll.

two_factor_enabled is nullable, not a False default -- same reasoning as migration 0040's
is_outside_collaborator: a row can exist before the poll has ever run for that tenant (created
by the webhook path), and NULL ("not yet known") must stay distinct from a real "poll checked,
2FA is off" False. The poll's own GitHub call for this (`GET /orgs/{org}/members?filter=
2fa_disabled`, org-owner-token-gated) is already best-effort in the live code today
(apps/api/src/routers/collab.py's list_members sets two_factor_overlay_available=False on
failure rather than guessing) -- this column preserves that same "unknown stays unknown"
posture rather than collapsing a failed check into a false negative.

org_membership_sync_cursors mirrors activity_sync_cursors (migration 0038) exactly: one row per
org-kind tenant (not per-member -- this cursor tracks "how far synced," the same shape as the
activity cursor, not a table of poll results), tenant_id as the primary key (upsert target, no
surrogate id), last_synced_at nullable (a tenant's row doesn't exist until its first successful
reconciliation run). org_login is stored here (unlike activity_sync_cursors, which already had
account_login available on every payload from PR #342's install-time trigger) because the
membership-reconcile sweep has no equivalent existing per-tenant payload to read it from -- a
tenant's org login lives on the orgs table, resolved once per sweep tick from there, and cached
here afterward the same way activity_sync_cursors caches account_login.

RLS is ENABLE-only, no FORCE, same reasoning as every migration since 0030. Grants mirror
migration 0038 exactly (SELECT/INSERT/UPDATE, no DELETE -- this cursor is only ever upserted,
never removed, same shape as activity_sync_cursors), including the clevis_api grant: CI runs
apps/worker's tests under DATABASE_URL=clevis_api (no clevis_worker CI provisioning exists), so
the worker-side cursor-upsert test needs it. The matching guarded grant block is added to
docker/provision-api-role-existing-deployment.sh in this same PR (the process fix from
Collaborators PR 1's own docstring -- add it alongside the migration, not as a follow-up once CI
catches the gap).

Upgrade is purely additive (one new nullable column, one new table, six conditional grants) --
zero data-loss risk. Downgrade drops the column and the table; safe since nothing reads either
yet (Collaborators PR 3 hasn't shipped).

Revision ID: 0041
Revises: 0040
Create Date: 2026-08-26
"""

import sqlalchemy as sa
from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None

_TENANT_FILTER = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::int"


def upgrade() -> None:
    op.add_column("org_members", sa.Column("two_factor_enabled", sa.Boolean(), nullable=True))

    op.create_table(
        "org_membership_sync_cursors",
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), primary_key=True),
        sa.Column("org_login", sa.String(), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(), onupdate=sa.func.now()
        ),
    )

    op.execute(sa.text("ALTER TABLE org_membership_sync_cursors ENABLE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            f"CREATE POLICY tenant_isolation ON org_membership_sync_cursors "
            f"USING ({_TENANT_FILTER}) WITH CHECK ({_TENANT_FILTER})"
        )
    )

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'clevis_worker') THEN
                GRANT SELECT, INSERT, UPDATE ON org_membership_sync_cursors TO clevis_worker;
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
                GRANT SELECT, INSERT, UPDATE ON org_membership_sync_cursors TO clevis_api;
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
                REVOKE SELECT, INSERT, UPDATE ON org_membership_sync_cursors FROM clevis_worker;
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
                REVOKE SELECT, INSERT, UPDATE ON org_membership_sync_cursors FROM clevis_api;
            END IF;
        END
        $$;
        """
    )
    op.execute(sa.text("DROP POLICY IF EXISTS tenant_isolation ON org_membership_sync_cursors"))
    op.drop_table("org_membership_sync_cursors")
    op.drop_column("org_members", "two_factor_enabled")
