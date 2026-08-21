"""Redis Streams consumer for webhook_events (issue #191, S4 PR 1 of N).

Normalizes each queued webhook delivery into the repo_events table, deduplicated by
delivery_id. Runs as a second daemon thread in worker.py's run(), alongside the
existing jobs-table poll loop -- not a separate process, so it shares the container's
healthcheck/deploy story.

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

from config import settings

log = logging.getLogger(__name__)

_DB_URL = settings.database_url.get_secret_value().replace("postgresql+psycopg://", "postgresql://")

# Must match apps/api/src/routers/webhooks.py's _WEBHOOK_STREAM_KEY -- the two services
# don't share code (independently deployable per AGENTS.md), so this is a duplicated
# constant, not an import; keep them in sync by hand if either ever changes.
_STREAM_KEY = "webhook_events"
_GROUP_NAME = "event_processors"
_CONSUMER_NAME = f"worker-{os.getpid()}"

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

    try:
        payload = json.loads(payload_bytes)
    except json.JSONDecodeError:
        log.error("webhook_deliveries row %s has malformed JSON payload, dropping", delivery_row_id)
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
        cur.execute(
            """
            INSERT INTO repo_events
                (tenant_id, delivery_id, event_type, actor, actor_avatar, repo, summary, occurred_at)
            VALUES (%(tenant_id)s, %(delivery_id)s, %(event_type)s, %(actor)s, %(actor_avatar)s,
                    %(repo)s, %(summary)s, %(occurred_at)s)
            ON CONFLICT (delivery_id) DO NOTHING
            """,
            {
                "tenant_id": tenant_id,
                "delivery_id": delivery_id,
                **normalized,
            },
        )
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

    to_drop = [p["message_id"] for p in pending if p["times_delivered"] > _MAX_DELIVERY_ATTEMPTS]
    to_claim = [p["message_id"] for p in pending if p["times_delivered"] <= _MAX_DELIVERY_ATTEMPTS]

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
    redis_client = _redis_client()
    _ensure_group(redis_client)
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
