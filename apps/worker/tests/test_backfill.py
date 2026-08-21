"""Tests for the install-time activity backfill (issue #191/S5 PR 1)."""

import json
from datetime import date
from unittest.mock import MagicMock, patch

import httpx
import psycopg
import pytest

import backfill
import worker
from _crypto import encrypt_job_token
from config import settings

_DB_URL = settings.database_url.get_secret_value().replace("postgresql+psycopg://", "postgresql://")


# ---------------------------------------------------------------------------
# backfill.py: pure unit tests (no DB, no worker.py job machinery)
# ---------------------------------------------------------------------------


def test_summarize_all_five_tracked_types():
    assert backfill._summarize("push", {"size": 2, "ref": "refs/heads/main"}) == "pushed 2 commits to main"
    assert (
        backfill._summarize("pull_request", {"action": "opened", "number": 7, "pull_request": {"title": "Add widgets"}})
        == "opened PR #7: Add widgets"
    )
    assert (
        backfill._summarize(
            "pull_request", {"action": "closed", "number": 7, "pull_request": {"title": "Add widgets", "merged": True}}
        )
        == "merged PR #7: Add widgets"
    )
    assert backfill._summarize("issues", {"action": "opened", "issue": {"number": 3, "title": "Bug"}}) == "opened issue #3: Bug"
    assert backfill._summarize("release", {"release": {"tag_name": "v1.2.3"}}) == "created release v1.2.3"
    assert backfill._summarize("create", {"ref_type": "branch", "ref": "feature-x"}) == "created branch feature-x"


def test_summarize_unknown_type_returns_the_type_itself():
    assert backfill._summarize("deployment", {}) == "deployment"


def _raw_event(event_type="PushEvent", **overrides):
    event = {
        "id": "111",
        "type": event_type,
        "actor": {"login": "octocat", "avatar_url": "https://example.com/a.png"},
        "repo": {"name": "acme/widgets"},
        "payload": {"size": 1, "ref": "refs/heads/main"},
        "created_at": "2026-08-21T00:30:00Z",
    }
    event.update(overrides)
    return event


def test_normalize_maps_a_known_type():
    normalized = backfill.normalize(_raw_event())
    assert normalized["delivery_id"] == "backfill:111"
    assert normalized["event_type"] == "push"
    assert normalized["actor"] == "octocat"
    assert normalized["actor_avatar"] == "https://example.com/a.png"
    assert normalized["repo"] == "acme/widgets"
    assert normalized["summary"] == "pushed 1 commit to main"
    assert normalized["occurred_at"].isoformat() == "2026-08-21T00:30:00+00:00"


def test_normalize_skips_an_untracked_event_type():
    assert backfill.normalize(_raw_event(event_type="WatchEvent")) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": None},
        {"repo": {}},
        {"created_at": None},
        {"created_at": "not-a-timestamp"},
    ],
)
def test_normalize_returns_none_for_missing_or_malformed_fields(overrides):
    assert backfill.normalize(_raw_event(**overrides)) is None


class _FakeResponse:
    def __init__(self, json_data, links=None, status_code=200, headers=None):
        self._json_data = json_data
        self.links = links or {}
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=MagicMock(), response=self)

    def json(self):
        return self._json_data


class _FakeClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, headers=None, params=None):
        self.calls.append((url, params))
        return self._responses.pop(0)


def test_fetch_events_follows_the_link_header():
    page1 = _FakeResponse([_raw_event(id="1")], links={"next": {"url": "https://api.github.com/orgs/acme/events?page=2"}})
    page2 = _FakeResponse([_raw_event(id="2")])
    client = _FakeClient([page1, page2])

    events = backfill.fetch_events(client, "https://api.github.com", {}, "acme", "Organization")

    assert [e["id"] for e in events] == ["1", "2"]
    assert client.calls[0][0] == "https://api.github.com/orgs/acme/events"
    assert client.calls[1][0] == "https://api.github.com/orgs/acme/events?page=2"


def test_fetch_events_stops_at_the_page_cap():
    pages = [
        _FakeResponse([_raw_event(id=str(i))], links={"next": {"url": f"https://api.github.com/orgs/acme/events?page={i + 1}"}})
        for i in range(5)
    ]
    client = _FakeClient(pages)

    events = backfill.fetch_events(client, "https://api.github.com", {}, "acme", "Organization")

    assert len(events) == backfill._MAX_PAGES
    assert len(client.calls) == backfill._MAX_PAGES


def test_fetch_events_uses_the_user_events_path_for_personal_installs():
    client = _FakeClient([_FakeResponse([])])
    backfill.fetch_events(client, "https://api.github.com", {}, "octocat", "User")
    assert client.calls[0][0] == "https://api.github.com/users/octocat/events"


# ---------------------------------------------------------------------------
# _get_with_retry / fetch_events: GitHub rate-limit retry (issue #192 fast-follow)
# ---------------------------------------------------------------------------


def test_fetch_events_retries_a_429_and_succeeds():
    rate_limited = _FakeResponse({}, status_code=429)
    success = _FakeResponse([_raw_event(id="1")])
    client = _FakeClient([rate_limited, success])

    with patch("backfill.time.sleep") as mock_sleep:
        events = backfill.fetch_events(client, "https://api.github.com", {}, "acme", "Organization")

    assert [e["id"] for e in events] == ["1"]
    assert len(client.calls) == 2
    mock_sleep.assert_called_once()


def test_fetch_events_retries_a_secondary_rate_limit_403_with_retry_after():
    rate_limited = _FakeResponse({}, status_code=403, headers={"Retry-After": "5"})
    success = _FakeResponse([_raw_event(id="1")])
    client = _FakeClient([rate_limited, success])

    with patch("backfill.time.sleep") as mock_sleep:
        events = backfill.fetch_events(client, "https://api.github.com", {}, "acme", "Organization")

    assert [e["id"] for e in events] == ["1"]
    mock_sleep.assert_called_once_with(5.0)


def test_fetch_events_retries_a_secondary_rate_limit_403_via_remaining_header():
    rate_limited = _FakeResponse({}, status_code=403, headers={"X-RateLimit-Remaining": "0"})
    success = _FakeResponse([_raw_event(id="1")])
    client = _FakeClient([rate_limited, success])

    with patch("backfill.time.sleep"):
        events = backfill.fetch_events(client, "https://api.github.com", {}, "acme", "Organization")

    assert [e["id"] for e in events] == ["1"]


def test_fetch_events_does_not_retry_a_genuine_permission_403():
    forbidden = _FakeResponse({}, status_code=403)  # no Retry-After, no X-RateLimit-Remaining
    client = _FakeClient([forbidden])

    with patch("backfill.time.sleep") as mock_sleep:
        with pytest.raises(httpx.HTTPStatusError):
            backfill.fetch_events(client, "https://api.github.com", {}, "acme", "Organization")

    assert len(client.calls) == 1
    mock_sleep.assert_not_called()


def test_fetch_events_raises_after_exhausting_all_retries():
    responses = [_FakeResponse({}, status_code=429) for _ in range(3)]
    client = _FakeClient(responses)

    with patch("backfill.time.sleep"):
        with pytest.raises(httpx.HTTPStatusError):
            backfill.fetch_events(client, "https://api.github.com", {}, "acme", "Organization")

    assert len(client.calls) == 3


def test_fetch_events_retries_a_5xx():
    server_error = _FakeResponse({}, status_code=502)
    success = _FakeResponse([_raw_event(id="1")])
    client = _FakeClient([server_error, success])

    with patch("backfill.time.sleep"):
        events = backfill.fetch_events(client, "https://api.github.com", {}, "acme", "Organization")

    assert [e["id"] for e in events] == ["1"]


def test_fetch_events_retries_a_connection_error_then_succeeds():
    success = _FakeResponse([_raw_event(id="1")])

    class _FlakyClient(_FakeClient):
        def get(self, url, headers=None, params=None):
            self.calls.append((url, params))
            if len(self.calls) == 1:
                raise httpx.RequestError("connection reset")
            return self._responses.pop(0)

    client = _FlakyClient([success])

    with patch("backfill.time.sleep"):
        events = backfill.fetch_events(client, "https://api.github.com", {}, "acme", "Organization")

    assert [e["id"] for e in events] == ["1"]
    assert len(client.calls) == 2


# ---------------------------------------------------------------------------
# worker._handle_backfill_repo_events: error paths, mirroring
# test_process_job.py's _FakeConn/_FakeCursor + patch("worker.httpx.Client") convention
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class _FakeConn:
    def __init__(self):
        self.committed = False
        self.rolled_back = False
        self._cursor = _FakeCursor()

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


def _payload(**kwargs):
    enc = encrypt_job_token("secret", settings.job_secret_key.get_secret_value())
    return json.dumps({"tenant_id": 1, "account_login": "acme", "account_type": "Organization", "token": enc, **kwargs})


def test_handler_marks_failed_on_invalid_payload():
    conn = _FakeConn()
    worker._handle_backfill_repo_events(conn, 1, "{}", 0)
    sql, params = conn._cursor.calls[0]
    assert "status='failed'" in sql


def test_handler_marks_failed_on_undecryptable_token():
    conn = _FakeConn()
    bad_payload = json.dumps({"tenant_id": 1, "account_login": "acme", "account_type": "Organization", "token": "not-encrypted"})
    worker._handle_backfill_repo_events(conn, 1, bad_payload, 0)
    sql, params = conn._cursor.calls[0]
    assert "status='failed'" in sql


def test_handler_marks_failed_on_4xx_from_github():
    conn = _FakeConn()
    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.json.side_effect = ValueError("not json")

    with patch("worker.httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get = MagicMock(return_value=mock_response)
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError("error", request=MagicMock(), response=mock_response)
        mock_client_cls.return_value = mock_client

        worker._handle_backfill_repo_events(conn, 2, _payload(), 0)

    sql, params = conn._cursor.calls[0]
    assert "status='failed'" in sql
    assert "GitHub API error" in params[0]


def test_handler_requeues_on_5xx_from_github():
    conn = _FakeConn()
    mock_response = MagicMock()
    mock_response.status_code = 502
    mock_response.json.side_effect = ValueError("not json")

    with patch("worker.httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get = MagicMock(return_value=mock_response)
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError("error", request=MagicMock(), response=mock_response)
        mock_client_cls.return_value = mock_client

        worker._handle_backfill_repo_events(conn, 3, _payload(), 0)

    sql, params = conn._cursor.calls[0]
    assert "status='queued'" in sql  # requeued, not failed -- retry_count 0 -> 1, well under MAX_RETRIES


def test_handler_requeues_on_network_error():
    conn = _FakeConn()

    with patch("worker.httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get = MagicMock(side_effect=httpx.RequestError("connection reset"))
        mock_client_cls.return_value = mock_client

        worker._handle_backfill_repo_events(conn, 4, _payload(), 0)

    sql, params = conn._cursor.calls[0]
    assert "status='queued'" in sql


class _FailingCursor(_FakeCursor):
    """Raises on the first execute() (the SET app.tenant_id call) to simulate a genuine
    DB error mid-insert-loop -- e.g. a constraint violation or serialization failure.
    Subsequent execute() calls succeed, same as _requeue_for_retry's own UPDATE would
    against a real connection once its rollback() has cleared the aborted transaction."""

    has_failed = False

    def execute(self, sql, params=None):
        if not self.has_failed:
            self.has_failed = True
            raise psycopg.OperationalError("simulated database failure")
        super().execute(sql, params)


class _FailingConn(_FakeConn):
    def __init__(self):
        super().__init__()
        self._cursor = _FailingCursor()


def test_handler_rolls_back_and_requeues_on_a_db_error_during_the_insert_loop():
    # Without a rollback here, the connection's transaction stays aborted and the
    # _mark_failed/_requeue_for_retry UPDATE that follows would itself raise
    # InFailedSqlTransaction, leaving the job stuck in 'processing' forever instead of
    # being requeued.
    conn = _FailingConn()
    events = [_raw_event(id="b-db-error-1")]

    with patch("worker.httpx.Client", return_value=_mock_github_client(events)):
        worker._handle_backfill_repo_events(conn, 5, _payload(), 0)

    assert conn.rolled_back is True
    sql, _params = conn._cursor.calls[0]
    assert "status='queued'" in sql  # requeued, not stuck in 'processing'
    # The activity_sync_cursors upsert never even runs -- the failure happens on the
    # very first execute() (SET app.tenant_id), before the insert loop or the cursor
    # upsert are reached.
    assert not any("activity_sync_cursors" in sql for sql, _params in conn._cursor.calls)


# ---------------------------------------------------------------------------
# worker._handle_backfill_repo_events: real Postgres, verifying the actual
# repo_events / repo_event_daily_counts side effects (mirrors test_event_consumer.py's
# pg_conn/tenant_id fixture pattern -- kept file-local rather than shared via conftest.py,
# matching how each worker test file already owns its own fixtures except worker_db).
# ---------------------------------------------------------------------------


@pytest.fixture()
def pg_conn():
    conn = psycopg.connect(_DB_URL, autocommit=False)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture()
def tenant_id(pg_conn):
    email = "s5-backfill-tests@example.com"
    with pg_conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE lower(email) = lower(%s)", (email,))
        row = cur.fetchone()
        user_id = row[0] if row else None
        if user_id is None:
            cur.execute("INSERT INTO users (email) VALUES (%s) RETURNING id", (email,))
            user_id = cur.fetchone()[0]
        cur.execute("SELECT id FROM tenants WHERE personal_user_id = %s", (user_id,))
        row = cur.fetchone()
        result = row[0] if row else None
        if result is None:
            cur.execute("INSERT INTO tenants (kind, personal_user_id) VALUES ('personal', %s) RETURNING id", (user_id,))
            result = cur.fetchone()[0]
    pg_conn.commit()
    return result


def _mock_github_client(events: list[dict]):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.links = {}
    mock_response.json = MagicMock(return_value=events)
    mock_response.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get = MagicMock(return_value=mock_response)
    return mock_client


def _repo_events_count(conn, tenant_id, repo):
    with conn.cursor() as cur:
        cur.execute(f"SET app.tenant_id = {int(tenant_id)}")
        cur.execute("SELECT count(*) FROM repo_events WHERE repo = %s AND tenant_id = %s", (repo, tenant_id))
        return cur.fetchone()[0]


def _daily_count(conn, tenant_id, repo, event_type, day):
    with conn.cursor() as cur:
        cur.execute(f"SET app.tenant_id = {int(tenant_id)}")
        cur.execute(
            "SELECT count FROM repo_event_daily_counts WHERE tenant_id = %s AND repo = %s AND event_type = %s AND day = %s",
            (tenant_id, repo, event_type, day),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _payload_for(tenant_id: int, account_login: str = "acme", account_type: str = "Organization") -> str:
    enc = encrypt_job_token("secret", settings.job_secret_key.get_secret_value())
    return json.dumps({"tenant_id": tenant_id, "account_login": account_login, "account_type": account_type, "token": enc})


def test_handler_inserts_events_and_daily_counts(pg_conn, tenant_id):
    repo = "acme/backfill-fresh"
    events = [
        _raw_event(id="b-fresh-1", event_type="PushEvent", repo={"name": repo}),
        _raw_event(id="b-fresh-2", event_type="IssuesEvent", payload={"action": "opened", "issue": {"number": 1, "title": "Bug"}}, repo={"name": repo}),
    ]

    with patch("worker.httpx.Client", return_value=_mock_github_client(events)):
        worker._handle_backfill_repo_events(pg_conn, 100, _payload_for(tenant_id), 0)

    assert _repo_events_count(pg_conn, tenant_id, repo) == 2
    assert _daily_count(pg_conn, tenant_id, repo, "push", date(2026, 8, 21)) == 1
    assert _daily_count(pg_conn, tenant_id, repo, "issues", date(2026, 8, 21)) == 1


def test_handler_skips_untracked_event_types(pg_conn, tenant_id):
    repo = "acme/backfill-skip"
    events = [_raw_event(id="b-skip-1", event_type="WatchEvent", repo={"name": repo})]

    with patch("worker.httpx.Client", return_value=_mock_github_client(events)):
        worker._handle_backfill_repo_events(pg_conn, 101, _payload_for(tenant_id), 0)

    assert _repo_events_count(pg_conn, tenant_id, repo) == 0


def test_handler_rerun_is_idempotent(pg_conn, tenant_id):
    repo = "acme/backfill-dedupe"
    events = [_raw_event(id="b-dedupe-1", event_type="CreateEvent", payload={"ref_type": "tag", "ref": "v1"}, repo={"name": repo})]

    with patch("worker.httpx.Client", return_value=_mock_github_client(events)):
        worker._handle_backfill_repo_events(pg_conn, 102, _payload_for(tenant_id), 0)
        worker._handle_backfill_repo_events(pg_conn, 103, _payload_for(tenant_id), 0)  # simulates a retried/re-triggered job

    assert _repo_events_count(pg_conn, tenant_id, repo) == 1
    assert _daily_count(pg_conn, tenant_id, repo, "create", date(2026, 8, 21)) == 1


def test_handler_uses_the_user_events_path_for_personal_installs(pg_conn, tenant_id):
    repo = "octocat/personal-repo"
    events = [_raw_event(id="b-personal-1", event_type="PushEvent", repo={"name": repo})]
    mock_client = _mock_github_client(events)

    with patch("worker.httpx.Client", return_value=mock_client):
        worker._handle_backfill_repo_events(pg_conn, 104, _payload_for(tenant_id, account_login="octocat", account_type="User"), 0)

    called_url = mock_client.get.call_args[0][0]
    assert "/users/octocat/events" in called_url
    assert _repo_events_count(pg_conn, tenant_id, repo) == 1


def _sync_cursor(conn, tenant_id):
    with conn.cursor() as cur:
        cur.execute(f"SET app.tenant_id = {int(tenant_id)}")
        cur.execute(
            "SELECT account_login, account_type, last_synced_at FROM activity_sync_cursors WHERE tenant_id = %s",
            (tenant_id,),
        )
        return cur.fetchone()


def test_handler_upserts_the_sync_cursor_on_success(pg_conn, tenant_id):
    repo = "acme/cursor-fresh"
    events = [_raw_event(id="b-cursor-1", event_type="PushEvent", repo={"name": repo})]

    with patch("worker.httpx.Client", return_value=_mock_github_client(events)):
        worker._handle_backfill_repo_events(pg_conn, 105, _payload_for(tenant_id), 0)

    row = _sync_cursor(pg_conn, tenant_id)
    assert row is not None
    account_login, account_type, last_synced_at = row
    assert account_login == "acme"
    assert account_type == "Organization"
    assert last_synced_at is not None


def test_handler_cursor_upsert_advances_last_synced_at_on_rerun(pg_conn, tenant_id):
    repo = "acme/cursor-rerun"
    events = [_raw_event(id="b-cursor-2", event_type="PushEvent", repo={"name": repo})]

    with patch("worker.httpx.Client", return_value=_mock_github_client(events)):
        worker._handle_backfill_repo_events(pg_conn, 106, _payload_for(tenant_id), 0)
        first_synced_at = _sync_cursor(pg_conn, tenant_id)[2]

        worker._handle_backfill_repo_events(pg_conn, 107, _payload_for(tenant_id), 0)
        second_synced_at = _sync_cursor(pg_conn, tenant_id)[2]

    assert second_synced_at >= first_synced_at
