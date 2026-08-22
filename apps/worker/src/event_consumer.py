"""Redis Streams consumer for webhook_events (issue #191, S4).

Normalizes each queued webhook delivery into the repo_events table, deduplicated by
delivery_id, and upserts a per-tenant/repo/event_type/day count into
repo_event_daily_counts in the same transaction (S4 PR 2) -- the materialized-aggregate
half of S4, gated on the repo_events insert having actually happened (not a deduped
redelivery), so a redelivered webhook can't double-count the rollup. That insert-then-
upsert logic itself lives in repo_events_store.py, shared with backfill.py (S5 PR 1) --
see that module's docstring. Runs as a second daemon thread in worker.py's run(),
alongside the existing jobs-table poll loop -- not a separate process, so it shares the
container's healthcheck/deploy story.

Consumer-group semantics (XREADGROUP, group "event_processors") rather than a bare
XREAD: today's docker-compose runs exactly one worker replica, but a bare XREAD has no
way to resume after a restart without either replaying the whole stream or tracking a
cursor somewhere else -- a consumer group's last-delivered-id and pending-entries-list
already live in Redis and survive a consumer restart for free, and the same mechanism
is what lets a second replica join safely whenever the worker is actually scaled (the
issue's own "autoscaled consumers" framing). XPENDING + XCLAIM cover the case where a
consumer dies mid-processing (mirrors worker.py's _reclaim_stale_jobs for the jobs
table); a poison-pill entry that fails past _MAX_DELIVERY_ATTEMPTS is XACKed (dropped
from the group) rather than reclaimed forever -- nothing is actually lost, since
webhook_deliveries keeps the raw payload permanently for manual inspection/reprocessing.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path

import psycopg
import redis

import org_membership_store
import repo_events_store
import security_alerts_store
from config import settings

log = logging.getLogger(__name__)

_DB_URL = settings.database_url.get_secret_value().replace("postgresql+psycopg://", "postgresql://")

# Must match apps/api/src/routers/webhooks.py's _WEBHOOK_STREAM_KEY -- the two services
# don't share code (independently deployable per AGENTS.md), so this is a duplicated
# constant, not an import; keep them in sync by hand if either ever changes.
_STREAM_KEY = "webhook_events"
_GROUP_NAME = "event_processors"
_CONSUMER_NAME = f"worker-{os.getpid()}"

# Security alert events (dependabot_alert/code_scanning_alert/secret_scanning_alert,
# S6-follow-on PR 1) are durably queued by the receiver onto the same shared stream as
# every other ingested event type, but normalize into security_alerts (post-S6 PR 2),
# not repo_events -- their payload.alert shape and upsert-on-state-change semantics
# don't fit repo_events's insert-once activity-log model. _process_entry branches on
# this set before calling the repo_events path.
_SECURITY_ALERT_EVENT_TYPES = {"dependabot_alert", "code_scanning_alert", "secret_scanning_alert"}

# member/organization normalize into org_members/repo_collaborators (Collaborators PR 1 of 3).
# membership/team are durably queued (same webhooks.py change) but have no normalizer yet --
# team-based repo access is deferred, see org_membership_store.py's module docstring -- so
# _process_entry acks-and-skips them via _NOT_YET_NORMALIZED_EVENT_TYPES, same placeholder
# pattern PR #350 used for the security alerts before their own consumer existed.
_ORG_MEMBERSHIP_EVENT_TYPES = {"member", "organization"}
_NOT_YET_NORMALIZED_EVENT_TYPES = {"membership", "team"}

# How long a claimed-but-unacked entry sits idle before another pass reclaims it --
# comfortably longer than any single event's normalize+insert should ever take.
_RECLAIM_IDLE_MS = 60_000
# Shares the jobs table's MAX_RETRIES posture (worker.py): a repeatedly-failing entry
# is dropped, not retried forever, since retrying can't fix a genuinely malformed
# payload and a stuck poison pill would otherwise block reclaim of everything behind it.
_MAX_DELIVERY_ATTEMPTS = 5
# Blocks up to this long per XREADGROUP call so the loop still wakes regularly to touch
# the heartbeat file and pick up a poison-pill sweep, even with no new stream entries.
_BLOCK_MS = 5_000
_BATCH_SIZE = 10

# Separate heartbeat file from worker.py's HEARTBEAT_FILE -- both threads touch their
# own file every loop iteration; the docker-compose healthcheck only reads worker.py's
# HEARTBEAT_FILE today (v1: a hung consumer thread doesn't fail the container
# healthcheck on its own, same gap as any other purely-Python-level hang would have
# without a dedicated liveness probe for this specific thread -- flagged, not solved
# here, since the jobs-table loop staying healthy is still useful signal on its own).
_HEARTBEAT_FILE = Path("/tmp/worker_event_consumer_heartbeat")

_client: redis.Redis | None = None


def _redis_client() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(
            settings.redis_url.get_secret_value(),
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
    return _client


def _touch_heartbeat() -> None:
    try:
        _HEARTBEAT_FILE.write_text(str(time.time()))
    except OSError as exc:
        log.warning("could not write event consumer heartbeat file %s: %s", _HEARTBEAT_FILE, exc)


def _ensure_group(client: redis.Redis) -> None:
    try:
        client.xgroup_create(_STREAM_KEY, _GROUP_NAME, id="0", mkstream=True)
    except redis.ResponseError as error:
        if "BUSYGROUP" not in str(error):
            raise


def _summarize(event_type: str, payload: dict) -> str:
    """Per-event-type summary text. Mirrors apps/api/src/routers/github.py's
    _summarize, adapted to the raw webhook body's shape (the top-level fields of a
    GitHub webhook payload for these five event types are the same shape as the
    Events API's nested `payload` object _summarize already handles -- e.g. a webhook
    pull_request event's top-level `action`/`number`/`pull_request` match
    payload.action/payload.number/payload.pull_request there) rather than a live
    GitHub Events-API response, since this consumer only ever sees the raw webhook
    body stored in webhook_deliveries.payload."""
    if event_type == "push":
        commits = payload.get("commits") or []
        count = len(commits)
        branch = (payload.get("ref") or "").removeprefix("refs/heads/")
        noun = "commit" if count == 1 else "commits"
        return f"pushed {count} {noun} to {branch}" if branch else f"pushed {count} {noun}"

    if event_type == "pull_request":
        pr = payload.get("pull_request") or {}
        action = payload.get("action", "")
        verb = "merged" if action == "closed" and pr.get("merged") else action
        return f"{verb} PR #{payload.get('number')}: {pr.get('title', '')}"

    if event_type == "issues":
        issue = payload.get("issue") or {}
        action = payload.get("action", "")
        return f"{action} issue #{issue.get('number')}: {issue.get('title', '')}"

    if event_type == "release":
        release = payload.get("release") or {}
        return f"created release {release.get('tag_name', '')}"

    if event_type == "create":
        ref_type = payload.get("ref_type", "")
        ref = payload.get("ref") or ""
        return f"created {ref_type} {ref}".strip()

    return event_type


def _normalize(event_type: str, payload: dict, received_at: datetime) -> dict | None:
    """Returns the repo_events column values for this webhook payload, or None if it
    can't be normalized (missing repository/sender -- shouldn't happen for a real
    GitHub delivery, but the receiver doesn't validate payload shape beyond JSON-ness,
    so defend against a malformed/test payload rather than crash the consumer loop)."""
    repository = payload.get("repository") or {}
    sender = payload.get("sender") or {}
    repo_full_name = repository.get("full_name")
    if not repo_full_name:
        return None
    return {
        "event_type": event_type,
        "actor": sender.get("login", ""),
        "actor_avatar": sender.get("avatar_url", ""),
        "repo": repo_full_name,
        "summary": _summarize(event_type, payload),
        # No single canonical top-level timestamp exists across all five webhook payload
        # shapes (e.g. push has no top-level timestamp at all) -- fall back to
        # webhook_deliveries.received_at, the ingestion time, rather than parse a
        # different nested field per event type for a v1 that isn't read by any UI yet.
        "occurred_at": received_at,
    }


# event_type -> security_alerts.kind. Kept separate from the event_type string itself
# (rather than storing "dependabot_alert" verbatim) so a future consumer of this table
# (PR 3's Security dashboard repoint) works with the same short vocabulary GitHub's own
# REST API uses for these alert categories (dependabot/code-scanning/secret-scanning),
# not webhook-specific event-type naming.
_ALERT_KIND_BY_EVENT_TYPE = {
    "dependabot_alert": "dependabot",
    "code_scanning_alert": "code_scanning",
    "secret_scanning_alert": "secret_scanning",
}


def _parse_alert_timestamp(value: str | None, fallback: datetime) -> datetime:
    """GitHub sends alert.created_at/updated_at as ISO 8601 with a trailing 'Z', which
    datetime.fromisoformat only accepts starting in Python 3.11 -- normalize by hand
    rather than assume the runtime's exact minor version. Falls back to received_at
    (webhook_deliveries ingestion time) for a missing/malformed value, same defensive
    posture as _normalize's occurred_at fallback."""
    if not value:
        return fallback
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return fallback


def _normalize_security_alert(event_type: str, payload: dict, received_at: datetime) -> dict | None:
    """Returns the security_alerts column values for this alert webhook payload, or
    None if it can't be normalized (missing repository/alert/number -- shouldn't happen
    for a real GitHub delivery, but defend against a malformed/test payload rather than
    crash the consumer loop, same posture as _normalize)."""
    repository = payload.get("repository") or {}
    repo_full_name = repository.get("full_name")
    alert = payload.get("alert") or {}
    number = alert.get("number")
    if not repo_full_name or number is None:
        return None

    kind = _ALERT_KIND_BY_EVENT_TYPE[event_type]
    if kind == "dependabot":
        severity = (alert.get("security_advisory") or {}).get("severity")
        details = {
            "action": payload.get("action"),
            "dependency": alert.get("dependency"),
            "security_advisory": alert.get("security_advisory"),
        }
    elif kind == "code_scanning":
        severity = (alert.get("rule") or {}).get("severity")
        details = {
            "action": payload.get("action"),
            "rule": alert.get("rule"),
            "tool": alert.get("tool"),
        }
    else:  # secret_scanning -- no severity in GitHub's payload for this alert type
        severity = None
        details = {
            "action": payload.get("action"),
            "secret_type": alert.get("secret_type"),
            "secret_type_display_name": alert.get("secret_type_display_name"),
            # Only present once the alert is resolved -- None on the initial "created"
            # webhook. Needed by the Security dashboard's secret-scanning panel (post-S6
            # PR 3) to show why an alert was closed, matching GitHub's own live API shape.
            "resolution": alert.get("resolution"),
        }

    return {
        "repo": repo_full_name,
        "kind": kind,
        "number": number,
        "state": alert.get("state", ""),
        "severity": severity,
        "details": details,
        "created_at": _parse_alert_timestamp(alert.get("created_at"), received_at),
        "updated_at": _parse_alert_timestamp(alert.get("updated_at"), received_at),
    }


def _normalize_member_event(payload: dict) -> dict | None:
    """Returns repo_collaborators column values for a `member` event payload, or None if
    it can't be normalized (missing repository/member.login). action is 'added'/'edited'/
    'removed'; permission comes from changes.permission.to, present on both 'added' and
    'edited' per GitHub's webhook docs (only absent on 'removed', where it's moot -- the
    row is deleted, not updated). is_outside_collaborator can't be determined from this
    event alone (GitHub's member payload doesn't carry org-membership status) -- defaults
    False here, corrected by the future reconciliation poll (Collaborators PR 2), same
    known-staleness posture as org_members.role."""
    repository = payload.get("repository") or {}
    repo_full_name = repository.get("full_name")
    member = payload.get("member") or {}
    login = member.get("login")
    if not repo_full_name or not login:
        return None

    permission = ((payload.get("changes") or {}).get("permission") or {}).get("to")
    return {
        "repo": repo_full_name,
        "login": login,
        "permission": permission or "unknown",
        "is_outside_collaborator": False,
    }


def _normalize_organization_event(payload: dict) -> dict | None:
    """Returns org_members column values for an `organization` event's member_added
    payload, or None if it can't be normalized (missing membership.user.login) -- also
    None (a no-op) for actions other than member_added/member_removed (member_invited/
    renamed/deleted don't affect this table)."""
    membership = payload.get("membership") or {}
    user = membership.get("user") or {}
    login = user.get("login")
    if not login:
        return None
    return {
        "login": login,
        "avatar_url": user.get("avatar_url", ""),
        "role": membership.get("role", "member"),
    }


def _process_entry(pg_conn: psycopg.Connection, redis_client: redis.Redis, entry_id: str, fields: dict) -> None:
    delivery_row_id = fields.get("delivery_row_id")
    if delivery_row_id is None:
        log.error("stream entry %s missing delivery_row_id, dropping: %r", entry_id, fields)
        redis_client.xack(_STREAM_KEY, _GROUP_NAME, entry_id)
        return

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT tenant_id, delivery_id, event_type, payload, received_at FROM webhook_deliveries WHERE id = %s",
            (delivery_row_id,),
        )
        row = cur.fetchone()

    if row is None:
        log.error("webhook_deliveries row %s referenced by stream entry %s not found, dropping", delivery_row_id, entry_id)
        redis_client.xack(_STREAM_KEY, _GROUP_NAME, entry_id)
        return

    tenant_id, delivery_id, event_type, payload_bytes, received_at = row
    if tenant_id is None:
        # Deliberate scope decision (see S4 PR 1's plan notes): an unresolved
        # installation has no tenant to scope a normalized row to. Ack so this entry
        # doesn't sit pending forever; the raw payload stays in webhook_deliveries.
        log.warning("webhook_deliveries row %s has no tenant_id, skipping normalization", delivery_row_id)
        redis_client.xack(_STREAM_KEY, _GROUP_NAME, entry_id)
        return

    if event_type in _NOT_YET_NORMALIZED_EVENT_TYPES:
        # Team-based repo access (membership/team events) is explicitly deferred -- see
        # org_membership_store.py's module docstring. Ack so this entry doesn't sit
        # pending forever; leave webhook_deliveries.status as 'queued' (not 'processed')
        # so a future consumer can still find it by event_type, same posture as PR #350's
        # original placeholder for the security alerts.
        log.debug("webhook_deliveries row %s is a %s event with no consumer yet, leaving queued", delivery_row_id, event_type)
        redis_client.xack(_STREAM_KEY, _GROUP_NAME, entry_id)
        return

    try:
        payload = json.loads(payload_bytes)
    except json.JSONDecodeError:
        log.error("webhook_deliveries row %s has malformed JSON payload, dropping", delivery_row_id)
        redis_client.xack(_STREAM_KEY, _GROUP_NAME, entry_id)
        return

    if event_type in _ORG_MEMBERSHIP_EVENT_TYPES:
        with pg_conn.cursor() as cur:
            cur.execute(f"SET app.tenant_id = {int(tenant_id)}")
            if event_type == "member":
                member_normalized = _normalize_member_event(payload)
                action = payload.get("action")
                if member_normalized is None:
                    log.error("webhook_deliveries row %s has no repository.full_name or member.login, dropping", delivery_row_id)
                    redis_client.xack(_STREAM_KEY, _GROUP_NAME, entry_id)
                    return
                if action == "removed":
                    org_membership_store.remove_repo_collaborator(
                        cur, tenant_id=tenant_id, repo=member_normalized["repo"], login=member_normalized["login"]
                    )
                else:
                    org_membership_store.upsert_repo_collaborator(cur, tenant_id=tenant_id, granted_at=received_at, **member_normalized)
            else:  # organization
                action = payload.get("action")
                if action == "member_removed":
                    login = ((payload.get("membership") or {}).get("user") or {}).get("login")
                    if not login:
                        log.error("webhook_deliveries row %s has no membership.user.login, dropping", delivery_row_id)
                        redis_client.xack(_STREAM_KEY, _GROUP_NAME, entry_id)
                        return
                    org_membership_store.remove_org_member(cur, tenant_id=tenant_id, login=login)
                elif action == "member_added":
                    org_normalized = _normalize_organization_event(payload)
                    if org_normalized is None:
                        log.error("webhook_deliveries row %s has no membership.user.login, dropping", delivery_row_id)
                        redis_client.xack(_STREAM_KEY, _GROUP_NAME, entry_id)
                        return
                    org_membership_store.upsert_org_member(cur, tenant_id=tenant_id, added_at=received_at, **org_normalized)
                else:
                    # member_invited/renamed/deleted don't affect org_members -- ack as a no-op.
                    log.debug("webhook_deliveries row %s is an organization/%s event, no-op for org_members", delivery_row_id, action)
            cur.execute("UPDATE webhook_deliveries SET status = 'processed' WHERE id = %s", (delivery_row_id,))
        pg_conn.commit()
        redis_client.xack(_STREAM_KEY, _GROUP_NAME, entry_id)
        return

    if event_type in _SECURITY_ALERT_EVENT_TYPES:
        alert_normalized = _normalize_security_alert(event_type, payload, received_at)
        if alert_normalized is None:
            log.error("webhook_deliveries row %s has no repository.full_name or alert.number, dropping", delivery_row_id)
            redis_client.xack(_STREAM_KEY, _GROUP_NAME, entry_id)
            return

        with pg_conn.cursor() as cur:
            # Mirrors src.core.rbac.set_tenant_session_context -- see the repo_events
            # branch below for the full rationale, same mechanism applies here.
            cur.execute(f"SET app.tenant_id = {int(tenant_id)}")
            security_alerts_store.upsert_security_alert(cur, tenant_id=tenant_id, **alert_normalized)
            cur.execute("UPDATE webhook_deliveries SET status = 'processed' WHERE id = %s", (delivery_row_id,))
        pg_conn.commit()
        redis_client.xack(_STREAM_KEY, _GROUP_NAME, entry_id)
        return

    normalized = _normalize(event_type, payload, received_at)
    if normalized is None:
        log.error("webhook_deliveries row %s has no repository.full_name, dropping", delivery_row_id)
        redis_client.xack(_STREAM_KEY, _GROUP_NAME, entry_id)
        return

    with pg_conn.cursor() as cur:
        # Mirrors src.core.rbac.set_tenant_session_context: a plain SET (not SET
        # LOCAL) read by this table's RLS policy (migration 0036), scoped to this
        # connection for the duration of the INSERT that follows -- a narrower,
        # explicit mechanism than granting clevis_worker BYPASSRLS, same precedent as
        # migration 0035's SECURITY DEFINER function.
        cur.execute(f"SET app.tenant_id = {int(tenant_id)}")
        repo_events_store.insert_event_and_upsert_daily_count(cur, tenant_id=tenant_id, delivery_id=delivery_id, **normalized)
        cur.execute("UPDATE webhook_deliveries SET status = 'processed' WHERE id = %s", (delivery_row_id,))
    pg_conn.commit()
    redis_client.xack(_STREAM_KEY, _GROUP_NAME, entry_id)


def _sweep_pending(pg_conn: psycopg.Connection, redis_client: redis.Redis) -> None:
    """Reclaims entries idle longer than _RECLAIM_IDLE_MS (a prior consumer likely
    crashed mid-processing), or drops ones that have failed too many times."""
    pending = redis_client.xpending_range(
        _STREAM_KEY, _GROUP_NAME, min="-", max="+", count=100, idle=_RECLAIM_IDLE_MS
    )
    if not pending:
        return

    # >= / < , not > / <=: XCLAIM itself increments times_delivered before this entry is
    # even processed, so an entry already at the limit would otherwise get a (limit + 1)th
    # attempt instead of being dropped (CodeRabbit finding on PR #340).
    to_drop = [p["message_id"] for p in pending if p["times_delivered"] >= _MAX_DELIVERY_ATTEMPTS]
    to_claim = [p["message_id"] for p in pending if p["times_delivered"] < _MAX_DELIVERY_ATTEMPTS]

    for message_id in to_drop:
        log.error("stream entry %s exceeded %d delivery attempts, dropping", message_id, _MAX_DELIVERY_ATTEMPTS)
        redis_client.xack(_STREAM_KEY, _GROUP_NAME, message_id)

    if not to_claim:
        return
    claimed = redis_client.xclaim(_STREAM_KEY, _GROUP_NAME, _CONSUMER_NAME, min_idle_time=_RECLAIM_IDLE_MS, message_ids=to_claim)
    for entry_id, fields in claimed:
        try:
            _process_entry(pg_conn, redis_client, entry_id, fields)
        except Exception:
            log.exception("failed to process reclaimed stream entry %s", entry_id)


def run() -> None:
    # Retried, not just attempted once: this runs on its own daemon thread (see
    # start_background_thread) with nothing else supervising it -- an uncaught
    # exception here (e.g. Redis not reachable yet at container startup) would
    # otherwise kill the thread silently and leave the consumer dead until the whole
    # worker process restarts (CodeRabbit finding on PR #340).
    redis_client = None
    while redis_client is None:
        try:
            redis_client = _redis_client()
            _ensure_group(redis_client)
        except (redis.RedisError, OSError) as error:
            log.error("event consumer initialization error: %s", type(error).__name__)
            redis_client = None
            time.sleep(5)
    log.info("event consumer started, group=%s consumer=%s", _GROUP_NAME, _CONSUMER_NAME)

    while True:
        _touch_heartbeat()
        try:
            with psycopg.connect(_DB_URL) as pg_conn:
                _sweep_pending(pg_conn, redis_client)

                response = redis_client.xreadgroup(
                    _GROUP_NAME, _CONSUMER_NAME, {_STREAM_KEY: ">"}, count=_BATCH_SIZE, block=_BLOCK_MS
                )
                for _stream_key, entries in response or []:
                    for entry_id, fields in entries:
                        try:
                            _process_entry(pg_conn, redis_client, entry_id, fields)
                        except Exception:
                            # Left unacked on purpose -- _sweep_pending reclaims it next
                            # pass once _RECLAIM_IDLE_MS has elapsed, same "one bad
                            # entry doesn't take down the loop" posture as
                            # worker.py's process_job.
                            log.exception("failed to process stream entry %s", entry_id)
        except (psycopg.OperationalError, redis.RedisError) as error:
            log.error("event consumer connection error: %s", type(error).__name__)
            time.sleep(5)
        except Exception as error:
            log.error("event consumer loop error: %s", type(error).__name__)
            time.sleep(5)


def start_background_thread() -> threading.Thread:
    thread = threading.Thread(target=run, daemon=True, name="event-consumer")
    thread.start()
    return thread
