"""Backfill one tenants/memberships row per existing org/org_membership (issue #190, PR 2 of 7).

Zero ambiguity backfill: every `orgs` row gets exactly one `tenants` row
(kind='org'), and every `org_memberships` row gets exactly one `memberships`
row pointing at that org's new tenant, carrying the same role. Nothing here
is read by application code yet (dual-write/cutover are later PRs in the
#190 plan), so this is safe to run against any existing deployment.

Personal tenants (one per `users` row) are added in a later PR, not here --
see the design comment on #190.

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-12
"""

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("INSERT INTO tenants (kind, org_id) SELECT 'org', id FROM orgs"))
    conn.execute(
        sa.text(
            "INSERT INTO memberships (tenant_id, user_id, role) "
            "SELECT t.id, om.user_id, om.role "
            "FROM org_memberships om "
            "JOIN tenants t ON t.org_id = om.org_id AND t.kind = 'org'"
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "DELETE FROM memberships WHERE tenant_id IN (SELECT id FROM tenants WHERE kind = 'org')"
        )
    )
    conn.execute(sa.text("DELETE FROM tenants WHERE kind = 'org'"))
