"""Drop the legacy org_memberships table (issue #331).

SCHEMA CHANGE -- this migration DROPs a table. It is not additive and not automatically
reversible with data.

Background: org_memberships (org_id-keyed) was the original org RBAC join table. Issue
#190 moved org membership onto the tenant-scoped `memberships` table: reads cut over in
step 6a (PR #326, every `require_org_role` call site) and writes were dual-written from
step 4 (PR #322) onward. Since then org_memberships has been pure write-amplification --
every membership change touched both tables, but nothing read org_memberships. #190's
own rollout plan (Phase E) explicitly scoped this drop as a tracked follow-up.

This PR removes the dual-write (org_membership_repo is now a thin org_id->tenant_id
adapter over tenant_repo, writing only `memberships`) and drops the now-unreferenced
table here.

Data loss: the org_memberships rows are discarded. This is safe because `memberships`
has held an equivalent row for every one of them since #190 PR 4's dual-write, and
migration 0029's docstring carries the verification query that asserted that parity.
Nothing else references org_memberships.id (it is a leaf join table).

downgrade() recreates the table structure and its clevis_api grants but CANNOT restore
the dropped rows -- it yields an empty table. A real rollback would additionally need to
re-run #190 PR 4's backfill from `memberships`.

Revision ID: 0045
Revises: 0044
Create Date: 2026-09-09
"""

import sqlalchemy as sa
from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # DROP TABLE cascades to the id sequence, the uq_org_memberships_org_user constraint,
    # the org_id/user_id foreign keys, and every GRANT on the table / its sequence
    # (migration 0032 and docker/provision-api-role-existing-deployment.sh both granted
    # clevis_api DML here). No RLS policy to drop -- org_memberships was deliberately
    # excluded from the RLS scaffolding in migration 0030.
    op.drop_table("org_memberships")


def downgrade() -> None:
    op.create_table(
        "org_memberships",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("org_id", sa.Integer, sa.ForeignKey("orgs.id"), nullable=False),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("role", sa.String, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("org_id", "user_id", name="uq_org_memberships_org_user"),
    )
    # Restore the clevis_api grants migration 0032 gave this table, guarded by role
    # existence (the role only exists when API_DB_PASSWORD is configured -- issue #330).
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
              IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'clevis_api') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON org_memberships TO clevis_api;
                GRANT USAGE, SELECT ON org_memberships_id_seq TO clevis_api;
              END IF;
            END
            $$;
            """
        )
    )
    # NOTE: the org_memberships rows themselves are not restored -- see module docstring.
