"""Enable RLS + tenant-isolation policies as scaffolding (issue #190, PR 5).

Enables ROW LEVEL SECURITY on every tenant-scoped table and adds a policy
that compares tenant_id against the app.tenant_id session variable already
set on every request by src.core.rbac's require_org_role/require_personal_tenant
(see rbac.py's _set_tenant_session_context, from PR #324/issue #190 PR 5a).
No RLS policy read that session variable until now -- this migration is the
first thing to actually wire it up.

Deliberately does NOT use FORCE ROW LEVEL SECURITY. Without FORCE, Postgres
RLS policies do not apply to a table's owner, and the API's own DB role
(clevis / whatever DB_USER is configured) owns every table it created via
Alembic. So ENABLE-only RLS changes zero production behavior in this PR --
the app keeps reading/writing exactly as before. This is intentional: it
lets the policies exist and be exercised against a non-owner test role
(see this migration's own docstring verification block) before the actual
read-cutover (issue #190 PR 6) adds FORCE and starts relying on RLS to
enforce isolation for real. Treat FORCE as a separate, higher-risk change
for that later PR, not something to sneak in here.

Two policy shapes, matching the two tenant_id nullability groups already
established by migrations 0021-0029:

  Group A (tenant_id NOT NULL -- memberships, github_installations,
  invitations): strict equality. A row is visible only when its tenant_id
  matches app.tenant_id.

  Group B (tenant_id nullable -- orgs, saved_tokens, scan_results):
  equality-OR-NULL, since these columns are permanently or best-effort
  nullable by design (see each column's own comment in db.py) and existing
  app code does not gate reads on tenant_id for them yet. Excluding the
  OR-NULL clause here would make every legacy/unbackfilled row invisible
  the moment FORCE is ever turned on, which is a policy-design decision for
  PR 6, not this one.

  Exception within Group B: audit_logs. Per the design decision already
  recorded in db.py's AuditLog.tenant_id comment, null-tenant audit rows
  must stay invisible to ordinary tenant members (they predate tenant
  attribution and were never meant to be member-visible) -- reachable only
  via a future require_workspace_admin-gated path, explicitly out of scope
  here. So audit_logs gets the strict Group A equality policy despite its
  column being nullable.

users, tenants, org_memberships (legacy, superseded by memberships),
jobs, and app_config are not tenant-scoped and get no RLS.

worker's clevis_worker role (migration 0020) only ever touches jobs and
app_config -- verified by grepping apps/worker/src/*.py for every SQL
statement it issues -- neither of which gets RLS here. No BYPASSRLS grant
is added in this migration because none would be exercised; add one in a
later PR only if the worker starts touching an RLS-enabled table.

Verification (run against the target environment before relying on this):

    -- as the app's own (owning) role, confirm row counts are unaffected --
    -- ENABLE without FORCE must not change what the owner sees:
    SELECT count(*) FROM memberships;  -- compare pre/post migration

    -- as a non-owner role with row_security on, confirm isolation works,
    -- e.g. from psql:
    SET SESSION AUTHORIZATION some_non_owner_test_role;
    SET app.tenant_id = 1;
    SELECT count(*) FROM memberships;  -- only tenant 1's rows
    SELECT count(*) FROM audit_logs;   -- excludes NULL-tenant rows even
                                        -- though orgs/scan_results would
                                        -- include their NULL-tenant rows
    RESET SESSION AUTHORIZATION;

Revision ID: 0030
Revises: 0029
Create Date: 2026-08-14
"""

import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

_TENANT_FILTER = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::int"

# Strict equality: tenant_id is NOT NULL, or (audit_logs) must hide NULL rows by design.
_STRICT_TABLES = ["memberships", "github_installations", "invitations", "audit_logs"]

# Equality-OR-NULL: tenant_id is nullable by design and existing rows without it must
# stay visible until the read-cutover (PR 6) actively gates on tenant_id for them.
_NULLABLE_TABLES = ["orgs", "saved_tokens", "scan_results"]


def upgrade() -> None:
    for table in _STRICT_TABLES:
        op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        op.execute(
            sa.text(f"CREATE POLICY tenant_isolation ON {table} USING ({_TENANT_FILTER})")
        )

    for table in _NULLABLE_TABLES:
        op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        op.execute(
            sa.text(
                f"CREATE POLICY tenant_isolation ON {table} "
                f"USING (tenant_id IS NULL OR {_TENANT_FILTER})"
            )
        )


def downgrade() -> None:
    for table in _STRICT_TABLES + _NULLABLE_TABLES:
        op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))
        op.execute(sa.text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))
