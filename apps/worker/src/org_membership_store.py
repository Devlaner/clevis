"""org_members + repo_collaborators write path (Collaborators PR 1 of 3).

Both tables represent current state, not a log -- upserted on add/edit, deleted on remove --
unlike repo_events_store's insert-and-skip shape. Mirrors security_alerts_store.py's separation
from event_consumer.py.
"""

from datetime import datetime

import psycopg


def upsert_org_member(
    cur: psycopg.Cursor, *, tenant_id: int, login: str, avatar_url: str, role: str, added_at: datetime
) -> None:
    """Upserts an org_members row keyed on (tenant_id, login) -- migration 0040's
    uq_org_members_tenant_login. `role` is only set on a true INSERT (first time this
    login is seen for this tenant); a conflict (redelivered member_added, or a re-add
    after a prior removal) leaves the existing row's role untouched -- a future
    reconciliation poll (Collaborators PR 2) may have already corrected it since the
    event this payload came from was generated, and blindly overwriting with a possibly
    stale event-time snapshot would undo that correction. See migration 0040's docstring
    for the full role-staleness rationale.
    """
    cur.execute(
        """
        INSERT INTO org_members (tenant_id, login, avatar_url, role, added_at)
        VALUES (%(tenant_id)s, %(login)s, %(avatar_url)s, %(role)s, %(added_at)s)
        ON CONFLICT (tenant_id, login)
        DO UPDATE SET avatar_url = EXCLUDED.avatar_url, added_at = EXCLUDED.added_at
        """,
        {"tenant_id": tenant_id, "login": login, "avatar_url": avatar_url, "role": role, "added_at": added_at},
    )


def remove_org_member(cur: psycopg.Cursor, *, tenant_id: int, login: str) -> None:
    cur.execute(
        "DELETE FROM org_members WHERE tenant_id = %(tenant_id)s AND login = %(login)s",
        {"tenant_id": tenant_id, "login": login},
    )


def upsert_repo_collaborator(
    cur: psycopg.Cursor,
    *,
    tenant_id: int,
    repo: str,
    login: str,
    permission: str,
    is_outside_collaborator: bool,
    granted_at: datetime,
) -> None:
    """Upserts a repo_collaborators row keyed on (tenant_id, repo, login) -- migration
    0040's uq_repo_collaborators_tenant_repo_login. `source` is always 'direct' here --
    this is only ever called from the `member` event branch (team-based access is
    deferred, see migration 0040's docstring)."""
    cur.execute(
        """
        INSERT INTO repo_collaborators
            (tenant_id, repo, login, permission, source, is_outside_collaborator, granted_at)
        VALUES (%(tenant_id)s, %(repo)s, %(login)s, %(permission)s, 'direct',
                %(is_outside_collaborator)s, %(granted_at)s)
        ON CONFLICT (tenant_id, repo, login)
        DO UPDATE SET
            permission = EXCLUDED.permission,
            is_outside_collaborator = EXCLUDED.is_outside_collaborator,
            granted_at = EXCLUDED.granted_at
        """,
        {
            "tenant_id": tenant_id,
            "repo": repo,
            "login": login,
            "permission": permission,
            "is_outside_collaborator": is_outside_collaborator,
            "granted_at": granted_at,
        },
    )


def remove_repo_collaborator(cur: psycopg.Cursor, *, tenant_id: int, repo: str, login: str) -> None:
    cur.execute(
        "DELETE FROM repo_collaborators WHERE tenant_id = %(tenant_id)s AND repo = %(repo)s AND login = %(login)s",
        {"tenant_id": tenant_id, "repo": repo, "login": login},
    )
