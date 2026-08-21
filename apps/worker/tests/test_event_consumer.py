"""Tests for the webhook_events Redis Streams consumer (issue #191/S4 PR 1)."""

import json

import psycopg
import pytest

import event_consumer
from config import settings

_DB_URL = settings.database_url.get_secret_value().replace("postgresql+psycopg://", "postgresql://")


@pytest.fixture()
def pg_conn():
    conn = psycopg.connect(_DB_URL, autocommit=False)
    # Only webhook_deliveries rows get cleaned up: clevis_api/clevis_worker are
    # deliberately not granted DELETE on repo_events (migration 0036 -- the consumer
    # only ever inserts a normalized row, never updates/deletes one), so a test
    # connection running as either role can't clean those up even if it wanted to.
    # Deleting the shared test tenant/user (see the tenant_id fixture below) would
    # also fail on repo_events's tenant_id FK for the same reason -- so that tenant is
    # never deleted either, just reused across runs. Harmless: every test here uses a
    # delivery_id unique to itself, and CI's Postgres service container is ephemeral
    # per run, so nothing accumulates across CI runs -- only repeated local runs
    # against a persistent dev volume leave a few small rows behind.
    state = {"delivery_ids": []}
    try:
        yield conn, state
    finally:
        # A failed assertion inside a `with conn.cursor()` block can leave the
        # connection's transaction aborted (INERROR) -- rollback first so the cleanup
        # DELETE below doesn't itself silently no-op on an already-broken transaction.
        conn.rollback()
        with conn.cursor() as cur:
            if state["delivery_ids"]:
                cur.execute("DELETE FROM webhook_deliveries WHERE id = ANY(%s)", (state["delivery_ids"],))
        conn.commit()
        conn.close()


@pytest.fixture()
def tenant_id(pg_conn):
    """A single shared tenant reused by every test in this file (find-or-create,
    never deleted -- see pg_conn's docstring for why)."""
    conn, _state = pg_conn
    email = "s4-consumer-tests@example.com"
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE lower(email) = lower(%s)", (email,))
        row = cur.fetchone()
        if row:
            user_id = row[0]
        else:
            cur.execute("INSERT INTO users (email) VALUES (%s) RETURNING id", (email,))
            user_id = cur.fetchone()[0]
        cur.execute("SELECT id FROM tenants WHERE personal_user_id = %s", (user_id,))
        row = cur.fetchone()
        if row:
            result = row[0]
        else:
            cur.execute("INSERT INTO tenants (kind, personal_user_id) VALUES ('personal', %s) RETURNING id", (user_id,))
            result = cur.fetchone()[0]
    conn.commit()
    return result


@pytest.fixture()
def redis_client():
    client = event_consumer._redis_client()
    client.flushdb()
    yield client
    client.flushdb()


def _make_delivery(conn, state, *, tenant_id, delivery_id, event_type, payload: dict) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO webhook_deliveries (tenant_id, delivery_id, event_type, payload, status)
            VALUES (%s, %s, %s, %s, 'queued')
            RETURNING id
            """,
            (tenant_id, delivery_id, event_type, json.dumps(payload).encode()),
        )
        row_id = cur.fetchone()[0]
    conn.commit()
    state["delivery_ids"].append(row_id)
    return row_id


def _entry_fields(row_id: int, event_type: str, tenant_id: int) -> dict:
    return {"delivery_row_id": str(row_id), "event_type": event_type, "tenant_id": str(tenant_id)}


class _FakeRedis:
    def __init__(self):
        self.acked = []

    def xack(self, *args, **kwargs):
        self.acked.append(args)


def test_normalizes_a_push_event(pg_conn, tenant_id):
    conn, state = pg_conn
    row_id = _make_delivery(
        conn,
        state,
        tenant_id=tenant_id,
        delivery_id="d-push-1",
        event_type="push",
        payload={
            "ref": "refs/heads/main",
            "commits": [{"id": "abc"}, {"id": "def"}],
            "repository": {"full_name": "acme/widgets"},
            "sender": {"login": "octocat", "avatar_url": "https://example.com/a.png"},
        },
    )

    event_consumer._process_entry(conn, _FakeRedis(), "1-0", _entry_fields(row_id, "push", tenant_id))

    with conn.cursor() as cur:
        cur.execute(f"SET app.tenant_id = {int(tenant_id)}")
        cur.execute(
            "SELECT tenant_id, event_type, actor, actor_avatar, repo, summary FROM repo_events WHERE delivery_id = %s",
            ("d-push-1",),
        )
        row = cur.fetchone()
    assert row == (tenant_id, "push", "octocat", "https://example.com/a.png", "acme/widgets", "pushed 2 commits to main")

    with conn.cursor() as cur:
        cur.execute("SELECT status FROM webhook_deliveries WHERE id = %s", (row_id,))
        assert cur.fetchone()[0] == "processed"


@pytest.mark.parametrize(
    "event_type,payload,expected_summary",
    [
        (
            "pull_request",
            {"action": "opened", "number": 7, "pull_request": {"title": "Add widgets", "merged": False}},
            "opened PR #7: Add widgets",
        ),
        (
            "pull_request",
            {"action": "closed", "number": 7, "pull_request": {"title": "Add widgets", "merged": True}},
            "merged PR #7: Add widgets",
        ),
        (
            "issues",
            {"action": "opened", "issue": {"number": 3, "title": "Bug"}},
            "opened issue #3: Bug",
        ),
        (
            "release",
            {"release": {"tag_name": "v1.2.3"}},
            "created release v1.2.3",
        ),
        (
            "create",
            {"ref_type": "branch", "ref": "feature-x"},
            "created branch feature-x",
        ),
    ],
)
def test_normalizes_each_ingested_event_type(pg_conn, tenant_id, event_type, payload, expected_summary):
    conn, state = pg_conn
    # Two pull_request cases share event_type ("opened" vs "closed"/merged) -- a
    # payload-derived suffix keeps every case's delivery_id unique regardless of how
    # many share the same event_type.
    delivery_id = f"d-{event_type}-{abs(hash(expected_summary))}"
    payload = {**payload, "repository": {"full_name": "acme/widgets"}, "sender": {"login": "octocat", "avatar_url": ""}}
    row_id = _make_delivery(conn, state, tenant_id=tenant_id, delivery_id=delivery_id, event_type=event_type, payload=payload)

    event_consumer._process_entry(conn, _FakeRedis(), "1-0", _entry_fields(row_id, event_type, tenant_id))

    with conn.cursor() as cur:
        cur.execute(f"SET app.tenant_id = {int(tenant_id)}")
        cur.execute("SELECT summary FROM repo_events WHERE delivery_id = %s", (delivery_id,))
        assert cur.fetchone()[0] == expected_summary


def test_processing_the_same_delivery_twice_is_idempotent(pg_conn, tenant_id):
    conn, state = pg_conn
    payload = {"repository": {"full_name": "acme/widgets"}, "sender": {"login": "octocat", "avatar_url": ""}, "ref_type": "tag", "ref": "v1"}
    row_id = _make_delivery(conn, state, tenant_id=tenant_id, delivery_id="d-dedupe-1", event_type="create", payload=payload)

    fields = _entry_fields(row_id, "create", tenant_id)
    event_consumer._process_entry(conn, _FakeRedis(), "1-0", fields)
    event_consumer._process_entry(conn, _FakeRedis(), "2-0", fields)  # simulates a redelivery/reprocess

    with conn.cursor() as cur:
        cur.execute(f"SET app.tenant_id = {int(tenant_id)}")
        cur.execute("SELECT count(*) FROM repo_events WHERE delivery_id = %s", ("d-dedupe-1",))
        assert cur.fetchone()[0] == 1


def test_null_tenant_delivery_is_skipped_not_normalized(pg_conn):
    conn, state = pg_conn
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO webhook_deliveries (tenant_id, delivery_id, event_type, payload, status)
            VALUES (NULL, %s, 'issues', %s, 'queued')
            RETURNING id
            """,
            ("d-no-tenant-1", json.dumps({"repository": {"full_name": "acme/widgets"}}).encode()),
        )
        row_id = cur.fetchone()[0]
    conn.commit()
    state["delivery_ids"].append(row_id)

    redis_client = _FakeRedis()
    event_consumer._process_entry(conn, redis_client, "1-0", {"delivery_row_id": str(row_id), "event_type": "issues", "tenant_id": ""})

    assert len(redis_client.acked) == 1  # acked so it doesn't sit pending forever
    with conn.cursor() as cur:
        # Asserting absence, not presence -- no row was ever inserted for this
        # delivery, so the count is 0 regardless of what app.tenant_id (unset here)
        # would otherwise restrict under RLS.
        cur.execute("SELECT count(*) FROM repo_events WHERE delivery_id = %s", ("d-no-tenant-1",))
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT status FROM webhook_deliveries WHERE id = %s", (row_id,))
        assert cur.fetchone()[0] == "queued"  # left as-is, not marked processed


def test_unknown_stream_entry_missing_delivery_row_id_is_dropped_not_crashed(pg_conn):
    conn, _state = pg_conn
    redis_client = _FakeRedis()

    event_consumer._process_entry(conn, redis_client, "1-0", {"event_type": "push"})

    assert len(redis_client.acked) == 1


def test_stream_entry_referencing_a_missing_webhook_delivery_row_is_dropped(pg_conn):
    conn, _state = pg_conn
    redis_client = _FakeRedis()

    event_consumer._process_entry(conn, redis_client, "1-0", {"delivery_row_id": "999999999", "event_type": "push", "tenant_id": ""})

    assert len(redis_client.acked) == 1


def test_full_loop_reads_from_redis_and_reclaims_a_crashed_consumers_entry(pg_conn, tenant_id, redis_client):
    conn, state = pg_conn
    payload = {"repository": {"full_name": "acme/widgets"}, "sender": {"login": "octocat", "avatar_url": ""}, "ref_type": "tag", "ref": "v2"}
    row_id = _make_delivery(conn, state, tenant_id=tenant_id, delivery_id="d-e2e-1", event_type="create", payload=payload)

    event_consumer._ensure_group(redis_client)
    redis_client.xadd(event_consumer._STREAM_KEY, _entry_fields(row_id, "create", tenant_id))

    # Simulate a crashed consumer: read (claims the entry, delivery count -> 1) but
    # never ack.
    resp = redis_client.xreadgroup(event_consumer._GROUP_NAME, "dead-consumer", {event_consumer._STREAM_KEY: ">"}, count=10)
    assert len(resp) == 1

    # _sweep_pending with idle=0 reclaims it immediately (no need to actually wait
    # _RECLAIM_IDLE_MS in a test) and processes it under this test's own connection.
    original_idle = event_consumer._RECLAIM_IDLE_MS
    event_consumer._RECLAIM_IDLE_MS = 0
    try:
        event_consumer._sweep_pending(conn, redis_client)
    finally:
        event_consumer._RECLAIM_IDLE_MS = original_idle

    with conn.cursor() as cur:
        cur.execute(f"SET app.tenant_id = {int(tenant_id)}")
        cur.execute("SELECT count(*) FROM repo_events WHERE delivery_id = %s", ("d-e2e-1",))
        assert cur.fetchone()[0] == 1
