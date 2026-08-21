"""Shared repo_events + repo_event_daily_counts write path (issue #191, S4-S5).

Both event_consumer.py (S4: live webhook normalization) and backfill.py (S5 PR 1:
install-time backfill) insert into repo_events and, only when that insert actually
happened (not a delivery_id dedupe skip), upsert the matching repo_event_daily_counts
row -- the exact same shape, so it lives here once rather than being duplicated a
second time within the same service. This is an in-service refactor, not the
cross-service duplication precedent between apps/api and apps/worker (e.g. this
module's callers each keep their own _summarize/normalize, since those really do differ
by source shape -- raw webhook body vs. Events API response).
"""

from datetime import datetime, timezone

import psycopg


def insert_event_and_upsert_daily_count(
    cur: psycopg.Cursor,
    *,
    tenant_id: int,
    delivery_id: str,
    event_type: str,
    actor: str,
    actor_avatar: str,
    repo: str,
    summary: str,
    occurred_at: datetime,
) -> bool:
    """Inserts a repo_events row (ON CONFLICT (delivery_id) DO NOTHING) and, only if a
    row was actually inserted, upserts the matching repo_event_daily_counts row.
    Returns True iff a new repo_events row was inserted -- callers that need to report
    "how many new events" (e.g. backfill.py's job result) use this return value rather
    than a separate row count.

    Caller is responsible for `SET app.tenant_id = <n>` on this cursor's connection
    before calling this (both callers already do, to satisfy RLS's WITH CHECK) -- not
    done here, so this function stays a plain, easily-testable SQL helper with no
    session-state side effects of its own.
    """
    cur.execute(
        """
        INSERT INTO repo_events
            (tenant_id, delivery_id, event_type, actor, actor_avatar, repo, summary, occurred_at)
        VALUES (%(tenant_id)s, %(delivery_id)s, %(event_type)s, %(actor)s, %(actor_avatar)s,
                %(repo)s, %(summary)s, %(occurred_at)s)
        ON CONFLICT (delivery_id) DO NOTHING
        RETURNING id
        """,
        {
            "tenant_id": tenant_id,
            "delivery_id": delivery_id,
            "event_type": event_type,
            "actor": actor,
            "actor_avatar": actor_avatar,
            "repo": repo,
            "summary": summary,
            "occurred_at": occurred_at,
        },
    )
    inserted = cur.fetchone() is not None
    if inserted:
        cur.execute(
            """
            INSERT INTO repo_event_daily_counts (tenant_id, repo, event_type, day, count)
            VALUES (%(tenant_id)s, %(repo)s, %(event_type)s, %(day)s, 1)
            ON CONFLICT (tenant_id, repo, event_type, day)
            DO UPDATE SET count = repo_event_daily_counts.count + 1
            """,
            {
                "tenant_id": tenant_id,
                "repo": repo,
                "event_type": event_type,
                # psycopg returns a timestamptz in whatever timezone this connection's
                # Postgres session happens to be in, not necessarily UTC -- .date() on
                # that raw value would misbucket an event near a non-UTC-session
                # midnight. Force UTC first so "day" always means the UTC calendar day,
                # matching the repo_event_daily_counts column docstring (originally a
                # CodeRabbit finding on #341).
                "day": occurred_at.astimezone(timezone.utc).date(),
            },
        )
    return inserted
