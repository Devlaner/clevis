"""enforce case-insensitive uniqueness on users.email

Issue #268: users.email had a plain (case-sensitive) unique constraint, and
every place that checks "does an account with this email exist" (register(),
login(), find_or_create_user()) compared case-sensitively too. Real mailboxes
are effectively case-insensitive, so "alice@example.com" and
"Alice@example.com" could register as two fully independent accounts --
undermining the identity-uniqueness invariant #217's email-verification work
relies on ("an account with this email" is unique).

Fix: normalize existing emails to lowercase, then replace the plain unique
constraint with a unique index on lower(email) so the DB enforces case-
insensitivity going forward (application code in auth.py/github_auth.py is
updated separately to query via lower(email) so it can't race past this).

Data-loss / backfill risk: if two *different* existing accounts already share
an email address differing only by case, lowercasing would collide and one
insert would fail outright when the new index is created -- this migration
detects that up front and raises before making any change, rather than
silently merging or dropping either account. No such collision is expected in
practice (case-sensitive uniqueness has been enforced since 0004), but this
guards against it if the check is ever wrong or data was loaded out of band.

Revision ID: 0019
Revises: 0018
Create Date: 2026-07-25
"""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    collisions = conn.execute(
        sa.text(
            "SELECT lower(email) FROM users GROUP BY lower(email) HAVING COUNT(*) > 1"
        )
    ).fetchall()
    if collisions:
        raise RuntimeError(
            "Cannot enforce case-insensitive email uniqueness: "
            f"{len(collisions)} email address(es) are shared by multiple existing users, "
            "differing only by case. Query \"SELECT lower(email) FROM users GROUP BY "
            "lower(email) HAVING COUNT(*) > 1\" to find them, then resolve these duplicate "
            "accounts manually (merge or rename one) before re-running this migration."
        )
    conn.execute(sa.text("UPDATE users SET email = lower(email) WHERE email <> lower(email)"))
    op.drop_constraint("users_email_key", "users", type_="unique")
    op.create_index("uq_users_email_lower", "users", [sa.text("lower(email)")], unique=True)


def downgrade() -> None:
    op.drop_index("uq_users_email_lower", table_name="users")
    op.create_unique_constraint("users_email_key", "users", ["email"])
