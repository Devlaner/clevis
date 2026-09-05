"""Add github_installations.granted_permissions / permissions_synced_at.

Records the GitHub App ``permissions`` object as last observed for an installation, so
Clevis can tell an org admin which optional write automations (issues #286–#291) are
currently blocked by a missing scope and prompt them to re-approve the installation.

Populated by the ``installation.new_permissions_accepted`` webhook and opportunistically
during install sync (both read GitHub's ``permissions`` dict, which every caller
previously discarded).

Both columns are nullable and additive — no backfill, no data touched, zero data-loss
risk. Existing rows keep ``NULL`` (rendered as "permissions not yet checked" in the UI)
until the next permission-accept webhook or a reconnect. Downgrade drops both columns.

RLS is unaffected: ``github_installations``'s ``tenant_isolation`` policy (migrations
0030/0031) filters on ``tenant_id``, which is unchanged. Postgres table-level privileges
already extend to new columns, so no new GRANT is needed for ``clevis_api`` /
``clevis_worker``.

Revision ID: 0044
Revises: 0043
Create Date: 2026-09-04
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "github_installations",
        sa.Column("granted_permissions", JSONB(), nullable=True),
    )
    op.add_column(
        "github_installations",
        sa.Column("permissions_synced_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("github_installations", "permissions_synced_at")
    op.drop_column("github_installations", "granted_permissions")
