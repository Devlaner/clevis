"""Covers run()'s single-iteration behavior end to end against a real Postgres instance,
in particular that a claimed job gets wrapped in _JobHeartbeat (issue #215) -- run() itself
is a `while True:` loop, so these tests break out after exactly one iteration by making
time.sleep raise a sentinel exception."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import worker
from _crypto import encrypt_job_token
from config import settings


class _StopLoop(Exception):
    pass


def _insert_queued_job(conn, created_ids) -> int:
    enc = encrypt_job_token("secret", settings.job_secret_key.get_secret_value())
    # A key-scoped clear takes the single-DELETE path, which the mocked httpx.Client below
    # stubs; a keyless (global) clear would first GET the cache list, which this test
    # doesn't mock.
    payload = json.dumps({"owner": "acme", "repo": "demo", "token": enc, "key": "build-cache"})
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO jobs (job_type, payload, status, updated_at)
            VALUES ('github.clear_actions_cache', %s, 'queued', NOW())
            RETURNING id
            """,
            (payload,),
        )
        job_id = cur.fetchone()[0]
    conn.commit()
    created_ids.append(job_id)
    return job_id


def _fetch(conn, job_id):
    with conn.cursor() as cur:
        cur.execute("SELECT status, heartbeat_at FROM jobs WHERE id = %s", (job_id,))
        return cur.fetchone()


def test_run_wraps_a_claimed_job_in_a_job_heartbeat(worker_db, monkeypatch):
    conn, created_ids = worker_db
    job_id = _insert_queued_job(conn, created_ids)

    monkeypatch.setattr(worker, "_read_poll_seconds", lambda: 1)
    monkeypatch.setattr(worker.time, "sleep", MagicMock(side_effect=_StopLoop))

    mock_response = MagicMock()
    mock_response.status_code = 204
    mock_response.text = ""

    with patch("worker.httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.delete = MagicMock(return_value=mock_response)
        mock_client_cls.return_value = mock_client

        try:
            worker.run()
        except _StopLoop:
            pass

    status, heartbeat_at = _fetch(conn, job_id)
    assert status == "done"
    # _JobHeartbeat.__enter__ ticks immediately, before process_job even starts -- proves
    # run() actually wrapped the claimed job rather than calling process_job bare.
    assert heartbeat_at is not None
    assert heartbeat_at > datetime.now(timezone.utc) - timedelta(seconds=10)
