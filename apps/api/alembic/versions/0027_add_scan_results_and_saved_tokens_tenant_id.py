"""Add tenant_id to scan_results and saved_tokens, best-effort backfill, stays nullable (issue #190, PR 3 of 7).

Unlike orgs/invitations/github_installations, neither table has a real FK to
join through: scan_results.owner and saved_tokens.org are free-text GitHub
login strings, matched against orgs.github_login on a best-effort basis.
Legacy rows that don't match any known org (renamed/deleted org, or a
personal-endpoint scan where owner is a user's own login rather than an
org) are left with tenant_id NULL rather than guessed at or dropped --
documented here, not chased. scan_results additionally falls back to the
scanning user's personal tenant via scanned_by_user_id when set.

Revision ID: 0027
Revises: 0026
Create Date: 2026-08-12
"""

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("scan_results", sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=True))
    op.add_column("saved_tokens", sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=True))

    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE scan_results sr SET tenant_id = t.id "
            "FROM orgs o JOIN tenants t ON t.org_id = o.id AND t.kind = 'org' "
            "WHERE o.github_login = sr.owner"
        )
    )
    conn.execute(
        sa.text(
            "UPDATE scan_results sr SET tenant_id = t.id "
            "FROM tenants t "
            "WHERE sr.tenant_id IS NULL AND sr.scanned_by_user_id IS NOT NULL "
            "AND t.personal_user_id = sr.scanned_by_user_id AND t.kind = 'personal'"
        )
    )
    conn.execute(
        sa.text(
            "UPDATE saved_tokens st SET tenant_id = t.id "
            "FROM orgs o JOIN tenants t ON t.org_id = o.id AND t.kind = 'org' "
            "WHERE o.github_login = st.org"
        )
    )


def downgrade() -> None:
    op.drop_column("saved_tokens", "tenant_id")
    op.drop_column("scan_results", "tenant_id")
