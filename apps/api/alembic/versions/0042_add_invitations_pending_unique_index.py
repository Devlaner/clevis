"""Partial unique index on invitations(org_id, lower(email)) where status='pending'.

Issue #270: create_invitation's "does a pending invite already exist" guard is a
non-atomic check-then-insert with no DB constraint behind it (unlike the sibling
org_membership_repo.get_or_create, which relies on uq_org_memberships_org_user).
Two concurrent POSTs for the same org+email can both pass the check and both
insert a pending row. This adds the missing partial unique index so the losing
insert fails with IntegrityError, which invitation_repo.create now catches.

Schema change: one partial unique index. No column changes, no NOT NULL, no data
rewrite. Existing *active* duplicate pending invites -- the exact bug this closes
-- would fail the unique index build, so upgrade() first collapses already-lapsed
'pending' rows to 'expired' (the app already treats them as expired) and then
fails loudly listing any genuine duplicates rather than erroring mid-build.

Revision ID: 0042
Revises: 0041
Create Date: 2026-09-02
"""

import sqlalchemy as sa
from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None

INDEX_NAME = "uq_invitations_org_email_pending"


def upgrade() -> None:
    conn = op.get_bind()
    # Hold a write lock on invitations for the whole migration transaction. Without it
    # a concurrent INSERT can land after the duplicate scan below but before
    # create_index() takes its own lock, so the index build fails with a generic
    # duplicate-key error instead of the explicit listing this migration produces.
    # SHARE ROW EXCLUSIVE conflicts with INSERT/UPDATE but not with reads.
    conn.execute(sa.text("LOCK TABLE invitations IN SHARE ROW EXCLUSIVE MODE"))
    conn.execute(
        sa.text(
            "UPDATE invitations SET status = 'expired' "
            "WHERE status = 'pending' AND expires_at <= now()"
        )
    )
    dupes = conn.execute(
        sa.text(
            """
            SELECT org_id, lower(email) AS email, count(*) AS n
            FROM invitations
            WHERE status = 'pending'
            GROUP BY org_id, lower(email)
            HAVING count(*) > 1
            """
        )
    ).fetchall()
    if dupes:
        listing = ", ".join(f"org_id={r.org_id} email={r.email} (x{r.n})" for r in dupes)
        raise RuntimeError(
            f"Cannot create {INDEX_NAME}: duplicate active pending invitations exist. "
            f"Revoke the extras, then re-run this migration: {listing}"
        )
    op.create_index(
        INDEX_NAME,
        "invitations",
        ["org_id", sa.text("lower(email)")],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="invitations")
