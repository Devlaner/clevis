"""security_alerts write path (post-S6, PR 2 of 3).

Companion to repo_events_store.py, but an upsert rather than an insert-and-skip: unlike
a repo_events row (an immutable activity log entry), a security alert's `state`
legitimately changes over its lifetime (e.g. open -> dismissed/fixed), and GitHub
redelivers the same alert's webhook on every state transition, not just once -- so a
redelivery must update the existing row, not be silently deduped away.
"""

from datetime import datetime

import psycopg
from psycopg.types.json import Json


def upsert_security_alert(
    cur: psycopg.Cursor,
    *,
    tenant_id: int,
    repo: str,
    kind: str,
    number: int,
    state: str,
    severity: str | None,
    details: dict,
    created_at: datetime,
    updated_at: datetime,
) -> bool:
    """Upserts a security_alerts row keyed on (tenant_id, repo, kind, number) --
    migration 0039's uq_security_alerts_tenant_repo_kind_number. Returns True iff this
    call inserted a brand-new row (first time this alert has been seen); False for a
    redelivery that updated an existing row's state/severity/details/updated_at --
    mirrors repo_events_store.insert_event_and_upsert_daily_count's `inserted` return,
    which callers use for "how many new events" reporting.

    Caller is responsible for `SET app.tenant_id = <n>` on this cursor's connection
    before calling this (mirrors repo_events_store.insert_event_and_upsert_daily_count),
    to satisfy this table's RLS WITH CHECK.
    """
    cur.execute(
        """
        INSERT INTO security_alerts
            (tenant_id, repo, kind, number, state, severity, details, created_at, updated_at)
        VALUES (%(tenant_id)s, %(repo)s, %(kind)s, %(number)s, %(state)s, %(severity)s,
                %(details)s, %(created_at)s, %(updated_at)s)
        ON CONFLICT (tenant_id, repo, kind, number)
        DO UPDATE SET
            state = EXCLUDED.state,
            severity = EXCLUDED.severity,
            details = EXCLUDED.details,
            updated_at = EXCLUDED.updated_at
        RETURNING (xmax = 0) AS inserted
        """,
        {
            "tenant_id": tenant_id,
            "repo": repo,
            "kind": kind,
            "number": number,
            "state": state,
            "severity": severity,
            "details": Json(details),
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )
    return cur.fetchone()[0]
