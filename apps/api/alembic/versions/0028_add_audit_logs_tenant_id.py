"""Add audit_logs.tenant_id, nullable, no backfill (issue #190, PR 3 of 7).

audit_logs.actor/target are free text (not FKs), so pre-migration rows
can't be reliably attributed to a tenant -- per the design decision
recorded on #190, these stay visible only through a require_workspace_admin
-gated view once RLS lands, never to ordinary tenant members. No backfill
attempted here, unlike scan_results/saved_tokens (0027) which at least
have a best-effort join available; audit_logs has no comparable joinable
field at all.

Revision ID: 0028
Revises: 0027
Create Date: 2026-08-12
"""

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("audit_logs", sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=True))


def downgrade() -> None:
    op.drop_column("audit_logs", "tenant_id")
