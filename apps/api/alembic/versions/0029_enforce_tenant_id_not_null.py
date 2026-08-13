"""Enforce NOT NULL on invitations/github_installations.tenant_id (issue #190, PR 5a of 7).

Migrations 0025/0026 added these columns nullable because, at the time, no
application code populated tenant_id yet -- enforcing NOT NULL then would
have broken installation/invitation creation the moment those migrations
deployed. PR 4 (dual-write, already merged) closed that gap: every write
path now sets tenant_id via src.repositories.tenant_repo. This migration
re-runs each column's original backfill UPDATE (idempotent -- a no-op for
already-tenanted rows) to catch any row created in the window between PR 3's
migrations landing and PR 4's dual-write code actually deploying, then
enforces NOT NULL.

orgs.tenant_id is deliberately NOT included here and stays nullable
permanently: creating a brand-new org requires inserting both the Org row
(needs tenant_id) and its reciprocal Tenant row (needs org_id via
fk_orgs_tenant_id_reciprocal's composite FK, migration 0024) -- each
references the other's not-yet-existing id. Postgres NOT NULL constraints
cannot be deferred (only FK/UNIQUE/PK/EXCLUSION constraints can), so there
is no way to insert a genuinely new org+tenant pair in one transaction if
orgs.tenant_id is hard NOT NULL, short of a more complex deferred-FK
migration (an explicit tradeoff, not attempted here -- see the PR
description). org_repo.get_or_create's self-healing dual-write plus the
verification queries below are the ongoing guarantee instead, the same
pattern already accepted for scan_results/saved_tokens.tenant_id.

Data-loss/backfill risk (AGENTS.md guardrail): if a real gap exists beyond
what the backfill UPDATE can resolve (e.g. an installation/invitation whose
org's tenant row was never created, not just never linked), the
ALTER COLUMN ... SET NOT NULL below fails atomically and the whole migration
rolls back -- it does not silently succeed with missing data. Run the
verification queries below against the target environment before applying
this migration there, and investigate any nonzero count rather than
retrying blindly. Verified end-to-end against real Postgres for this PR: a
deliberately-seeded gap (an org with no tenant row, an invitation pointing
at it) reproduced the atomic-rollback failure below before being fixed.

    -- every org has a reciprocal tenant
    SELECT count(*) FROM orgs o WHERE NOT EXISTS (
        SELECT 1 FROM tenants t WHERE t.org_id = o.id AND t.kind = 'org');

    -- invitations/github_installations.tenant_id are populated
    SELECT count(*) FROM invitations WHERE tenant_id IS NULL;
    SELECT count(*) FROM github_installations WHERE tenant_id IS NULL;

    -- every org_membership has a matching Membership row (dual-write parity)
    SELECT count(*) FROM org_memberships om JOIN orgs o ON o.id = om.org_id
    WHERE o.tenant_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM memberships m WHERE m.tenant_id = o.tenant_id
        AND m.user_id = om.user_id AND m.role = om.role);

Revision ID: 0029
Revises: 0028
Create Date: 2026-08-13
"""

import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(
        sa.text(
            "UPDATE invitations i SET tenant_id = t.id "
            "FROM tenants t WHERE t.org_id = i.org_id AND t.kind = 'org' AND i.tenant_id IS NULL"
        )
    )
    op.alter_column("invitations", "tenant_id", nullable=False)

    conn.execute(
        sa.text(
            "UPDATE github_installations gi SET tenant_id = t.id "
            "FROM tenants t "
            "WHERE gi.tenant_id IS NULL "
            "AND ((gi.org_id IS NOT NULL AND t.org_id = gi.org_id AND t.kind = 'org') "
            "OR (gi.owner_user_id IS NOT NULL AND t.personal_user_id = gi.owner_user_id AND t.kind = 'personal'))"
        )
    )
    op.alter_column("github_installations", "tenant_id", nullable=False)


def downgrade() -> None:
    op.alter_column("github_installations", "tenant_id", nullable=True)
    op.alter_column("invitations", "tenant_id", nullable=True)
