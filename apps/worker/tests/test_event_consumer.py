"""Tests for the webhook_events Redis Streams consumer (issue #191/S4)."""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import psycopg
import pytest
import redis

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
            # Unlike repo_events/security_alerts, clevis_api/clevis_worker ARE granted
            # DELETE on org_members/repo_collaborators (migration 0040 -- the consumer
            # deletes a row on member_removed/removed, so it needs the privilege anyway)
            # -- clean up test rows scoped to the shared tenant rather than letting them
            # accumulate across local runs, since there's no per-row id to track like
            # delivery_ids. find-or-create's own SELECT (below) can return no row on a
            # fresh DB where no test in this session has run yet -- guard against that.
            cur.execute(
                "SELECT tenants.id FROM tenants JOIN users ON users.id = tenants.personal_user_id "
                "WHERE lower(users.email) = lower(%s)",
                ("s4-consumer-tests@example.com",),
            )
            row = cur.fetchone()
            if row is not None:
                cur.execute(f"SET app.tenant_id = {int(row[0])}")
                cur.execute("DELETE FROM org_members WHERE tenant_id = %s", (row[0],))
                cur.execute("DELETE FROM repo_collaborators WHERE tenant_id = %s", (row[0],))
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


def _make_delivery(conn, state, *, tenant_id, delivery_id, event_type, payload: dict, received_at=None) -> int:
    with conn.cursor() as cur:
        if received_at is None:
            cur.execute(
                """
                INSERT INTO webhook_deliveries (tenant_id, delivery_id, event_type, payload, status)
                VALUES (%s, %s, %s, %s, 'queued')
                RETURNING id
                """,
                (tenant_id, delivery_id, event_type, json.dumps(payload).encode()),
            )
        else:
            cur.execute(
                """
                INSERT INTO webhook_deliveries (tenant_id, delivery_id, event_type, payload, status, received_at)
                VALUES (%s, %s, %s, %s, 'queued', %s)
                RETURNING id
                """,
                (tenant_id, delivery_id, event_type, json.dumps(payload).encode(), received_at),
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


def _daily_count(conn, tenant_id, repo, event_type, day):
    with conn.cursor() as cur:
        cur.execute(f"SET app.tenant_id = {int(tenant_id)}")
        cur.execute(
            "SELECT count FROM repo_event_daily_counts WHERE tenant_id = %s AND repo = %s AND event_type = %s AND day = %s",
            (tenant_id, repo, event_type, day),
        )
        row = cur.fetchone()
    return row[0] if row else None


def test_aggregates_a_daily_count_on_insert(pg_conn, tenant_id):
    conn, state = pg_conn
    received_at = datetime(2026, 8, 21, 10, 0, tzinfo=timezone.utc)
    payload = {"repository": {"full_name": "acme/agg-fresh"}, "sender": {"login": "octocat", "avatar_url": ""}, "ref_type": "tag", "ref": "v1"}
    row_id = _make_delivery(conn, state, tenant_id=tenant_id, delivery_id="d-agg-fresh-1", event_type="create", payload=payload, received_at=received_at)

    event_consumer._process_entry(conn, _FakeRedis(), "1-0", _entry_fields(row_id, "create", tenant_id))

    assert _daily_count(conn, tenant_id, "acme/agg-fresh", "create", received_at.date()) == 1


def test_aggregates_increment_on_a_second_distinct_event_same_day(pg_conn, tenant_id):
    conn, state = pg_conn
    received_at = datetime(2026, 8, 21, 11, 0, tzinfo=timezone.utc)
    payload_a = {"repository": {"full_name": "acme/agg-increment"}, "sender": {"login": "octocat", "avatar_url": ""}, "ref_type": "tag", "ref": "v1"}
    payload_b = {"repository": {"full_name": "acme/agg-increment"}, "sender": {"login": "octocat", "avatar_url": ""}, "ref_type": "tag", "ref": "v2"}
    row_a = _make_delivery(conn, state, tenant_id=tenant_id, delivery_id="d-agg-increment-1", event_type="create", payload=payload_a, received_at=received_at)
    row_b = _make_delivery(conn, state, tenant_id=tenant_id, delivery_id="d-agg-increment-2", event_type="create", payload=payload_b, received_at=received_at)

    event_consumer._process_entry(conn, _FakeRedis(), "1-0", _entry_fields(row_a, "create", tenant_id))
    event_consumer._process_entry(conn, _FakeRedis(), "2-0", _entry_fields(row_b, "create", tenant_id))

    assert _daily_count(conn, tenant_id, "acme/agg-increment", "create", received_at.date()) == 2


def test_aggregates_unchanged_when_the_same_delivery_is_reprocessed(pg_conn, tenant_id):
    conn, state = pg_conn
    received_at = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    payload = {"repository": {"full_name": "acme/agg-dedupe"}, "sender": {"login": "octocat", "avatar_url": ""}, "ref_type": "tag", "ref": "v1"}
    row_id = _make_delivery(conn, state, tenant_id=tenant_id, delivery_id="d-agg-dedupe-1", event_type="create", payload=payload, received_at=received_at)

    fields = _entry_fields(row_id, "create", tenant_id)
    event_consumer._process_entry(conn, _FakeRedis(), "1-0", fields)
    event_consumer._process_entry(conn, _FakeRedis(), "2-0", fields)  # simulates a redelivery/reprocess

    assert _daily_count(conn, tenant_id, "acme/agg-dedupe", "create", received_at.date()) == 1


def test_aggregates_separate_days_produce_separate_rows(pg_conn, tenant_id):
    conn, state = pg_conn
    day_one = datetime(2026, 8, 20, 23, 0, tzinfo=timezone.utc)
    day_two = datetime(2026, 8, 21, 1, 0, tzinfo=timezone.utc)
    payload_a = {"repository": {"full_name": "acme/agg-days"}, "sender": {"login": "octocat", "avatar_url": ""}, "ref_type": "tag", "ref": "v1"}
    payload_b = {"repository": {"full_name": "acme/agg-days"}, "sender": {"login": "octocat", "avatar_url": ""}, "ref_type": "tag", "ref": "v2"}
    row_a = _make_delivery(conn, state, tenant_id=tenant_id, delivery_id="d-agg-days-1", event_type="create", payload=payload_a, received_at=day_one)
    row_b = _make_delivery(conn, state, tenant_id=tenant_id, delivery_id="d-agg-days-2", event_type="create", payload=payload_b, received_at=day_two)

    event_consumer._process_entry(conn, _FakeRedis(), "1-0", _entry_fields(row_a, "create", tenant_id))
    event_consumer._process_entry(conn, _FakeRedis(), "2-0", _entry_fields(row_b, "create", tenant_id))

    assert _daily_count(conn, tenant_id, "acme/agg-days", "create", day_one.date()) == 1
    assert _daily_count(conn, tenant_id, "acme/agg-days", "create", day_two.date()) == 1


def test_aggregates_use_the_utc_day_regardless_of_session_timezone(pg_conn, tenant_id):
    conn, state = pg_conn
    # 00:30 UTC on Aug 21 is still Aug 20 in America/Los_Angeles (UTC-7 in August) -- the
    # aggregate's "day" must bucket by UTC, not whatever timezone this Postgres session
    # happens to be in (CodeRabbit finding on #341: psycopg returns timestamptz values in
    # the session's TimeZone, not UTC, so a naive .date() call would misbucket this).
    received_at = datetime(2026, 8, 21, 0, 30, tzinfo=timezone.utc)
    payload = {"repository": {"full_name": "acme/agg-tz"}, "sender": {"login": "octocat", "avatar_url": ""}, "ref_type": "tag", "ref": "v1"}
    row_id = _make_delivery(conn, state, tenant_id=tenant_id, delivery_id="d-agg-tz-1", event_type="create", payload=payload, received_at=received_at)

    with conn.cursor() as cur:
        cur.execute("SET TIME ZONE 'America/Los_Angeles'")
    event_consumer._process_entry(conn, _FakeRedis(), "1-0", _entry_fields(row_id, "create", tenant_id))

    assert _daily_count(conn, tenant_id, "acme/agg-tz", "create", received_at.date()) == 1  # UTC day, not LA day (Aug 20)


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


def _security_alert(conn, tenant_id, repo, kind, number):
    with conn.cursor() as cur:
        cur.execute(f"SET app.tenant_id = {int(tenant_id)}")
        cur.execute(
            "SELECT state, severity, details, created_at, updated_at FROM security_alerts "
            "WHERE tenant_id = %s AND repo = %s AND kind = %s AND number = %s",
            (tenant_id, repo, kind, number),
        )
        return cur.fetchone()


_ALERT_PAYLOADS = [
    (
        "dependabot_alert",
        "dependabot",
        {
            "action": "created",
            "repository": {"full_name": "acme/widgets"},
            "sender": {"login": "octocat"},
            "alert": {
                "number": 7,
                "state": "open",
                "security_advisory": {"severity": "high"},
                "dependency": {"package": {"name": "lodash"}},
                "created_at": "2026-08-20T10:00:00Z",
                "updated_at": "2026-08-20T10:00:00Z",
            },
        },
    ),
    (
        "code_scanning_alert",
        "code_scanning",
        {
            "action": "created",
            "repository": {"full_name": "acme/widgets"},
            "sender": {"login": "octocat"},
            "alert": {
                "number": 3,
                "state": "open",
                "rule": {"id": "js/sql-injection", "severity": "error"},
                "tool": {"name": "CodeQL"},
                "created_at": "2026-08-20T11:00:00Z",
                "updated_at": "2026-08-20T11:00:00Z",
            },
        },
    ),
    (
        "secret_scanning_alert",
        "secret_scanning",
        {
            "action": "created",
            "repository": {"full_name": "acme/widgets"},
            "sender": {"login": "octocat"},
            "alert": {
                "number": 1,
                "state": "open",
                "secret_type": "github_personal_access_token",
                "secret_type_display_name": "GitHub Personal Access Token",
                "created_at": "2026-08-20T12:00:00Z",
                "updated_at": "2026-08-20T12:00:00Z",
            },
        },
    ),
]


@pytest.mark.parametrize("event_type,kind,payload", _ALERT_PAYLOADS)
def test_normalizes_each_security_alert_kind_into_security_alerts(event_type, kind, payload, pg_conn, tenant_id):
    conn, state = pg_conn
    number = payload["alert"]["number"]
    row_id = _make_delivery(
        conn, state, tenant_id=tenant_id, delivery_id=f"d-{event_type}-1", event_type=event_type, payload=payload
    )

    redis_client = _FakeRedis()
    event_consumer._process_entry(conn, redis_client, "1-0", _entry_fields(row_id, event_type, tenant_id))

    assert redis_client.acked == [(event_consumer._STREAM_KEY, event_consumer._GROUP_NAME, "1-0")]
    row = _security_alert(conn, tenant_id, "acme/widgets", kind, number)
    assert row is not None
    state_col, severity, details, created_at, updated_at = row
    assert state_col == "open"
    assert created_at == event_consumer._parse_alert_timestamp(payload["alert"]["created_at"], None)
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM webhook_deliveries WHERE id = %s", (row_id,))
        assert cur.fetchone()[0] == "processed"


def test_security_alert_severity_and_details_are_kind_specific(pg_conn, tenant_id):
    conn, state = pg_conn
    dependabot_payload = _ALERT_PAYLOADS[0][2]
    row_id = _make_delivery(
        conn, state, tenant_id=tenant_id, delivery_id="d-dependabot-severity-1", event_type="dependabot_alert", payload=dependabot_payload
    )
    event_consumer._process_entry(conn, _FakeRedis(), "1-0", _entry_fields(row_id, "dependabot_alert", tenant_id))
    _, severity, details, _, _ = _security_alert(conn, tenant_id, "acme/widgets", "dependabot", 7)
    assert severity == "high"
    assert details["dependency"]["package"]["name"] == "lodash"

    secret_payload = _ALERT_PAYLOADS[2][2]
    row_id_2 = _make_delivery(
        conn, state, tenant_id=tenant_id, delivery_id="d-secret-severity-1", event_type="secret_scanning_alert", payload=secret_payload
    )
    event_consumer._process_entry(conn, _FakeRedis(), "2-0", _entry_fields(row_id_2, "secret_scanning_alert", tenant_id))
    _, severity_2, details_2, _, _ = _security_alert(conn, tenant_id, "acme/widgets", "secret_scanning", 1)
    assert severity_2 is None  # secret-scanning alerts carry no severity in GitHub's payload
    assert details_2["secret_type"] == "github_personal_access_token"
    assert details_2["resolution"] is None  # not yet resolved on the initial "created" webhook


def test_redelivered_security_alert_updates_state_instead_of_duplicating(pg_conn, tenant_id):
    conn, state = pg_conn
    payload = {
        "action": "created",
        "repository": {"full_name": "acme/widgets"},
        "sender": {"login": "octocat"},
        "alert": {
            "number": 42,
            "state": "open",
            "security_advisory": {"severity": "critical"},
            "dependency": {},
            "created_at": "2026-08-20T09:00:00Z",
            "updated_at": "2026-08-20T09:00:00Z",
        },
    }
    row_id = _make_delivery(conn, state, tenant_id=tenant_id, delivery_id="d-alert-dismiss-1", event_type="dependabot_alert", payload=payload)
    event_consumer._process_entry(conn, _FakeRedis(), "1-0", _entry_fields(row_id, "dependabot_alert", tenant_id))

    dismissed_payload = {**payload, "action": "dismissed", "alert": {**payload["alert"], "state": "dismissed", "updated_at": "2026-08-21T09:00:00Z"}}
    row_id_2 = _make_delivery(
        conn, state, tenant_id=tenant_id, delivery_id="d-alert-dismiss-2", event_type="dependabot_alert", payload=dismissed_payload
    )
    event_consumer._process_entry(conn, _FakeRedis(), "2-0", _entry_fields(row_id_2, "dependabot_alert", tenant_id))

    with conn.cursor() as cur:
        cur.execute(f"SET app.tenant_id = {int(tenant_id)}")
        cur.execute(
            "SELECT count(*) FROM security_alerts WHERE tenant_id = %s AND repo = %s AND kind = %s AND number = %s",
            (tenant_id, "acme/widgets", "dependabot", 42),
        )
        assert cur.fetchone()[0] == 1  # one row, upserted, not duplicated
    row = _security_alert(conn, tenant_id, "acme/widgets", "dependabot", 42)
    assert row[0] == "dismissed"


def test_security_alert_with_missing_alert_number_is_dropped_not_crashed(pg_conn, tenant_id):
    conn, state = pg_conn
    payload = {"repository": {"full_name": "acme/widgets"}, "sender": {"login": "octocat"}, "alert": {"state": "open"}}
    row_id = _make_delivery(conn, state, tenant_id=tenant_id, delivery_id="d-alert-no-number-1", event_type="dependabot_alert", payload=payload)

    redis_client = _FakeRedis()
    event_consumer._process_entry(conn, redis_client, "1-0", _entry_fields(row_id, "dependabot_alert", tenant_id))

    assert len(redis_client.acked) == 1
    with conn.cursor() as cur:
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


def test_summarize_unknown_event_type_returns_the_event_type_itself():
    assert event_consumer._summarize("deployment", {}) == "deployment"


def test_normalize_returns_none_when_repository_full_name_is_missing():
    assert event_consumer._normalize("push", {"sender": {"login": "octocat"}}, None) is None


def test_parse_alert_timestamp_falls_back_on_missing_or_malformed_value():
    fallback = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert event_consumer._parse_alert_timestamp(None, fallback) == fallback
    assert event_consumer._parse_alert_timestamp("not-a-real-timestamp", fallback) == fallback


def _org_member(conn, tenant_id, login):
    with conn.cursor() as cur:
        cur.execute(f"SET app.tenant_id = {int(tenant_id)}")
        cur.execute("SELECT avatar_url, role FROM org_members WHERE tenant_id = %s AND login = %s", (tenant_id, login))
        return cur.fetchone()


def _repo_collaborator(conn, tenant_id, repo, login):
    with conn.cursor() as cur:
        cur.execute(f"SET app.tenant_id = {int(tenant_id)}")
        cur.execute(
            "SELECT permission, source, is_outside_collaborator FROM repo_collaborators "
            "WHERE tenant_id = %s AND repo = %s AND login = %s",
            (tenant_id, repo, login),
        )
        return cur.fetchone()


def test_organization_member_added_upserts_org_member(pg_conn, tenant_id):
    conn, state = pg_conn
    payload = {
        "action": "member_added",
        "membership": {"role": "admin", "user": {"login": "octocat", "avatar_url": "https://example.com/o.png"}},
    }
    row_id = _make_delivery(conn, state, tenant_id=tenant_id, delivery_id="d-org-member-added-1", event_type="organization", payload=payload)

    redis_client = _FakeRedis()
    event_consumer._process_entry(conn, redis_client, "1-0", _entry_fields(row_id, "organization", tenant_id))

    assert redis_client.acked == [(event_consumer._STREAM_KEY, event_consumer._GROUP_NAME, "1-0")]
    row = _org_member(conn, tenant_id, "octocat")
    assert row == ("https://example.com/o.png", "admin")
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM webhook_deliveries WHERE id = %s", (row_id,))
        assert cur.fetchone()[0] == "processed"


def test_organization_member_removed_deletes_org_member(pg_conn, tenant_id):
    conn, state = pg_conn
    added_payload = {
        "action": "member_added",
        "membership": {"role": "member", "user": {"login": "removeme", "avatar_url": ""}},
    }
    row_id = _make_delivery(conn, state, tenant_id=tenant_id, delivery_id="d-org-member-remove-1", event_type="organization", payload=added_payload)
    event_consumer._process_entry(conn, _FakeRedis(), "1-0", _entry_fields(row_id, "organization", tenant_id))
    assert _org_member(conn, tenant_id, "removeme") is not None

    removed_payload = {"action": "member_removed", "membership": {"user": {"login": "removeme"}}}
    row_id_2 = _make_delivery(conn, state, tenant_id=tenant_id, delivery_id="d-org-member-remove-2", event_type="organization", payload=removed_payload)
    event_consumer._process_entry(conn, _FakeRedis(), "2-0", _entry_fields(row_id_2, "organization", tenant_id))

    assert _org_member(conn, tenant_id, "removeme") is None


def test_organization_member_invited_is_a_noop_for_org_members(pg_conn, tenant_id):
    conn, state = pg_conn
    payload = {"action": "member_invited", "membership": {"user": {"login": "pending-invite"}}}
    row_id = _make_delivery(conn, state, tenant_id=tenant_id, delivery_id="d-org-invited-1", event_type="organization", payload=payload)

    redis_client = _FakeRedis()
    event_consumer._process_entry(conn, redis_client, "1-0", _entry_fields(row_id, "organization", tenant_id))

    assert redis_client.acked == [(event_consumer._STREAM_KEY, event_consumer._GROUP_NAME, "1-0")]
    assert _org_member(conn, tenant_id, "pending-invite") is None
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM webhook_deliveries WHERE id = %s", (row_id,))
        assert cur.fetchone()[0] == "processed"


def test_redelivered_member_added_does_not_overwrite_role(pg_conn, tenant_id):
    conn, state = pg_conn
    payload = {"action": "member_added", "membership": {"role": "admin", "user": {"login": "stable-role", "avatar_url": "https://example.com/a.png"}}}
    row_id = _make_delivery(conn, state, tenant_id=tenant_id, delivery_id="d-org-role-1", event_type="organization", payload=payload)
    event_consumer._process_entry(conn, _FakeRedis(), "1-0", _entry_fields(row_id, "organization", tenant_id))

    # Redelivery (or a re-add with a stale role snapshot) must not clobber a role a
    # future reconciliation poll may have already corrected -- see org_membership_store.py.
    redelivered_payload = {"action": "member_added", "membership": {"role": "member", "user": {"login": "stable-role", "avatar_url": "https://example.com/b.png"}}}
    row_id_2 = _make_delivery(conn, state, tenant_id=tenant_id, delivery_id="d-org-role-2", event_type="organization", payload=redelivered_payload)
    event_consumer._process_entry(conn, _FakeRedis(), "2-0", _entry_fields(row_id_2, "organization", tenant_id))

    row = _org_member(conn, tenant_id, "stable-role")
    assert row == ("https://example.com/b.png", "admin")  # avatar refreshed, role untouched


def test_member_added_upserts_repo_collaborator(pg_conn, tenant_id):
    conn, state = pg_conn
    payload = {
        "action": "added",
        "repository": {"full_name": "acme/widgets"},
        "member": {"login": "collab1"},
        "changes": {"permission": {"to": "push"}},
    }
    row_id = _make_delivery(conn, state, tenant_id=tenant_id, delivery_id="d-member-added-1", event_type="member", payload=payload)

    redis_client = _FakeRedis()
    event_consumer._process_entry(conn, redis_client, "1-0", _entry_fields(row_id, "member", tenant_id))

    assert redis_client.acked == [(event_consumer._STREAM_KEY, event_consumer._GROUP_NAME, "1-0")]
    row = _repo_collaborator(conn, tenant_id, "acme/widgets", "collab1")
    assert row == ("push", "direct", False)


def test_member_edited_updates_permission(pg_conn, tenant_id):
    conn, state = pg_conn
    added_payload = {
        "action": "added", "repository": {"full_name": "acme/widgets"}, "member": {"login": "collab2"},
        "changes": {"permission": {"to": "pull"}},
    }
    row_id = _make_delivery(conn, state, tenant_id=tenant_id, delivery_id="d-member-edit-1", event_type="member", payload=added_payload)
    event_consumer._process_entry(conn, _FakeRedis(), "1-0", _entry_fields(row_id, "member", tenant_id))

    edited_payload = {
        "action": "edited", "repository": {"full_name": "acme/widgets"}, "member": {"login": "collab2"},
        "changes": {"permission": {"from": "pull", "to": "admin"}},
    }
    row_id_2 = _make_delivery(conn, state, tenant_id=tenant_id, delivery_id="d-member-edit-2", event_type="member", payload=edited_payload)
    event_consumer._process_entry(conn, _FakeRedis(), "2-0", _entry_fields(row_id_2, "member", tenant_id))

    row = _repo_collaborator(conn, tenant_id, "acme/widgets", "collab2")
    assert row == ("admin", "direct", False)


def test_member_removed_deletes_repo_collaborator(pg_conn, tenant_id):
    conn, state = pg_conn
    added_payload = {
        "action": "added", "repository": {"full_name": "acme/widgets"}, "member": {"login": "collab3"},
        "changes": {"permission": {"to": "push"}},
    }
    row_id = _make_delivery(conn, state, tenant_id=tenant_id, delivery_id="d-member-remove-1", event_type="member", payload=added_payload)
    event_consumer._process_entry(conn, _FakeRedis(), "1-0", _entry_fields(row_id, "member", tenant_id))
    assert _repo_collaborator(conn, tenant_id, "acme/widgets", "collab3") is not None

    removed_payload = {"action": "removed", "repository": {"full_name": "acme/widgets"}, "member": {"login": "collab3"}}
    row_id_2 = _make_delivery(conn, state, tenant_id=tenant_id, delivery_id="d-member-remove-2", event_type="member", payload=removed_payload)
    event_consumer._process_entry(conn, _FakeRedis(), "2-0", _entry_fields(row_id_2, "member", tenant_id))

    assert _repo_collaborator(conn, tenant_id, "acme/widgets", "collab3") is None


@pytest.mark.parametrize("event_type", ["membership", "team"])
def test_membership_and_team_events_are_acked_but_not_normalized(event_type, pg_conn, tenant_id):
    # Team-based repo access is explicitly deferred (org_membership_store.py's module
    # docstring) -- these event types are durably queued (webhooks.py) but have no
    # consumer yet, same placeholder posture PR #350 used for the security alerts.
    conn, state = pg_conn
    row_id = _make_delivery(
        conn, state, tenant_id=tenant_id, delivery_id=f"d-{event_type}-1", event_type=event_type,
        payload={"action": "added"},
    )

    redis_client = _FakeRedis()
    event_consumer._process_entry(conn, redis_client, "1-0", _entry_fields(row_id, event_type, tenant_id))

    assert redis_client.acked == [(event_consumer._STREAM_KEY, event_consumer._GROUP_NAME, "1-0")]
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM webhook_deliveries WHERE id = %s", (row_id,))
        assert cur.fetchone()[0] == "queued"  # left as-is, not marked processed


def test_member_event_with_missing_login_is_dropped_not_crashed(pg_conn, tenant_id):
    conn, state = pg_conn
    payload = {"action": "added", "repository": {"full_name": "acme/widgets"}, "member": {}}
    row_id = _make_delivery(conn, state, tenant_id=tenant_id, delivery_id="d-member-no-login-1", event_type="member", payload=payload)

    redis_client = _FakeRedis()
    event_consumer._process_entry(conn, redis_client, "1-0", _entry_fields(row_id, "member", tenant_id))

    assert len(redis_client.acked) == 1
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM webhook_deliveries WHERE id = %s", (row_id,))
        assert cur.fetchone()[0] == "queued"


def test_organization_member_added_with_missing_login_is_dropped_not_crashed(pg_conn, tenant_id):
    conn, state = pg_conn
    payload = {"action": "member_added", "membership": {"role": "member", "user": {}}}
    row_id = _make_delivery(conn, state, tenant_id=tenant_id, delivery_id="d-org-no-login-1", event_type="organization", payload=payload)

    redis_client = _FakeRedis()
    event_consumer._process_entry(conn, redis_client, "1-0", _entry_fields(row_id, "organization", tenant_id))

    assert len(redis_client.acked) == 1
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM webhook_deliveries WHERE id = %s", (row_id,))
        assert cur.fetchone()[0] == "queued"


def test_process_entry_drops_malformed_json_payload(pg_conn, tenant_id):
    conn, state = pg_conn
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO webhook_deliveries (tenant_id, delivery_id, event_type, payload, status)
            VALUES (%s, 'd-malformed-1', 'push', %s, 'queued')
            RETURNING id
            """,
            (tenant_id, b"not json"),
        )
        row_id = cur.fetchone()[0]
    conn.commit()
    state["delivery_ids"].append(row_id)

    redis_client = _FakeRedis()
    event_consumer._process_entry(conn, redis_client, "1-0", _entry_fields(row_id, "push", tenant_id))

    assert len(redis_client.acked) == 1
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM webhook_deliveries WHERE id = %s", (row_id,))
        assert cur.fetchone()[0] == "queued"


def test_process_entry_drops_payload_missing_repository(pg_conn, tenant_id):
    conn, state = pg_conn
    row_id = _make_delivery(
        conn, state, tenant_id=tenant_id, delivery_id="d-no-repo-1", event_type="push",
        payload={"sender": {"login": "octocat"}},
    )

    redis_client = _FakeRedis()
    event_consumer._process_entry(conn, redis_client, "1-0", _entry_fields(row_id, "push", tenant_id))

    assert len(redis_client.acked) == 1
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM webhook_deliveries WHERE id = %s", (row_id,))
        assert cur.fetchone()[0] == "queued"


def test_ensure_group_is_idempotent_when_group_already_exists(redis_client):
    event_consumer._ensure_group(redis_client)
    event_consumer._ensure_group(redis_client)  # BUSYGROUP the second time -- must not raise


def test_ensure_group_reraises_non_busygroup_errors(monkeypatch):
    fake = MagicMock()
    fake.xgroup_create.side_effect = redis.ResponseError("some other error")
    with pytest.raises(redis.ResponseError):
        event_consumer._ensure_group(fake)


def test_touch_heartbeat_writes_the_file():
    event_consumer._touch_heartbeat()
    assert event_consumer._HEARTBEAT_FILE.exists()


def test_touch_heartbeat_swallows_oserror(monkeypatch):
    monkeypatch.setattr(
        event_consumer._HEARTBEAT_FILE.__class__, "write_text", MagicMock(side_effect=OSError("disk full"))
    )
    event_consumer._touch_heartbeat()  # must not raise


def test_sweep_pending_with_nothing_pending_is_a_noop(pg_conn, redis_client):
    conn, _state = pg_conn
    event_consumer._ensure_group(redis_client)
    event_consumer._sweep_pending(conn, redis_client)  # no pending entries -- returns early


def test_sweep_pending_drops_entries_past_max_delivery_attempts(pg_conn, tenant_id, redis_client):
    conn, state = pg_conn
    payload = {"repository": {"full_name": "acme/widgets"}, "sender": {"login": "octocat", "avatar_url": ""}, "ref_type": "tag", "ref": "v3"}
    row_id = _make_delivery(conn, state, tenant_id=tenant_id, delivery_id="d-poison-1", event_type="create", payload=payload)

    event_consumer._ensure_group(redis_client)
    entry_id = redis_client.xadd(event_consumer._STREAM_KEY, _entry_fields(row_id, "create", tenant_id))
    redis_client.xreadgroup(event_consumer._GROUP_NAME, "c1", {event_consumer._STREAM_KEY: ">"}, count=10)
    # xclaim increments the delivery counter each call -- push it past _MAX_DELIVERY_ATTEMPTS.
    for _ in range(event_consumer._MAX_DELIVERY_ATTEMPTS + 1):
        redis_client.xclaim(event_consumer._STREAM_KEY, event_consumer._GROUP_NAME, "c1", min_idle_time=0, message_ids=[entry_id])

    # _sweep_pending only considers entries idle at least _RECLAIM_IDLE_MS -- these were
    # just claimed, so drop it to 0 for this test rather than sleep for real.
    original_idle = event_consumer._RECLAIM_IDLE_MS
    event_consumer._RECLAIM_IDLE_MS = 0
    try:
        event_consumer._sweep_pending(conn, redis_client)
    finally:
        event_consumer._RECLAIM_IDLE_MS = original_idle

    pending = redis_client.xpending_range(event_consumer._STREAM_KEY, event_consumer._GROUP_NAME, min="-", max="+", count=10)
    assert pending == []  # dropped (acked), not reclaimed/reprocessed
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM webhook_deliveries WHERE id = %s", (row_id,))
        assert cur.fetchone()[0] == "queued"  # never actually processed


def test_sweep_pending_logs_and_continues_when_claimed_entry_processing_raises(pg_conn, tenant_id, redis_client, monkeypatch):
    conn, state = pg_conn
    payload = {"repository": {"full_name": "acme/widgets"}, "sender": {"login": "octocat", "avatar_url": ""}, "ref_type": "tag", "ref": "v4"}
    row_id = _make_delivery(conn, state, tenant_id=tenant_id, delivery_id="d-raises-1", event_type="create", payload=payload)

    event_consumer._ensure_group(redis_client)
    redis_client.xadd(event_consumer._STREAM_KEY, _entry_fields(row_id, "create", tenant_id))
    redis_client.xreadgroup(event_consumer._GROUP_NAME, "dead-consumer", {event_consumer._STREAM_KEY: ">"}, count=10)

    monkeypatch.setattr(event_consumer, "_process_entry", MagicMock(side_effect=RuntimeError("boom")))
    original_idle = event_consumer._RECLAIM_IDLE_MS
    event_consumer._RECLAIM_IDLE_MS = 0
    try:
        event_consumer._sweep_pending(conn, redis_client)  # must not raise
    finally:
        event_consumer._RECLAIM_IDLE_MS = original_idle


class _StopLoop(Exception):
    pass


def test_run_processes_a_stream_entry_then_stops(pg_conn, tenant_id, redis_client, monkeypatch):
    conn, state = pg_conn
    payload = {"repository": {"full_name": "acme/widgets"}, "sender": {"login": "octocat", "avatar_url": ""}, "ref_type": "tag", "ref": "v5"}
    row_id = _make_delivery(conn, state, tenant_id=tenant_id, delivery_id="d-run-loop-1", event_type="create", payload=payload)

    event_consumer._ensure_group(redis_client)
    redis_client.xadd(event_consumer._STREAM_KEY, _entry_fields(row_id, "create", tenant_id))

    monkeypatch.setattr(event_consumer, "_redis_client", lambda: redis_client)
    monkeypatch.setattr(event_consumer, "_BLOCK_MS", 100)
    # _touch_heartbeat is called first thing every loop iteration -- no-op on the first
    # call (let the iteration actually run), raise on the second so run()'s `while True`
    # stops after processing exactly one batch.
    monkeypatch.setattr(event_consumer, "_touch_heartbeat", MagicMock(side_effect=[None, _StopLoop]))

    with pytest.raises(_StopLoop):
        event_consumer.run()

    with conn.cursor() as cur:
        cur.execute(f"SET app.tenant_id = {int(tenant_id)}")
        cur.execute("SELECT count(*) FROM repo_events WHERE delivery_id = %s", ("d-run-loop-1",))
        assert cur.fetchone()[0] == 1


def test_run_recovers_from_a_connection_error(monkeypatch):
    # psycopg.connect raising is caught *inside* run()'s own try/except (by design --
    # it must never let a connection blip kill the loop), so a sentinel raised there
    # would just be swallowed by that same except, not reach this test. Use the
    # _touch_heartbeat hook (called once per iteration, outside the try) to stop the
    # loop instead, and assert connect() was retried across iterations.
    monkeypatch.setattr(event_consumer, "_redis_client", lambda: MagicMock())
    monkeypatch.setattr(event_consumer, "_ensure_group", MagicMock())
    monkeypatch.setattr(event_consumer.psycopg, "connect", MagicMock(side_effect=psycopg.OperationalError("refused")))
    monkeypatch.setattr(event_consumer.time, "sleep", MagicMock())
    monkeypatch.setattr(event_consumer, "_touch_heartbeat", MagicMock(side_effect=[None, None, _StopLoop]))

    with pytest.raises(_StopLoop):
        event_consumer.run()

    assert event_consumer.psycopg.connect.call_count == 2  # retried instead of crashing after the first failure


def test_run_retries_initialization_instead_of_dying_on_a_startup_redis_error(monkeypatch):
    # A Redis error constructing the client or creating the consumer group used to be
    # completely uncaught -- run() would raise straight out, silently killing the
    # daemon thread it's started on forever (CodeRabbit finding on PR #340).
    attempts = {"n": 0}

    def _flaky_redis_client():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise redis.ConnectionError("not ready yet")
        return MagicMock()

    monkeypatch.setattr(event_consumer, "_redis_client", _flaky_redis_client)
    monkeypatch.setattr(event_consumer, "_ensure_group", MagicMock())
    monkeypatch.setattr(event_consumer.time, "sleep", MagicMock())
    monkeypatch.setattr(event_consumer, "_touch_heartbeat", MagicMock(side_effect=_StopLoop))

    with pytest.raises(_StopLoop):
        event_consumer.run()

    assert attempts["n"] == 2  # retried after the first failure instead of raising out of run()


def test_start_background_thread_runs_in_the_background(monkeypatch):
    ran = []
    monkeypatch.setattr(event_consumer, "run", lambda: ran.append(True))

    thread = event_consumer.start_background_thread()
    thread.join(timeout=2)

    assert ran == [True]
    assert not thread.is_alive()
