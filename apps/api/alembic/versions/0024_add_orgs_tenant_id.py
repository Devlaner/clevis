"""Add orgs.tenant_id, backfilled, nullable (issue #190, PR 3 of 7).

Every existing orgs row already has exactly one 'org'-kind tenants row from
migration 0022, so the backfill join itself is small and unambiguous. NOT
NULL is *not* enforced here, though -- app code (org_provisioning.py and
anywhere else an Org row is created) doesn't set tenant_id yet; that wiring
is explicitly PR 4's job (dual-write). Enforcing NOT NULL now would break
every org-creation code path the moment this migration deploys, well before
PR 4 ships. NOT NULL enforcement is deferred to its own follow-up migration,
written once PR 4 lands, mirroring github_installations' 0025/(future)
enforce-not-null split rather than the org-xor-owner backfill's original
"safe to enforce immediately" framing in #190's design comment -- that
framing didn't account for app code being the actual writer, not just this
migration's one-time backfill.

Revision ID: 0024
Revises: 0023
Create Date: 2026-08-12
"""

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orgs", sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=True))
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE orgs SET tenant_id = t.id "
            "FROM tenants t WHERE t.org_id = orgs.id AND t.kind = 'org'"
        )
    )


def downgrade() -> None:
    op.drop_column("orgs", "tenant_id")
