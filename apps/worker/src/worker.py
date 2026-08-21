import json
import logging
import threading
import time
from pathlib import Path

import httpx
import psycopg
from pydantic import BaseModel, Field, ValidationError

import backfill
import repo_events_store
from _crypto import decrypt_job_token
from _sanitize import sanitize_error
from config import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# Touched once per poll loop iteration so the docker-compose healthcheck can tell a hung
# worker (process alive but stuck, e.g. blocked on a network call with no timeout) apart
# from a genuinely healthy one -- `restart: unless-stopped` only fires on a hard crash,
# not a hang, so without this a stuck worker container would never be restarted.
HEARTBEAT_FILE = Path("/tmp/worker_heartbeat")
# The docker-compose healthcheck treats the heartbeat file as stale past 60s (see
# docker-compose.yml). worker_poll_seconds is a live-editable app_config value with no
# upper bound otherwise, and the heartbeat only gets touched once per loop iteration --
# an operator setting it above this cap would make every iteration's normal sleep alone
# exceed the healthcheck's staleness threshold, permanently false-positive-ing the worker
# as hung. Kept comfortably below 60s so real job processing inside an iteration still
# has margin before the healthcheck's threshold is reached.
_MAX_POLL_SECONDS = 30

# psycopg.connect() expects plain postgresql://, not the SQLAlchemy +psycopg dialect prefix
_DB_URL = settings.database_url.get_secret_value().replace("postgresql+psycopg://", "postgresql://")

# Shared cap on jobs.retry_count, incremented by both the reclaim sweep (a worker
# crashed mid-job) and a transient-failure requeue in process_job — either path marks
# the job 'failed' once exceeded, so a job can't retry forever regardless of cause.
MAX_RETRIES = 5
# A job left in 'processing' longer than this almost certainly had its worker crash or
# get killed mid-job (see _reclaim_stale_jobs) rather than still being genuinely in flight
# -- unless its heartbeat_at is still fresh (see _JobHeartbeat / _reclaim_stale_jobs).
RECLAIM_TIMEOUT_MINUTES = 30

# How often _JobHeartbeat touches jobs.heartbeat_at while a handler is running. Comfortably
# below RECLAIM_TIMEOUT_MINUTES so a genuinely slow-but-alive job's heartbeat always stays
# fresh well ahead of the reclaim sweep's staleness check.
_JOB_HEARTBEAT_INTERVAL_SECONDS = 10


def _read_app_config(key: str, default: str) -> str:
    """Read a single value from app_config. Falls back to default on any error."""
    try:
        with psycopg.connect(_DB_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM app_config WHERE key = %s", (key,))
                row = cur.fetchone()
        return row[0] if row else default
    except Exception as exc:
        log.warning("Could not read app_config[%r]: %s; using default %r", key, exc, default)
        return default


def _read_poll_seconds() -> int:
    """Read worker_poll_seconds, clamped to [1, _MAX_POLL_SECONDS]. Falls back to 5 on a
    malformed value so a bad config row can never crash or busy-loop the worker. The upper
    clamp keeps the heartbeat healthcheck's staleness threshold meaningful -- see
    _MAX_POLL_SECONDS."""
    raw = _read_app_config("worker_poll_seconds", "5")
    try:
        return max(1, min(_MAX_POLL_SECONDS, int(raw)))
    except ValueError:
        log.warning("worker_poll_seconds %r is not an integer; using 5", raw)
        return 5


class ClearActionsCachePayload(BaseModel):
    owner: str = Field(min_length=1)
    repo: str = Field(min_length=1)
    token: str = Field(min_length=1)
    key: str | None = None
    ref: str | None = None


class BackfillRepoEventsPayload(BaseModel):
    tenant_id: int
    account_login: str = Field(min_length=1)
    account_type: str = Field(min_length=1)
    token: str = Field(min_length=1)


def _mark_done(conn: psycopg.Connection, job_id: int, result: dict, expected_retry_count: int) -> None:
    # WHERE status='processing' AND retry_count=expected_retry_count fences this update
    # against not just a lost update (reclaim already reset this job out from under us --
    # status no longer 'processing') but also the narrower race where a *second* worker
    # has since re-claimed the same job: reclaim bumps retry_count when it resets a stale
    # job back to 'queued', so if that happened and another worker's SELECT ... FOR UPDATE
    # picked it up again, status is back to 'processing' but retry_count no longer matches
    # what *this* worker observed when it originally claimed the job. Without the
    # retry_count check, this stale completion would silently clobber the second worker's
    # in-flight row (issue #253).
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET status='done', result=%s, updated_at=NOW() "
            "WHERE id=%s AND status='processing' AND retry_count=%s",
            (json.dumps(result), job_id, expected_retry_count),
        )
    conn.commit()


def _mark_failed(conn: psycopg.Connection, job_id: int, error_text: str, expected_retry_count: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET status='failed', result=%s, updated_at=NOW() "
            "WHERE id=%s AND status='processing' AND retry_count=%s",
            (error_text, job_id, expected_retry_count),
        )
    conn.commit()


def _requeue_for_retry(conn: psycopg.Connection, job_id: int, retry_count: int, error_text: str) -> None:
    # `retry_count` here is the value this worker observed at claim time -- same fencing
    # reasoning as _mark_done/_mark_failed above, checked against the ORIGINAL value
    # before it's incremented into new_count below.
    new_count = retry_count + 1
    if new_count > MAX_RETRIES:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET status='failed', retry_count=%s, result=%s, updated_at=NOW() "
                "WHERE id=%s AND status='processing' AND retry_count=%s",
                (new_count, f"exceeded max retry attempts ({MAX_RETRIES}): {error_text}", job_id, retry_count),
            )
    else:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET status='queued', retry_count=%s, result=%s, updated_at=NOW() "
                "WHERE id=%s AND status='processing' AND retry_count=%s",
                (new_count, error_text, job_id, retry_count),
            )
    conn.commit()


def process_job(conn: psycopg.Connection, job_id: int, job_type: str, payload_raw: str, retry_count: int = 0) -> None:
    handler = JOB_HANDLERS.get(job_type)
    if handler is None:
        log.error("job %d has no handler registered for job_type %r", job_id, job_type)
        _mark_failed(conn, job_id, f"no handler registered for job_type {job_type!r}", retry_count)
        return
    try:
        handler(conn, job_id, payload_raw, retry_count)
    except Exception as error:
        # Safety net for anything not handled above (e.g. a bug in the handler, or an
        # unanticipated exception type) — without this, the job would stay 'processing'
        # until the reclaim sweep picks it up, up to RECLAIM_TIMEOUT_MINUTES later,
        # instead of failing/retrying immediately.
        log.error("job %d hit an unexpected error: %s", job_id, error)
        _mark_failed(conn, job_id, sanitize_error(error), retry_count)


def _github_error_message(resp: httpx.Response) -> str:
    """Prefer GitHub's own error message over a bare status code, so jobs.result
    tells an operator *why* a clear failed (e.g. "Resource not accessible by
    integration") instead of just "GitHub API error: 403"."""
    try:
        message = resp.json().get("message")
    except (ValueError, AttributeError):
        message = None
    if message:
        return f"GitHub API error: {resp.status_code} - {message}"
    return f"GitHub API error: {resp.status_code}"


def _handle_clear_actions_cache(conn: psycopg.Connection, job_id: int, payload_raw: str, retry_count: int) -> None:
    try:
        payload = ClearActionsCachePayload.model_validate_json(payload_raw)
    except ValidationError as error:
        log.error("job %d has an invalid payload: %s", job_id, error)
        _mark_failed(conn, job_id, sanitize_error(error), retry_count)
        return

    try:
        token = decrypt_job_token(payload.token, settings.job_secret_key.get_secret_value())
    except Exception as error:
        log.error("job %d failed to decrypt its token: %s", job_id, error)
        _mark_failed(conn, job_id, sanitize_error(error), retry_count)
        return

    base = settings.github_api_base
    params = {k: v for k, v in (("key", payload.key), ("ref", payload.ref)) if v}
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        with httpx.Client(timeout=20) as client:
            resp = client.delete(
                f"{base}/repos/{payload.owner}/{payload.repo}/actions/caches",
                headers=headers,
                params=params,
            )
    except httpx.RequestError as error:
        log.warning("job %d hit a network error (attempt %d): %s", job_id, retry_count + 1, error)
        _requeue_for_retry(conn, job_id, retry_count, sanitize_error(error))
        return

    if resp.status_code >= 500:
        # 5xx is presumed transient (GitHub-side issue) — worth retrying, unlike 4xx.
        log.warning("job %d got a %d from GitHub (attempt %d)", job_id, resp.status_code, retry_count + 1)
        _requeue_for_retry(conn, job_id, retry_count, sanitize_error(_github_error_message(resp)))
        return

    if resp.status_code >= 300:
        log.error("job %d failed: GitHub API error %d", job_id, resp.status_code)
        _mark_failed(conn, job_id, sanitize_error(_github_error_message(resp)), retry_count)
        return

    _mark_done(conn, job_id, {"ok": True, "status": resp.status_code}, retry_count)
    log.info("job %d done", job_id)


def _handle_backfill_repo_events(conn: psycopg.Connection, job_id: int, payload_raw: str, retry_count: int) -> None:
    try:
        payload = BackfillRepoEventsPayload.model_validate_json(payload_raw)
    except ValidationError as error:
        log.error("job %d has an invalid payload: %s", job_id, error)
        _mark_failed(conn, job_id, sanitize_error(error), retry_count)
        return

    try:
        token = decrypt_job_token(payload.token, settings.job_secret_key.get_secret_value())
    except Exception as error:
        log.error("job %d failed to decrypt its token: %s", job_id, error)
        _mark_failed(conn, job_id, sanitize_error(error), retry_count)
        return

    base = settings.github_api_base
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        with httpx.Client(timeout=20) as client:
            raw_events = backfill.fetch_events(client, base, headers, payload.account_login, payload.account_type)
    except httpx.HTTPStatusError as error:
        resp = error.response
        if resp.status_code >= 500:
            # 5xx is presumed transient (GitHub-side issue) — worth retrying, unlike 4xx.
            log.warning("job %d got a %d from GitHub (attempt %d)", job_id, resp.status_code, retry_count + 1)
            _requeue_for_retry(conn, job_id, retry_count, sanitize_error(_github_error_message(resp)))
        else:
            log.error("job %d failed: GitHub API error %d", job_id, resp.status_code)
            _mark_failed(conn, job_id, sanitize_error(_github_error_message(resp)), retry_count)
        return
    except httpx.RequestError as error:
        log.warning("job %d hit a network error (attempt %d): %s", job_id, retry_count + 1, error)
        _requeue_for_retry(conn, job_id, retry_count, sanitize_error(error))
        return

    inserted_count = 0
    with conn.cursor() as cur:
        # Same session-context mechanism as event_consumer.py's _process_entry -- see
        # its comment for why this isn't a clevis_worker BYPASSRLS grant instead.
        cur.execute(f"SET app.tenant_id = {int(payload.tenant_id)}")
        for raw_event in raw_events:
            normalized = backfill.normalize(raw_event)
            if normalized is None:
                continue
            if repo_events_store.insert_event_and_upsert_daily_count(cur, tenant_id=payload.tenant_id, **normalized):
                inserted_count += 1
    conn.commit()

    _mark_done(
        conn, job_id, {"ok": True, "events_seen": len(raw_events), "events_inserted": inserted_count}, retry_count
    )
    log.info("job %d done: backfilled %d/%d events for %s", job_id, inserted_count, len(raw_events), payload.account_login)


# job_type -> handler. Each handler takes (conn, job_id, payload_raw, retry_count) and is
# responsible for its own payload validation and terminal/retry outcome via _mark_done /
# _mark_failed / _requeue_for_retry.
JOB_HANDLERS = {
    "github.clear_actions_cache": _handle_clear_actions_cache,
    "github.backfill_repo_events": _handle_backfill_repo_events,
}


def _reclaim_stale_jobs(conn: psycopg.Connection) -> None:
    """Reset jobs stuck in 'processing' past RECLAIM_TIMEOUT_MINUTES back to 'queued' —
    the worker that claimed them almost certainly crashed or was killed mid-job, and the
    poll query only ever selects 'queued' rows, so without this such a job is stuck
    forever. Shares retry_count/MAX_RETRIES with process_job's transient-failure retry so
    a job that repeatedly crashes its worker eventually gets marked 'failed' instead of
    looping indefinitely.

    Only reclaims a job whose heartbeat_at is ALSO stale (or null, for a job claimed before
    this column existed / before its handler's first heartbeat tick) -- updated_at alone is
    set once at claim time and never again until the job finishes, so on its own it can't
    tell a legitimately slow job from a crashed one. heartbeat_at is touched every
    _JOB_HEARTBEAT_INTERVAL_SECONDS by _JobHeartbeat while a handler is actually running
    (see issue #215), so a still-alive job's heartbeat stays fresh well past 30 minutes."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE jobs
            SET status = CASE WHEN retry_count + 1 > %(max_retries)s THEN 'failed' ELSE 'queued' END,
                retry_count = retry_count + 1,
                result = CASE WHEN retry_count + 1 > %(max_retries)s THEN %(exceeded_message)s ELSE result END,
                updated_at = NOW()
            WHERE status = 'processing'
              AND updated_at < NOW() - make_interval(mins => %(timeout_minutes)s)
              AND (heartbeat_at IS NULL OR heartbeat_at < NOW() - make_interval(mins => %(timeout_minutes)s))
            RETURNING id, status
            """,
            {
                "max_retries": MAX_RETRIES,
                "exceeded_message": f"exceeded max reclaim attempts ({MAX_RETRIES})",
                "timeout_minutes": RECLAIM_TIMEOUT_MINUTES,
            },
        )
        reclaimed = cur.fetchall()
    conn.commit()
    for job_id, status in reclaimed:
        log.warning("reclaimed stale job %d -> %s", job_id, status)


def _touch_job_heartbeat(job_id: int) -> None:
    """Runs on its own DB connection, separate from the one process_job uses on the main
    thread -- psycopg connections aren't safe to share across threads."""
    try:
        with psycopg.connect(_DB_URL) as hb_conn:
            with hb_conn.cursor() as cur:
                cur.execute(
                    "UPDATE jobs SET heartbeat_at = NOW() WHERE id = %s AND status = 'processing'",
                    (job_id,),
                )
            hb_conn.commit()
    except Exception as exc:
        # Non-fatal -- worst case the reclaim sweep sees a stale heartbeat and reclaims a
        # job that's actually still running, the same failure mode as before this existed.
        log.warning("could not touch heartbeat for job %d: %s", job_id, exc)
    # Also refresh the container-level file heartbeat (see _touch_heartbeat/HEARTBEAT_FILE):
    # run()'s loop only touches it once per poll iteration, before a job is even claimed, so
    # without this a job handler running past the healthcheck's 60s staleness threshold would
    # get the worker marked unhealthy mid-job -- exactly during the long-running handlers this
    # DB heartbeat was added to support.
    _touch_heartbeat()


class _JobHeartbeat:
    """Context manager: touches jobs.heartbeat_at for `job_id` every
    _JOB_HEARTBEAT_INTERVAL_SECONDS on a background thread for as long as the `with` block
    runs, so _reclaim_stale_jobs can tell this job apart from a crashed one. See issue #215."""

    def __init__(self, job_id: int):
        self._job_id = job_id
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(_JOB_HEARTBEAT_INTERVAL_SECONDS):
            _touch_job_heartbeat(self._job_id)

    def __enter__(self) -> "_JobHeartbeat":
        _touch_job_heartbeat(self._job_id)  # immediate first tick, don't wait a full interval
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        self._thread.join(timeout=_JOB_HEARTBEAT_INTERVAL_SECONDS)


def _touch_heartbeat() -> None:
    try:
        HEARTBEAT_FILE.write_text(str(time.time()))
    except OSError as exc:
        # Non-fatal -- the heartbeat is only a liveness signal for the healthcheck, not
        # required for job processing itself.
        log.warning("could not write heartbeat file %s: %s", HEARTBEAT_FILE, exc)


def run() -> None:
    poll_seconds = _read_poll_seconds()
    log.info("worker started, polling every %ds", poll_seconds)
    while True:
        _touch_heartbeat()
        # Re-read poll interval each cycle so changes in settings take effect without restart
        poll_seconds = _read_poll_seconds()
        try:
            with psycopg.connect(_DB_URL) as conn:
                _reclaim_stale_jobs(conn)

                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE jobs SET status = 'processing', updated_at = NOW()
                        WHERE id = (
                            SELECT id FROM jobs
                            WHERE status = 'queued'
                            ORDER BY id
                            LIMIT 1
                            FOR UPDATE SKIP LOCKED
                        )
                        RETURNING id, job_type, payload, retry_count
                    """)
                    row = cur.fetchone()

                if row:
                    conn.commit()
                    with _JobHeartbeat(row[0]):
                        process_job(conn, *row)
        except psycopg.OperationalError:
            log.error("database connection failed, retrying in %ds", poll_seconds)
        except Exception as error:
            log.error("worker poll error: %s", type(error).__name__)

        time.sleep(poll_seconds)


if __name__ == "__main__":
    import event_consumer

    # Runs on its own daemon thread, independent of this module's jobs-table poll loop
    # below -- see event_consumer.py's module docstring for why (Redis Streams consumer
    # group, issue #191/S4 PR 1). A crash inside it is caught and logged by its own
    # run() loop and doesn't take down the process; it does not currently fail the
    # container healthcheck on its own (see event_consumer.py's _HEARTBEAT_FILE note).
    event_consumer.start_background_thread()
    run()
