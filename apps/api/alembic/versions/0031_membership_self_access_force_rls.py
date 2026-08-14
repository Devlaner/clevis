"""Widen memberships/github_installations RLS with self-access + enable FORCE (issue #190, PR 6c).

*** IMPORTANT CAVEAT, discovered while building this migration ***
The API's own DB role (DB_USER, e.g. "clevis") is a Postgres SUPERUSER in the default
docker-compose deployment: docker-compose.yml sets POSTGRES_USER: ${DB_USER}, and the
official postgres image always makes that role the initdb bootstrap superuser -- there is
no way to make it non-superuser through that env var alone. Postgres superusers
unconditionally bypass RLS, ENABLE or FORCE, no exceptions -- so as of this migration, FORCE
ROW LEVEL SECURITY has NO EFFECT on the actual running system, despite everything in this
migration and its policies being logically correct (verified against a manually-created
non-superuser test role, not the app's real connection). This mirrors a gap migration 0020
already solved for the *worker* (a dedicated clevis_worker non-superuser role with explicit
GRANTs) -- the API itself never got the equivalent treatment. Giving the API its own
non-superuser role is separate, dedicated infrastructure work (new role, GRANTs, connection
string/entrypoint changes) tracked as a follow-up, not folded into this migration. Everything
here is still worth doing now: it's the correct, safe policy design and the FORCE flag itself
is inert-but-harmless until that follow-up lands, at which point it starts actually enforcing
with no further schema change needed.

Migration 0030 enabled RLS (ENABLE, not FORCE) and added tenant_id-equality policies, but
left FORCE off precisely because a write-path audit (done for this migration) found every
write into `memberships` in the entire codebase never ran through require_org_role/
require_personal_tenant -- the only places that set the app.tenant_id session variable
migration 0030's policies read. Under FORCE, those policies' implicit WITH CHECK (Postgres
defaults it to the same expression as USING when a policy omits it) would reject every one
of those writes.

The audit (see PR description) found something useful: every single one of those writes is
a *self-write* -- the row's user_id (memberships) or owner_user_id (github_installations,
personal installations only) always equals the currently-authenticated caller:
  - installations.py's _bootstrap_org_admin_from_installation (org-admin bootstrap)
  - installations.py's sync_personal_installation (via installation_repo.upsert's internal
    tenant_repo.ensure_personal_tenant call)
  - invitations.py's accept_invitation
  - org_provisioning.py's sync_org_admin_memberships (OAuth-login provisioning, both loops)
  - rbac.py's own require_personal_tenant had an internal ordering bug: it wrote the
    membership *before* calling set_tenant_session_context -- even the "guarded" path
    wasn't guarded for its own write
  - auth.py's setup/register and github_auth.py's find_or_create_user (pre-login personal
    tenant/membership creation, same transaction as the new User row)

Rather than add a tenant-resolving call at each of those sites individually (fragile, as
require_personal_tenant's own bug demonstrates -- easy to miss one as the code evolves),
the app-code half of this PR sets app.user_id broadly in require_auth itself (the one
dependency nearly every authenticated route already uses, via the new
src.core.db.set_session_user), plus a few explicit calls at routes with no request-scoped
caller identity (pre-login flows, the OAuth callback's multi-org provisioning loop). This
migration is the matching database-side half: widen the policies with an additional
self-access clause, so a row is also visible/writable when its own user_id/owner_user_id
matches app.user_id, regardless of tenant session context.

This is safe: since every real write is confirmed self-scoped, letting a user always read/
write their OWN membership or personal-installation row can never leak another tenant's or
another user's data -- the isolation boundary that actually matters (a *mismatched*
user_id) is still fully enforced by the unchanged tenant_id-equality half of each policy.
Verified against real Postgres (see PR description): a non-owner test role CAN read/write
its own row regardless of session tenant context, and CANNOT read/write a *different*
user's row in a tenant it doesn't belong to.

github_installations' org-scoped rows (org_id set, owner_user_id NULL) have no per-row user
column, so the self-access clause only helps owner_user_id-scoped (personal) rows. The one
remaining org-scoped write (installations.py's sync_org_installation, installation_repo.create)
now calls set_tenant_session_context explicitly instead, once org.tenant_id is resolved (it
already is, from ensure_tenant_linked -- issue #190 step 6a).

FORCE ROW LEVEL SECURITY is added here for memberships, github_installations, and
scan_results (already write-safe since PR #327's tenant_id backfill -- both its write paths
run after a real tenant is resolved via require_org_role/ensure_personal_tenant, no
self-access clause needed there).

orgs, invitations, audit_logs, and saved_tokens are deliberately NOT forced, and this is an
architectural exclusion, not a deferral:
  - orgs (org_repo.get_by_login) and invitations (invitation_repo.get_by_token) are both
    read to *discover* which tenant something belongs to -- a read that structurally can't
    be gated behind already knowing that tenant. FORCE would break org lookup and invitation
    acceptance outright, not just need a session-context fix.
  - audit_logs (routers/audit.py's list_audit_logs) and saved_tokens (routers/tokens.py's
    list_tokens/upsert_token/resolve_token/delete_token) are confirmed cross-tenant-by-design
    admin panels -- gated only by require_workspace_admin, intentionally listing across
    every org. FORCE would break them outright. BYPASSRLS isn't a fix either: it's an
    all-or-nothing role attribute that would neuter RLS for every query the app's one DB
    role makes, not just these two routers'.

Revision ID: 0031
Revises: 0030
Create Date: 2026-08-14
"""

import sqlalchemy as sa
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None

_TENANT_FILTER = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::int"
_USER_FILTER = "user_id = NULLIF(current_setting('app.user_id', true), '')::int"
_OWNER_USER_FILTER = "owner_user_id = NULLIF(current_setting('app.user_id', true), '')::int"

_FORCE_TABLES = ["memberships", "github_installations", "scan_results"]


def upgrade() -> None:
    op.execute(sa.text("DROP POLICY tenant_isolation ON memberships"))
    op.execute(
        sa.text(
            f"CREATE POLICY tenant_isolation ON memberships "
            f"USING ({_TENANT_FILTER} OR {_USER_FILTER}) "
            f"WITH CHECK ({_TENANT_FILTER} OR {_USER_FILTER})"
        )
    )

    op.execute(sa.text("DROP POLICY tenant_isolation ON github_installations"))
    op.execute(
        sa.text(
            f"CREATE POLICY tenant_isolation ON github_installations "
            f"USING ({_TENANT_FILTER} OR {_OWNER_USER_FILTER}) "
            f"WITH CHECK ({_TENANT_FILTER} OR {_OWNER_USER_FILTER})"
        )
    )

    for table in _FORCE_TABLES:
        op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))


def downgrade() -> None:
    for table in _FORCE_TABLES:
        op.execute(sa.text(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"))

    op.execute(sa.text("DROP POLICY tenant_isolation ON github_installations"))
    op.execute(
        sa.text(
            "CREATE POLICY tenant_isolation ON github_installations "
            f"USING ({_TENANT_FILTER})"
        )
    )

    op.execute(sa.text("DROP POLICY tenant_isolation ON memberships"))
    op.execute(sa.text(f"CREATE POLICY tenant_isolation ON memberships USING ({_TENANT_FILTER})"))
