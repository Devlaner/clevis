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

Uses a composite FK, (orgs.tenant_id, orgs.id) -> (tenants.id, tenants.org_id),
instead of a plain single-column FK to tenants.id (per CodeRabbit review on
#190's PR 3: a plain FK lets orgs.tenant_id point at a personal tenant,
another org's tenant, or a tenant shared by multiple orgs -- nothing catches
a backfill/dual-write bug that assigns the wrong tenant). The composite FK
requires that whatever tenant orgs.tenant_id names must itself have
org_id = this exact org's id, enforcing the reciprocal 1:1 association at
the database level. Depends on migration 0021's uq_tenants_id_org_id unique
constraint on tenants(id, org_id), which the composite FK references.

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
    op.add_column("orgs", sa.Column("tenant_id", sa.Integer(), nullable=True))
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE orgs SET tenant_id = t.id "
            "FROM tenants t WHERE t.org_id = orgs.id AND t.kind = 'org'"
        )
    )
    op.create_foreign_key(
        "fk_orgs_tenant_id_reciprocal",
        "orgs",
        "tenants",
        ["tenant_id", "id"],
        ["id", "org_id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_orgs_tenant_id_reciprocal", "orgs", type_="foreignkey")
    op.drop_column("orgs", "tenant_id")
