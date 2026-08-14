"""stop tenant-gating orgs/saved_tokens/scan_results reads; loosen WITH CHECK for NULL tenant_id

Two related fixes to the orgs/saved_tokens/scan_results tenant_isolation policies, both
found by running the full test suite with the API connected as clevis_api (issue #330
PR 2) -- migration 0030 never got exercised against a genuinely non-owner, non-superuser
role before now, so these gaps were invisible until this cutover.

**WITH CHECK was too strict.** Migration 0030 deliberately gave orgs/saved_tokens/
scan_results a strict-equality WITH CHECK (no OR-NULL escape), reasoning that a future
write clearing tenant_id should be rejected rather than silently producing a row
invisible from every tenant forever -- and explicitly flagged that org creation, and
any SavedToken/ScanResult write omitting tenant_id, would fail under it once FORCE (or
a genuinely non-owner runtime role) made it actually apply.

  - orgs: org_repo.get_or_create always inserts a brand-new org with tenant_id=NULL,
    then a separate UPDATE links its tenant afterward (ensure_tenant_linked). This is
    structurally permanent, not a deferred gap to close -- creating the Tenant row
    requires the Org's id first (see the composite FK orgs(tenant_id, id) ->
    tenants(id, org_id), migration 0024), so the two rows can never be inserted
    atomically.

  - saved_tokens: tokens.py's upsert_token now best-effort resolves tenant_id at
    insert time, but it's a legacy fallback letting an admin save a token for
    literally any org string, including ones Clevis has never connected -- for that
    case tenant_id has no value to set and must legitimately stay NULL permanently,
    not just until a later write fills it in.

  - scan_results is deliberately NOT touched here: both of its real write paths
    (apps/api/src/routers/analytics.py's org and personal analytics flows) already
    pass a real tenant_id on every insert (confirmed by reading both call sites) --
    the only NULL-tenant scan_results inserts were test-seeding convenience calls,
    fixed directly in the test files instead of loosening a policy that production
    code already satisfies. Keeping WITH CHECK strict here preserves 0030's original
    protection against a future accidental NULL write on this table.

**USING (reads) was actively broken, not just strict.** orgs' original USING clause
(tenant_id IS NULL OR match) seemed permissive, but breaks the moment an org has a
real (non-NULL) tenant_id -- which is true almost immediately after creation. Any
lookup by github_login/github_org_id (org_repo.get_by_login, get_by_org_id -- used
everywhere: webhooks, rbac.resolve_org_role before any tenant is even known,
org_provisioning's multi-org reconciliation loop) silently returns nothing for an
org whose tenant doesn't match whatever app.tenant_id the current session happens to
have set, which for a multi-org operation is only ever true for one org at a time.
Confirmed as a real bug, not just a test artifact: org_provisioning.sync_org_admin_memberships
looping over a GitHub user's multiple org admin memberships tries to
org_repo.get_or_create every org in the list; once the first org's lookup pins
app.tenant_id, the second org's already-existing row becomes invisible under RLS,
and the code attempts to INSERT it again -- hitting a genuine unique constraint
violation on github_org_id. This mirrors exactly why the tenants table itself was
excluded from RLS entirely in the original #190 design (the org-switcher's
inherently cross-tenant "all my memberships" query) -- org identity/metadata
(login, github_org_id) isn't sensitive, and real access control is already enforced
downstream via memberships, not by hiding org rows. saved_tokens gets the same
USING fix for the same structural reason: it's queried by a free-text org string
across an unbounded set of orgs, and per test_owner_only_routes.py's own framing
it's "instance-wide data with no per-org column to scope by" -- entirely gated by
require_workspace_admin already, so tenant-gating its reads adds no real isolation,
only breaks lookups the same way orgs' did. scan_results gets the same USING fix for
the same structural reason, confirmed by GET /me/analytics/history?owner=... returning
403 for a legitimately-owned scan row: a personal user's scan history legitimately
spans scans across multiple different orgs/tenants in one session, and real access
control there is already enforced by the router's own scanned_by_user_id/membership
checks (apps/api/src/routers/analytics.py), not by RLS -- tenant-gating reads only
hides rows the app's own authorization logic already correctly permits. Unlike
orgs/saved_tokens, scan_results' WITH CHECK stays strict/unchanged (see above).

Migration 0030 is already merged and must not be hand-edited (AGENTS.md) -- this
ships the fix as a policy redefinition instead (DROP + CREATE, since ALTER POLICY
cannot change WITH CHECK for a FOR ALL policy created without USING/WITH CHECK
listed as separate clauses in one statement as cleanly as recreating it).

Data-loss / backfill risk: none. This migration only redefines two RLS policies; it
does not alter any table's schema or data.

Revision ID: 0034
Revises: 0033
Create Date: 2026-08-14
"""

import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

_TENANT_FILTER = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::int"

# orgs/saved_tokens: WITH CHECK loosened too (tenant_id IS NULL OR match) -- both have
# real, permanent NULL-tenant write paths (see this migration's module docstring).
_LOOSENED_WITH_CHECK_TABLES = ["orgs", "saved_tokens"]

# scan_results: USING loosened the same way (identity/history reads span multiple
# tenants in one session -- confirmed by GET /me/analytics/history?owner=... returning
# 403 for a legitimately-owned scan row under strict tenant-gated reads, since real
# access control here is already enforced by the router's own scanned_by_user_id/
# membership checks, not by RLS). WITH CHECK stays strict/unchanged: both real write
# paths (apps/api/src/routers/analytics.py's org and personal analytics flows) already
# always pass a real tenant_id, so there's no NULL-write case to loosen for.
_STRICT_WITH_CHECK_TABLES = ["scan_results"]


def upgrade() -> None:
    for table in _LOOSENED_WITH_CHECK_TABLES:
        op.execute(sa.text(f"DROP POLICY tenant_isolation ON {table}"))
        op.execute(
            sa.text(f"CREATE POLICY tenant_isolation ON {table} USING (true) WITH CHECK (tenant_id IS NULL OR {_TENANT_FILTER})")
        )
    for table in _STRICT_WITH_CHECK_TABLES:
        op.execute(sa.text(f"DROP POLICY tenant_isolation ON {table}"))
        op.execute(sa.text(f"CREATE POLICY tenant_isolation ON {table} USING (true) WITH CHECK ({_TENANT_FILTER})"))


def downgrade() -> None:
    for table in _LOOSENED_WITH_CHECK_TABLES:
        op.execute(sa.text(f"DROP POLICY tenant_isolation ON {table}"))
        op.execute(
            sa.text(
                f"CREATE POLICY tenant_isolation ON {table} "
                f"USING (tenant_id IS NULL OR {_TENANT_FILTER}) "
                f"WITH CHECK ({_TENANT_FILTER})"
            )
        )
    for table in _STRICT_WITH_CHECK_TABLES:
        op.execute(sa.text(f"DROP POLICY tenant_isolation ON {table}"))
        op.execute(
            sa.text(
                f"CREATE POLICY tenant_isolation ON {table} "
                f"USING (tenant_id IS NULL OR {_TENANT_FILTER}) "
                f"WITH CHECK ({_TENANT_FILTER})"
            )
        )
