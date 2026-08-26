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


def reconcile_org_members(cur: psycopg.Cursor, *, tenant_id: int, members: list[dict], synced_at: datetime) -> None:
    """Full-roster reconciliation write (Collaborators PR 2 of 3) -- the counterpart to
    upsert_org_member/remove_org_member above, called from the periodic poll instead of a
    single webhook event. Unlike the webhook path (which deliberately leaves `role` untouched
    on conflict so it doesn't clobber a correction this very function already made), this
    treats every field the poll saw as authoritative: `role` and `avatar_url` are always
    overwritten, since this poll IS the reconciliation for exactly the staleness
    upsert_org_member's own docstring describes. `two_factor_enabled` uses COALESCE instead of
    a blind overwrite -- the poll's 2FA overlay call is best-effort (see
    membership_reconcile.py's fetch_org_roster) and a caller passes None for it when that
    single call failed this cycle; COALESCE preserves a previously-known value instead of
    losing it to one transient failure.

    Also removes any existing org_members row for this tenant whose login the poll did NOT
    see -- a real departure the removal webhook missed (delivery lost, or arrived while this
    tenant had no App installation to receive it). This is the actual reconciliation: the
    webhook path alone can drift (this migration 0040's own docstring), and this poll is what
    corrects it, not just fills gaps.

    `added_at` is preserved for a login the poll already had a row for (its real
    webhook-sourced join time); for a login the poll discovers with no existing row (a missed
    member_added), added_at is set to `synced_at` -- the poll has no way to know the true join
    time, and "first seen by reconciliation" is a better lower-bound guess than leaving it
    unset. An empty `members` list is treated as a no-op, not "remove everyone" -- GitHub
    always has at least one org owner, so an empty roster from a real API call would be a bug
    somewhere upstream (a transient failure, a malformed response), not a genuine "this org now
    has zero members" state; wiping the table on that basis would be worse than doing nothing
    for one poll cycle."""
    if not members:
        return

    seen_logins = [m["login"] for m in members]
    for member in members:
        cur.execute(
            """
            INSERT INTO org_members (tenant_id, login, avatar_url, role, two_factor_enabled, added_at)
            VALUES (%(tenant_id)s, %(login)s, %(avatar_url)s, %(role)s, %(two_factor_enabled)s, %(synced_at)s)
            ON CONFLICT (tenant_id, login) DO UPDATE SET
                avatar_url = EXCLUDED.avatar_url,
                role = EXCLUDED.role,
                two_factor_enabled = COALESCE(EXCLUDED.two_factor_enabled, org_members.two_factor_enabled)
            """,
            {
                "tenant_id": tenant_id,
                "login": member["login"],
                "avatar_url": member["avatar_url"],
                "role": member["role"],
                "two_factor_enabled": member.get("two_factor_enabled"),
                "synced_at": synced_at,
            },
        )

    cur.execute(
        "DELETE FROM org_members WHERE tenant_id = %(tenant_id)s AND NOT (login = ANY(%(seen_logins)s))",
        {"tenant_id": tenant_id, "seen_logins": seen_logins},
    )


def reconcile_repo_collaborator_outside_status(
    cur: psycopg.Cursor, *, tenant_id: int, member_logins: set[str], outside_logins: set[str]
) -> None:
    """Fills in repo_collaborators.is_outside_collaborator for every row this tenant already
    has from the webhook path (migration 0040 -- the `member` event alone can't determine
    this, see that migration's docstring), using the exact same heuristic
    apps/api/src/routers/collab.py's permission_audit already computes live: `is_outside =
    login not in member_logins or login in outside_logins`. Implemented as "mark everyone
    outside, then correct the ones who are actually direct members" rather than computing set
    membership per row in Python, since this function only knows the org's member/outside
    logins (from the GitHub roster), not the full set of logins already present in
    repo_collaborators for this tenant -- the two-step UPDATE lets Postgres apply the formula
    without that enumeration. Only ever updates existing rows -- this function creates no new
    repo_collaborators rows (that's still the webhook path's job); a login this tenant has
    never seen via a `member` event has no row to update here regardless of what GitHub's
    roster says about it."""
    cur.execute(
        "UPDATE repo_collaborators SET is_outside_collaborator = TRUE WHERE tenant_id = %(tenant_id)s",
        {"tenant_id": tenant_id},
    )
    direct_members = member_logins - outside_logins
    if direct_members:
        cur.execute(
            "UPDATE repo_collaborators SET is_outside_collaborator = FALSE "
            "WHERE tenant_id = %(tenant_id)s AND login = ANY(%(logins)s)",
            {"tenant_id": tenant_id, "logins": list(direct_members)},
        )
