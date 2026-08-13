"""Add github_installations.tenant_id, backfilled, nullable (issue #190, PR 3 of 7).

github_installations.org_id/owner_user_id are already mutually exclusive
(ck_github_installations_org_xor_owner), so the backfill join picks the
org's tenant when org_id is set, the owner's personal tenant otherwise.
NOT NULL is deferred to migration 0029, run only after a verification
query confirms zero NULLs in the target environment -- this table's
backfill correctness depends on migrations 0022/0023 having already run
cleanly there, which this migration can't itself guarantee.

Revision ID: 0025
Revises: 0024
Create Date: 2026-08-12
"""

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "github_installations", sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=True)
    )
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE github_installations gi SET tenant_id = t.id "
            "FROM tenants t "
            "WHERE (gi.org_id IS NOT NULL AND t.org_id = gi.org_id AND t.kind = 'org') "
            "OR (gi.owner_user_id IS NOT NULL AND t.personal_user_id = gi.owner_user_id AND t.kind = 'personal')"
        )
    )


def downgrade() -> None:
    op.drop_column("github_installations", "tenant_id")
