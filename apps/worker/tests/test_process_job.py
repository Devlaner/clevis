"""Worker job processing: unit-style tests with mocked GitHub HTTP and psycopg connection."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import httpx

from _crypto import encrypt_job_token
from config import settings
from worker import MAX_RETRIES, RECLAIM_TIMEOUT_MINUTES, _reclaim_stale_jobs, process_job


class _FakeCursor:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class _FakeConn:
    def __init__(self):
        self.committed = False
        self._cursor = _FakeCursor()

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True


def _payload(**kwargs):
    enc = encrypt_job_token("secret", settings.job_secret_key.get_secret_value())
    return json.dumps({"owner": "acme", "repo": "demo", "token": enc, **kwargs})


def test_process_job_marks_done_on_success():
    conn = _FakeConn()

    mock_response = MagicMock()
    mock_response.status_code = 204
    mock_response.text = ""

    with patch("worker.httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.delete = MagicMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        process_job(conn, 1, "github.clear_actions_cache", _payload())

    sql, params = conn._cursor.calls[0]
    assert "status='done'" in sql
    assert params[1] == 1
    result = json.loads(params[0])
    assert result["ok"] is True
    assert result["status"] == 204
    assert conn.committed is True


def test_process_job_marks_failed_on_4xx_http_error():
    """4xx is a permanent failure (bad token, missing repo, etc.) — never worth retrying."""
    conn = _FakeConn()

    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.text = "not found"

    with patch("worker.httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.delete = MagicMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        process_job(conn, 2, "github.clear_actions_cache", _payload())

    sql, params = conn._cursor.calls[0]
    assert "status='failed'" in sql
    assert params[1] == 2
    assert "GitHub API error" in params[0]
    assert conn.committed is True


def test_process_job_marks_failed_with_status_code_only_when_error_body_is_not_json():
    """_github_error_message must fall back to a bare status code (not crash) when
    GitHub's error body isn't valid JSON."""
    conn = _FakeConn()

    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.text = "not found"
    mock_response.json.side_effect = ValueError("not json")

    with patch("worker.httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.delete = MagicMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        process_job(conn, 9, "github.clear_actions_cache", _payload())

    sql, params = conn._cursor.calls[0]
    assert "status='failed'" in sql
    assert params[0] == "GitHub API error: 404"
    assert conn.committed is True


def test_process_job_marks_failed_when_token_decryption_fails():
    """A payload with a token that fails to decrypt is a permanent failure, not a retry."""
    conn = _FakeConn()

    payload = json.dumps({"owner": "acme", "repo": "demo", "token": "not-a-valid-encrypted-token"})

    process_job(conn, 3, "github.clear_actions_cache", payload)

    sql, params = conn._cursor.calls[0]
    assert "status='failed'" in sql
    assert "retry_count=%s" in sql
    assert params[1] == 3
    assert params[2] == 0
    assert conn.committed is True


def test_process_job_requeues_on_5xx_http_error():
    """5xx is presumed transient (GitHub-side issue) — worth a bounded retry, unlike 4xx."""
    conn = _FakeConn()

    mock_response = MagicMock()
    mock_response.status_code = 503
    mock_response.text = "upstream error"

    with patch("worker.httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.delete = MagicMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        process_job(conn, 6, "github.clear_actions_cache", _payload(), retry_count=0)

    sql, params = conn._cursor.calls[0]
    assert "status='queued'" in sql
    new_retry_count, result_text, job_id, expected_retry_count = params
    assert new_retry_count == 1
    assert job_id == 6
    assert expected_retry_count == 0  # fences against a job reclaimed/re-claimed since this worker started
    assert "GitHub API error" in result_text
    assert conn.committed is True


def test_process_job_requeues_on_httpx_request_error():
    """A genuine httpx.RequestError (what httpx actually raises for network failures) is
    treated as transient and requeued, distinct from an unrecognized exception type."""
    conn = _FakeConn()

    with patch("worker.httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.delete = MagicMock(side_effect=httpx.ConnectError("connection refused"))
        mock_client_cls.return_value = mock_client

        process_job(conn, 7, "github.clear_actions_cache", _payload(), retry_count=2)

    sql, params = conn._cursor.calls[0]
    assert "status='queued'" in sql
    new_retry_count, result_text, job_id, expected_retry_count = params
    assert new_retry_count == 3  # incremented from the passed-in retry_count=2
    assert job_id == 7
    assert expected_retry_count == 2
    assert "connection refused" in result_text
    assert conn.committed is True


def test_process_job_fails_once_retry_cap_exceeded():
    conn = _FakeConn()

    mock_response = MagicMock()
    mock_response.status_code = 503

    with patch("worker.httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.delete = MagicMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        process_job(conn, 8, "github.clear_actions_cache", _payload(), retry_count=MAX_RETRIES)

    sql, params = conn._cursor.calls[0]
    assert "status='failed'" in sql
    new_retry_count, result_text, job_id, expected_retry_count = params
    assert new_retry_count == MAX_RETRIES + 1
    assert job_id == 8
    assert expected_retry_count == MAX_RETRIES
    assert "exceeded max retry attempts" in result_text
    assert conn.committed is True


def test_process_job_marks_failed_on_unrecognized_exception_safety_net():
    """Something other than httpx.RequestError (a bug, an unanticipated exception type)
    must still fail the job immediately rather than leaving it stuck in 'processing'
    until the reclaim sweep eventually picks it up."""
    conn = _FakeConn()

    with patch("worker.httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.delete = MagicMock(side_effect=ConnectionError("timeout"))
        mock_client_cls.return_value = mock_client

        process_job(conn, 3, "github.clear_actions_cache", _payload())

    sql, params = conn._cursor.calls[0]
    assert "status='failed'" in sql
    assert "timeout" in params[0]
    assert conn.committed is True


def test_process_job_truncates_long_error():
    conn = _FakeConn()

    with patch("worker.httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.delete = MagicMock(side_effect=ConnectionError("x" * 1000))
        mock_client_cls.return_value = mock_client

        process_job(conn, 4, "github.clear_actions_cache", _payload())

    sql, params = conn._cursor.calls[0]
    assert len(params[0]) <= 500
    assert params[0].endswith("...(truncated)")


def test_process_job_redacts_token_shaped_text_in_error():
    conn = _FakeConn()

    with patch("worker.httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.delete = MagicMock(
            side_effect=ConnectionError("failed with ghp_abcdefghijklmnopqrstuvwxyz0123456789")
        )
        mock_client_cls.return_value = mock_client

        process_job(conn, 5, "github.clear_actions_cache", _payload())

    sql, params = conn._cursor.calls[0]
    assert "ghp_" not in params[0]
    assert "[redacted]" in params[0]


def test_process_job_rejects_invalid_payload_without_calling_github():
    conn = _FakeConn()
    bad_payload = json.dumps({"owner": "acme"})  # missing repo/token

    with patch("worker.httpx.Client") as mock_client_cls:
        process_job(conn, 9, "github.clear_actions_cache", bad_payload)
        mock_client_cls.assert_not_called()

    sql, params = conn._cursor.calls[0]
    assert "status='failed'" in sql
    assert params[1] == 9
    assert conn.committed is True


def test_process_job_rejects_empty_string_fields():
    conn = _FakeConn()
    bad_payload = json.dumps({"owner": "", "repo": "demo", "token": "x"})

    with patch("worker.httpx.Client") as mock_client_cls:
        process_job(conn, 10, "github.clear_actions_cache", bad_payload)
        mock_client_cls.assert_not_called()

    sql, params = conn._cursor.calls[0]
    assert "status='failed'" in sql
    assert conn.committed is True


def test_process_job_terminal_write_is_a_noop_if_reclaimed_out_from_under_it(worker_db):
    """If the reclaim sweep resets this job back to 'queued' (or another worker later
    claims it) while this call is still in flight, process_job's own terminal write
    must not clobber that newer state — the WHERE status='processing' guard should
    make it a no-op instead of a lost update."""
    conn, created_ids = worker_db
    stale = datetime.now(timezone.utc) - timedelta(minutes=RECLAIM_TIMEOUT_MINUTES + 5)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO jobs (job_type, payload, status, retry_count, updated_at)
            VALUES ('github.clear_actions_cache', '{}', 'processing', 0, %s)
            RETURNING id
            """,
            (stale,),
        )
        job_id = cur.fetchone()[0]
    conn.commit()
    created_ids.append(job_id)

    # Simulate the reclaim sweep firing while this worker is still mid-process_job for
    # the same job (e.g. this worker stalled past the reclaim timeout).
    _reclaim_stale_jobs(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT status, retry_count FROM jobs WHERE id = %s", (job_id,))
        reclaimed_status, reclaimed_retry_count = cur.fetchone()
    assert reclaimed_status == "queued"
    assert reclaimed_retry_count == 1

    mock_response = MagicMock()
    mock_response.status_code = 204
    mock_response.text = ""
    with patch("worker.httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.delete = MagicMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        # This worker's in-memory retry_count is stale (captured before the reclaim).
        process_job(conn, job_id, "github.clear_actions_cache", _payload(), retry_count=0)

    with conn.cursor() as cur:
        cur.execute("SELECT status, retry_count FROM jobs WHERE id = %s", (job_id,))
        final_status, final_retry_count = cur.fetchone()
    assert final_status == "queued"
    assert final_retry_count == 1


def test_process_job_terminal_write_is_a_noop_if_a_second_worker_reclaimed_and_reprocessed_it(worker_db):
    """Narrower race than the reclaim-to-'queued' case above (#253): the reclaim sweep
    resets this job to 'queued' (bumping retry_count) AND a second worker's own
    SELECT ... FOR UPDATE picks it back up, setting status back to 'processing' before
    this (first) worker's stale terminal write runs. WHERE status='processing' alone
    would then incorrectly match again -- the retry_count fence is what actually
    prevents this worker's stale completion from clobbering the second worker's
    in-flight row."""
    conn, created_ids = worker_db
    stale = datetime.now(timezone.utc) - timedelta(minutes=RECLAIM_TIMEOUT_MINUTES + 5)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO jobs (job_type, payload, status, retry_count, updated_at)
            VALUES ('github.clear_actions_cache', '{}', 'processing', 0, %s)
            RETURNING id
            """,
            (stale,),
        )
        job_id = cur.fetchone()[0]
    conn.commit()
    created_ids.append(job_id)

    # Reclaim sweep fires (job -> 'queued', retry_count -> 1), then a second worker's
    # own claim query picks it back up (job -> 'processing' again, same bumped retry_count).
    _reclaim_stale_jobs(conn)
    with conn.cursor() as cur:
        cur.execute("UPDATE jobs SET status = 'processing', updated_at = NOW() WHERE id = %s", (job_id,))
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT status, retry_count FROM jobs WHERE id = %s", (job_id,))
        reprocessed_status, reprocessed_retry_count = cur.fetchone()
    assert reprocessed_status == "processing"
    assert reprocessed_retry_count == 1

    mock_response = MagicMock()
    mock_response.status_code = 204
    mock_response.text = ""
    with patch("worker.httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.delete = MagicMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        # This (first) worker's in-memory retry_count is stale (captured before the
        # reclaim) -- its completion must not touch the row the second worker now owns.
        process_job(conn, job_id, "github.clear_actions_cache", _payload(), retry_count=0)

    with conn.cursor() as cur:
        cur.execute("SELECT status, retry_count FROM jobs WHERE id = %s", (job_id,))
        final_status, final_retry_count = cur.fetchone()
    assert final_status == "processing"
    assert final_retry_count == 1


def test_process_job_marks_unknown_job_type_failed_without_calling_github():
    conn = _FakeConn()

    with patch("worker.httpx.Client") as mock_client_cls:
        process_job(conn, 6, "some.future.job_type", _payload())
        mock_client_cls.assert_not_called()

    sql, params = conn._cursor.calls[0]
    assert "status='failed'" in sql
    assert params[1] == 6
    assert "some.future.job_type" in params[0]
    assert conn.committed is True


def test_process_job_dispatches_known_job_type_to_its_handler():
    conn = _FakeConn()

    mock_response = MagicMock()
    mock_response.status_code = 204
    mock_response.text = ""

    with patch("worker.httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.delete = MagicMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        process_job(conn, 7, "github.clear_actions_cache", _payload())

    sql, params = conn._cursor.calls[0]
    assert "status='done'" in sql
    assert params[1] == 7
