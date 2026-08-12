"""Add tenants and memberships tables (issue #190, S2 multi-tenancy PR 2 of 7).

Pure additive schema change, no backfill and no existing table touched --
zero data-loss risk. See the design comment on #190 for the full plan this
is one step of: https://github.com/nazarli-shabnam/clevis/issues/190

`tenants` is a new table, not a rename of `orgs` -- every org gets a 1:1
`tenants` row (kind='org') and every user gets an implicit personal tenant
(kind='personal', added in a later PR), so personal-scope resources get
real DB-level isolation once Row-Level Security lands, not just
`owner_user_id` filtering. `memberships` mirrors `org_memberships`'
role vocabulary ("admin"|"member") to avoid renaming that concept mid-flight.

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-12
"""

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.Text(), nullable=False),  # 'org' | 'personal'
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("orgs.id"), nullable=True),
        sa.Column("personal_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "(kind = 'org' AND org_id IS NOT NULL AND personal_user_id IS NULL) OR "
            "(kind = 'personal' AND org_id IS NULL AND personal_user_id IS NOT NULL)",
            name="ck_tenants_kind_xor",
        ),
    )
    op.create_index(
        "uq_tenants_org_id",
        "tenants",
        ["org_id"],
        unique=True,
        postgresql_where=sa.text("org_id IS NOT NULL"),
    )
    op.create_index(
        "uq_tenants_personal_user_id",
        "tenants",
        ["personal_user_id"],
        unique=True,
        postgresql_where=sa.text("personal_user_id IS NOT NULL"),
    )
    # (id, org_id) as a composite unique constraint -- redundant with id's own PK
    # uniqueness on its own, but required so a later composite FK from orgs.tenant_id
    # can reference this exact column pair (see migration 0024's reciprocal-association
    # fix on orgs.tenant_id).
    op.create_unique_constraint("uq_tenants_id_org_id", "tenants", ["id", "org_id"])

    op.create_table(
        "memberships",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("role", sa.String(), nullable=False),  # 'admin' | 'member'
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "user_id", name="uq_memberships_tenant_user"),
    )


def downgrade() -> None:
    op.drop_table("memberships")
    op.drop_constraint("uq_tenants_id_org_id", "tenants", type_="unique")
    op.drop_index("uq_tenants_personal_user_id", table_name="tenants")
    op.drop_index("uq_tenants_org_id", table_name="tenants")
    op.drop_table("tenants")
