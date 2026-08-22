"""Add org_members + repo_collaborators tables + grants (Collaborators PR 1 of 3).

Normalized store for the org membership / repo access webhook events durably queued since
this PR (member/organization/membership/team -- see apps/api/src/routers/webhooks.py's
_INGESTED_EVENT_TYPES). Populated by apps/worker's event_consumer.py.

Two tables, not one polymorphic table like security_alerts (migration 0039): a person (org
membership) and a repo-access grant are genuinely different shapes with different keys, unlike
the three GitHub alert kinds which shared one spine. Both represent *current state*, not a log
(rows are deleted on removal, not soft-marked) -- closer to activity_sync_cursors's posture
than repo_events's append-only one.

org_members.role is captured from the `organization` webhook event's member_added payload
(`membership.role`, e.g. "member"/"admin") at add-time -- accurate then. But no webhook event
covers a role changing *after* that (verified against GitHub's webhook docs -- no
member_updated/role-change action exists in the organization event's action list), so this
column can silently go stale for an existing member whose role later changes, until a future
reconciliation poll (Collaborators PR 2, not built yet) corrects it -- that poll, not this
ingestion path, is the only source of truth for an existing member's *current* role. Documented
here so a future reader doesn't assume this table's role column always reflects live GitHub
state.

repo_collaborators.source is 'direct' only in this PR -- team-based repo access ('team') is
explicitly deferred: computing it requires joining the team webhook event's
added_to_repository/removed_from_repository actions against the membership event's per-team
roster to derive effective per-user access, a materially bigger modeling problem than a direct
member-event grant, and GitHub's own team-event payloads don't reliably convey the granted
permission level either. membership/team events are durably queued (this PR's webhooks.py
change) but acked-and-skipped by the consumer for now, not silently dropped -- see
event_consumer.py's own guard for the exact mechanism, mirroring the security-alert stage's PR 1
placeholder pattern.

RLS is ENABLE-only, no FORCE -- same reasoning as every migration since 0030: the table-owning
migration role is unaffected either way, and this only starts enforcing for a non-owner role
once that role is actually granted access to it (this migration).

Grants mirror migration 0039 exactly: clevis_worker gets SELECT+INSERT+UPDATE+DELETE (it
upserts and deletes, unlike repo_events' insert-only shape) and clevis_api gets the same grant,
not because the API writes these tables yet, but because CI runs apps/worker's tests under
DATABASE_URL=clevis_api (no clevis_worker CI provisioning exists) -- without this, the new
consumer's own tests would fail in CI with the same InsufficientPrivilege class of bug already
hit on migrations 0035/0036/0039 (0039's own inline grant was a no-op in CI for the same root
cause: docker/provision-api-role-existing-deployment.sh provisions clevis_api *after* migrations
run there, so this migration's guarded grant never finds the role -- the matching guarded block
is added to that script in this same PR, not as a follow-up fix).

Upgrade is purely additive (two new tables, four conditional grants) -- zero data-loss risk.
Downgrade drops both tables; safe since nothing reads them yet (Collaborators PR 3 hasn't
shipped).

Revision ID: 0040
Revises: 0039
Create Date: 2026-08-22
"""

import sqlalchemy as sa
from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None

_TENANT_FILTER = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::int"


def upgrade() -> None:
    op.create_table(
        "org_members",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("login", sa.String(), nullable=False),
        sa.Column("avatar_url", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False, server_default="member"),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_org_members_tenant_id", "org_members", ["tenant_id"])
    op.create_unique_constraint("uq_org_members_tenant_login", "org_members", ["tenant_id", "login"])

    op.create_table(
        "repo_collaborators",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("repo", sa.String(), nullable=False),
        sa.Column("login", sa.String(), nullable=False),
        sa.Column("permission", sa.String(), nullable=False),
        # 'direct' only in this PR -- see this migration's own docstring for why 'team' is
        # deferred rather than built now.
        sa.Column("source", sa.String(), nullable=False, server_default="direct"),
        sa.Column("is_outside_collaborator", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_repo_collaborators_tenant_id", "repo_collaborators", ["tenant_id"])
    op.create_index("ix_repo_collaborators_tenant_id_repo", "repo_collaborators", ["tenant_id", "repo"])
    op.create_unique_constraint(
        "uq_repo_collaborators_tenant_repo_login", "repo_collaborators", ["tenant_id", "repo", "login"]
    )

    for table in ("org_members", "repo_collaborators"):
        op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        op.execute(
            sa.text(f"CREATE POLICY tenant_isolation ON {table} USING ({_TENANT_FILTER}) WITH CHECK ({_TENANT_FILTER})")
        )

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'clevis_worker') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON org_members TO clevis_worker;
                GRANT USAGE, SELECT ON org_members_id_seq TO clevis_worker;
                GRANT SELECT, INSERT, UPDATE, DELETE ON repo_collaborators TO clevis_worker;
                GRANT USAGE, SELECT ON repo_collaborators_id_seq TO clevis_worker;
            END IF;
        END
        $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'clevis_api') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON org_members TO clevis_api;
                GRANT USAGE, SELECT ON org_members_id_seq TO clevis_api;
                GRANT SELECT, INSERT, UPDATE, DELETE ON repo_collaborators TO clevis_api;
                GRANT USAGE, SELECT ON repo_collaborators_id_seq TO clevis_api;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'clevis_worker') THEN
                REVOKE SELECT, INSERT, UPDATE, DELETE ON org_members FROM clevis_worker;
                REVOKE USAGE, SELECT ON org_members_id_seq FROM clevis_worker;
                REVOKE SELECT, INSERT, UPDATE, DELETE ON repo_collaborators FROM clevis_worker;
                REVOKE USAGE, SELECT ON repo_collaborators_id_seq FROM clevis_worker;
            END IF;
        END
        $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'clevis_api') THEN
                REVOKE SELECT, INSERT, UPDATE, DELETE ON org_members FROM clevis_api;
                REVOKE USAGE, SELECT ON org_members_id_seq FROM clevis_api;
                REVOKE SELECT, INSERT, UPDATE, DELETE ON repo_collaborators FROM clevis_api;
                REVOKE USAGE, SELECT ON repo_collaborators_id_seq FROM clevis_api;
            END IF;
        END
        $$;
        """
    )
    for table in ("org_members", "repo_collaborators"):
        op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))
    op.drop_table("repo_collaborators")
    op.drop_table("org_members")
