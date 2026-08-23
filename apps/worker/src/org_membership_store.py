"""org_members + repo_collaborators write path (Collaborators PR 1 of 3).

Both tables represent current state, not a log -- upserted on add/edit, deleted on remove --
unlike repo_events_store's insert-and-skip shape. Mirrors security_alerts_store.py's separation
from event_consumer.py.

Ordering: GitHub doesn't guarantee webhook delivery order, and this consumer's own reclaim path
(event_consumer.py's _sweep_pending) can retry a crashed-then-recovered delivery well after a
later delivery for the same login/repo already processed successfully -- so an "add" and a
"remove" for the same key can be applied out of order. Every write here is guarded by the
event's own received_at (the same ingestion-time signal repo_events uses for occurred_at) against
the stored row's added_at/granted_at: an upsert only applies if it isn't older than what's
already stored (CodeRabbit finding on Collaborators PR 1), and a removal only applies if the
event that caused it isn't older than the row's last write -- so a stale "removed" can't delete a
grant a newer "added" already re-established, and a stale "added" can't resurrect/overwrite a
newer "removed". Residual gap, accepted rather than solved here: a "removed" that's processed
*before* a still-older "added" for a login with no existing row at all has nothing to compare
against (the row doesn't exist yet) and will still be clobbered by that late-arriving stale
"added" -- closing that fully would need a tombstone (remembering removed logins even after
deletion), not just a comparison against a row that no longer exists. Narrow window, not
addressed in this PR.
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
    for the full role-staleness rationale, and this module's own docstring for the
    added_at ordering guard.
    """
    cur.execute(
        """
        INSERT INTO org_members (tenant_id, login, avatar_url, role, added_at)
        VALUES (%(tenant_id)s, %(login)s, %(avatar_url)s, %(role)s, %(added_at)s)
        ON CONFLICT (tenant_id, login)
        DO UPDATE SET avatar_url = EXCLUDED.avatar_url, added_at = EXCLUDED.added_at
        WHERE EXCLUDED.added_at >= org_members.added_at
        """,
        {"tenant_id": tenant_id, "login": login, "avatar_url": avatar_url, "role": role, "added_at": added_at},
    )


def remove_org_member(cur: psycopg.Cursor, *, tenant_id: int, login: str, event_received_at: datetime) -> None:
    """Deletes an org_members row, but only if the removal event isn't older than the
    row's last write (`added_at`) -- see this module's own docstring for why."""
    cur.execute(
        "DELETE FROM org_members WHERE tenant_id = %(tenant_id)s AND login = %(login)s AND added_at <= %(event_received_at)s",
        {"tenant_id": tenant_id, "login": login, "event_received_at": event_received_at},
    )


def upsert_repo_collaborator(
    cur: psycopg.Cursor,
    *,
    tenant_id: int,
    repo: str,
    login: str,
    permission: str,
    is_outside_collaborator: bool | None,
    granted_at: datetime,
) -> None:
    """Upserts a repo_collaborators row keyed on (tenant_id, repo, login) -- migration
    0040's uq_repo_collaborators_tenant_repo_login. `source` is always 'direct' here --
    this is only ever called from the `member` event branch (team-based access is
    deferred, see migration 0040's docstring).

    `is_outside_collaborator` is only set on a true INSERT, same reasoning as
    org_members.role in upsert_org_member: this ingestion path can't determine it (always
    None from the `member` event alone), and a conflict (redelivered/edited event) must
    not clobber a value the future reconciliation poll (Collaborators PR 2) may have
    already filled in. See this module's own docstring for the granted_at ordering guard.
    """
    cur.execute(
        """
        INSERT INTO repo_collaborators
            (tenant_id, repo, login, permission, source, is_outside_collaborator, granted_at)
        VALUES (%(tenant_id)s, %(repo)s, %(login)s, %(permission)s, 'direct',
                %(is_outside_collaborator)s, %(granted_at)s)
        ON CONFLICT (tenant_id, repo, login)
        DO UPDATE SET permission = EXCLUDED.permission, granted_at = EXCLUDED.granted_at
        WHERE EXCLUDED.granted_at >= repo_collaborators.granted_at
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


def remove_repo_collaborator(cur: psycopg.Cursor, *, tenant_id: int, repo: str, login: str, event_received_at: datetime) -> None:
    """Deletes a repo_collaborators row, but only if the removal event isn't older than
    the row's last write (`granted_at`) -- see this module's own docstring for why."""
    cur.execute(
        "DELETE FROM repo_collaborators WHERE tenant_id = %(tenant_id)s AND repo = %(repo)s AND login = %(login)s "
        "AND granted_at <= %(event_received_at)s",
        {"tenant_id": tenant_id, "repo": repo, "login": login, "event_received_at": event_received_at},
    )
